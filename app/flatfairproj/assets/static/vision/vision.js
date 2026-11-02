function initVisionPanel(panel, tries = 0) {
  const tabs = [...panel.querySelectorAll('[role=tab]')];

  const turnOn = tab => {
    tabs.forEach(t => {
      const active = t === tab;

      t.setAttribute('aria-selected', String(active));

      t.tabIndex = active ? 0 : -1;

      const content = document.getElementById(t.getAttribute('aria-controls'));

      if (content) content.hidden = !active;
    });
  };

  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => turnOn(tab));

    tab.addEventListener('keydown', e => {
      let next;

      if (e.key === 'ArrowRight') next = (i + 1) % tabs.length;

      if (e.key === 'ArrowLeft') next = (i + tabs.length - 1) % tabs.length;

      if (e.key === 'Home') next = 0;

      if (e.key === 'End') next = tabs.length - 1;

      if (next !== undefined) {
        e.preventDefault();

        turnOn(tabs[next]);

        tabs[next].focus();
      }
    });
  });

  const bindShape = shape => {
    const select = () => {
      const card = document.getElementById(shape.dataset.select);

      if (!card) return;

      panel.querySelectorAll('.selected').forEach(x => x.classList.remove('selected'));

      shape.classList.add('selected');

      card.classList.add('selected');

      card.focus({
        preventScroll: true
      });

      card.scrollIntoView({
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        block: 'nearest'
      });
    };

    shape.addEventListener('click', select);

    shape.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();

        select();
      }
    });

    document.getElementById(shape.dataset.select)?.addEventListener('click', e => {
      if (e.target.closest('a,summary')) return;

      panel.querySelectorAll('.selected').forEach(x => x.classList.remove('selected'));

      shape.classList.add('selected');

      e.currentTarget.classList.add('selected');
    });
  };

  panel.querySelectorAll('[data-select]').forEach(bindShape);



  const csrf = panel.querySelector('[name=csrfmiddlewaretoken]')?.value || '';

  panel.querySelectorAll('.vision-stage[data-pick-url]').forEach(stage => bindPick(panel, stage, csrf, bindShape));

  if (panel.dataset.visionState !== 'pending') return;

  let fails = 0;

  const stamp = node => node.dataset.visionState + '|' + [...node.querySelectorAll('[role=tabpanel]')].map(x => x.id + ':' + Boolean(x.querySelector('[data-vision-pending]'))).join('|') + node.querySelector('.vision-count')?.textContent;

  function finish(message) {
    panel.dataset.visionState = 'complete';

    panel.querySelectorAll('[data-vision-pending]').forEach(status => {
      status.removeAttribute('data-vision-pending');



      status.textContent = message;

      const retry = document.createElement('a');

      retry.href = location.href;

      retry.textContent = 'Refresh results';

      status.append(' ', retry);
    });
  }

  function replace(next) {
    const active = panel.contains(document.activeElement) ? document.activeElement : null,
      inputs = 'summary,button,a,[tabindex]',
      focusId = active?.id,
      focusAt = [...panel.querySelectorAll(inputs)].indexOf(active),
      tab = panel.querySelector('[role=tab][aria-selected=true]')?.id;

    panel.replaceChildren(...next.childNodes);

    panel.dataset.visionState = next.dataset.visionState;

    initVisionPanel(panel, tries);


    if (tab) panel.querySelector('#' + CSS.escape(tab))?.click();

    if (active) {
      const swap = focusId ? panel.querySelector('#' + CSS.escape(focusId)) : panel.querySelectorAll(inputs)[focusAt];

      (swap || panel.querySelector('summary'))?.focus({
        preventScroll: true
      });
    }
  }

  async function poll() {
    if (!panel.isConnected) return;

    if (document.hidden) {
      setTimeout(poll, 5000);

      return;
    }

    if (tries++ >= 60) {
      finish('This is taking longer than usual. Refresh the page later.');

      return;
    }

    const stopper = new AbortController(),
      timeout = setTimeout(() => stopper.abort(), 10000);

    try {
      const reply = await fetch(location.href, {
        headers: {
          Accept: 'text/html'
        },
        credentials: 'same-origin',
        signal: stopper.signal
      });

      if (!reply.ok) throw Error('Analysis request failed');

      const doc = new DOMParser().parseFromString(await reply.text(), 'text/html'),
        next = doc.querySelector('.vision-panel');

      fails = 0;

      if (!next) {
        finish('We couldn’t check these photos. Try again later.');

        return;
      }

      if (stamp(next) !== stamp(panel)) {
        replace(next);

        return;
      }
    } catch (_) {
      if (++fails >= 3) {
        finish('We couldn’t check these photos. Try again later.');

        return;
      }
    } finally {
      clearTimeout(timeout);
    }

    setTimeout(poll, 5000);
  }

  setTimeout(poll, 5000);
}

