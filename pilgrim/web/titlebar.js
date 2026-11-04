/* Pilgrim Dot Farm — sizes the pilgrim-hat logo box to the full height of the
   title block. The title wraps to two lines on narrow screens, and CSS can't
   tie an image's height to a sibling's wrapped text height, so measure it here.
   Cosmetic only: no network, never touches playback or the request line. */
(() => {
  "use strict";

  const bar = document.getElementById("titlebar");
  const hat = bar && bar.querySelector(".hat");
  const logo = bar && bar.querySelector(".logo");
  if (!hat || !logo) return;

  const RATIO = 452 / 375; // intrinsic aspect of img/pilgrim_hat.png

  function fit() {
    // Two passes: widening the hat narrows the title column, which can add a
    // wrapped line (and so a taller title block) on narrow screens.
    for (let i = 0; i < 2; i++) {
      const h = Math.round(logo.getBoundingClientRect().height);
      if (!h) return;
      hat.style.height = h + "px";
      hat.style.width = Math.round(h * RATIO) + "px";
    }
  }

  fit();
  addEventListener("resize", fit);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fit);
})();


/* Bottom fade cue for the scrolling #desktop stack (COSMETIC_PATCHING §3).
   Shows while there is content below the fold, like the queue's .has-more. */
(() => {
  "use strict";
  const desk = document.getElementById("desktop");
  if (!desk) return;
  function cue() {
    desk.classList.toggle(
      "has-more",
      desk.scrollHeight - desk.scrollTop - desk.clientHeight > 4);
  }
  desk.addEventListener("scroll", cue);
  addEventListener("resize", cue);
  if (window.MutationObserver) {
    new MutationObserver(cue).observe(desk, { childList: true, subtree: true });
  }
  cue();
})();
