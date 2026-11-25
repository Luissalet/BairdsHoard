"""Read the monospaced cells in a code crop using examples from its other OCR lines."""
import re


def quote_run(patch, pitch):
    """Count repeated straight double quotes at an ink edge, including italic quote strokes."""
    import numpy as np
    from scipy.signal import find_peaks
    if patch.size == 0 or patch.shape[1] < pitch:
        return 0
    patch = patch[:, :int(7*pitch)]
    top = min(len(patch), max(2, round(.65*pitch)))
    profile = patch[:top].sum(axis=0)
    strength = profile[:max(1,round(pitch))].max()
    if strength < 40:
        return 0
    peaks = find_peaks(np.r_[0,profile,0], prominence=strength*.27)[0]-1
    if len(peaks)<4 or peaks[0] > pitch*.42:
        return 0
    count = 0
    for i in range(0,len(peaks)-1,2):
        left,right = peaks[i:i+2]
        if (abs(left-peaks[0]-count*pitch)>max(.8,pitch*.12) or
                not pitch*.15 <= right-left <= pitch*.55 or
                profile[left:right+1].min() > .52*min(profile[left],profile[right])):
            break
        strokes = patch[:,left:right+1]
        if strokes[top:].sum() > .12 * strokes[:top].sum():
            break
        count += 1
    return count if count>=2 else 0


def restore_quotes(text, patch, pitch):
    """An edge correction must improve agreement with the measured length of the entire line."""
    if not text or not text.strip('"'):
        return text
    changed = text
    if text.startswith('"'):
        n = quote_run(patch,pitch)
        if n:
            changed = '"'*n + changed.lstrip('"')
    if text.endswith('"'):
        n = quote_run(patch[:,::-1],pitch)
        if n:
            changed = changed.rstrip('"') + '"'*n
    extent = (patch.shape[1]-1)/pitch + .5
    return changed if abs(len(changed)-extent) < abs(len(text)-extent) else text


def glyph_corrections(ink, readings, pitch, step):
    """Return evidence per line. Labels come only from geometrically consistent, concordant OCR."""
    import numpy as np
    from scipy.ndimage import map_coordinates

    if len(readings) < 3 or not 7 <= step <= 72:
        return {}
    # Refine the physical grid at dark-stroke valleys. Its origin belongs to this crop.
    height, width = ink.shape
    count = min(512, int(width / pitch))
    if count < 4:
        return {}
    columns = np.arange(count)
    projection = ink.sum(axis=0)
    phases = np.linspace(-pitch*.28, pitch*.48, 13)
    origin = min(phases, key=lambda dx: projection[np.clip(np.rint(dx+columns*pitch).astype(int), 0, width-1)].sum()) + .5
    # Snap vertical centers to the observed line spacing, including sparse punctuation rows.
    centers = np.asarray([(a+b)/2 for a,b,*_ in readings])
    residual = (centers - centers[0] + step/2) % step - step/2
    center0 = centers[0] + np.median(residual)
    rows = []
    bank = {}
    for row, (a,b,left,right,detail,context,complete) in enumerate(readings):
        if right > origin+count*pitch+1:
            rows.append(None)
            continue
        center = center0 + round((centers[row]-center0)/step)*step
        if abs(center-centers[row]) > step*.4:
            rows.append(None)
            continue
        # Isolate this line, retaining the baseline so quotes and commas stay distinguishable.
        y = center + ((np.arange(20)+.5)/20-.5)*step
        x = origin + (columns[:,None]+(np.arange(8)+.5)/8)*pitch - .5
        yy, xx = np.broadcast_arrays(y[None,:,None],x[:,None,:])
        values = map_coordinates(ink[a:b], [yy-a,xx], order=1, mode='constant',
                                 prefilter=False).reshape(count,-1).astype('float32')
        mask = values.max(axis=1) > 39
        occupied = np.flatnonzero(mask)
        if not occupied.size:
            rows.append(None)
            continue
        first,last = int(occupied[0]),int(occupied[-1]+1)
        values /= np.maximum(1, np.linalg.norm(values,axis=1,keepdims=True))
        glyphs = values[first:last]
        filled = mask[first:last]
        rows.append((first, glyphs, filled))
        if (not complete or detail != context or len(detail) != len(glyphs) or
                any(not ch.isprintable() or ch.isspace() == bit for ch,bit in zip(detail,filled))):
            continue
        # A different line must support any substitution. Bound the bank even for noisy screenshots.
        for ch, vector in zip(detail, glyphs):
            if ch != ' ' and (ch in bank or len(bank) < 192):
                examples = bank.setdefault(ch, [])
                if len(examples)<32 and not any(r==row and np.dot(v,vector)>.99 for r,v in examples):
                    examples.append((row, vector))
    if len(bank)<8:
        return {}
    chars = sorted(bank)
    exemplars = np.stack([v for ch in chars for _,v in bank[ch]])
    donors = np.asarray([r for ch in chars for r,_ in bank[ch]])
    starts = np.cumsum([0]+[len(bank[ch]) for ch in chars[:-1]])
    result = {}
    for row, cells in enumerate(rows):
        if cells is None:
            continue
        first, vectors, filled = cells
        scores = vectors @ exemplars.T
        scores[:,donors==row] = -1
        scores = np.maximum.reduceat(scores, starts, axis=1)
        best = scores.argmax(axis=1)
        peak = scores[np.arange(len(scores)), best].copy()
        scores[np.arange(len(scores)), best] = -1
        trusted = (peak >= .978) & (peak - scores.max(axis=1) >= .035)
        result[row] = (first, ''.join(' ' if not bit else chars[i] if ok else '\x00'
                                      for bit,i,ok in zip(filled,best,trusted)))
    return result


