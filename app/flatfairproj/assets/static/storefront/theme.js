function moveSmoothly(update) {
  if (!document.startViewTransition || matchMedia('(prefers-reduced-motion: reduce)').matches) {
    update();

    return;
  }

  const shift = document.startViewTransition(update);

  shift.ready.catch(() => {});

  shift.finished.catch(() => {});
}

window.moveSmoothly = moveSmoothly;

const header = document.querySelector('[data-site-header]');

const navToggle = header ? header.querySelector('.nav-toggle') : null;

document.querySelectorAll('[data-dismiss-toast]').forEach(button => {
  button.addEventListener('click', () => {
    button.closest('.toast, .page-notice').hidden = true;
  });
});

const smallScreen = matchMedia('(max-width: 1100px)');

let menuOpener = navToggle;

const backgroundInert = new Map();

function sheetFocusables() {
  return [...header.querySelectorAll('.header-sheet a[href], .header-sheet button:not(:disabled), .header-sheet input:not(:disabled), .header-sheet summary, .header-sheet [tabindex="0"]')].filter(element => element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden');
}

function showMenu(open) {
  header.classList.toggle('menu-open', open);

  document.documentElement.classList.toggle('nav-locked', open);

  header.querySelector('.nav-backdrop').hidden = !open;

  navToggle.setAttribute('aria-expanded', open);

  navToggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');

  const account = header.querySelector('.header-account');

  if (account && smallScreen.matches) {
    account.open = open;
  }

  const sheet = header.querySelector('.header-sheet');

  if (open) {
    sheet.setAttribute('role', 'dialog');

    sheet.setAttribute('aria-modal', 'true');

    sheet.setAttribute('aria-label', 'Navigation');

    document.querySelectorAll('main, footer, .toast-stack').forEach(element => {
      if (!backgroundInert.has(element)) backgroundInert.set(element, element.inert);

      element.inert = true;
    });

    sheetFocusables()[0].focus();
  } else {
    sheet.removeAttribute('role');

    sheet.removeAttribute('aria-modal');

    sheet.removeAttribute('aria-label');

    backgroundInert.forEach((inert, element) => { element.inert = inert; });

    backgroundInert.clear();


    menuOpener.focus();
  }
}

function flipMenu(event) {
  menuOpener = event.currentTarget;

  showMenu(!header.classList.contains('menu-open'));
}

if (navToggle) {
  navToggle.addEventListener('click', flipMenu);

  header.querySelector('.nav-backdrop').addEventListener('click', () => showMenu(false));

  header.querySelector('[data-sheet-close]').addEventListener('click', () => showMenu(false));

  header.querySelector('.header-sheet').addEventListener('click', event => {
    if (event.target.closest('a[href]') && header.classList.contains('menu-open')) showMenu(false);
  });
}

function closeMenus(target) {
  let query = 'details.save-menu[open], details.coll-menu[open]';

  if (!smallScreen.matches) {
    query += ', details.header-account[open]';
  }

  document.querySelectorAll(query).forEach(menu => {
    if (!target || !menu.contains(target)) {
      menu.open = false;
    }
  });
}

document.addEventListener('click', event => closeMenus(event.target));

smallScreen.addEventListener('change', () => {
  if (navToggle && header.classList.contains('menu-open')) showMenu(false);
});

document.addEventListener('focusin', event => {
  if (navToggle && header.classList.contains('menu-open') && !header.querySelector('.header-sheet').contains(event.target)) sheetFocusables()[0].focus();
});

document.addEventListener('keydown', event => {
  if (event.key === 'Tab' && navToggle && header.classList.contains('menu-open')) {
    const fields = sheetFocusables();

    const first = fields[0];

    const last = fields[fields.length - 1];

    if (event.shiftKey && document.activeElement === first || !event.shiftKey && document.activeElement === last) {
      event.preventDefault();

      (event.shiftKey ? last : first).focus();
    }
  }

  if (event.key !== 'Escape') {
    return;
  }

  if (navToggle && header.classList.contains('menu-open')) {
    showMenu(false);
  }

  closeMenus(null);
});

document.querySelectorAll('dialog[data-open-on-load]').forEach(dialog => {
  const form = dialog.querySelector('form[data-dismiss]');

  let sent = false;

  form.addEventListener('submit', () => { sent = true; });

  dialog.addEventListener('close', () => {
    if (sent) return;

    fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-Beacon': '1' }, credentials: 'same-origin', keepalive: true });
  });

  dialog.querySelectorAll('a:not([target])').forEach(link => link.addEventListener('click', () => { sent = true; }));

  dialog.querySelectorAll('form:not([data-dismiss])').forEach(other => other.addEventListener('submit', () => { sent = true; }));

  dialog.showModal();
});

function askFirst(source, proceed) {
  const dialog = document.createElement('dialog');

  const askText = document.createElement('p');

  const actions = document.createElement('div');

  const keep = document.createElement('button');

  const go = document.createElement('button');

  dialog.className = 'ask';

  askText.id = 'ask-question';

  askText.textContent = source.dataset.ask;

  dialog.setAttribute('aria-labelledby', askText.id);

  actions.className = 'ask-actions';

  keep.type = 'button';

  keep.className = 'btn btn-ghost';

  keep.textContent = source.dataset.no || 'Go back';

  go.type = 'button';

  go.className = source.matches('.btn-danger') || source.querySelector('.btn-danger') ? 'btn btn-danger' : 'btn btn-brand';

  go.textContent = source.dataset.yes || 'Continue';

  keep.addEventListener('click', () => dialog.close());

  go.addEventListener('click', () => dialog.close('go'));

  dialog.addEventListener('close', () => {
    dialog.remove();

    if (dialog.returnValue === 'go') proceed();
  });

  actions.append(keep, go);

  dialog.append(askText, actions);

  document.body.append(dialog);

  dialog.showModal();

  keep.focus();
}

document.addEventListener('submit', event => {
  const form = event.target;

  const sender = event.submitter;

  const source = sender && sender.dataset.ask ? sender : form;

  if (!source.dataset.ask) return;

  if (form.dataset.asked) {
    delete form.dataset.asked;

    return;
  }

  event.preventDefault();

  event.stopImmediatePropagation();

  askFirst(source, () => {
    form.dataset.asked = '1';

    form.requestSubmit(sender || undefined);
  });
}, true);

document.querySelectorAll('[data-offer-amount]').forEach(button => {
  button.addEventListener('click', () => {
    document.getElementById('offer-amount').value = button.dataset.offerAmount;
  });
});

document.querySelectorAll('[data-lens-search]').forEach(form => {
  form.addEventListener('submit', event => {
    if (event.defaultPrevented) return;

    document.getElementById('lens-status').textContent = 'Searching your photo… This may take a moment.';

    form.setAttribute('aria-busy', 'true');

    form.querySelector('button[type="submit"], button:not([type])').disabled = true;
  });
});

document.querySelectorAll('[data-auto-submit]').forEach(select => {
  select.addEventListener('change', () => select.form.submit());
});

const browseFilters = document.querySelector('.browse-filters');

if (browseFilters) {
  const drawer = document.getElementById('filters-open');

  const kept = JSON.parse(sessionStorage.getItem('browseKept') || 'null');

  sessionStorage.removeItem('browseKept');

  if (kept) {
    drawer.checked = kept.open;

    window.scrollTo({ top: kept.top, behavior: 'instant' });

    const field = browseFilters.querySelector('[name="' + kept.name + '"]:checked') || browseFilters.querySelector('[name="' + kept.name + '"]');

    if (field) field.focus({ preventScroll: true });
  }

  browseFilters.querySelectorAll('input[type="radio"], input[type="range"]').forEach(input => {
    input.addEventListener('change', () => {
      sessionStorage.setItem('browseKept', JSON.stringify({ top: window.scrollY, open: drawer.checked, name: input.name }));

      browseFilters.submit();
    });
  });
}

document.querySelectorAll('[data-budget-range]').forEach(budget => {
  const lowest = budget.querySelector('[name="minPrice"]');

  const highest = budget.querySelector('[name="maxPrice"]');

  const readout = budget.querySelector('output');

  const money = value => '$' + Number(value).toLocaleString('en-SG');

  function update(event) {
    if (Number(lowest.value) > Number(highest.value)) {
      if (event && event.target === lowest) lowest.value = highest.value;
      else highest.value = lowest.value;
    }

    readout.textContent = money(lowest.value) + ' – ' + money(highest.value);
  }

  lowest.addEventListener('input', update);

  highest.addEventListener('input', update);

  budget.closest('form').addEventListener('reset', () => requestAnimationFrame(() => update()));

  update();
});

function countUp(number) {
  const target = Number(number.dataset.count);

  const start = performance.now();

  function step(now) {
    const done = Math.min((now - start) / 1200, 1);

    number.textContent = '$' + Math.round(target * done * (2 - done)).toLocaleString('en-SG');

    if (done < 1) {
      requestAnimationFrame(step);
    }
  }

  requestAnimationFrame(step);
}

const reveals = document.querySelectorAll('[data-reveal]');

const calm = matchMedia('(prefers-reduced-motion: reduce)').matches;

if (reveals.length && 'IntersectionObserver' in window && !calm) {
  document.documentElement.classList.add('motion');

  const watcher = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-in');

        entry.target.querySelectorAll('[data-count]').forEach(countUp);

        watcher.unobserve(entry.target);
      }
    });
  }, {
    threshold: 0.25
  });

  reveals.forEach(section => watcher.observe(section));
}

