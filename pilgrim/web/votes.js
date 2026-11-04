/* Pilgrim Dot Farm — request-queue voting (VOTING.md Step 6).
   Builds the ↑ score ↓ cell for one queue row; requests.js owns the table.
   All DOM is created with createElement/textContent — listener text never
   becomes HTML. Own votes are remembered in localStorage and confirmed by the
   POST response; a POST score is sticky for 4 s against the 6 s board poll
   (the edge may serve a 2 s-old board), so a vote never visibly flickers. */
(() => {
  "use strict";
  const VOTER = window.PD_VOTER;
  if (!VOTER) {
    // voter.js must load first; if it did not, keep the table renderable with
    // an empty vote column instead of throwing inside requests.js.
    window.PD_VOTES = { cell: () => {
      const td = document.createElement("td");
      td.className = "req-vote";
      return td;
    }, refresh: () => {} };
    return;
  }
  const STICKY_MS = 4000;
  const sticky = new Map();   // request id -> {score, until}
  /* Cosmetic patch: the column starts at one vote, not zero, so a freshly
     queued request reads "1" instead of "0". Display only — ordering and
     burying keep using the raw server score (a uniform offset cannot change
     either). */
  const BASE_SCORE = 1;

  function scoreFor(row) {
    const s = sticky.get(row.id);
    if (s && Date.now() < s.until) return s.score;
    return typeof row.score === "number" ? row.score : 0;
  }

  function mineFor(id) {
    const raw = parseInt(VOTER.recall("pd_vote:" + id) || "0", 10);
    return raw === 1 || raw === -1 ? raw : 0;
  }

  function setSticky(id, score) {
    sticky.set(id, { score, until: Date.now() + STICKY_MS });
  }

  function arrow(cls, glyph, label) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = glyph;
    b.setAttribute("aria-label", label);
    b.setAttribute("aria-pressed", "false");
    return b;
  }

  function paint(row, wrap) {
    const mine = mineFor(row.id);
    const score = wrap.querySelector(".vote-score");
    const up = wrap.querySelector(".vote-up");
    const down = wrap.querySelector(".vote-down");
    score.textContent = String(BASE_SCORE + scoreFor(row));
    up.classList.toggle("pressed", mine === 1);
    down.classList.toggle("pressed", mine === -1);
    up.setAttribute("aria-pressed", mine === 1 ? "true" : "false");
    down.setAttribute("aria-pressed", mine === -1 ? "true" : "false");
  }

  function disable(wrap) {
    wrap.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  }

  async function cast(row, wrap, direction) {
    const mine = mineFor(row.id);
    const value = mine === direction ? 0 : direction;
    const buttons = wrap.querySelectorAll("button");
    buttons.forEach((b) => { b.disabled = true; });
    try {
      const r = await fetch("/api/requests/" + row.id + "/vote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voter_id: VOTER.id(), value }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 200) {
        VOTER.remember("pd_vote:" + row.id, d.mine);
        setSticky(row.id, d.score);
        paint(row, wrap);
        buttons.forEach((b) => { b.disabled = false; });
      } else if (r.status === 409) {
        // Voting closed (the row began airing): show the truth, then let the
        // next board poll relabel it.
        if (typeof d.score === "number") setSticky(row.id, d.score);
        VOTER.remember("pd_vote:" + row.id, 0);
        paint(row, wrap);
        disable(wrap);
      } else if (r.status === 429) {
        flash(wrap);
      } else {
        flash(wrap);
        buttons.forEach((b) => { b.disabled = false; });
      }
    } catch (e) {
      flash(wrap);
      buttons.forEach((b) => { b.disabled = false; });
    }
  }

  function flash(wrap) {
    wrap.classList.remove("err");
    // reflow so the animation restarts even on a second failure
    void wrap.offsetWidth;
    wrap.classList.add("err");
    setTimeout(() => wrap.classList.remove("err"), 700);
  }

  function cell(row) {
    const td = document.createElement("td");
    td.className = "req-vote";
    if (!row.votable) return td;   // paid row or voting disabled: empty cell
    const wrap = document.createElement("div");
    wrap.className = "vote";
    wrap.dataset.id = String(row.id);
    const up = arrow("vote-up", "↑", "Upvote request #" + row.id);
    const score = document.createElement("span");
    score.className = "vote-score";
    score.setAttribute("aria-live", "polite");
    const down = arrow("vote-down", "↓", "Downvote request #" + row.id);
    wrap.appendChild(up);
    wrap.appendChild(score);
    wrap.appendChild(down);
    td.appendChild(wrap);
    paint(row, wrap);
    up.addEventListener("click", () => cast(row, wrap, 1));
    down.addEventListener("click", () => cast(row, wrap, -1));
    return td;
  }

  function refresh(row, td) {
    const wrap = td && td.querySelector(".vote");
    if (wrap) paint(row, wrap);
  }

  window.PD_VOTES = { cell, refresh };
})();
