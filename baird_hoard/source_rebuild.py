"""Associate visible editor rows with their printed line numbers and source frames."""
from collections import Counter, defaultdict
import base64
import html
import io
import re
from pathlib import PurePosixPath
from urllib.parse import quote, unquote
import zipfile

EXT = r"(?:py|js|jsx|ts|tsx|java|go|rs|c|cc|cpp|h|hpp|cs|kt|rb|php|sh|ps1|sql|yml|yaml|json|xml|html|css|toml|ini|md|mmd|txt)"
PART = r"[\w@+.-]+"
PATH = re.compile(rf"(?:{PART}/)*{PART}\.{EXT}$", re.I)
DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.I)


def source_label(lines):
    """A breadcrumb or standalone file path, or a truncated selected-file URL as a hint."""
    paths, hints = set(), set()
    for raw in lines:
        line = raw.strip()
        if re.search(r"https?://|[?&](?:id|selected)=", line, re.I):
            decoded = unquote(line)
            selected = re.search(r"[?&]selected=[^&]*?:([^:&]+)(?:&|$)", decoded, re.I)
            if selected:
                tail = selected[1].strip()
                if PATH.fullmatch(tail):
                    paths.add(tail)
                elif tail.endswith(("...", "…")):
                    # A clipped URL can stop halfway through a percent-encoded slash.
                    prefix = re.sub(r"%[\da-fA-F]?$", "", tail.rstrip(".…"))
                    if prefix:
                        hints.add(prefix)
            continue
        parts = re.split(r"\s*[>›»]\s*", line)
        if len(parts) >= 3:
            # OCR can append the copy-button icon to the filename.
            parts[-1] = re.split(r"\s+", parts[-1], maxsplit=1)[0]
            # Project > folder > file. Repositories may share a basename.
            if all(re.fullmatch(PART, p) for p in parts):
                roots = {"src", "lib", "tests", "test", "app", "packages", "docs", "config"}
                project = parts[0].lower() not in roots and parts[1].lower() in roots
                path = "/".join(parts[1:] if project else parts)
                if PATH.fullmatch(path):
                    paths.add(path)
        else:
            path = line.replace("\\", "/")
            if PATH.fullmatch(path):
                paths.add(path)
    if len(paths) == 1:
        return paths.pop(), ""
    return "", next(iter(hints)) if len(hints) == 1 else ""


def numbered_slots(words, layout, scale, origin):
    """Use agreeing printed numbers locally; discontinuities and folds break interpolation."""
    left, right = layout["gutter"]
    center, step = layout["center"], layout["step"]
    top, bottom = layout["box"][1::2]
    slots = round((bottom-top)/step)
    votes = defaultdict(set)
    for x, y, w, h, word in words:
        if not re.fullmatch(r"[0-9]{1,7}", word):
            continue
        x0, x1 = origin[0]+x/scale, origin[0]+(x+w)/scale
        if min(abs(x0-left), abs(x1-right)) > max(4, step*.25):
            continue
        pos = (origin[1]+(y+h/2)/scale-center)/step
        slot = round(pos)
        if 0 <= slot < slots and abs(slot-pos) < .38 and int(word) > 0:
            votes[slot].add(int(word)-slot)
    strong = {}
    for i, values in votes.items():
        for offset in values:
            support = sum(offset in votes.get(j, ()) for j in range(i-4, i+5))
            if support >= 3:
                if i in strong and strong[i] != offset:
                    strong.pop(i)
                    break
                strong[i] = offset
    numbers = {}
    for i in range(slots):
        near = [(j, offset) for j, offset in strong.items() if abs(j-i) <= 3]
        offsets = {offset for _, offset in near}
        if len(offsets) == 1:
            offset = offsets.pop()
            # Interior holes need consistent neighbors. One or two missing edge digits are
            # recoverable where several adjacent numbers agree on the origin.
            bracketed = any(j <= i for j, _ in near) and any(j >= i for j, _ in near)
            extrapolate = (len(near) >= 2 and i in (0, 1, slots-2, slots-1) and
                           not votes.get(i, set()) - {offset})
            if (bracketed or extrapolate) and i+offset > 0:
                numbers[i] = i+offset
    return numbers


