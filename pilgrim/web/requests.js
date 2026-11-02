/* Pilgrim Dot Farm — Request line (FIFO top-10, LLM-moderated).
   Submits a listener request to /api/requests and renders the live queue,
   oldest first. No build step; plain DOM. Always render request text via
   textContent so no listener content is ever injected as HTML.
*/
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const input = $("req-input"), send = $("req-send");
  const queueEl = $("req-queue"), errEl = $("req-err");
  let lastQueue = [];

  let hideTimer = null;

  function showErr(msg, ok = false, sticky = false) {
    // one timer: an earlier message's auto-hide must not blank a newer one
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    errEl.textContent = msg || "";
    errEl.classList.toggle("hidden", !msg);
    errEl.style.color = ok ? "var(--ok)" : "var(--warn)";
    if (msg && !sticky) hideTimer = setTimeout(() => { errEl.classList.add("hidden"); }, 5000);
  }

  function cell(cls, text) {
    const td = document.createElement("td");
    td.className = cls;
    td.textContent = text;   // textContent = no HTML injection
    return td;
  }

  function emptyRow(text) {
    const tr = document.createElement("tr");
    tr.className = "req-empty";
    const td = document.createElement("td");
    td.colSpan = 3;
    td.textContent = text;
    tr.appendChild(td);
    return tr;
  }

  function promptCell(r, num) {
    const td = document.createElement("td");
    td.className = "req-text";
    if (num) {
      const n = document.createElement("span");
      n.className = "req-num"; n.textContent = "#" + num;
      td.appendChild(n);
    }
    if (r.paid) {
      const icon = document.createElement("img");
      icon.className = "req-paid-icon";
      icon.src = "img/cornucopia.png";
      icon.alt = "Paid priority request";
      icon.width = 60; icon.height = 50;  // 1.25x of 48x40, same 6:5 ratio
      td.appendChild(icon);
    }
    td.appendChild(document.createTextNode(r.text || ""));
    return td;
  }

  function render(queue, recent) {
    lastQueue = queue || [];
    queueEl.textContent = "";
    if (!lastQueue.length) {
      queueEl.appendChild(emptyRow("— no requests queued —"));
    } else {
      // FIFO queue: the oldest ready request is the next one to air; any
      // later ready request is rendered but still behind it. Don't label both
      // "up next" (the old bug that claimed a slot three songs away).
      let readySeen = false;
      lastQueue.forEach((r, i) => {
        const tr = document.createElement("tr");
        tr.className = "req-item";
        // producing has two stages: the lyrics/brief are being written, then
        // the song model is rendering ("being generated"); older servers send
        // no stage, which reads as "being written"
        let label = ({ queued: "in line", producing: "being written" })[r.status] || "";
        if (r.status === "producing" && r.stage === "generating") label = "being generated";
        if (r.status === "producing" && r.stage === "waiting") label = "in line";
        // song rendered; its intro is made, then it waits for the requests
        // ahead of it (ready order is air order)
        if (r.status === "producing" && r.stage === "rendered") label = "generated, in line";
        if (r.status === "ready") {
          label = readySeen ? "ready to air" : "up next";
          readySeen = true;
        }
        tr.appendChild(promptCell(r, i + 1));
        tr.appendChild(cell("req-title", r.song_title || "—"));
        tr.appendChild(cell("req-status", label));
        queueEl.appendChild(tr);
      });
    }
    // aired requests: prompt -> song title, and how long it took to air
    const recentEl = $("req-recent");
    if (!recentEl) return;
    recentEl.textContent = "";
    // defensive: server sends 5; slice keeps a mixed-version deploy honest
    ((recent || []).slice(0, 5)).forEach((r) => {
      const tr = document.createElement("tr");
      tr.className = "req-recent-item";
      tr.appendChild(promptCell(r, 0));
      const title = r.song_title
        ? r.song_title + (r.song_artist ? " · " + r.song_artist : "") : "—";
      tr.appendChild(cell("req-title", title));
      tr.appendChild(cell("req-wait", waitLabel(r.wait_s)));
      recentEl.appendChild(tr);
    });
    if (!recentEl.childElementCount) {
      recentEl.appendChild(emptyRow("— nothing played for you yet —"));
    }
  }

  function waitLabel(seconds) {
    // the column is headed "Prep Time": lyrics writing -> on air. Rows that
    // never recorded a writing start (aired before that column existed) get
    // an em dash: measured-or-nothing, never queue idle dressed up as prep.
    if (typeof seconds !== "number" || !isFinite(seconds)) return "—";
    const mins = Math.round(seconds / 60);
    if (mins < 60) return Math.max(1, mins) + " min";
    const hours = Math.floor(mins / 60), rem = mins % 60;
    if (hours < 48) return hours + " h" + (rem ? " " + rem + " m" : "");
    const days = Math.floor(hours / 24), hh = hours % 24;
    return days + " d" + (hh ? " " + hh + " h" : "");
  }

  async function refresh() {
    try {
      const r = await fetch("/api/requests");
      if (!r.ok) return;
      const d = await r.json();
      render(d.queue, d.recent);
      if (window.PD_PAY && typeof window.PD_PAY.setEnabled === "function") {
        window.PD_PAY.setEnabled(d.paid_enabled !== false);
      }
    } catch (e) { /* station down: keep last known queue */ }
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) { showErr("Type a request first."); return; }
    send.disabled = true;
    // The POST covers the prefilter + LLM moderation (seconds, occasionally a
    // minute when the model retries): say so, and say so again if it drags.
    showErr("Checking for unseemly content…", true, true);
    const slow = setTimeout(() => {
      showErr("Still checking — the DJ's filter is thinking it over…", true, true);
    }, 15000);
    try {
      const r = await fetch("/api/requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 503 && d.off_air) {
        showErr("The station is off the air. Requests are closed.");
        input.select();
      } else if (r.status === 429) {
        showErr(d.detail || "Easy there — a few requests every ten minutes, please.");
        input.select();
      } else if (d.ok) {
        input.value = "";
        showErr("✓ Got it — we'll write you a song.", true);
      } else {
        showErr("Head's up: " + (d.reason || "that didn't pass the DJ's taste filter."));
        input.select();
      }
      render(d.queue || lastQueue, d.recent);
      if (window.PD_PAY && typeof window.PD_PAY.setEnabled === "function"
          && typeof d.paid_enabled === "boolean") {
        window.PD_PAY.setEnabled(d.paid_enabled);
      }
    } catch (e) {
      showErr("Couldn't reach the station. Try again.");
    } finally {
      clearTimeout(slow);
      send.disabled = false;
    }
  }

  send.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  setInterval(refresh, 6000);
  refresh();
})();