document.querySelectorAll("[data-address], [data-search-suggestions]").forEach((input, index) => {
  const search = input.hasAttribute("data-search-suggestions");

  const list = document.createElement("ul");

  const status = document.createElement("span");

  const kinds = {postal: "Postal code", block: "Block", street: "Street", town: "Town", area: "Area", search: "Search"};

  const marks = {
    postal: "M3 6h18v12H3zM3 7l9 6 9-6",
    block: "M5 21V4h10v17M15 9h4v12M3 21h18M8.5 8h3M8.5 12h3M8.5 16h3",
    street: "M8 3 5 21M16 3l3 18M12 4v3M12 10.5v3M12 17v3",



    town: "M12 21s7-6.2 7-11.5a7 7 0 1 0-14 0C5 14.8 12 21 12 21zM12 12a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
    area: "M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14",
    search: "M11 17.5a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM16 16l4.5 4.5"
  };

  const tries = [
    {kind: "town", label: "Tampines", value: "Tampines"},
    {kind: "area", label: "Yew Tee", value: "Yew Tee"},
    {kind: "search", label: "4-room under $600k", value: "4-room under 600k"},
    {kind: "search", label: "5-room in Punggol", value: "5-room in Punggol"}
  ];

  let items = [];

  let active = -1;

  let timer = 0;

  let tick = 0;

  if (!input.id) input.id = "address-" + index;

  const label = input.closest("label");

  if (label && !input.hasAttribute("aria-label") && !input.hasAttribute("aria-labelledby")) {
    const nodes = [...label.childNodes];

    const caption = nodes.slice(0, nodes.indexOf(input)).map(node => node.textContent).join("").trim();

    if (caption) input.setAttribute("aria-label", caption);
  }

  list.id = input.id + "-list";

  list.className = search ? "addr-list search-list" : "addr-list";

  list.hidden = true;

  list.setAttribute("role", "listbox");

  list.setAttribute("aria-label", "Address suggestions");

  status.className = "visually-hidden";

  status.setAttribute("role", "status");

  status.setAttribute("aria-atomic", "true");

  input.after(list, status);

  input.autocomplete = "off";

  if (!search) {
    input.removeAttribute("pattern");

    Object.assign(input, {
      inputMode: "text",
      maxLength: 60,
      placeholder: "Postal code, or block and street"
    });
  }

  input.setAttribute("role", "combobox");

  input.setAttribute("aria-autocomplete", "list");

  input.setAttribute("aria-controls", list.id);

  input.setAttribute("aria-expanded", "false");

  function show(open, index = -1) {
    list.hidden = !open;

    active = index;

    input.setAttribute("aria-expanded", String(open));

    if (index < 0) input.removeAttribute("aria-activedescendant");
    else input.setAttribute("aria-activedescendant", list.id + "-" + index);

    options().forEach((option, i) => option.setAttribute("aria-selected", String(i === index)));
  }

  function options() {
    return [...list.querySelectorAll('[role="option"]')];
  }

  function row(item, i, typed) {
    const option = document.createElement("li");

    option.id = list.id + "-" + i;

    option.setAttribute("role", "option");

    if (!search) {
      option.textContent = item.label + ", " + item.townLabel + " " + item.postal;

      return option;
    }

    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");

    const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");

    const name = document.createElement("span");

    const kind = document.createElement("span");

    const at = typed ? item.label.toLowerCase().indexOf(typed.toLowerCase()) : -1;

    icon.setAttribute("viewBox", "0 0 24 24");

    icon.setAttribute("aria-hidden", "true");

    shape.setAttribute("d", marks[item.kind]);

    icon.append(shape);

    name.className = "hit-name";

    if (at < 0) {
      name.textContent = item.label;
    } else {
      const bold = document.createElement("b");

      bold.textContent = item.label.slice(at, at + typed.length);

      name.append(item.label.slice(0, at), bold, item.label.slice(at + typed.length));
    }

    kind.className = "hit-kind";

    kind.textContent = item.kind === "search" ? "" : kinds[item.kind];

    option.append(icon, name, kind);

    return option;
  }

  function showTries() {
    if (!search || input.value.trim() !== "" || document.activeElement !== input) return;

    const head = document.createElement("li");

    head.className = "search-try";

    head.setAttribute("role", "presentation");

    head.textContent = "Try searching for";

    items = tries;

    list.replaceChildren(head, ...items.map((item, i) => row(item, i, "")));

    list.style.top = input.offsetTop + input.offsetHeight + "px";

    show(true);
  }

  function close() {
    clearTimeout(timer);

    tick += 1;

    show(false);

    status.textContent = "";
  }

  function wipeTown() {
    if (!search) return;

    const town = input.form.elements.namedItem("town");

    if (town && town.type === "hidden") town.value = "";
  }

  function pick(item) {
    close();

    wipeTown();

    input.value = search ? item.value : item.postal;

    if (!search) {
      const fill = {block: item.block, town: item.town, lease: item.leaseYear, leaseCommenceYear: item.leaseYear};

      for (const name in fill) {
        if (input.form.elements[name] && fill[name]) input.form.elements[name].value = fill[name];
      }
    }

    input.dispatchEvent(new Event("change", {bubbles: true}));

    if (search) input.form.requestSubmit();
  }

  async function suggest() {
    const current = tick;

    try {
      const path = search ? input.dataset.searchSuggestions : "/api/addresses";

      const reply = await fetch(path + "?q=" + encodeURIComponent(input.value));

      if (!reply.ok) throw Error("Address request failed");

      const result = await reply.json();

      if (current !== tick || document.activeElement !== input) return;

      items = result.results;

      const typed = input.value.trim();

      list.replaceChildren(...items.map((item, i) => row(item, i, typed)));

      list.style.top = input.offsetTop + input.offsetHeight + "px";

      show(items.length > 0);

      status.textContent = items.length + " suggestions available.";
    } catch (error) {
      if (current === tick) close();
    }
  }

  input.addEventListener("input", () => {
    wipeTown();

    close();

    if (input.value.trim().length >= 2) timer = setTimeout(suggest, 200);
    else showTries();
  });



  input.addEventListener("focus", showTries);

  input.addEventListener("click", showTries);

  input.addEventListener("blur", close);



  const clear = search ? input.form.querySelector(".search-clear") : null;

  if (clear) {
    const sync = () => { clear.hidden = input.value === ""; };

    input.addEventListener("input", sync);

    input.addEventListener("change", sync);

    clear.addEventListener("pointerdown", event => event.preventDefault());

    clear.addEventListener("click", () => {
      input.value = "";

      input.focus();

      input.dispatchEvent(new Event("input", {bubbles: true}));
    });

    sync();
  }

  document.addEventListener("pointerdown", event => {
    if (event.target !== input && !list.contains(event.target)) close();
  });

  list.addEventListener("pointerdown", event => {
    const option = event.target.closest("li");

    if (option) event.preventDefault();
  });

  list.addEventListener("click", event => {
    const option = event.target.closest("li");

    if (option && option.matches('[role="option"]')) pick(items[options().indexOf(option)]);
  });

  input.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      if (!list.hidden) event.preventDefault();

      close();
    } else if (!list.hidden && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
      event.preventDefault();

      const next = active + (event.key === "ArrowDown" ? 1 : -1);

      show(true, next < 0 ? items.length - 1 : next % items.length);

      options()[active].scrollIntoView({block: "nearest"});
    } else if (!list.hidden && event.key === "Enter" && active >= 0) {
      event.preventDefault();

      pick(items[active]);
    }
  });
});

