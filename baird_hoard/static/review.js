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
  } catch (err) {
    if (request === reviewRequest) { reviewDialog.close(); fail(err); }
  }
}

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
