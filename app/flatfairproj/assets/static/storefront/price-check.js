const form = document.querySelector("[data-steps]");

const landed = document.querySelector(".val-grid--result #valuation-feedback");

if (landed) landed.focus({preventScroll: true});

if (form) {
  const grid = form.closest(".val-grid");

  const fields = form.elements;

  const steps = [...form.querySelectorAll(".val-step")];

  const items = [...grid.querySelectorAll(".val-list li")];

  const layout = grid.querySelector("[data-layout]");

  const image = layout.querySelector("img");

  const names = {flatType: "flatType", area: "floorAreaSqm", flatModel: "flatModel", lease: "leaseCommenceYear", storey: "storey"};

  const start = Number(form.dataset.start);

  let now = start;

  let timer = 0;

  let hop = 0;

  let found = {errors: {}, warnings: {}, fixes: {}};

  let shown = layout.hidden ? "" : image.getAttribute("src");

  function part(name) {
    return grid.querySelector("[data-val=" + name + "]");
  }

  function typeTag() {
    const picked = form.querySelector("[name=flatType]:checked");


    return picked ? picked.parentNode.textContent.trim() : "";
  }

  function townTag() {
    return fields.town.value ? fields.town.selectedOptions[0].text : "";
  }

  function planUrl() {
    const query = new URLSearchParams();

    for (const name in names) {
      const field = fields[name];

      const fine = field.checkValidity ? field.checkValidity() : true;

      if (field.value && fine) query.set(names[name], field.value);
    }

    if (!query.has("flatType") || !query.has("floorAreaSqm")) return "";

    return layout.dataset.layout + "?" + query;
  }

  function refresh() {
    const url = planUrl();

    if (!url) {
      layout.hidden = true;

      shown = "";

      return;
    }

    if (url === shown) return;

    shown = url;

    image.src = url;

    image.parentNode.href = url;

    image.alt = "Typical layout: " + typeTag() + " flat, " + fields.area.value + " sqm";


    layout.hidden = false;
  }

  function answers() {
    const price = fields.ask.value && "$" + Number(fields.ask.value).toLocaleString("en-SG");

    return [
      townTag() || fields.postal.value,
      typeTag(),
      fields.area.value && fields.area.value + " sqm",
      fields.storey.value && "Floor " + fields.storey.value,
      fields.lease.value,
      [fields.flatModel.value, price].filter(Boolean).join(", "),
    ];
  }

  function paint() {
    const list = answers();

    const fine = list.map((text, i) => text !== "" && !steps[i].querySelector("[data-note].is-error"));

    const done = fine.filter(Boolean).length;

    const left = fine.slice(0, 5).filter(good => !good).length;

    const place = "Question " + now + " of " + steps.length;



    let close = left + " more to see your price";

    if (left === 1 && now === steps.length - 1) close = "Last one";

    if (left === 0) close = "Ready for your price";

    if (now === steps.length) close = "Optional extras";

    part("step-now").textContent = place;

    part("step-left").textContent = close;

    part("segments").setAttribute("aria-valuenow", done);

    part("segments").setAttribute("aria-valuetext", place + ", " + close);

    part("ring").style.strokeDasharray = done / steps.length * 151 + " 151";


    part("ring-text").textContent = done + "/" + steps.length;


    part("ready").textContent = left ? "after " + left + " more answer" + (left > 1 ? "s" : "") : "ready";

    list.forEach((text, i) => {
      const state = i + 1 === now ? "now" : text ? "done" : "";

      const marked = [...steps[i].querySelectorAll("[data-note]")].filter(line => line.textContent);

      const flag = items[i].querySelector(".val-flag");

      part("segments").children[i].dataset.state = state;



      items[i].dataset.state = state;

      items[i].querySelector("button").disabled = state !== "done";

      items[i].querySelector("b").textContent = text;

      flag.hidden = !marked.length || state !== "done";

      flag.textContent = marked.some(line => line.classList.contains("is-error")) ? "Fix" : "Check";
    });

    part("summary").replaceChildren(...list.map((text, i) => {
      const link = document.createElement("button");

      link.type = "button";

      link.dataset.goto = i + 1;



      link.textContent = text;

      link.hidden = !text || i + 1 === now;



      return link;
    }));
  }

  function noteText(line) {
    return found.errors[line.dataset.note] || found.warnings[line.dataset.note] || "";
  }

  function notes() {
    form.querySelectorAll("[data-note]").forEach(line => {
      line.textContent = noteText(line);

      line.classList.toggle("is-error", Boolean(found.errors[line.dataset.note]));
    });

    const other = found.fixes.town && fields.town.value && found.fixes.town !== fields.town.value;

    part("fix").hidden = !other;

    part("fix").textContent = "Use " + found.fixes.townLabel;

    if (found.fixes.town && !fields.town.value) {
      fields.town.value = found.fixes.town;

      fields.town.dispatchEvent(new Event("change", {bubbles: true}));
    }

    paint();
  }

  async function check(reached) {
    const query = new URLSearchParams(new FormData(form));

    query.set("step", reached);

    try {
      const reply = await fetch(form.dataset.check + "?" + query);

      if (reply.ok) {
        found = await reply.json();

        return true;
      }
    } catch (error) {
      found = {errors: {}, warnings: {}, fixes: {}};
    }

    found = {errors: {}, warnings: {}, fixes: {}};

    return false;
  }

  function glide(move, change) {
    document.documentElement.dataset.valMove = move;

    moveSmoothly(change);
  }

  function settle() {
    refresh();

    notes();
  }

  function later() {
    paint();



    clearTimeout(timer);

    timer = setTimeout(async () => {
      const from = now;

      await check(from - 1);

      if (now !== from) return;

      const same = [...form.querySelectorAll("[data-note]")].every(line => line.textContent === noteText(line));

      if (same && Boolean(planUrl()) !== layout.hidden) settle();
      else glide("stay", settle);
    }, 400);
  }

  function show(step) {
    now = step;



    clearTimeout(timer);

    refresh();



    steps.forEach((item, i) => item.hidden = i + 1 !== step);

    const typing = steps[0].classList.contains("val-step--postal");

    part("back").hidden = step === 1;

    part("next").hidden = step === steps.length || step === 1 && !typing;

    part("skip").hidden = step !== steps.length;

    part("switch").hidden = step !== 1;

    part("switch").textContent = typing ? "Pick on the map instead" : "I know the postal code";

    form.querySelector(".val-submit").hidden = step !== steps.length;

    grid.classList.toggle("val-grid--asking", step > 1);

    form.querySelectorAll("[data-size-for]").forEach(line => line.hidden = line.dataset.sizeFor !== fields.flatType.value);

    document.getElementById("id_area_hint").hidden = fields.flatType.value !== "";

    paint();
  }

  function go(step, push) {
    glide(step < now ? "back" : "forward", () => {
      notes();

      show(step);

      const typed = steps[step - 1].querySelector(".val-unit input");

      (typed || steps[step - 1].querySelector("legend")).focus({preventScroll: true});
    });

    if (push) history.pushState(null, "", "#step-" + step);
  }

  function ready() {
    const wrong = [...steps[now - 1].querySelectorAll("input, select")].find(field => !field.checkValidity());

    if (wrong) wrong.reportValidity();

    return !wrong;
  }

  function send() {
    part("wait-line").textContent = "Comparing with recent sales of " + typeTag() + " flats" + (townTag() ? " in " + townTag() : "");

    glide("stay", () => {
      notes();

      form.classList.add("val-ask--sending");

      part("wait").hidden = false;
    });

    form.action = form.action.split("#")[0];

    setTimeout(() => form.submit(), 700);
  }

  async function advance() {
    const from = now;



    const reached = await check(from);

    const wrong = Object.keys(found.errors).map(name => steps.findIndex(step => step.querySelector("[data-note=" + name + "]")));

    const stuck = Math.min(from, ...wrong.filter(at => at >= 0));

    if (now !== from || !reached && !ready()) return;

    if (stuck < from) {
      if (stuck + 1 === from) glide("stay", notes);
      else go(stuck + 1, true);

      return;
    }

    if (from < steps.length) go(from + 1, true);
    else send();
  }

  function soon() {
    const from = now;

    clearTimeout(hop);

    hop = setTimeout(() => {
      if (now === from) advance();
    }, 350);
  }

  form.addEventListener("submit", event => {
    event.preventDefault();

    advance();
  });

  grid.addEventListener("click", event => {
    const link = event.target.closest(".error-summary a");

    const target = link && form.querySelector(link.getAttribute("href"));

    const step = target && target.closest(".val-step");

    const jump = event.target.closest("[data-goto]");

    if (step) show(steps.indexOf(step) + 1);

    if (jump) go(Number(jump.dataset.goto), true);

    if (event.target.name === "flatType" && event.detail) soon();
  });

  grid.addEventListener("townmap:pick", soon);

  form.addEventListener("change", event => {
    if (event.target === fields.town) soon();

    if (event.target === fields.postal && fields.town.value) fields.town.dispatchEvent(new Event("change", {bubbles: true}));

    later();
  });

  form.addEventListener("input", later);

  part("back").addEventListener("click", () => go(now - 1, true));

  part("fix").addEventListener("click", () => {
    fields.town.value = found.fixes.town;

    fields.town.dispatchEvent(new Event("change", {bubbles: true}));
  });

  part("switch").addEventListener("click", () => {
    glide("stay", () => {
      steps[0].classList.toggle("val-step--postal");

      show(1);

      if (steps[0].classList.contains("val-step--postal")) fields.postal.focus({preventScroll: true});
    });
  });

  addEventListener("popstate", () => {
    const match = location.hash.match(/^#step-(\d)$/);

    go(match ? Number(match[1]) : start, false);
  });

  addEventListener("pageshow", event => {
    if (event.persisted) {
      form.classList.remove("val-ask--sending");

      part("wait").hidden = true;
    }
  });

  form.querySelectorAll("[data-note]").forEach(line => {
    const kind = line.classList.contains("is-error") ? found.errors : found.warnings;

    if (line.textContent) kind[line.dataset.note] = line.textContent;
  });

  form.classList.add("val-ask--live");

  grid.classList.add("val-grid--live");

  fields.town.closest("label").hidden = true;

  fields.postal.closest("details").open = true;

  steps[0].classList.toggle("val-step--postal", fields.postal.value !== "");

  part("progress").hidden = false;

  part("track").hidden = false;

  show(start);
}
