"""Live capture feedback using the Windows OCR word boxes already read for the transcript."""
from collections import defaultdict
from statistics import median
import re

from .source_rebuild import source_label


def gutter(lines, *, centers=None):
    columns = defaultdict(list)
    for line in lines:
        for x, y, w, h, word in line[5] if len(line) > 5 else ():
            if not re.fullmatch(r"[0-9]{1,7}", word) or int(word) == 0:
                continue
            for edge, pos in (("left", x), ("right", x+w)):
                for shift in (0, 4):
                    columns[edge, shift, (pos+shift)//8].append((y+h/2, int(word), h))
    best = []
    for entries in columns.values():
        rows = sorted(set(entries))
        if len(rows) < max(4, len(best)):
            continue
        # Consistent vertical spacing and printed consecutive numbers reject unrelated labels.
        for i, (y, n, h) in enumerate(rows[:-2]):
            for yy, nn, _ in rows[i+1:i+4]:
                if not 1 <= nn-n <= 3:
                    continue
                step = (yy-y)/(nn-n)
                if not .9*h <= step <= 4.5*h:
                    continue
                support = [(cy, num) for cy, num, _ in rows if abs(cy-y-(num-n)*step) <= max(3,step*.22)]
                if len(support) > len(best):
                    best = support
    if len(best) < 4:
        return []
    if centers is not None:
        centers.update((number, cy) for cy, number in best)
    groups = []
    for _, number in sorted(best):
        if groups and number <= groups[-1][1]+3:
            groups[-1] = (groups[-1][0], max(number, groups[-1][1]))
        else:
            groups.append((number, number))
    return [r for r in groups if r[1]-r[0] >= 2]


def merge_ranges(ranges):
    merged = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1]+1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def edge_profile(gray):
    if not gray:
        return None, 0
    # Ratio of strong to moderate local gradients; compare only within this recording.
    left = memoryview(gray)[::3]
    right = memoryview(gray)[1::3]
    edges = [abs(a-b) for a,b in zip(left,right)]
    return (sum(e >= 45 for e in edges)/max(1, sum(e >= 8 for e in edges)),
            sum(e >= 3 for e in edges)/max(1,len(edges)))


def detail_level(gray):
    return edge_profile(gray)[0]


def header_identity(lines):
    """Windows can split a path across word boxes. Compare its shape without asserting a filename."""
    for x,y,w,h,text,*_ in lines:
        if not re.match(r"^[\w.:-]+[/\\][\w./\\ -]+",text.strip()):
            continue
        near = sorted((row[0],row[4]) for row in lines if row[0] >= x and
                      abs(row[1]-y) <= max(2,h*.4) and row[0] < x+w+16*h)
        joined = "".join(s for _,s in near)
        match = re.match(r"([\w./\\ -]+\.\s*[A-Za-z]{1,8})\b",joined)
        if match:
            return re.sub(r"[\s_-]", "", match[1]).casefold()
    return ""