def inspect_editor(image, editor, reader, folder, index, lines, areas):
    from PIL import Image, ImageDraw, ImageFont
    box, step = editor["box"], editor["step"]
    def read(region, name, wanted, *, gutter=False):
        cut = image.crop(region)
        scale = min(wanted, 3900/max(cut.size))
        scaled = cut.resize((max(1, round(cut.width*scale)), max(1, round(cut.height*scale))))
        pad = 0
        if gutter:
            # Windows OCR frequently ignores isolated one-digit rows. A nonnumeric label
            # gives them word context while leaving every original digit intact.
            size = max(10, round(step*scale*.65))
            try:
                font = ImageFont.truetype("consola.ttf", size)
            except OSError:
                font = ImageFont.load_default()
            pad = max(60, round(font.getlength("Line "))+10)
            bg = scaled.getpixel((0,0))
            sheet = Image.new("RGB", (scaled.width+pad,scaled.height), bg)
            sheet.paste(scaled, (pad,0))
            draw = ImageDraw.Draw(sheet)
            color = "#444444" if sum(bg) > 380 else "#eeeeee"
            y = (editor["center"]-region[1])*scale
            while y < sheet.height:
                draw.text((pad-3,y), "Line", font=font, fill=color, anchor="rm")
                y += step*scale
            scaled = sheet
        path = folder/f"{index}-{name}.png"
        scaled.save(path)
        try:
            mapped = reader.read(path)["lines"]
        finally:
            try:
                path.unlink(missing_ok=True)
            except PermissionError:
                pass  # Released on helper shutdown; the job owns its scratch directory.
        if pad:
            for line in mapped:
                for word in line[5]:
                    word[0] -= pad
        return mapped, scale

    header, _ = read((0, 0, image.width, max(1, box[1])), "header", 1.75)
    name, hint = source_label([line[4] for line in header])
    gl, gr = editor["gutter"]
    gutter = (max(0, gl-4), box[1], min(image.width, gr+6), box[3])
    textmap, scale = read(gutter, "numbers", 3, gutter=True)
    numbers = numbered_slots([w for line in textmap for w in line[5]], editor, scale, gutter)
    at = defaultdict(list)
    for a, b, ix in editor["positions"]:
        slot = round(((a+b)/2-editor["center"])/step)
        at[slot].append(ix)
    uncertain = {area[0] for area in areas}
    rows = {}
    associated = set()
    for slot, number in numbers.items():
        matches = at.get(slot, [])
        if len(matches) == 1:
            ix = matches[0]
            rows[number] = (lines[ix], ix not in uncertain)
            associated.add(ix)
        elif not matches:
            cy = editor["center"] + slot*step
            patch = image.crop((box[0], max(box[1], round(cy-step*.42)), box[2],
                                min(box[3], round(cy+step*.42))))
            # A shortened band (for example a closing bracket) must not become a blank line.
            if patch.height:
                low, high = patch.convert("L").getextrema()
                if high-low < 32:
                    rows[number] = ("", True)
    editor["unplaced"] = len(editor["positions"]) - len(associated)
    return name, hint, rows


