(() => {
  "use strict";



  document.querySelectorAll("[data-listing-description]").forEach(blurb => {


    const preview = blurb.querySelector(".listing-description-preview");



    const details = blurb.querySelector("details");



    preview.classList.add("is-clamped");



    function setBlurb() {
      if (details.open) return;



      details.hidden = preview.scrollHeight <= preview.clientHeight + 1;
    }



    details.addEventListener("toggle", () => {
      preview.hidden = details.open;



      setBlurb();
    });



    setBlurb();


    if ("ResizeObserver" in window) {
      new ResizeObserver(setBlurb).observe(preview);
    } else {
      window.addEventListener("resize", setBlurb);
    }



    if (document.fonts) document.fonts.ready.then(setBlurb);
  });



})();
