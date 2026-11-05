/* Pilgrim Dot Farm — persistent unique-visitor counter (RADIO §14,
   COSMETIC_PATCHING §6). One POST /api/visitors per page load; the server
   derives the client IP itself (never client-supplied). Counts once per
   distinct network address. The same response carries the DB request counts
   shown above it (RADIO §9.1): paid priority requests and every request row.
   A failed request falls back to 0 and never disturbs playback or the request
   line. Also refreshes the "Listening now" line from the live listener count. */
(() => {
  "use strict";

  const el = document.getElementById("visitors");
  if (!el) return;

  const paidEl = document.getElementById("paid-requests");
  const totalEl = document.getElementById("total-requests");

  function setCount(node, label, raw) {
    const n = Number(raw);
    if (node) node.textContent = label + (Number.isInteger(n) && n >= 0 ? n : 0);
  }

  async function load() {
    try {
      const r = await fetch("/api/visitors", { method: "POST" });
      if (!r.ok) throw new Error("visitor HTTP " + r.status);
      const d = await r.json();
      setCount(el, "Unique visitors: ", d && d.unique_visitors);
      setCount(paidEl, "Paid requests: ", d && d.paid_requests);
      setCount(totalEl, "Total request: ", d && d.total_requests);
    } catch (e) {
      setCount(el, "Unique visitors: ", 0);
      setCount(paidEl, "Paid requests: ", 0);
      setCount(totalEl, "Total request: ", 0);
    }
  }

  load(); // once per page load; no continuous refresh needed (§6)

  /* Listening now: "69." + (live listener count + LISTENER_OFFSET), from
     /api/station/state (operator decision, RADIO §9.1; constant +10 offset
     added 2026-10-03). A failed poll keeps the last value. */
  const LISTENER_OFFSET = 10; // constant added to the displayed listener count
  const lis = document.getElementById("listeners");
  async function pollListeners() {
    try {
      const r = await fetch("/api/station/state");
      if (!r.ok) throw new Error("state HTTP " + r.status);
      const n = Number((await r.json()).listeners);
      if (Number.isInteger(n) && n >= 0) lis.textContent = "Listening now: 68." + (n + LISTENER_OFFSET);
    } catch (e) { /* keep last value */ }
  }
  if (lis) {
    pollListeners();
    setInterval(pollListeners, 15000);
  }

})();
