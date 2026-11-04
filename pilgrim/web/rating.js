/* Pilgrim Dot Farm — five-turkey song ratings (VOTING.md Step 5).
   The control is open only while the song it belongs to is audible. The server
   decides the window (`rating_window`); this file only hides/disables the
   control and renders what the server says. It never touches audio (§9.2). */
(() => {
  "use strict";
  const VOTER = window.PD_VOTER;
  const row = document.getElementById("rate-row");
  if (!row || !VOTER) return;
  const label = row.querySelector(".rate-label");
  const agg = document.getElementById("rate-agg");
  const turkeys = Array.from(row.querySelectorAll(".turkey"));
  const LABEL = "RATE THIS SONG";
  const POLL_MS = 5000;

  let current = null;        // {epoch, seq} of the airing the row belongs to
  let savedStars = 0;        // this browser's rating for `current`
  let preview = 0;           // hover/focus level
  let busy = false;
  let pollTimer = null;
  let labelTimer = null;

  function setEnabled(on) {
    row.setAttribute("aria-disabled", on ? "false" : "true");
    turkeys.forEach((t) => { t.disabled = !on; });
  }

  function setLabel(text) {
    label.textContent = text;
  }

  function flashLabel(text, ms) {
    if (labelTimer) clearTimeout(labelTimer);
    setLabel(text);
    labelTimer = setTimeout(() => { setLabel(LABEL); labelTimer = null; }, ms);
  }

  function paint(level) {
    turkeys.forEach((t, i) => {
      const on = i < level;
      t.classList.toggle("filled", on);
      t.setAttribute("aria-checked", i === level - 1 ? "true" : "false");
      t.tabIndex = i === 0 ? 0 : -1;
    });
  }

  function showAggregate(count, avg) {
    agg.textContent = (count > 0 && typeof avg === "number")
      ? avg.toFixed(1) + " · " + count : "";
  }

  function storageKey(airing) {
    return "pd_rating:" + airing.epoch + ":" + airing.seq;
  }

  function savedFor(airing) {
    const n = parseInt(VOTER.recall(storageKey(airing)) || "0", 10);
    return n >= 1 && n <= 5 ? n : 0;
  }

  async function poll() {
    if (!current) return;
    try {
      const r = await fetch("/api/station/state");
      if (!r.ok) return;
      const st = await r.json();
      if (!current) return;
      const rating = st.rating;
      if (!rating || !rating.open || st.current_seq !== current.seq) {
        // the song is over (or ratings were switched off)
        current = null;
        savedStars = 0; preview = 0;
        paint(0);
        showAggregate(0, null);
        setEnabled(false);
        stopPoll();
        return;
      }
      showAggregate(rating.count, rating.avg);
    } catch (e) { /* keep the control; retry on the next poll */ }
  }

  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(poll, POLL_MS);
  }
  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  async function submit(stars) {
    if (!current || busy) return;
    const airing = current;
    busy = true;
    turkeys.forEach((t) => { t.disabled = true; });
    try {
      const r = await fetch("/api/station/rating", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voter_id: VOTER.id(), epoch: airing.epoch,
                               seq: airing.seq, stars }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 200) {
        savedStars = d.mine;
        VOTER.remember(storageKey(airing), d.mine);
        preview = 0;
        paint(savedStars);
        showAggregate(d.count, d.avg);
        setEnabled(true);
      } else if (r.status === 409) {
        // the song ended while the click was in flight (V8 grace)
        current = null;
        savedStars = 0; preview = 0;
        paint(0);
        showAggregate(0, null);
        setEnabled(false);
        stopPoll();
        flashLabel("VOTING CLOSED", 3000);
      } else {
        flashLabel("TRY AGAIN", 3000);
        setEnabled(true);
        paint(savedStars);
      }
    } catch (e) {
      flashLabel("TRY AGAIN", 3000);
      setEnabled(true);
      paint(savedStars);
    } finally {
      busy = false;
    }
  }

  /* pd:nowplaying is dispatched by player.js from its audio-clock playhead:
     the detail is the song that is AUDIBLE now, or null. */
  document.addEventListener("pd:nowplaying", (e) => {
    const d = e.detail;
    if (!d) {
      current = null; savedStars = 0; preview = 0;
      paint(0); showAggregate(0, null);
      setEnabled(false); stopPoll();
      if (labelTimer) { clearTimeout(labelTimer); labelTimer = null; }
      setLabel(LABEL);
      return;
    }
    if (current && current.epoch === d.epoch && current.seq === d.seq) return;
    current = { epoch: d.epoch, seq: d.seq };
    savedStars = savedFor(current);
    preview = 0;
    setEnabled(true);
    paint(savedStars);
    setLabel(LABEL);
    startPoll();
    void poll();
  });

  turkeys.forEach((t, i) => {
    const stars = i + 1;
    t.addEventListener("click", () => submit(stars));
    t.addEventListener("mouseenter", () => {
      if (!t.disabled) { preview = stars; paint(preview); }
    });
    t.addEventListener("focus", () => {
      if (!t.disabled) { preview = stars; paint(preview); }
    });
    t.addEventListener("keydown", (e) => {
      const step = (e.key === "ArrowRight" || e.key === "ArrowDown") ? 1
        : (e.key === "ArrowLeft" || e.key === "ArrowUp") ? -1 : 0;
      if (!step) return;
      e.preventDefault();
      const next = turkeys[(i + step + turkeys.length) % turkeys.length];
      next.focus();
    });
  });

  row.querySelector(".turkeys").addEventListener("mouseleave", () => {
    preview = 0;
    paint(savedStars);
  });
  row.addEventListener("focusout", (e) => {
    if (!row.contains(e.relatedTarget)) { preview = 0; paint(savedStars); }
  });

  setEnabled(false);
  paint(0);
})();
