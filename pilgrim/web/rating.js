/* Pilgrim Dot Farm — five-turkey song ratings (VOTING.md Step 5; FIX_RATINGS.md Step 3).
   The control is open only while the song it belongs to is audible. The server
   decides the window (`rating_window`); this file only enables/disables the
   control and renders what the server says. It never touches audio (§9.2).

   Colours: pink (magenta) = the community average, rounded to the nearest
   turkey, shown until this browser rates the airing; cyan = the rating this
   browser gave THIS airing. The exact aggregate (`avg · n`) is always shown
   for the audible song: first from the programme row, then from the state
   poll (FIX_RATINGS.md D1/D2). */
(() => {
  "use strict";
  const VOTER = window.PD_VOTER;
  const row = document.getElementById("rate-row");
  if (!row || !VOTER) return;
  const label = row.querySelector(".rate-label");
  const agg = document.getElementById("rate-agg");
  const turkeys = Array.from(row.querySelectorAll(".turkey"));
  const LABEL = "RATE THIS SONG";
  const NONE = "NO RATINGS YET";
  const POLL_MS = 5000;

  let current = null;   // {epoch, seq, media_id} of the audible song
  let savedStars = 0;   // this browser's rating for THIS airing (cyan)
  let communityStars = 0; // the community average, rounded (pink), 0 when unrated
  let preview = 0;      // hover/focus level
  let busy = false;
  let pollTimer = null;
  let labelTimer = null;

  function setEnabled(on) {
    row.setAttribute("aria-disabled", on ? "false" : "true");
    turkeys.forEach((t) => { t.disabled = !on; });
  }

  function setLabel(text) { label.textContent = text; }

  function flashLabel(text, ms) {
    if (labelTimer) clearTimeout(labelTimer);
    setLabel(text);
    labelTimer = setTimeout(() => { setLabel(LABEL); labelTimer = null; }, ms);
  }

  /* Priority: hover preview (cyan) > this airing's rating (cyan) >
     community average (pink) > empty. */
  function paint() {
    let level = 0, cls = "filled";
    if (preview) { level = preview; }
    else if (savedStars) { level = savedStars; }
    else if (communityStars) { level = communityStars; cls = "prior"; }
    turkeys.forEach((t, i) => {
      const on = i < level;
      t.classList.toggle("filled", on && cls === "filled");
      t.classList.toggle("prior", on && cls === "prior");
      t.setAttribute("aria-checked", i === level - 1 ? "true" : "false");
      t.tabIndex = i === 0 ? 0 : -1;
    });
  }

  /* The community view: the exact text, and the rounded pink level (D2). */
  function showAggregate(count, avg) {
    const has = count > 0 && typeof avg === "number";
    agg.textContent = has ? avg.toFixed(1) + " · " + count : NONE;
    communityStars = has ? Math.min(5, Math.max(1, Math.round(avg))) : 0;
    paint();
  }
  function clearAggregate() { agg.textContent = ""; communityStars = 0; }

  function airingKey(a) { return "pd_rating:" + a.epoch + ":" + a.seq; }
  function recallStars(key) {
    const n = parseInt(VOTER.recall(key) || "0", 10);
    return n >= 1 && n <= 5 ? n : 0;
  }

  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(poll, POLL_MS);
  }

  /* Nothing audible (or STOP): empty row, no aggregate. */
  function reset() {
    current = null;
    savedStars = 0; preview = 0;
    clearAggregate();
    paint();
    setEnabled(false);
    stopPoll();
  }

  /* The window closed on the server while the song is still audible here:
     stop taking clicks but keep what the listener sees. */
  function closeWindow() {
    preview = 0;
    paint();
    setEnabled(false);
    stopPoll();
  }

  async function poll() {
    if (!current) return;
    const mine = current;
    try {
      const r = await fetch("/api/station/state");
      if (!r.ok) return;
      const st = await r.json();
      if (current !== mine) return;          // the song changed while we waited
      const rating = st.rating;
      if (!rating || st.epoch !== mine.epoch) { reset(); return; }  // ratings off / programme reset
      if (st.current_seq === mine.seq) {
        if (rating.open) {
          showAggregate(rating.count, rating.avg);
          if (!busy) setEnabled(true);
        }
        return;
      }
      if (typeof st.current_seq === "number" && st.current_seq > mine.seq) {
        closeWindow();                        // the server has moved past this song
      }
      // st.current_seq < mine.seq: the snapshot is a tick behind our playhead
      // (song start). Keep the control; the next poll will catch up.
    } catch (e) { /* keep the control; retry on the next poll */ }
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
      if (current !== airing) return;        // the song changed while in flight
      if (r.status === 200) {
        savedStars = d.mine;                 // cyan now outranks the pink
        preview = 0;
        VOTER.remember(airingKey(airing), d.mine);
        showAggregate(d.count, d.avg);       // repaints
        setEnabled(true);
      } else if (r.status === 409) {
        // the song ended while the click was in flight (V8 grace)
        closeWindow();
        flashLabel("VOTING CLOSED", 3000);
      } else {
        flashLabel("TRY AGAIN", 3000);
        preview = 0;
        paint();
        setEnabled(true);
      }
    } catch (e) {
      if (current === airing) {
        flashLabel("TRY AGAIN", 3000);
        preview = 0;
        paint();
        setEnabled(true);
      }
    } finally {
      busy = false;
    }
  }

  /* pd:nowplaying is dispatched by player.js from its audio-clock playhead:
     the detail is the song that is AUDIBLE now, or null. */
  document.addEventListener("pd:nowplaying", (e) => {
    const d = e.detail;
    if (!d) {
      reset();
      if (labelTimer) { clearTimeout(labelTimer); labelTimer = null; }
      setLabel(LABEL);
      return;
    }
    if (current && current.epoch === d.epoch && current.seq === d.seq) return;
    current = { epoch: d.epoch, seq: d.seq, media_id: d.media_id };
    savedStars = recallStars(airingKey(current));
    preview = 0;
    setEnabled(true);
    showAggregate(d.rating_count || 0,
                  (typeof d.rating_avg === "number") ? d.rating_avg : null);  // paints
    setLabel(LABEL);
    startPoll();
    void poll();
  });

  turkeys.forEach((t, i) => {
    const stars = i + 1;
    t.addEventListener("click", () => submit(stars));
    t.addEventListener("mouseenter", () => {
      if (!t.disabled) { preview = stars; paint(); }
    });
    t.addEventListener("focus", () => {
      if (!t.disabled) { preview = stars; paint(); }
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
    paint();
  });
  row.addEventListener("focusout", (e) => {
    if (!row.contains(e.relatedTarget)) { preview = 0; paint(); }
  });

  setEnabled(false);
  paint();
})();