class CaptureFeedback:
    def __init__(self):
        self.file = ""
        self.hint = ""
        self.identity = ""
        self.segment = 0
        self.history = {}
        self.gaps = {}
        self.last_range = None
        self.baseline = []
        self.previous = set()
        self.switches = 0
        self.row_text = {}
        self.last_time = None
        self.previous_phrases = set()

    def lost_context(self):
        self.last_range = None
        self.file = self.hint = self.identity = ""
        self.segment += 1
        self.previous.clear()
        self.previous_phrases.clear()
        self.switches = 0
        self.row_text.clear()

    def observe(self, lines, seconds, gray=b"", *, failed=False):
        if failed:
            self.lost_context()
            return {"kind": "unreadable", "text": "El OCR no ha podido leer esta vista. Pausa en el documento.", "seconds": seconds}
        texts = [str(line[4]) for line in lines]
        name, hint = source_label(texts)
        identity = re.sub(r"[\s_-]", "", name).casefold() if name else header_identity(lines)
        centers = {}
        ranges = gutter(lines, centers=centers)
        changed = ((name and self.file and name != self.file) or (hint and self.hint and hint != self.hint)
                   or (identity and self.identity and identity != self.identity))
        if changed:
            self.lost_context()
            self.baseline.clear()
        if name:
            self.file = name
        if hint:
            self.hint = hint
        if identity:
            self.identity = identity
        # A fuzzy header fingerprint can detect a transition, but distinct confirmed paths
        # (including punctuation and case) must never share captured or missing lines.
        key = ("path:" + self.file if self.file else "header:" + self.identity if self.identity
               else "hint:" + self.hint if self.hint else "segment:" + str(self.segment))
        tokens = {word.casefold() for text in texts for word in re.findall(r"[\w.]{3,}", text)}
        density = sum(len(t.split()) for t in texts)
        sharp, texture = edge_profile(gray)
        old_count = median(p[0] for p in self.baseline) if self.baseline else 0
        old_sharp = median(p[1] for p in self.baseline) if self.baseline else 0
        old_texture = median(p[2] for p in self.baseline) if self.baseline else 0
        def notice(kind, message):
            return {"kind": kind, "text": message, "seconds": seconds}

        interval = seconds-self.last_time if self.last_time is not None else 0
        self.last_time = seconds
        if (old_count >= 8 and sharp is not None and old_sharp > .06 and sharp < old_sharp*.53
                and density < old_count*.8 and texture >= old_texture*.24):
            self.lost_context()
            return notice("blur", "El texto ha perdido nitidez. Detén el desplazamiento hasta que se lea bien.")
        if old_count >= 8 and (density < old_count*.33 or (self.last_range and not ranges and
                len(tokens & self.previous) < max(2, len(self.previous)*.12) and not name)):
            self.lost_context()
            return notice("visibility", "Ya no se distingue el texto anterior. Comprueba si una ventana lo tapa y vuelve a mostrarlo.")
        if sharp is not None and density >= 8:
            self.baseline = (self.baseline+[(density, sharp, texture)])[-5:]
        bottom = max((row[1]+row[3] for row in lines), default=0)
        phrases = set()
        for row in lines:
            if .17*bottom <= row[1]+row[3]/2 <= .94*bottom:
                words = re.findall(r"[\w.]+", row[4].casefold())
                phrases.update(zip(words, words[1:]))
        if self.previous_phrases and phrases:
            shared = len(self.previous_phrases & phrases)/min(len(self.previous_phrases),len(phrases))
            self.switches = self.switches+1 if shared < .12 and 0 < interval <= 1.6 else 0
        else:
            self.switches = 0
        self.previous_phrases = phrases
        self.previous = tokens
        if ranges:
            # A floating window can cover the middle of the code while the gutter and
            # surrounding text remain legible. Compare only rows demonstrated in both views.
            ordered = sorted(centers)
            pitch = median((centers[j]-centers[i])/(j-i) for i,j in zip(ordered,ordered[1:]))
            first = ordered[0]
            row_words = defaultdict(set)
            for line in lines:
                for _,y,_,h,word in line[5] if len(line) > 5 else ():
                    row = first+round((y+h/2-centers[first])/pitch)
                    if row in centers and abs(y+h/2-centers[row]) < pitch*.48 and len(word)>2 and not word.isdigit():
                        row_words[row].add(word.casefold())
            stable = [n for n,words in self.row_text.items() if words and words & row_words[n]]
            obscured = [n for n in sorted(centers) if self.row_text.get(n) and not self.row_text[n] & row_words[n]]
            if len(stable) >= 2:
                for lo,hi in merge_ranges([(n,n) for n in obscured]):
                    if hi-lo >= 2 and min(stable)<lo and max(stable)>hi:
                        return notice("visibility", f"Ha cambiado la lectura de las líneas {lo}–{hi}. Comprueba si hay una ventana encima.")
            self.row_text = dict(row_words)
            old = self.last_range
            # Only gaps between successive views are actionable. A fold already on screen is not a scroll gap.
            missing = self.gaps.setdefault(key, [])
            before = bool(missing)
            if old and old[0] == key:
                lo, hi = ranges[0][0], ranges[-1][1]
                if lo > old[2]+1:
                    missing.append((old[2]+1,lo-1))
                elif hi < old[1]-1:
                    missing.append((hi+1,old[1]-1))
            self.last_range = (key,ranges[0][0],ranges[-1][1])
            covered = self.history[key] = merge_ranges(self.history.get(key, [])+ranges)
            for a,b in covered:
                missing = [(x,y) for lo,hi in missing for x,y in ((lo,min(hi,a-1)),(max(lo,b+1),hi)) if x<=y]
            self.gaps[key] = missing = merge_ranges(missing)
            if missing:
                lo,hi = missing[0]
                where = f" de {self.file}" if self.file and identity else " si sigues en el mismo archivo"
                gap = f"Falta la línea {lo}" if lo == hi else f"Faltan las líneas {lo}–{hi}"
                return notice("gap", f"{gap}{where}. Vuelve a ese tramo y pausa.")
            if before:
                return notice("recovered", "Ya se ha capturado el tramo pendiente.")
        if self.switches >= 2:
            return notice("speed", "Las vistas cambian sin texto común. Desplázate más despacio y deja unas líneas de solape.")
        return None