document.querySelectorAll('input[type="password"]').forEach(input => {
  const wrap = document.createElement('span');

  wrap.className = 'password-wrap';

  input.before(wrap);

  wrap.append(input);

  const button = document.createElement('button');

  button.type = 'button';

  button.className = 'password-eye';

  button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><g class="eye-lid"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></g><path class="eye-slash" d="M4 4l16 16"/></svg>';

  button.setAttribute('aria-label', 'Show password');

  button.setAttribute('aria-pressed', 'false');

  button.addEventListener('click', () => {
    const shown = input.type === 'text';

    input.type = shown ? 'password' : 'text';

    button.classList.toggle('is-shown', !shown);

    button.classList.remove('blink');

    void button.offsetWidth;

    button.classList.add('blink');

    button.setAttribute('aria-label', shown ? 'Show password' : 'Hide password');

    button.setAttribute('aria-pressed', shown ? 'false' : 'true');
  });

  wrap.append(button);
});

document.querySelectorAll('[data-resend-button]').forEach(button => {
  let seconds = Number(button.dataset.resendSeconds || 0);


  const count = button.querySelector('[data-resend-countdown]');

  const timer = window.setInterval(() => {
    seconds -= 1;

    if (seconds > 0) {
      if (count) count.textContent = String(seconds);

      return;
    }

    window.clearInterval(timer);

    button.disabled = false;

    if (count) count.textContent = '0';
  }, 1000);
});

