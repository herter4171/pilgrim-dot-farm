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

  function showErr(msg, ok = false) {
    errEl.textContent = msg || "";
    errEl.classList.toggle("hidden", !msg);
    errEl.style.color = ok ? "var(--ok)" : "var(--warn)";
    if (msg) setTimeout(() => { errEl.classList.add("hidden"); }, 5000);
  }

  function render(queue) {
    lastQueue = queue || [];
    queueEl.textContent = "";
    if (!lastQueue.length) {
      const li = document.createElement("li");
      li.className = "req-empty";
      li.textContent = "— no requests yet —";
      queueEl.appendChild(li);
      return;
    }
    lastQueue.forEach((r, i) => {
      const li = document.createElement("li");
      li.className = "req-item";
      const n = document.createElement("span");
      n.className = "req-num"; n.textContent = "#" + (i + 1);
      const t = document.createElement("span");
      t.className = "req-text"; t.textContent = r.text;  // textContent = no HTML injection
      li.appendChild(n); li.appendChild(t);
      queueEl.appendChild(li);
    });
  }

  async function refresh() {
    try {
      const r = await fetch("/api/requests");
      if (!r.ok) return;
      const d = await r.json();
      render(d.queue);
    } catch (e) { /* station down: keep last known queue */ }
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) { showErr("Type a request first."); return; }
    send.disabled = true;
    try {
      const r = await fetch("/api/requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 429) {
        showErr(d.detail || "Easy there — a few requests every ten minutes, please.");
        input.select();
      } else if (d.ok) {
        input.value = "";
        showErr("✓ Got it — we'll write you a song.", true);
      } else {
        showErr("Head's up: " + (d.reason || "that didn't pass the DJ's taste filter."));
        input.select();
      }
      render(d.queue || lastQueue);
    } catch (e) {
      showErr("Couldn't reach the station. Try again.");
    } finally {
      send.disabled = false;
    }
  }

  send.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  setInterval(refresh, 6000);
  refresh();
})();