def word_grid(ink, a, b, left, right, pitch):
    """Choose the grid with least ink on cell boundaries, allowing an italic slant."""
    import numpy as np
    from scipy.ndimage import map_coordinates
    block=ink[a:b]
    start=int(left/pitch)
    end=min(int(round(right/pitch))+2,int(block.shape[1]/pitch))
    js=np.arange(max(0,start-1),end)
    if not len(js) or len(js)>512: return None
    y=np.arange(b-a)[:,None]
    best=1e20
    for slope in (0.,-.2,-.35,.2):
        for origin in np.arange(-.25*pitch,.62*pitch,.5):
            edges=origin+pitch*js+slope*(y-(b-a)/2)
            valley=map_coordinates(block,np.broadcast_arrays(y,edges),order=1,prefilter=False).sum()
            if valley<best:
                best=valley
                dx=origin+slope*(y-(b-a)/2)
    x=dx[:,:,None]+pitch*(js[None,:,None]+(np.arange(7)+.5)/7)
    vals=map_coordinates(block,np.broadcast_arrays(y[:,:,None],x),order=1,prefilter=False)
    filled=(vals[:, :,1:-1].max(0).max(1)>39)|(vals.sum((0,2))>block.max()*3.5)
    occupied = np.flatnonzero(filled)
    if not len(occupied):
        return None
    first, last = int(occupied[0]), int(occupied[-1]+1)
    mask = ''.join('x' if bit else ' ' for bit in filled[first:last])
    offset = int(js[first])
    bounds = [(max(0, int(np.floor((offset+m.start())*pitch+dx.min()))),
               min(ink.shape[1], int(np.ceil((offset+m.end())*pitch+dx.max()))))
              for m in re.finditer('x+',mask)]
    return mask, bounds


def apply_word_grid(text, grid, reread):
    """Accept new spacing or an extra OCR reading only when it fits the photographed word widths."""
    if not grid:
        return text
    mask, bounds = grid
    observed = text.lstrip()
    groups = mask.split()
    lengths = [len(w) for w in groups]
    words = observed.split()
    compact = ''.join(words)
    if not words or len(groups) < 2 or len(groups)>40:
        return text
    if [len(w) for w in words] == lengths:
        chosen = words
    else:
        if abs(len(compact)-sum(lengths))>max(3, len(compact)*.18):
            return text
        # Use one extra model call with wider visual gaps while preserving each word's pixels.
        alternative = reread(bounds).split()
        if [len(w) for w in alternative] == lengths:
            from difflib import SequenceMatcher
            if SequenceMatcher(None, compact, ''.join(alternative), autojunk=False).ratio()<.80:
                return text
            chosen = alternative
        elif len(compact)==sum(lengths):
            cursor = 0
            chosen = []
            for length in lengths:
                chosen.append(compact[cursor:cursor+length])
                cursor += length
        else:
            return text
    tokens = iter(chosen)
    return text[:len(text)-len(observed)] + re.sub('x+', lambda _: next(tokens), mask)


def _distances(shape, word):
    table = [list(range(len(word)+1))]
    for i, ch in enumerate(shape, 1):
        previous = table[-1]
        row = [i]
        for j, old in enumerate(word, 1):
            row.append(min(previous[j]+1, row[-1]+1, previous[j-1]+(ch not in ('\x00', old))))
        table.append(row)
    return table


def repair_word(word, shape):
    """An unread cell must align to the same OCR character in every minimum-edit alignment."""
    unknown = '\x00'
    if len(shape) == len(word):
        return ''.join(old if ch == unknown else ch for old,ch in zip(word,shape))
    if unknown not in shape:
        return shape
    if (max(len(word),len(shape)) > 128 or abs(len(word)-len(shape)) > 10 or
            shape.count(unknown) > len(shape)*.45):
        return word
    forward = _distances(shape,word)
    cost = forward[-1][-1]
    if cost > max(2,len(word)*.4):
        return word
    reverse = _distances(shape[::-1],word[::-1])
    output = list(shape)
    for i,ch in enumerate(shape):
        if ch != unknown:
            continue
        tail = reverse[len(shape)-i-1]
        prefix = forward[i]
        possible = set()
        for j, before in enumerate(prefix):
            # A path with no OCR character at this cell cannot supply a reliable letter.
            if before+1+tail[len(word)-j] == cost:
                return word
            if j < len(word) and before+tail[len(word)-j-1] == cost:
                possible.add(word[j])
        if len(possible) != 1:
            return word
        output[i] = possible.pop()
    return ''.join(output)


def reconcile(text, cells):
    first, visible = cells
    if '\x00' not in visible:
        return ' '*first + visible
    original = text.lstrip()
    measured = list(re.finditer(r'\S+', visible))
    words = list(re.finditer(r'\S+', original))
    if len(measured) != len(words):
        return text
    corrected = [repair_word(word.group(), cell.group()) for word,cell in zip(words,measured)]
    if all(len(s)==len(cell.group()) for s,cell in zip(corrected,measured)):
        # The word boundaries also agree with the cell grid. Keep the imaged internal spaces.
        tokens = iter(corrected)
        return text[:len(text)-len(original)] + re.sub(r'\S+', lambda _: next(tokens), visible)
    parts = list(original)
    for word,result in reversed(list(zip(words,corrected))):
        parts[word.start():word.end()] = result
    return text[:len(text)-len(original)]+''.join(parts)
