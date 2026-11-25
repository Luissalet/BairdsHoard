"""One offline UnlimitedOCR job. Heavy dependencies belong to the configured GPU runtime."""
import base64
import hashlib
import html
import io
import json
from pathlib import Path
import re
import sys

from .transcribe import HEADER

BLOCK = re.compile(r"<\|det\|>\s*([\w-]+)\s*\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]<\|/det\|>(.*?)(?=<\|det\|>|\Z)", re.S)


def views(source):
    """Consume only Baird's timestamped JPEGs. Source HTML never enters the new document."""
    stamp = None
    with source.open(encoding="utf-8-sig") as stream:
        while line := stream.readline(32 * 1024 * 1024):
            if len(line) >= 32 * 1024 * 1024:
                raise ValueError("Vista demasiado grande.")
            heading = re.match(r"<section><h2>(\d+:[0-5]\d\.\d)</h2>", line)
            if heading:
                stamp = heading[1]
            if stamp and (match := re.search(r'<img [^>]*src="data:image/jpeg;base64,([A-Za-z0-9+/=]+)"', line)):
                yield stamp, match[1]
                stamp = None


class Model:
    def __init__(self, folder, gpu):
        import torch
        from transformers import AutoTokenizer, UnlimitedOcrForConditionalGeneration
        self.device = f"cuda:{gpu}"
        self.model, info = UnlimitedOcrForConditionalGeneration.from_pretrained(folder, dtype=torch.bfloat16,
            local_files_only=True, output_loading_info=True)
        if any(info.get(k) for k in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs")):
            raise RuntimeError("Los pesos de UnlimitedOCR no coinciden con el modelo.")
        self.model.to(self.device).eval()
        self.tokenizer = tok = AutoTokenizer.from_pretrained(folder, local_files_only=True, trust_remote_code=False)
        # Base mode (1024 px): 16 rows of 16 patches with row separators and one image separator.
        prefix = [tok.bos_token_id] + [tok.convert_tokens_to_ids("<image>")] * (16 * 17 + 1)
        self.tokens = torch.tensor([prefix + tok.encode("document parsing.", add_special_tokens=False)], device=self.device)
        self.cache = {}

    def read(self, image):
        import numpy as np
        import torch
        from PIL import ImageOps
        key = (image.size, hashlib.sha256(image.tobytes()).digest())
        if key in self.cache:
            return self.cache[key]
        square = ImageOps.pad(image, (1024, 1024), color=(127, 127, 127))
        data = torch.from_numpy(np.asarray(square).copy()).permute(2, 0, 1)[None].to(self.device, dtype=torch.float32)
        with torch.inference_mode():
            out = self.model.generate(input_ids=self.tokens, attention_mask=torch.ones_like(self.tokens),
                pixel_values=(data / 127.5 - 1).bfloat16(), max_new_tokens=1200, do_sample=False,
                pad_token_id=self.tokenizer.eos_token_id)
        ids = out[0, self.tokens.shape[-1]:]
        raw = self.tokenizer.decode(ids, skip_special_tokens=False).replace(self.tokenizer.eos_token, "")
        blocks = [(kind, tuple(map(int, (x,y,r,b))), t.strip()) for kind,x,y,r,b,t in BLOCK.findall(raw)]
        print('{"reading":true}', flush=True)
        complete = bool(len(ids) and ids[-1].item() == self.tokenizer.eos_token_id)
        result = (blocks, complete)
        if len(self.cache) >= 1024:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = result
        return result


def label_boxes(image):
    import numpy as np
    from scipy import ndimage as nd
    pieces, _ = nd.label(np.asarray(image.convert("L")) < 140, structure=np.ones((3, 3)))
    ink = np.zeros(pieces.shape, dtype=bool)
    for idx, region in enumerate(nd.find_objects(pieces), 1):
        if region:
            y, x = region
            if y.stop-y.start <= 32 and x.stop-x.start <= 32:
                ink[region] |= pieces[region] == idx
    groups, _ = nd.label(nd.maximum_filter(ink, size=(3, 11)))
    for y, x in nd.find_objects(groups):
        w, h = x.stop-x.start-10, y.stop-y.start-2
        if w >= 7 and 4 <= h <= 42:
            yield (max(0,x.start+2), max(0,y.start-1), min(image.width,x.stop-2), min(image.height,y.stop+1))


def detail_text(model, image, box):
    from PIL import ImageOps
    blocks, complete = model.read(ImageOps.expand(image.crop(box), border=(12, 10), fill="white"))
    return " ".join(t for _, _, t in blocks if t), complete


def document(model, image, blocks):
    figures = [box for kind, box, _ in blocks if kind in ("image", "figure", "chart")]
    lines = [s for _, _, text in blocks for s in text.splitlines()]
    for box in label_boxes(image) if figures else ():
        x = (box[0]+box[2])*500/image.width
        y = (box[1]+box[3])*500/image.height
        if any(l-10 <= x <= r+10 and t-10 <= y <= b+10 for l,t,r,b in figures):
            label, _ = detail_text(model, image, box)
            if label:
                lines.append(label)
    warnings = ["La figura original conserva la posición de los rótulos y las conexiones."] if figures else []
    return lines, warnings


def estimate_pitch(energy, frequencies, widths):
    """Use several independently located OCR lines to disambiguate harmonics of the character grid."""
    import numpy as np
    power = np.square(energy).mean(axis=0)
    pitches = 1 / frequencies
    fallback = float(pitches[power.argmax()])
    if len(widths) < 3:
        return fallback, False
    median = float(np.median(widths))
    supporters = [v for v in widths if abs(v - median) <= median * .07]
    if len(supporters) < max(3, len(widths) * .65):
        return fallback, False
    region = np.flatnonzero(abs(pitches - median) <= median * .06)
    if not len(region):
        return fallback, False
    return float(pitches[region[power[region].argmax()]]), True


def conflict_area(chosen, other, pitch, anchored, left, right, top, bottom, row, indent):
    """Localize the first divergence. After an insertion, the remaining columns may no longer align."""
    start = end = -1
    x, last = left, right
    if anchored and chosen and other and chosen != other:
        prefix = 0
        while prefix < min(len(chosen), len(other)) and chosen[prefix] == other[prefix]:
            prefix += 1
        suffix = 0
        while suffix < min(len(chosen), len(other)) - prefix and chosen[-1-suffix] == other[-1-suffix]:
            suffix += 1
        # A large or poorly anchored disagreement is more useful as a whole line.
        length = max(len(chosen), len(other)) - suffix - prefix
        if length <= 12 and abs(len(chosen) - (right-left)/pitch) <= 2:
            start = indent + prefix
            end = indent + len(chosen) - suffix
            x = min(right-1, max(left, round(start*pitch)))
            last = min(right, max(x+1, round((start+max(1,length))*pitch)))
    return [row, start, end, x, top, last-x, bottom-top]


def repeated_delimiters(ink, readings, blocks, size, pitch, step):
    """Recover isolated brackets from matching pixels at another position on the same code grid."""
    import numpy as np
    if not step:
        return {}
    w, h = size
    brackets = set("()[]{}")
    boxes = [(text, (x+r)*w/2000, (y+b)*h/2000) for _, (x,y,r,b), text in blocks
             if text in brackets and 0 < (r-x)*w/1000 < 2.5*pitch]
    small = []
    for i, (a,b,left,right,detail,context,complete) in enumerate(readings):
        if right-left > pitch*1.25 or b-a < 5:
            continue
        patch = ink[a:b, left:right]
        patch = patch / max(float(np.linalg.norm(patch)), 1)
        center = (a+b)/2
        labels = {ch for ch, x, y in boxes if left-pitch/2 <= x <= right+pitch/2 and a <= y <= b}
        if complete and detail == context and detail in brackets:
            labels.add(detail)
        small.append((i, center, patch, labels))
    recovered = {}
    for i, y, patch, own in small:
        seen = set()
        for j, other_y, other, labels in small:
            if i == j or not labels or patch.shape != other.shape:
                continue
            delta = (other_y-y)/step
            if abs(delta-round(delta))*step <= 1.5 and np.sum(patch*other) >= .985:
                seen.update(labels)
        if len(seen) == 1 and (not own or own == seen):
            recovered[i] = next(iter(seen))
    return recovered


def code(model, image, blocks, *, areas=None):
    import numpy as np
    from scipy import ndimage as nd
    from PIL import ImageFilter
    gray = image.convert("L")
    bg = np.asarray(gray.filter(ImageFilter.MedianFilter(13)), dtype=float)
    px = np.asarray(gray, dtype=float)
    ink = np.maximum(np.where(bg > 120, bg-px, px-bg)-12, 0)
    rows = nd.binary_closing((ink > 20).sum(axis=1) >= 2, structure=np.ones(3))
    bands = [(s[0].start, s[0].stop) for s in nd.find_objects(nd.label(rows)[0]) if 4 <= s[0].stop-s[0].start <= 36]
    if not bands:
        return [], ["No se distinguen líneas de código en esta vista."]
    freq = np.fft.rfftfreq(16384)
    select = (freq >= 1/22) & (freq <= 1/5.5)
    energy = [abs(np.fft.rfft(ink[a:b].sum(axis=0), 16384)[select]) / max(1, ink[a:b].sum()) for a,b in bands]

    by_y = []
    for _, (_,y,_,b), text in blocks:
        parts = text.splitlines()
        for n, part in enumerate(parts):
            center = (y+(n+0.5)*(b-y)/len(parts))*image.height/1000
            by_y.append((center, part.strip()))
    readings = []
    widths = []
    for a,b in bands:
        xs = np.flatnonzero((ink[a:b] > 35).any(axis=0))
        if not len(xs):
            continue
        left, right = int(xs[0]), int(xs[-1]+1)
        crop = (max(0,left-3), max(0,a-3), min(image.width,right+3), min(image.height,b+3))
        detail, complete = detail_text(model, image, crop)
        center = (a+b)/2
        near = min(by_y, key=lambda pair:abs(pair[0]-center), default=(float("inf"), ""))
        context = near[1] if abs(near[0]-center) < max(4,(b-a)*.75) else ""
        if complete and len(detail) >= 10 and detail == context:
            widths.append((right-left)/len(detail))
        readings.append((a,b,left,right,detail,context,complete))
    pitch, anchored = estimate_pitch(energy, freq[select], widths)
    lines, warnings = [], []
    centers = [(a+b)/2 for a,b,*_ in readings]
    gaps = np.diff(centers)
    step = np.median(gaps[gaps <= np.median(gaps)]) if len(gaps) else 0
    repeated = repeated_delimiters(ink, readings, blocks, image.size, pitch, step) if anchored else {}
    for i, (a,b,left,right,detail,context,complete) in enumerate(readings):
        # Glyphs on a subpixel cell boundary can begin one raster pixel before it.
        indent = int((left + min(1.0, pitch * .12))/pitch)
        # Compare with the physical character cells; a model may invent a plausible, longer identifier.
        width = max(1,round((right-indent*pitch)/pitch))
        candidates = [s for s in (detail, context) if s]
        text = min(candidates, key=lambda s:abs(len(s)-width), default="")
        recovered = i in repeated and len(text) != 1
        if recovered:
            text = repeated[i]
        if i and step:
            lines.extend([""] * min(8,max(0,round((centers[i]-centers[i-1])/step)-1)))
        lines.append(" "*indent + (text or "[ilegible]"))
        if recovered or not complete or not text or detail != context or abs(len(text)-width) > 1:
            if areas is not None:
                other = context if text == detail else detail
                if recovered:
                    areas.append([len(lines)-1, indent, indent+1, left, a, right-left, b-a])
                else:
                    areas.append(conflict_area(text, other, pitch, anchored, left, right, a, b, len(lines)-1, indent))
            warnings.append(f"Línea visible {i+1} (y={a} px). Revisar con la imagen.")
    return lines, warnings


def new_tail(previous, current):
    # Only discard a contiguous overlap at the scroll boundary. Repeated code elsewhere is meaningful.
    if current == previous:
        return []
    for n in range(min(len(previous), len(current)), 1, -1):
        if previous[-n:] == current[:n] and sum(bool(s.strip()) for s in current[:n]) >= 2:
            return current[n:]
    return current


def main(model_dir, gpu, source, mode, output):
    from PIL import Image
    model = Model(model_dir, gpu)
    previous, transcript, review = [], [], []
    count = 0
    with Path(output).open("w", encoding="utf-8") as out:
        out.write(HEADER + "<p>Segunda lectura local con UnlimitedOCR · " + html.escape(Path(source).name) +
                  "</p><p>Texto automático. Comprueba los símbolos, la sangría y los rótulos con la imagen.</p>\n")
        for stamp, data in views(Path(source)):
            with Image.open(io.BytesIO(base64.b64decode(data, validate=True))) as original:
                if original.format != "JPEG" or original.width * original.height > 34_000_000:
                    raise ValueError("Imagen de documento no admitida.")
                image = original.convert("RGB")
            blocks, complete = model.read(image)
            areas = []
            if mode == "code":
                lines, warnings = code(model, image, blocks, areas=areas)
            else:
                lines, warnings = document(model, image, blocks)
            if not complete or not lines:
                warnings.append("Lectura incompleta.")
            review.extend([stamp, *lines, *warnings, ""])
            transcript.extend(new_tail(previous, lines))
            previous = lines
            out.write(f'<section><h2>{stamp}</h2><pre>' + html.escape("\n".join(lines)) + "</pre>")
            if areas:
                out.write(f'<span hidden data-ocr-areas="{json.dumps(areas, separators=(",", ":"))}"></span>')
            if warnings:
                out.write("<p>" + "<br>".join(map(html.escape, warnings)) + "</p>")
            out.write(f'<details open><summary>Vista original</summary><img loading="lazy" alt="{stamp}" src="data:image/jpeg;base64,{data}"></details></section>\n')
            count += 1
            print(json.dumps({"views":count}), flush=True)
        if not count:
            raise ValueError("El HTML no contiene vistas originales de Baird.")
        plain = base64.b64encode("\n".join(transcript).encode("utf-8")).decode("ascii")
        checked = base64.b64encode("\n".join(review).encode("utf-8")).decode("ascii")
        out.write(f'<p><a download="lectura-por-tiempo.txt" href="data:text/plain;charset=utf-8;base64,{checked}">Descargar vistas con avisos</a></p>')
        out.write(f'<p><a download="transcripcion.txt" href="data:text/plain;charset=utf-8;base64,{plain}">Descargar texto automático</a></p></html>')


if __name__ == "__main__":
    try:
        main(*sys.argv[1:])
    except Exception as exc:
        print(json.dumps({"error": str(exc)[:300]}), flush=True)
        sys.exit(1)
