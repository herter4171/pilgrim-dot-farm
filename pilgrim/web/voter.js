/* Pilgrim Dot Farm — per-browser voter id (VOTING.md V1).
   One random 32-hex id in localStorage, generated once. It is indicative only:
   the server stores it with the canonical client IP and caps distinct ids per
   IP per target (V3). No accounts, no PII. Storage can throw (private mode), so
   every access is wrapped; an in-memory id keeps voting working for the tab. */
(() => {
  "use strict";
  const KEY = "pd_voter";

  function randomId() {
    const bytes = new Uint8Array(16);
    if (window.crypto && typeof crypto.getRandomValues === "function") {
      crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  let id = null;
  try { id = localStorage.getItem(KEY); } catch (e) { /* private mode */ }
  if (!/^[0-9a-f]{32}$/.test(id || "")) {
    id = randomId();
    try { localStorage.setItem(KEY, id); } catch (e) { /* keep the in-memory id */ }
  }

  function remember(key, value) {
    try { localStorage.setItem(key, String(value)); } catch (e) { /* ignore */ }
  }
  function recall(key) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? null : value;
    } catch (e) { return null; }
  }

  window.PD_VOTER = { id: () => id, remember, recall };
})();
