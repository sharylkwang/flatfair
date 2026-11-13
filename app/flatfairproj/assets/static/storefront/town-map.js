document.querySelectorAll('.townmap:not([data-ready])').forEach(map => {
  map.dataset.ready = '1';



  map.hidden = false;

  const pick = map.dataset.mode === 'pick';

  const smooth = window.moveSmoothly || (change => change());

  const svg = map.querySelector('.townmap-svg');

  const whole = svg.getAttribute('viewBox').split(' ').map(Number);

  const stage = map.querySelector('.townmap-stage');

  const caption = map.querySelector('.townmap-caption');

  const status = map.querySelector('.townmap-status');

  const hoverTag = map.querySelector('.townmap-hover');

  const pin = map.querySelector('.townmap-pin');

  const areas = [...map.querySelectorAll('.townmap-area')];


  const names = [...map.querySelectorAll('.townmap-name')];

  const chips = [...map.querySelectorAll('button.townmap-chip')];


  const links = [...map.querySelectorAll('a.townmap-chip')];

  const rows = [...map.querySelectorAll('.townmap-towns')];

  const townKeys = [...map.querySelectorAll('.townmap-town')];

  const list = map.querySelector('.townmap-list select');



  const field = document.getElementById(map.dataset.field);

  let view = whole;

  let sector = '';

  let active = null;

  let hot = '';

  let frame = 0;

  let zoneText = '';

  function inZone(area) {
    return area.classList.contains('region-' + sector);
  }

  function chosen() {
    return areas.find(area => area.classList.contains('is-selected'));
  }

  function restText() {
    if (!pick) return sector ? zoneText : caption.dataset.rest || (stage.clientWidth < 560 ? map.dataset.promptSmall : '');

    if (chosen()) return chosen().dataset.caption + map.dataset.chosen;

    if (sector) return map.dataset.promptZoom;

    return map.clientWidth < 560 ? map.dataset.promptSmall : map.dataset.prompt;
  }

  function place(label, area) {
    const matrix = svg.getScreenCTM();

    label.hidden = !area || !matrix;

    if (label.hidden) return;

    const box = stage.getBoundingClientRect();


    const point = new DOMPoint(area.dataset.x, area.dataset.y).matrixTransform(matrix);


    const x = point.x - box.left;

    const y = point.y - box.top;

    label.lastElementChild.textContent = area.dataset.caption;

    label.hidden = x < 0 || x > box.width || y < 0 || y > box.height;

    const name = names[areas.indexOf(area)];

    const above = name.classList.contains('is-shown') ? name.getBoundingClientRect().top - box.top - 4 : y - 12;

    const half = label.offsetWidth / 2;

    label.style.left = Math.min(Math.max(x, half), box.width - half) + 'px';

    label.style.top = Math.max(above, label.offsetHeight) + 'px';
  }

  function fits(area, name) {
    const box = name.getBBox();

    const paths = [...area.querySelectorAll('path')];

    return [0, 0.5, 1].every(across => [0.12, 0.88].every(down => {
      const corner = new DOMPoint(box.x + box.width * across, box.y + box.height * down);

      return paths.some(path => path.isPointInFill(corner));
    }));
  }

  function layout() {
    const matrix = svg.getScreenCTM();

    if (!matrix || !matrix.a) return;

    const wide = matrix.a >= 0.6;

    const roomy = stage.clientWidth >= 560;

    const size = sector ? (roomy ? 14 : 12) : Math.min(Math.max(matrix.a * 16, 11), 14);

    svg.style.setProperty('--name-size', size / matrix.a + 'px');

    const placed = [];

    const loose = [];

    areas.forEach((area, index) => {
      const name = names[index];

      name.classList.add('is-shown');

      const wanted = sector ? inZone(area) : wide;

      const inside = wanted && (sector && roomy || fits(area, name));

      name.classList.toggle('is-shown', inside);

      if (inside) placed.push(name.getBBox());
      else if (wanted) loose.push(name);
    });

    loose.forEach(name => {
      name.classList.add('is-shown');

      const box = name.getBBox();


      const clear = placed.every(other => box.x > other.x + other.width + 2 || other.x > box.x + box.width + 2 || box.y > other.y + other.height + 2 || other.y > box.y + box.height + 2);

      name.classList.toggle('is-shown', clear);

      if (clear) placed.push(box);
    });


    place(pin, chosen());

    place(hoverTag, null);

    caption.textContent = restText();



    litNames();
  }

  function litNames() {
    areas.forEach((area, index) => {
      const lit = area === active || area.classList.contains('is-selected') || area.classList.contains('is-region') || area.classList.contains('region-' + hot);

      names[index].classList.toggle('is-lit', lit);
    });
  }

  function showHover(event) {
    const item = event.target.closest('[data-caption]');

    const area = event.target.closest('.townmap-area');

    const chip = event.target.closest('.townmap-chip');

    const named = pick && area && names[areas.indexOf(area)].classList.contains('is-shown');

    active = area;

    hot = chip && !sector ? chip.dataset.region || '' : '';

    caption.textContent = item ? item.dataset.caption : restText();

    place(hoverTag, area && area !== chosen() && !named ? area : null);

    litNames();
  }

  function endHover() {
    active = null;

    hot = '';

    caption.textContent = restText();



    place(hoverTag, null);

    litNames();
  }


  function zoom(region) {
    const towns = areas.filter(area => area.classList.contains('region-' + region));

    let target = whole;

    if (towns.length) {
      const boxes = towns.flatMap(area => [...area.querySelectorAll('path:not(.townmap-near)')].map(path => path.getBBox()));

      const left = Math.min(...boxes.map(box => box.x));

      const top = Math.min(...boxes.map(box => box.y));

      const right = Math.max(...boxes.map(box => box.x + box.width));

      const bottom = Math.max(...boxes.map(box => box.y + box.height));

      const edge = Math.max(right - left, bottom - top) * 0.05;

      target = [left - edge, top - edge, right - left + edge * 2, bottom - top + edge * 2];
    }

    const chip = chips.find(item => item.dataset.region === region);

    sector = towns.length ? region : '';

    smooth(() => {
      if (rows.some(row => row.contains(document.activeElement))) chip.focus({ preventScroll: true });

      rows.forEach(row => { row.hidden = !sector || row.dataset.towns !== sector; });

      map.classList.toggle('is-zoomed', Boolean(sector));
    });

    map.classList.add('is-moving');

    chips.forEach(item => item.setAttribute('aria-pressed', item === chip));

    areas.forEach(area => {
      const out = Boolean(sector) && !inZone(area);

      area.classList.toggle('is-out', out);

      area.setAttribute('tabindex', out ? -1 : 0);
    });


    const from = view;

    const began = performance.now();

    const quick = matchMedia('(prefers-reduced-motion: reduce)').matches;

    function step(now) {
      const part = quick ? 1 : Math.min(Math.max((now - began) / 300, 0), 1);

      const eased = 1 - (1 - part) ** 3;

      view = part < 1 ? from.map((value, index) => value + (target[index] - value) * eased) : target;

      svg.setAttribute('viewBox', view.join(' '));

      if (part < 1) {
        frame = requestAnimationFrame(step);

        return;
      }

      map.classList.remove('is-moving');

      layout();
    }

    cancelAnimationFrame(frame);

    step(began);
  }

  function markTown(town) {
    areas.forEach(area => {
      const chosen = area.dataset.town === town;

      area.classList.toggle('is-selected', chosen);

      area.setAttribute('aria-pressed', chosen);
    });

    townKeys.forEach(button => button.setAttribute('aria-pressed', button.dataset.town === town));

    layout();
  }

  function pickTown(event) {
    if (event.type === 'keydown' && event.key !== ' ') return;

    event.preventDefault();

    const town = event.currentTarget.dataset.town;

    const label = event.currentTarget.dataset.label || event.currentTarget.dataset.caption;

    if (field) {
      field.value = town;

      field.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      markTown(town);
    }

    status.textContent = label + map.dataset.chosen;

    map.dispatchEvent(new CustomEvent('townmap:pick', { bubbles: true, detail: { town, label } }));
  }

  map.addEventListener('mouseover', showHover);

  map.addEventListener('focusin', showHover);

  map.addEventListener('mouseleave', endHover);

  map.addEventListener('focusout', endHover);

  new ResizeObserver(layout).observe(stage);

  document.fonts.ready.then(layout);

  if (list) list.addEventListener('change', () => list.form.submit());

  if (!pick) {
    links.forEach(link => link.addEventListener('click', event => {
      if (stage.clientWidth >= 560) return;

      event.preventDefault();

      zoneText = link.dataset.region ? link.dataset.caption : '';

      links.forEach(item => item.toggleAttribute('aria-current', item === link));

      zoom(link.dataset.region || '');
    }));

    return;
  }

  chips.forEach(chip => chip.addEventListener('click', () => zoom(chip.dataset.region)));

  map.addEventListener('keydown', event => {
    if (event.key === 'Escape' && sector) zoom('');
  });

  areas.forEach(area => {
    area.addEventListener('click', pickTown);

    area.addEventListener('keydown', pickTown);
  });

  townKeys.forEach(button => button.addEventListener('click', pickTown));

  if (field) {
    field.addEventListener('change', () => markTown(field.value));

    markTown(field.value);
  }
});
