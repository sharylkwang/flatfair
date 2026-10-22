const photoForm = document.querySelector("form[data-photo-check]");

if (photoForm) {
  const token = photoForm.elements.draftToken.value;

  const csrf = photoForm.elements.csrfmiddlewaretoken.value;

  const slots = new Map();

  let queue = Promise.resolve();

  let waiting = 0;

  let timer = null;

  let checkingSince = 0;

  async function getReply(url, options = {}, timeout = 20000) {
    const stopper = new AbortController();

    const timeoutId = setTimeout(() => stopper.abort(), timeout);

    try {
      const reply = await fetch(url, {...options, signal: stopper.signal});

      return {ok: reply.ok, reply: await reply.json()};
    } finally {
      clearTimeout(timeoutId);
    }
  }

  function runLater() {
    clearTimeout(timer);

    if (![...slots.values()].some(slot => slot.checking)) {
      checkingSince = 0;

      return;
    }

    checkingSince = checkingSince || Date.now();

    timer = setTimeout(poll, Date.now() - checkingSince > 60000 ? 10000 : 1500);
  }

  async function poll() {
    if (waiting) {
      runLater();

      return;
    }

    queue = queue.then(async () => {
      const rounds = new Map([...slots].map(([room, slot]) => [room, slot.generation]));

      try {
        const url = new URL(photoForm.dataset.photoStatus, location.href);

        url.searchParams.set("draftToken", token);

        const {ok, reply} = await getReply(url, {credentials: "same-origin", cache: "no-store"}, 10000);

        if (ok) {
          Object.entries(reply.photos || {}).forEach(([room, result]) => {
            const slot = slots.get(room);

            const source = slots.get(result.movedFrom);

            if (source && source.checking && !source.unsaved && source.generation === rounds.get(result.movedFrom)) {
              source.apply({verdict: "moved", message: result.movedNote, movedTo: room, target: result, url: result.movedFromUrl});
            } else if (slot && slot.checking && !slot.unsaved && slot.generation === rounds.get(room)) {
              slot.apply(result);
            }
          });
        }
      } catch (error) {
        slots.forEach((slot, room) => {
          if (slot.checking && !slot.unsaved && slot.generation === rounds.get(room)) {
            slot.status.textContent = "Checking… refresh to see the result.";
          }
        });
      }
    }).finally(runLater);

    await queue;
  }

  photoForm.querySelectorAll(".photo-slot").forEach(element => {
    const input = element.querySelector("input[type=file]");



    const img = element.querySelector("img");

    const status = element.querySelector(".photo-status");

    const action = element.querySelector(".slot-action");

    const appeal = element.querySelector(".photo-appeal");

    const pleaBox = appeal.querySelector("input");

    const back = element.querySelector(".photo-back");



    let latest = null;

    let preview = img.getAttribute("src");

    const slot = {checking: element.dataset.checking === "true", status, generation: 0, unsaved: false};

    slots.set(element.dataset.room, slot);

    function showPic(url) {
      if (url) {
        img.src = url;
      } else {
        img.removeAttribute("src");
      }

      img.hidden = !url;

      element.classList.toggle("has-photo", Boolean(url));

      action.textContent = url ? "Change photo" : "Add photo";
    }

    function showDone(tone, message) {
      element.classList.remove("checking", "good", "bad", "flagged", "moved");

      if (tone) {
        element.classList.add(tone);
      }


      status.textContent = message;
    }

    slot.apply = reply => {
      slot.checking = reply.verdict === "checking";

      element.dataset.checking = String(slot.checking);

      if (!reply.verdict) {
        showDone("flagged", reply.error || "We could not save it now. Send the form to save this photo.");

        return;
      }

      input.value = "";

      appeal.hidden = !reply.appealable;

      pleaBox.checked = false;



      back.hidden = reply.verdict !== "moved";

      if (reply.verdict === "moved") {
        showPic(reply.url);

        showDone("moved", reply.message || "We moved this photo to the room it shows. This room still needs its own photo.");



        slots.get(reply.movedTo).apply(reply.target);
      } else if (reply.verdict === "block" || reply.verdict === "invalid") {
        showPic("");

        action.textContent = "Replace photo";

        showDone("bad", "Not accepted. " + reply.message);
      } else {
        showPic(reply.url || preview);


        const error = element.querySelector(".err");


        if (error) {
          error.remove();
        }

        input.removeAttribute("aria-invalid");

        input.removeAttribute("aria-describedby");


        if (slot.checking) {
          showDone("", "Checking…");
        } else if (reply.verdict === "accept") {
          showDone("good", "Accepted. " + reply.message);
        } else {
          showDone("flagged", reply.message + (reply.reason ? " Reason: " + reply.reason + "." : ""));
        }
      }

      runLater();
    };

    async function upload(file, pleading, round) {
      if (round !== slot.generation || (!pleading && file !== latest)) {
        return;
      }

      const data = new FormData();

      data.append("draftToken", token);

      data.append("room", element.dataset.room);

      data.append("bedrooms", photoForm.elements.bedrooms.value);

      data.append("bathrooms", photoForm.elements.bathrooms.value);

      data.append(pleading ? "appeal" : "photo", pleading ? "1" : file);

      try {
        const {reply} = await getReply(photoForm.dataset.photoCheck, {
          method: "POST", body: data, headers: {"X-CSRFToken": csrf}, credentials: "same-origin"
        });

        if (round === slot.generation) {
          if (reply.verdict) {
            slot.unsaved = false;
          }

          slot.apply(reply);
        }
      } catch (error) {
        if (round === slot.generation) {
          slot.checking = false;

          element.dataset.checking = "false";

          showDone("flagged", pleading
            ? "We could not send the request now. Send the form to ask staff to check this photo."
            : "We could not save it now. Send the form to save this photo.");
        }
      }
    }

    input.addEventListener("change", () => {
      const file = input.files[0];

      if (!file) {
        return;
      }

      const round = ++slot.generation;

      if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
        slot.checking = false;


        slot.unsaved = false;

        element.dataset.checking = "false";

        input.value = "";

        showDone("bad", "This file is not a photo. Choose a JPG, PNG or WebP.");


        return;
      }


      latest = file;

      slot.unsaved = true;

      const remove = element.querySelector("input[name=removePhoto]");

      if (remove) {
        remove.checked = false;
      }

      if (preview && preview.startsWith("blob:")) {
        URL.revokeObjectURL(preview);
      }

      preview = URL.createObjectURL(file);

      showPic(preview);

      appeal.hidden = true;

      pleaBox.checked = false;

      back.hidden = true;

      slot.checking = true;

      showDone("", "Checking…");

      waiting += 1;

      queue = queue.then(() => upload(file, false, round)).finally(() => { waiting -= 1; runLater(); });
    });

    pleaBox.addEventListener("change", () => {
      if (pleaBox.checked) {
        const round = slot.generation;



        const file = latest;

        waiting += 1;

        queue = queue.then(() => upload(file, true, round)).finally(() => { waiting -= 1; runLater(); });
      }
    });
  });

  photoForm.addEventListener("submit", async event => {
    const button = event.submitter;

    if (!waiting || (button && ["saveDraft", "discardDraft"].includes(button.value))) {
      return;
    }

    event.preventDefault();

    await queue;

    if (button) {
      photoForm.requestSubmit(button);
    } else {
      photoForm.requestSubmit();
    }
  });

  runLater();
}