const SVG_NS = 'http://www.w3.org/2000/svg';

const PICK_FAILED = 'Sorry, that didn’t work. Please try again.';

function make(tag, attrs = {}, text = '') {
  const node = Object.assign(document.createElement(tag), attrs);

  if (text) node.textContent = text;

  return node;
}

async function pickJson(url, options = {}) {
  const reply = await fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json', ...(options.headers || {}) }, ...options });

  const data = await reply.json().catch(() => ({}));


  if (!reply.ok && !data.message) data.message = PICK_FAILED;

  if (!reply.ok) data.status = 'failed';

  return data;
}

function pickCard(data) {
  const card = make('article', { id: 'vision-pick-' + data.id, className: 'vision-card picked', tabIndex: -1 });

  card.append(make('img', { src: data.crop, alt: data.label }));

  const body = make('div');

  body.append(make('p', { className: 'vision-card-name' }, data.label));

  if (data.confident) body.append(make('p', {}, 'You picked this'));

  const lens = make('a', { className: 'vision-lens', href: data.search, target: '_blank', rel: 'noopener noreferrer' }, 'Search with Google Lens');

  lens.dataset.lensCrop = data.crop;



  lens.dataset.lensWords = data.words;

  lens.append(make('span', { className: 'visually-hidden' }, ' (opens in new tab)'));

  body.append(lens);

  if (data.similar?.length) {
    const more = make('details', { className: 'vision-similar' });

    more.append(make('summary', {}, `Similar in other flats (${data.similar.length})`));

    data.similar.forEach(other => more.append(make('a', { href: other.url }, `${other.label} · ${other.town}`)));

    body.append(more);
  }

  card.append(body);

  return card;
}