document.querySelectorAll('[data-account-wizard]').forEach(wizard => {
  const tabs = [...wizard.querySelectorAll('[data-account-tab]')];

  const steps = [...wizard.querySelectorAll('[data-account-step]')];

  const groups = {
    profile: ['name', 'email'],
    password: ['password'],
    authenticator: ['authenticator'],
    account: ['delete'],
  };

  const firstStep = wizard.dataset.start || 'name';

  const firstTab = tabs.find(tab => groups[tab.dataset.accountTab]?.includes(firstStep))?.dataset.accountTab || 'profile';

  function showTab(tabName, moveFocus = false) {
    const shownSteps = groups[tabName];

    tabs.forEach(tab => {
      const active = tab.dataset.accountTab === tabName;

      tab.setAttribute('aria-selected', active);

      tab.tabIndex = active ? 0 : -1;
    });

    steps.forEach(step => {
      const active = shownSteps.includes(step.dataset.accountStep);

      step.hidden = !active;

      step.setAttribute('aria-hidden', !active);

      if (active && moveFocus) step.focus();
    });
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => showTab(tab.dataset.accountTab, true));

    tab.addEventListener('keydown', event => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;

      event.preventDefault();

      const offset = event.key === 'ArrowRight' ? 1 : -1;

      const nextTab = tabs[(index + offset + tabs.length) % tabs.length];

      nextTab.focus();

      showTab(nextTab.dataset.accountTab);
    });
  });

  showTab(firstTab);
});