class Reconstruction:
    def __init__(self):
        self.views = []

    def add(self, stamp, seconds, name, hint, rows):
        self.views.append((stamp, seconds, name, hint, rows))

    def collect(self):
        files = defaultdict(lambda: defaultdict(list))
        previous = None
        for stamp, seconds, name, hint, rows in self.views:
            if not rows:
                previous = None
                continue
            if not name and previous:
                prev_name, prev_rows, prev_hint = previous
                overlap = rows.keys() & prev_rows.keys()
                compared = [n for n in overlap if rows[n][0].strip() or prev_rows[n][0].strip()]
                matches = [rows[n][0].strip() for n in compared if rows[n][0] == prev_rows[n][0]]
                distinct = {text for text in matches if len(text) >= 8}
                substantive = any(not re.match(r"(?:from|import|using|package|include)\b", text)
                                  for text in distinct)
                # Shared imports and braces occur at the same positions in unrelated files.
                # A truncated breadcrumb can veto attribution, never establish it alone.
                compatible = not hint or prev_name.startswith(hint) or (
                    prev_name.startswith("Fragmento sin ruta ") and hint == prev_hint)
                if len(distinct) >= 2 and substantive and len(matches) >= .8*len(compared) and compatible:
                    name = prev_name
            if not name:
                name = f"Fragmento sin ruta {stamp.replace(':','-')}"
            for n, (text, sure) in rows.items():
                files[name][n].append((text, sure, stamp, seconds))
            previous = name, rows, hint
        return files

    def export(self, out, video):
        files = self.collect()
        if not files:
            out.write('<p id="archivos">No se pudo asociar código a números de línea en estas vistas.</p>')
            return
        packed = io.BytesIO()
        out.write('<h2 id="archivos">Archivos reconstruidos</h2>'
                  '<p>Cada texto termina en la última línea reconocida. Los saltos de numeración aparecen entre corchetes.</p>')
        used = set()
        index = ["archivo\tlinea_inicial\tlinea_final\tsegundo\testado\tsegundos_observados"]
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, numbered in files.items():
                # OCR names are display data. Never write them as filesystem paths.
                parts = [re.sub(r"[^\w .@+-]", "_", p).strip(" .")[:100] or "_"
                         for p in PurePosixPath(path).parts if p not in ("/", ".", "..")]
                parts = ["_" + p if DEVICE.match(p) else p for p in parts]
                base = "/".join(parts) or "fragmento"
                member = base + ".txt"
                suffix = 1
                while member.casefold() in used:
                    suffix += 1
                    member = f"{base}-{suffix}.txt"
                used.add(member.casefold())
                out.write("<h3>"+html.escape(path)+"</h3>")
                content, spans = [], []
                prev = 0
                for n, observations in sorted(numbered.items()):
                    tally = Counter(o[0] for o in observations)
                    reliable = Counter(o[0] for o in observations if o[1])
                    chosen = max(tally, key=lambda text: (tally[text], reliable[text]))
                    _, _, stamp, seconds = next(o for o in observations if o[0] == chosen)
                    if n > prev+1:
                        content.append(f"[Líneas {prev+1}–{n-1} sin capturar]")
                        index.append(f"{member}\t{prev+1}\t{n-1}\t\tsin capturar\t")
                    conflict = len(tally) > 1
                    other_views = tuple(dict.fromkeys((s,t) for _,_,s,t in observations if s != stamp))
                    if spans and prev == n-1 and spans[-1][2:] == [stamp, seconds, conflict, other_views]:
                        spans[-1][1] = n
                    else:
                        spans.append([n, n, stamp, seconds, conflict, other_views])
                    content.append(chosen)
                    prev = n
                payload = "\n".join(content).encode("utf-8")
                archive.writestr(member, payload)
                encoded = base64.b64encode(payload).decode("ascii")
                out.write(f'<a download="{html.escape(member.split("/")[-1], quote=True)}" href="data:text/plain;charset=utf-8;base64,{encoded}">Descargar texto del archivo</a><ul>')
                for a,b,stamp,seconds,conflict,other_views in spans:
                    flag = " · lecturas distintas" if conflict else ""
                    observed = ",".join(f"{t:g}" for t in (seconds, *(t for _,t in other_views)))
                    index.append(f"{member}\t{a}\t{b}\t{seconds:g}\t" +
                                 ("lecturas distintas" if conflict else "visible") + "\t" + observed)
                    seek = f' · <a href="{quote(video, safe="")}#t={seconds:g}">Ver vídeo</a>' if video else ""
                    also = "".join(f' · <a href="#vista-{s}">{s}</a>' for s,_ in other_views)
                    out.write(f'<li>Líneas {a}–{b} · <a href="#vista-{stamp}">{stamp}</a>{seek}{also}{flag}</li>')
                out.write("</ul>\n")
            archive.writestr("indice.tsv", "\n".join(index).encode("utf-8-sig"))
        encoded = base64.b64encode(packed.getvalue()).decode("ascii")
        out.write(f'<p><a download="archivos-reconstruidos.zip" href="data:application/zip;base64,{encoded}">Descargar todos los archivos (.zip)</a></p>\n')
