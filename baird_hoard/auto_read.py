"""Choose the local reader from visible layout. A numbered editor may occupy part of the screen."""
import re

CODE = re.compile(
    r"^(?:async\s+)?(?:def|class|from|import|return|raise|if|elif|else|for|while|try|except|with|"
    r"function|const|let|public|private|namespace|package|subgraph|graph|flowchart|end)\b|"
    r"^(?:[)\]};,{]|[@#]|//|%%)|(?:-->|=>|::|->)|"
    r"^[\w.\[\]\"'-]+\s*(?:=(?!=)|:\s*\S+[,}]?$)|[({;][})\];,]*$"
)


def code_layout(blocks):
    rows = []
    for kind, (x,y,r,b), text in blocks:
        if kind in ("image", "figure", "chart", "table") and (r-x)*(b-y) > 35000:
            return False
        if kind in ("header","footer","page_number"):
            continue
        rows.extend(line.strip() for line in text.splitlines() if line.strip())
    signals = sum(bool(CODE.search(line)) for line in rows)
    return signals >= 2 and signals >= len(rows)*.38


def numbered_editor(image, *, geometry=None):
    """Find a tightly aligned number gutter and its physical separator; never crop on syntax alone."""
    import numpy as np
    from scipy import ndimage as nd
    if image.width < 550 or image.height < 250:
        return None
    gray = np.asarray(image.convert('L'))
    # Search both contrast polarities, so the gutter need not share the desktop theme.
    for ink in (gray < 167, gray > 185):
        blobs, _ = nd.label(nd.maximum_filter(ink, size=(3,5)))
        columns = {}
        boxes = []
        for sl in nd.find_objects(blobs):
            if sl is None:
                continue
            y,x = sl
            h, w = y.stop-y.start-2, x.stop-x.start-4
            if 5 <= h <= 28 and 2 <= w <= h*3.5 and x.start < image.width*.75:
                boxes.append(((y.start+y.stop)/2, x.start+2, x.stop-2))
                for anchor in (x.start+2,x.stop-2):
                    columns.setdefault(round(anchor/4),[]).append(((y.start+y.stop)/2, x.stop-2, h))
        best = None
        for group in columns.values():
            if len(group)<10:
                continue
            group.sort()
            for start in range(len(group)-9):
                gap = group[start+1][0]-group[start][0]
                if not 11 <= gap <= 48 or not 1.15 <= gap/group[start][2] <= 3.8:
                    continue
                stop = start+2
                while stop < len(group) and abs(group[stop][0]-group[stop-1][0]-gap)<=1.5:
                    stop += 1
                run = group[start:stop]
                if len(run)>=10 and (best is None or len(run)>len(best[0])):
                    best = run, gap
        if best is None:
            continue
        run,gap = best
        right = max(t[1] for t in run)
        left = min(x for y,x,r in boxes if abs(y-run[0][0])<=1 and r==run[0][1])
        aligned = {round((y-run[0][0])/gap) for y,x,r in boxes
                   if (abs(x-left)<=2 or abs(r-right)<=2) and
                   abs((y-run[0][0])/gap-round((y-run[0][0])/gap))<.14}
        first,last = 0,len(run)-1
        while first-1 in aligned:
            first -= 1
        while last+1 in aligned:
            last += 1
        top = max(0,round(run[0][0]+(first-.5)*gap))
        bottom = min(image.height,round(run[0][0]+(last+.5)*gap))
        # A divider persists through consecutive text rows; ordinary letter strokes do not.
        band = ink[top:bottom, right+1:min(image.width,right+round(8*gap))]
        if band.size == 0:
            continue
        vertical = nd.minimum_filter1d(band, size=round(gap*1.2), axis=0, mode="constant")
        dividers = np.flatnonzero(vertical.mean(axis=0)>.04)
        if not len(dividers):
            continue
        chunks = np.split(dividers,np.flatnonzero(np.diff(dividers)>1)+1)
        divider = next((c for c in chunks if len(c)<=gap*.5),None)
        if divider is None:
            continue
        after_divider = right + 2 + int(divider[-1])
        x0 = min(image.width, after_divider + round(gap * .52))
        # Some editors place the first character immediately after the divider.
        margin_ink = np.flatnonzero(ink[top:bottom, after_divider:x0].any(axis=0))
        if margin_ink.size:
            x0 = after_divider + max(0, int(margin_ink[0]) - 2)
        region = ink[top:bottom,x0:]
        if region.shape[1]<150:
            continue
        # Remove a distant narrow scrollbar, never truncate a word after a long indent.
        occupied = np.flatnonzero(region.sum(axis=0)>1)
        if not len(occupied):
            continue
        last = occupied[-1]
        margin = occupied < region.shape[1] - round(2.5*gap)
        if margin.any() and (~margin).any() and occupied[~margin][0]-occupied[margin][-1] > 5*gap:
            # A pointer beside the scrollbar can span more than one text row's width.
            # Require an actual continuous vertical stroke before excluding the margin.
            start_ui = occupied[~margin][0]
            right_edge = region[:, start_ui:]
            separator = gray[top:bottom, x0+occupied[margin][-1]+1:x0+start_ui].astype(np.int16)
            panel_edge = (abs(np.diff(separator, axis=1)) > 4).mean(axis=0)
            if ((panel_edge > .8).any() and
                    nd.minimum_filter1d(right_edge, size=round(3*gap), axis=0, mode="constant").any()):
                last = occupied[margin][-1]
        x1 = min(image.width,x0+int(last)+round(gap))
        if x1-x0>=150:
            if geometry is not None:
                geometry.update(gutter=(left, right), step=float(gap),
                                center=float(run[0][0]+first*gap))
            return x0,top,x1,bottom
    return None