let humanScript = null;

function loadHumanCheck() {
  if (!humanScript) {
    humanScript = new Promise((resolve, reject) => {
      window.flatfairHumanReady = () => resolve(window.turnstile);

      const script = document.createElement('script');

      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=flatfairHumanReady';

      script.async = true;

      script.onerror = () => {
        humanScript = null;

        script.remove();

        reject(Error('Check did not load'));
      };

      document.head.append(script);
    });
  }

  return humanScript;
}

document.querySelectorAll('[data-human-check]').forEach(slot => {
  const form = slot.closest('form');

  const box = slot.querySelector('.human-box');

  const note = slot.querySelector('.human-note');

  const button = form.querySelector('button:not([type="button"]):not([formaction])');

  const auto = slot.hasAttribute('data-auto');

  const onSend = slot.hasAttribute('data-on-submit');



  const fields = [...form.querySelectorAll('input[required], select[required], textarea[required]')];

  const restTag = button.textContent;

  const askText = note.textContent;

  let widget = null;

  let loading = false;

  let ready = false;

  let failed = false;

  let waiting = false;

  let sender = null;

  let timer = 0;

  function filled() {
    return fields.every(field => {
      if (field.type === 'radio') return form.querySelector(`input[name="${field.name}"]:checked`);

      if (field.type === 'checkbox') return field.checked;

      return field.value.trim() !== '';
    });
  }



  function open(on, text, bad) {
    if (text) note.textContent = text;

    slot.classList.toggle('is-bad', Boolean(bad));

    slot.classList.toggle('is-open', on);

    slot.querySelector('.human-inner').inert = !on;
  }

  function wait(on) {
    waiting = on;

    button.setAttribute('aria-busy', String(on));

    button.textContent = on ? 'Checking…' : restTag;
  }

  function fail() {
    ready = false;

    failed = true;

    loading = false;

    wait(false);

    open(true, 'We couldn’t load the security check. Please check your connection and try again.', true);
  }

  function passed() {
    ready = true;

    failed = false;

    open(true, askText, false);

    if (!waiting) return;

    if (!form.checkValidity()) {
      wait(false);

      form.reportValidity();

      return;
    }

    form.requestSubmit(sender && sender.isConnected ? sender : undefined);
  }

  function start() {
    if (failed && widget !== null) {
      failed = false;

      open(true, askText, false);

      window.turnstile.reset(widget);

      return;
    }

    if (loading || widget !== null) return;

    loading = true;

    failed = false;

    open(true, askText, false);

    loadHumanCheck().then(api => {
      loading = false;

      widget = api.render(box, {
        sitekey: slot.dataset.sitekey,
        theme: 'light',
        size: 'flexible',
        appearance: 'always',
        callback: passed,
        'before-interactive-callback': () => open(true, askText, false),
        'expired-callback': () => { ready = false; },
        'timeout-callback': () => { ready = false; api.reset(widget); },
        'error-callback': () => { fail(); return true; }
      });
    }).catch(fail);
  }

  function weigh() {
    if (onSend) return;

    if (widget === null && !failed && filled()) start();
  }

  open(false);

  form.addEventListener('change', weigh);

  form.addEventListener('input', () => {
    clearTimeout(timer);

    timer = setTimeout(weigh, 700);
  });

  form.addEventListener('submit', event => {
    const by = event.submitter;

    if (by && by.getAttribute('formaction')?.startsWith('/accounts/google')) return;

    if (ready) return;

    event.preventDefault();

    if (waiting) return;

    sender = by;

    wait(true);

    start();
  });

  addEventListener('pageshow', event => {
    if (!event.persisted) return;

    ready = false;

    wait(false);

    if (widget !== null) window.turnstile.reset(widget);
  });

  if (auto) {
    if (!form.querySelector('.err')) wait(true);

    start();
  } else {
    weigh();
  }
});

