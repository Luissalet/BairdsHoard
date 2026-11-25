/* Review a single saved view at a time. OCR remains a draft until the reader checks its image. */
const reviewDialog = document.getElementById('review-dialog');
const reviewEl = name => document.getElementById('review-' + name);
let reviewDoc = null;
let reviewRequest = 0;

async function openDocumentReview(item) {
  reviewDoc = {name: item.name, stamps: [], entries: {}, key: '', index: 0, saved: true};
  reviewDialog.showModal();
  await loadReviewView(0);
}

async function loadReviewView(index) {
  const doc = reviewDoc;
  const request = ++reviewRequest;
  reviewEl('focus').hidden = true;
  for (const control of ['index', 'next', 'text', 'ok', 'download']) reviewEl(control).disabled = true;
  reviewEl('count').textContent = 'Cargando vista…';
  try {
    const view = await api('/api/media/' + encodeURIComponent(doc.name) + '/review/' + index);
    if (!reviewDialog.open || request !== reviewRequest) return;
    const key = 'baird/review/' + doc.name + '/' + view.version;
    if (doc.key !== key) {
      doc.key = key;
      try { doc.entries = JSON.parse(localStorage.getItem(key)) || {}; }
      catch { doc.entries = {}; }
      doc.stamps = view.stamps;
      reviewEl('index').replaceChildren(...doc.stamps.map((stamp, i) => new Option(stamp, String(i))));
    }
    doc.index = index;
    reviewEl('index').value = String(index);
    reviewEl('index').disabled = false;
    reviewEl('text').disabled = false;
    reviewEl('ok').disabled = false;
    reviewEl('image').src = view.image;
    reviewEl('zoom').setAttribute('aria-pressed', 'false');
    reviewEl('notes').textContent = view.notes;
    reviewEl('text').value = doc.entries[index]?.text ?? view.text;
    reviewEl('ok').checked = !!doc.entries[index]?.ok;
    updateReviewCounts();
    prepareLens(view, request);
  } catch (err) {
    if (request === reviewRequest) { reviewDialog.close(); fail(err); }
  }
}

async function prepareLens(view, request) {
  const image = reviewEl('image');
  if (!view.areas?.length) return;
  try { await image.decode(); } catch { return; }
  if (request !== reviewRequest || !reviewDialog.open) return;
  const areas = view.areas.filter(([, , , x, y]) => x < image.naturalWidth && y < image.naturalHeight);
  if (!areas.length) return;
  reviewDoc.areas = areas;
  reviewDoc.original = view.text;
  reviewEl('area').replaceChildren(...areas.map(([row, start, end], i) => {
    let label = 'Línea ' + (row + 1);
    if (start >= 0) label += end - start > 1 ? ' · columnas ' + (start + 1) + '–' + end : ' · columna ' + (start + 1);
    return new Option(label, String(i));
  }));
  reviewEl('focus').hidden = false;
  drawLens(reviewEl('lens'), true);
}

function drawLens(canvas, mark) {
  const image = reviewEl('image');
  const [, start, , x, y, w, h] = reviewDoc.areas[Number(reviewEl('area').value)];
  const span = w || image.naturalWidth;
  const padX = start < 0 ? 6 : Math.max(55, h * 4);
  const left = Math.max(0, x - padX), top = Math.max(0, y - h);
  const width = Math.min(image.naturalWidth, x + span + padX) - left;
  const height = Math.min(image.naturalHeight, y + h * 2) - top;
  const scale = Math.min(start < 0 ? 2 : 4, 4096 / width, 640 / height);
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(image, left, top, width, height, 0, 0, canvas.width, canvas.height);
  if (mark && start >= 0) {
    ctx.strokeStyle = '#e16c14'; ctx.lineWidth = 2;
    ctx.strokeRect((x-left)*scale-1, (y-top)*scale-1, w*scale+2, h*scale+2);
  }
}

reviewEl('area').addEventListener('change', () => {
  drawLens(reviewEl('lens'), true);
  const field = reviewEl('text');
  if (field.value !== reviewDoc.original) return;
  const [row, start, end] = reviewDoc.areas[Number(reviewEl('area').value)];
  const lines = field.value.split('\n');
  const offset = lines.slice(0, row).reduce((n, line) => n + line.length + 1, 0);
  const units = n => Array.from(lines[row]).slice(0, n).join('').length;
  field.setSelectionRange(offset + (start < 0 ? 0 : units(start)), offset + (end < 0 ? lines[row].length : units(end)));
});

reviewEl('crop').addEventListener('click', () => {
  const canvas = document.createElement('canvas');
  drawLens(canvas, false);
  const [row] = reviewDoc.areas[Number(reviewEl('area').value)];
  const stamp = reviewDoc.stamps[reviewDoc.index].replace(':', '-');
  const a = document.createElement('a');
  a.download = reviewDoc.name.replace(/\.html$/i, '') + ' - ' + stamp + ' - línea ' + (row+1) + '.png';
  a.href = canvas.toDataURL('image/png'); a.click();
});

function updateReviewCounts() {
  const doc = reviewDoc;
  const count = doc.stamps.reduce((n, _, i) => n + (doc.entries[i]?.ok ? 1 : 0), 0);
  reviewEl('next').disabled = count === doc.stamps.length;
  reviewEl('download').disabled = count === 0;
  reviewEl('count').textContent = count + ' de ' + doc.stamps.length + ' vistas cotejadas' +
    (doc.saved ? '' : ' · El navegador no pudo guardar el borrador');
}

function saveReview(edited) {
  const doc = reviewDoc;
  if (edited) reviewEl('ok').checked = false;
  doc.entries[doc.index] = {text: reviewEl('text').value, ok: reviewEl('ok').checked};
  try { localStorage.setItem(doc.key, JSON.stringify(doc.entries)); doc.saved = true; }
  catch { doc.saved = false; }
  updateReviewCounts();
}

reviewEl('text').addEventListener('input', () => saveReview(true));
reviewEl('ok').addEventListener('change', () => saveReview(false));
reviewEl('index').addEventListener('change', event => loadReviewView(Number(event.target.value)));
reviewEl('next').addEventListener('click', () => {
  for (let step = 1; step <= reviewDoc.stamps.length; step++) {
    const i = (reviewDoc.index + step) % reviewDoc.stamps.length;
    if (!reviewDoc.entries[i]?.ok) { loadReviewView(i); break; }
  }
});
reviewEl('zoom').addEventListener('click', event => {
  const btn = event.currentTarget;
  btn.setAttribute('aria-pressed', String(btn.getAttribute('aria-pressed') !== 'true'));
});
reviewEl('download').addEventListener('click', () => {
  const doc = reviewDoc;
  const text = [doc.name, ...doc.stamps.map((stamp, i) =>
    stamp + '\n' + (doc.entries[i]?.ok ? doc.entries[i].text : '[Pendiente de revisar]'))].join('\n\n');
  const url = URL.createObjectURL(new Blob([text], {type: 'text/plain;charset=utf-8'}));
  const a = document.createElement('a');
  a.href = url; a.download = doc.name.replace(/\.html$/i, '') + ' - revisado.txt'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
});
reviewEl('close').addEventListener('click', () => reviewDialog.close());
reviewDialog.addEventListener('close', () => {
  reviewRequest++; reviewDoc = null; reviewEl('image').removeAttribute('src');
  reviewEl('text').value = ''; reviewEl('notes').textContent = '';
});