function bindPick(panel, stage, csrf, bindShape) {
  const svg = stage.querySelector('svg'),
    view = svg.viewBox.baseVal,
    status = stage.parentElement.querySelector('[data-pick-status]'),
    cards = stage.closest('.vision-workspace')?.querySelector('.vision-cards');

  if (!status || !cards) return;

  let busy = false;

  const show = data => {
    panel.querySelectorAll('.selected').forEach(x => x.classList.remove('selected'));

    let card = document.getElementById('vision-pick-' + data.id),
      shape = svg.querySelector(`[data-select="vision-pick-${data.id}"]`);

    if (!card) {
      card = pickCard(data);

      cards.querySelector(':scope > p')?.remove();

      cards.prepend(card);
    }

    if (!shape) {
      shape = document.createElementNS(SVG_NS, 'polygon');

      shape.setAttribute('points', data.points);

      shape.setAttribute('class', 'picked');

      shape.setAttribute('data-select', 'vision-pick-' + data.id);

      shape.setAttribute('tabindex', '0');

      shape.setAttribute('role', 'button');

      shape.setAttribute('aria-label', 'Select ' + data.label);

      svg.append(shape);

      bindShape(shape);
    }

    shape.classList.add('selected');



    card.classList.add('selected');

    card.scrollIntoView({ behavior: 'auto', block: 'nearest' });
  };

  svg.addEventListener('click', async event => {
    if (event.target.closest('polygon') || busy) return;

    const box = svg.getBoundingClientRect(),
      x = (event.clientX - box.left) / box.width,
      y = (event.clientY - box.top) / box.height;

    if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1)) return;

    svg.querySelector('.vision-tap')?.remove();

    const dot = document.createElementNS(SVG_NS, 'circle');

    dot.setAttribute('class', 'vision-tap');

    dot.setAttribute('cx', x * view.width);


    dot.setAttribute('cy', y * view.height);

    dot.setAttribute('r', Math.max(view.width, view.height) * 0.014);

    svg.append(dot);

    busy = true;

    stage.setAttribute('aria-busy', 'true');

    status.textContent = 'Finding that item…';

    try {
      const form = new FormData();

      form.append('x', x.toFixed(4));

      form.append('y', y.toFixed(4));

      let data = await pickJson(stage.dataset.pickUrl, { method: 'POST', body: form, headers: { 'X-CSRFToken': csrf } });

      const until = Date.now() + 90000,
        slow = Date.now() + 6000;

      while (data.status === 'pending' || data.status === 'running') {
        if (Date.now() > slow) status.textContent = 'Still looking… the first search can take up to half a minute.';

        if (Date.now() > until) {
          data = { status: 'failed', message: 'This is taking longer than usual. Please try again in a minute.' };

          break;
        }

        await new Promise(done => setTimeout(done, 1200));

        data = await pickJson(data.url);
      }

      if (data.status === 'done') {
        dot.remove();

        show(data);


        status.textContent = 'Tap anything else in the photo to look it up.';
      } else {
        status.textContent = data.message || PICK_FAILED;
      }
    } catch (_) {
      status.textContent = PICK_FAILED;
    } finally {
      busy = false;

      stage.removeAttribute('aria-busy');
    }
  });
}

document.querySelectorAll('.vision-panel').forEach(panel => initVisionPanel(panel));

const LENS_UPLOAD = 'https://lens.google.com/v3/upload';

async function lensFile(src) {
  const blob = await (await fetch(src, {
    credentials: 'same-origin'
  })).blob();

  const img = await createImageBitmap(blob);

  const pad = Math.round(Math.max(img.width, img.height) * 0.08),
    canvas = document.createElement('canvas');

  canvas.width = img.width + pad * 2;

  canvas.height = img.height + pad * 2;

  const ctx = canvas.getContext('2d');

  ctx.fillStyle = '#fff';

  ctx.fillRect(0, 0, canvas.width, canvas.height);


  ctx.drawImage(img, pad, pad);

  const jpeg = await new Promise(done => canvas.toBlob(done, 'image/jpeg', 0.92));

  return new File([jpeg], 'item.jpg', {
    type: 'image/jpeg'
  });
}

document.addEventListener('click', async event => {
  const link = event.target.closest('a.vision-lens[data-lens-crop]');

  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;



  if (typeof DataTransfer === 'undefined' || typeof createImageBitmap === 'undefined') return;

  event.preventDefault();

  if (link.getAttribute('aria-busy') === 'true') return;

  const name = 'lens-' + Date.now();



  const tab = window.open('about:blank', name);

  link.setAttribute('aria-busy', 'true');

  try {
    const file = await lensFile(link.dataset.lensCrop),
      files = new DataTransfer();

    files.items.add(file);

    const form = Object.assign(document.createElement('form'), {
      method: 'POST',
      enctype: 'multipart/form-data',
      target: tab ? name : '_blank',


      action: `${LENS_UPLOAD}?hl=en-SG&re=df&st=${Date.now()}&ep=gsbubb` + (link.dataset.lensWords ? '&q=' + encodeURIComponent(link.dataset.lensWords) : '')
    });

    const input = Object.assign(document.createElement('input'), {
      type: 'file',
      name: 'encoded_image'
    });

    input.files = files.files;

    form.hidden = true;

    form.append(input);

    document.body.append(form);

    form.submit();

    form.remove();
  } catch (_) {
    if (tab) tab.location = link.href;else window.open(link.href, '_blank', 'noopener');
  } finally {
    link.removeAttribute('aria-busy');
  }
});