document.querySelectorAll('[data-auth-form]').forEach(form => {
  const ways = form.querySelector('[data-auth-ways]');

  if (!ways) return;

  const email = form.querySelector('[data-auth-email]');

  const back = form.querySelector('[data-auth-back]');



  const google = ways.querySelector('[formaction]');

  const show = useEmail => {
    ways.hidden = useEmail;

    email.hidden = !useEmail;

    back.hidden = !useEmail;

    google.type = useEmail ? 'button' : 'submit';

    google.disabled = useEmail;
  };

  show(Boolean(form.querySelector('.err, .errorlist')));

  const role = form.querySelector('input[name="role"]');

  google.addEventListener('click', event => {
    if (role && !role.reportValidity()) event.preventDefault();
  });

  form.querySelector('[data-auth-email-open]').addEventListener('click', () => {
    moveSmoothly(() => {
      show(true);

      email.querySelector('input:not([type="hidden"])').focus({ preventScroll: true });
    });
  });

  back.addEventListener('click', () => {
    moveSmoothly(() => {
      show(false);

      ways.querySelector('.btn-way').focus({ preventScroll: true });
    });
  });
});

const lensResults = document.querySelector('[data-lens-results]');

if (lensResults && !location.hash) {
  lensResults.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
}

const phoneMenu = document.querySelector('[data-phone-menu]');

const phoneScreen = matchMedia('(max-width: 760px)');

