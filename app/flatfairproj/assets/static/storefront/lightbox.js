(() => {
  'use strict';

  if (!('HTMLDialogElement' in window)) return;

  const dialog = document.createElement('dialog');

  dialog.className = 'image-viewer';

  dialog.setAttribute('aria-label', 'Photo viewer');


  dialog.innerHTML = '<button type="button" class="viewer-close" aria-label="Close photo viewer">×</button>' + '<button type="button" class="viewer-prev" aria-label="Previous photo">‹</button>' + '<figure><img alt="" draggable="false"><figcaption><span class="viewer-caption"></span>' + '<span class="viewer-counter" aria-live="polite"></span></figcaption></figure>' + '<button type="button" class="viewer-next" aria-label="Next photo">›</button>';

  document.body.append(dialog);

  const picture = dialog.querySelector('img');

  const caption = dialog.querySelector('.viewer-caption');



  const counter = dialog.querySelector('.viewer-counter');

  const prior = dialog.querySelector('.viewer-prev');


  const next = dialog.querySelector('.viewer-next');

  const closer = dialog.querySelector('.viewer-close');

  let entries = [];

  let index = 0;

  let refocus = null;

  let touch = null;

  function itemFor(link) {
    const image = link.querySelector('img');

    return {
      url: image.currentSrc || image.src,
      caption: link.dataset.caption || image.alt || 'Photo'
    };
  }

  function show() {
    const entry = entries[index];

    picture.src = entry.url;


    picture.alt = entry.caption;



    caption.textContent = entry.caption;



    counter.textContent = `${index + 1} / ${entries.length}`;

    prior.disabled = index === 0;

    next.disabled = index === entries.length - 1;
  }

  function move(step) {
    const target = index + step;

    if (target < 0 || target >= entries.length) return;

    index = target;

    show();

    if (document.activeElement.disabled) closer.focus();
  }

  function open(link) {
    const group = link.closest('[data-lightbox-group]') || document;



    const seen = [];

    entries = [];

    group.querySelectorAll('[data-lightbox]').forEach(item => {
      const entry = itemFor(item);

      if (!seen.includes(entry.url)) {
        seen.push(entry.url);


        entries.push(entry);
      }
    });

    index = Math.max(0, seen.indexOf(itemFor(link).url));

    refocus = link;

    show();

    document.documentElement.style.overflow = 'hidden';

    dialog.showModal();

    closer.focus({
      preventScroll: true
    });
  }

  dialog.addEventListener('close', () => {
    document.documentElement.style.overflow = '';

    picture.removeAttribute('src');

    if (refocus && refocus.isConnected) refocus.focus({
      preventScroll: true
    });
  });

  closer.addEventListener('click', () => dialog.close());

  prior.addEventListener('click', () => move(-1));

  next.addEventListener('click', () => move(1));

  dialog.addEventListener('click', event => {
    if (event.target === dialog || event.target === dialog.querySelector('figure')) dialog.close();
  });



  dialog.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();

      move(-1);
    }

    if (event.key === 'ArrowRight') {
      event.preventDefault();

      move(1);
    }
  });

  dialog.addEventListener('touchstart', event => {
    const point = event.touches[0];

    touch = event.touches.length === 1 ? {
      x: point.clientX,
      y: point.clientY
    } : null;
  }, {
    passive: true
  });


  dialog.addEventListener('touchend', event => {
    if (!touch) return;

    const point = event.changedTouches[0];

    const dx = point.clientX - touch.x;

    const dy = point.clientY - touch.y;

    touch = null;

    if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.5) move(dx < 0 ? 1 : -1);
  }, {
    passive: true
  });

  document.addEventListener('click', event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;

    const link = event.target instanceof Element ? event.target.closest('[data-lightbox]') : null;



    if (!link || !link.querySelector('img')) return;

    event.preventDefault();

    open(link);
  });


  document.querySelectorAll('[data-lightbox]').forEach(link => link.setAttribute('aria-haspopup', 'dialog'));
})();
