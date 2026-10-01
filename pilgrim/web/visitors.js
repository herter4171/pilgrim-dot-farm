/* Pilgrim Dot Farm — persistent unique-visitor counter (RADIO §14,
   COSMETIC_PATCHING §6). One POST /api/visitors per page load; the server
   derives and hashes the client identity (never a client-supplied signature).
   Counts once per distinct network address browser-wide. A failed or
   unavailable counter shows "—", never a fabricated zero, and never disturbs
   playback or the request line. */
(() => {
  "use strict";

  const el = document.getElementById("visitors");
  if (!el) return;

  async function load() {
    try {
      const r = await fetch("/api/visitors", { method: "POST" });
      if (!r.ok) throw new Error("visitor HTTP " + r.status);
      const d = await r.json();
      const n = Number(d && d.unique_visitors);
      el.textContent = Number.isInteger(n) && n >= 0
        ? "Unique visitors: " + n
        : "Unique visitors: —";
    } catch (e) {
      el.textContent = "Unique visitors: —";
    }
  }

  load(); // once per page load; no continuous refresh needed (§6)
})();