if (phoneMenu) {
  const toggle = phoneMenu.querySelector('summary');

  const sheet = phoneMenu.querySelector('.phone-sheet');

  const inertElements = new Map();

  const focusables = () => [toggle, ...sheet.querySelectorAll('a[href], button:not(:disabled), summary')].filter(element => element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden');

  const close = () => { phoneMenu.open = false; };

  phoneMenu.addEventListener('toggle', () => {
    const open = phoneMenu.open;

    toggle.setAttribute('aria-expanded', open);

    document.documentElement.classList.toggle('phone-nav-locked', open);

    if (open) {
      phoneMenu.querySelector('.phone-more')?.removeAttribute('open');

      document.querySelectorAll('main, footer, .toast-stack, .header-inner > .site-logo, .header-search, .header-saved, .header-bell').forEach(element => {
        inertElements.set(element, element.inert);

        element.inert = true;
      });

      focusables()[1]?.focus();
    } else {
      inertElements.forEach((inert, element) => { element.inert = inert; });

      inertElements.clear();

      if (phoneScreen.matches) toggle.focus();
    }
  });

  phoneMenu.querySelector('[data-phone-close]').addEventListener('click', close);

  sheet.addEventListener('click', event => {
    if (event.target.closest('a[href]')) close();
  });

  document.addEventListener('focusin', event => {
    if (phoneMenu.open && !phoneMenu.contains(event.target)) focusables()[0]?.focus();
  });

  document.addEventListener('keydown', event => {
    if (!phoneMenu.open) return;

    if (event.key === 'Escape') {
      event.preventDefault();

      close();
    } else if (event.key === 'Tab') {
      const fields = focusables();

      const first = fields[0];

      const last = fields[fields.length - 1];

      if (event.shiftKey && document.activeElement === first || !event.shiftKey && document.activeElement === last) {
        event.preventDefault();

        (event.shiftKey ? last : first)?.focus();
      }
    }
  });

  phoneScreen.addEventListener('change', () => {
    close();

    if (header.classList.contains('menu-open')) showMenu(false);
  });
}

function openTargetFold() {
  if (location.hash.length < 2) return;

  const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));



  const fold = target ? target.closest('details') : null;

  if (fold) fold.open = true;
}

const buyBox = document.querySelector('.listing-summary');

if (buyBox) {
  const fitBuyBox = () => {
    buyBox.classList.toggle('is-tall', buyBox.scrollHeight > innerHeight - header.offsetHeight - 32);
  };

  const buyBoxWatch = new ResizeObserver(fitBuyBox);

  [...buyBox.children].forEach(child => buyBoxWatch.observe(child));

  addEventListener('resize', fitBuyBox);

  fitBuyBox();
}

addEventListener('hashchange', openTargetFold);

openTargetFold();

const signedKey = 'flatfair-signed';

const signedHere = document.body.dataset.signed || '';

let formEdited = false;

document.addEventListener('input', event => {
  const form = event.target.closest('form');

  if (form && form.method === 'post') formEdited = true;
});

function matchOtherTabs() {
  let signedNow = null;

  try {
    signedNow = localStorage.getItem(signedKey);
  } catch (error) {
    return;
  }

  if (signedNow === null || signedNow === signedHere) return;

  if (!formEdited) {
    location.reload();

    return;
  }

  if (document.querySelector('[data-signed-note]')) return;

  const note = document.createElement('div');

  note.className = 'toast-stack';

  note.dataset.signedNote = '';

  note.innerHTML = '<div class="toast flash info" role="status"><span>You signed in or out in another tab.</span><button type="button" class="toast-dismiss">Reload</button></div>';

  note.querySelector('button').addEventListener('click', () => location.reload());

  document.body.append(note);
}

try {
  localStorage.setItem(signedKey, signedHere);
} catch (error) {
  signedHere.trim();
}

addEventListener('storage', event => {
  if (event.key === signedKey) matchOtherTabs();
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) matchOtherTabs();
});

const saveNote = document.createElement('div');

saveNote.className = 'toast-stack toast-stack--low';

saveNote.setAttribute('role', 'status');

document.body.append(saveNote);

let listsVersion = 0;

function showSaveNote(message, failed) {
  const toast = document.createElement('div');

  toast.className = 'toast flash ' + (failed ? 'error' : 'success');

  toast.textContent = message;

  saveNote.replaceChildren(toast);

  clearTimeout(saveNote.timer);

  saveNote.timer = setTimeout(() => saveNote.replaceChildren(), 4000);
}

function showSavedCount(count) {
  const header = document.querySelector('.header-saved');

  if (!header) return;

  header.setAttribute('aria-label', 'Saved flats, ' + count);

  let badge = header.querySelector('span');

  if (!badge) {
    badge = document.createElement('span');

    badge.setAttribute('aria-hidden', 'true');

    header.append(badge);
  }

  badge.textContent = count;

  badge.hidden = !count;
}

function saveSetFrom(html) {
  const holder = document.createElement('template');

  holder.innerHTML = html.trim();

  const set = holder.content.firstElementChild;

  set.dataset.version = String(listsVersion);

  return set;
}

function saveExtras(set, body) {
  if (set.dataset.big) body.set('big', '1');

  if (set.dataset.scope) body.set('scope', set.dataset.scope);

  return body;
}

async function refreshSaveSet(set, open) {
  const form = set.querySelector('form.save-form');

  if (!form) return;

  const query = saveExtras(set, new URLSearchParams({ next: location.pathname + location.search }));

  try {
    const reply = await fetch(form.action + '?' + query, { headers: { Accept: 'application/json' } });

    if (!reply.ok) return;

    const fresh = saveSetFrom((await reply.json()).menu);

    fresh.querySelector('details').open = open;

    if (set.isConnected) set.replaceWith(fresh);
  } catch (error) {}
}

document.addEventListener('toggle', event => {
  const menu = event.target;

  if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains('save-menu') || !menu.open) return;

  const set = menu.closest('[data-save-set]');

  if (set && (set.dataset.version || '0') !== String(listsVersion)) refreshSaveSet(set, true);
}, true);

document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;

  const menu = document.querySelector('details.save-menu[open]');

  if (!menu) return;

  menu.open = false;

  menu.querySelector('summary').focus();
});

document.addEventListener('submit', async event => {
  const form = event.target.closest('form.save-form, form.saved-heart');

  if (!form) return;


  event.preventDefault();

  const set = form.closest('[data-save-set]');

  const body = new FormData(form);

  if (set) saveExtras(set, body);

  const buttons = (set || form).querySelectorAll('button');

  buttons.forEach(button => { button.disabled = true; });

  let answer;

  try {
    const reply = await fetch(form.action, { method: 'POST', body, headers: { Accept: 'application/json' } });

    if (!reply.ok || !(reply.headers.get('Content-Type') || '').includes('json')) throw new Error('not saved');

    answer = await reply.json();
  } catch (error) {
    buttons.forEach(button => { button.disabled = false; });

    form.submit();

    return;
  }

  listsVersion += 1;

  const flatId = set ? set.dataset.flat : '';

  const apply = () => {
    if (set) {
      const fresh = saveSetFrom(answer.menu);



      const menu = fresh.querySelector('details');

      set.replaceWith(fresh);

      let focusSpot = fresh.querySelector('summary');

      if (body.get('remove') === '1') {
        menu.open = false;
      } else if (body.get('collection')) {
        const picked = fresh.querySelector('input[name="collection"][value="' + CSS.escape(body.get('collection')) + '"]');

        if (picked) focusSpot = picked.form.querySelector('button');
      } else if (body.get('newName')) {
        const name = String(body.get('newName')).trim().replace(/\s+/g, ' ').toLowerCase();

        const option = [...fresh.querySelectorAll('.save-opt')].find(item => item.querySelector('.nm').textContent.trim().toLowerCase() === name);

        if (option) focusSpot = option;
      }

      focusSpot.focus();
    } else {
      buttons.forEach(button => { button.disabled = false; });

      const button = form.querySelector('button');

      button.setAttribute('aria-pressed', String(answer.saved));

      if (!answer.saved && answer.menu && !form.querySelector('input[name="collection"]')) {
        form.replaceWith(saveSetFrom(answer.menu));
      }
    }

    showSavedCount(answer.count);


    showSaveNote(answer.message, answer.failed);
  };

  if (set) apply(); else moveSmoothly(apply);

  if (flatId) {
    document.querySelectorAll('[data-save-set][data-flat="' + CSS.escape(flatId) + '"]').forEach(other => {
      if (other.dataset.version !== String(listsVersion)) refreshSaveSet(other, false);
    });
  }
});
