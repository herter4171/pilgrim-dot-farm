/* Pilgrim Dot Farm — Web Audio client (RADIO.md §9).
   AudioContext created inside PLAY (autoplay). Decodes current+next2, schedules
   sample-accurate gapless joins, proves audio flows via an AnalyserNode meter.
   Shows OFF AIR when production services are unreachable. The station stays on
   air server-side; the client just joins the committed program at "now".
*/
(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const btn = $("btn-play"), ind = $("indicator"), indLabel = $("ind-label");
  const level = $("level"); const lctx = level.getContext("2d");
  const offairEl = $("offair"), statusEl = $("status");

  let ctx = null, analyser = null, raf = null;
  let items = [];            // {seq, media_id, type, duration_s, buffer}
  let cursor = 0;            // index of the item currently on air
  let scheduleCursor = 0;    // index of the next item to schedule (in order)
  let nextWhen = 0;
  let playing = false;
  let loopTimer = null, hbTimer = null, healthTimer = null;

  /* ---------------- UI ---------------- */
  function setState(s) { ind.className = "indicator " + s; indLabel.textContent = s; }
  function setStatus(t) { statusEl.textContent = t || ""; }

  /* ---------------- rain canvas (old_style flair) ---------------- */
  const cvs = $("rain"), rctx = cvs.getContext("2d");
  function sizeCvs() { cvs.width = innerWidth; cvs.height = innerHeight; }
  sizeCvs(); addEventListener("resize", sizeCvs);
  const cols = []; for (let i = 0; i < 60; i++) cols.push(Math.random() * -80);
  setInterval(() => {
    rctx.fillStyle = "rgba(10,1,24,0.12)"; rctx.fillRect(0, 0, cvs.width, cvs.height);
    rctx.font = "14px monospace";
    for (let i = 0; i < 60; i++) {
      const ch = "♪♫♩♬♭♮0123456789".charAt(Math.floor(Math.random() * 15));
      rctx.fillStyle = Math.random() > .5 ? "#ff2fd6" : "#24e0ff";
      rctx.fillText(ch, i * (cvs.width / 60), cols[i] * 18);
      if (cols[i] * 18 > cvs.height && Math.random() > .975) cols[i] = 0;
      cols[i]++;
    }
  }, 90);

  /* ---------------- health / OFF AIR ---------------- */
  async function pollHealth() {
    try {
      const r = await fetch("/api/health");
      const h = await r.json();
      if (!h.on_air) {
        if (playing) stop();
        btn.disabled = true; offairEl.classList.remove("hidden"); setState("idle");
        setStatus(""); return h;
      }
      offairEl.classList.add("hidden"); btn.disabled = false;
      const inv = h.inventory || {};
      setStatus(`songs ${inv.song?.have||0} · liners ${inv.liner?.have||0} · ` +
                `dj ${inv.dj_talk?.have||0} · cover ${Math.round(h.committed_coverage_s)}s`);
      return h;
    } catch (e) {
      if (playing) stop();
      btn.disabled = true; offairEl.classList.remove("hidden"); setState("idle");
      return null;
    }
  }
  function startHealth() {
    healthTimer = setInterval(pollHealth, 8000);
    pollHealth();
  }

  /* ---------------- program + decode ---------------- */
  async function fetchProgram(afterSeq) {
    const url = afterSeq == null ? "/api/station/program" : "/api/station/program?after_seq=" + afterSeq;
    const r = await fetch(url);
    if (!r.ok) throw new Error("program HTTP " + r.status);
    return r.json();
  }
  async function decode(mediaId) {
    const r = await fetch("/api/media/" + mediaId);
    if (!r.ok) throw new Error("media HTTP " + r.status);
    return await ctx.decodeAudioData(await r.arrayBuffer());
  }

  async function getDecoded(i) {
    const it = items[i];
    if (!it) return null;
    if (!it.buffer) it.buffer = await decode(it.media_id).catch(() => null);
    return it.buffer;
  }

  /* ---------------- scheduling ---------------- */
  function scheduleOne(i, offset) {
    const it = items[i];
    const src = ctx.createBufferSource();
    src.buffer = it.buffer;
    const g = ctx.createGain();
    src.connect(g); g.connect(analyser); analyser.connect(ctx.destination);
    src.start(nextWhen, offset);
    it._when = nextWhen;
    const end = nextWhen + (it.buffer.duration - offset);
    nextWhen = end;
    it._scheduled = true;
    src.addEventListener("ended", () => sendHeartbeat(it.seq, offset));
    return src;
  }

  /* Keep scheduling forward: decode cursor..cursor+2, extend program near tail,
     and schedule newly-decoded items in strict order. */
  async function fillWindow(startOffset) {
    while (playing) {
      // honour the client-decoded window (current + next 2)
      const target = cursor + 3;
      while (items.length < target) {
        const lastSeq = items.length ? items[items.length - 1].seq : null;
        const p = await fetchProgram(lastSeq);
        if (!p.items || !p.items.length) break;
        for (const it of p.items) if (!items.some(x => x.seq === it.seq)) items.push(it);
        if (!p.items.length) break;
      }
      // ensure the needed items are decoded
      for (let i = scheduleCursor; i < Math.min(items.length, cursor + 3); i++) {
        await getDecoded(i);
      }
      // schedule in strict index order up to the decoded frontier
      while (scheduleCursor < items.length &&
             (scheduleCursor === 0 || items[scheduleCursor - 1]._scheduled)) {
        const it = items[scheduleCursor];
        if (!it.buffer) break;
        if (scheduleCursor === cursor && startOffset != null) {
          scheduleOne(scheduleCursor, startOffset); startOffset = null;
        } else {
          scheduleOne(scheduleCursor, 0);
        }
        scheduleCursor++;
      }
      await new Promise(r => setTimeout(r, 800));
    }
  }

  /* ---------------- heartbeat ---------------- */
  function sendHeartbeat(seq, position) {
    fetch("/api/station/heartbeat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seq, position, started_at: ctx ? ctx.currentTime : 0,
                             type: (items.find(i => i.seq === seq) || {}).type })
    }).catch(() => {});
  }

  /* ---------------- live level meter ---------------- */
  function drawLevel() {
    raf = requestAnimationFrame(drawLevel);
    lctx.clearRect(0, 0, level.width, level.height);
    const data = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (let i = 0; i < data.length; i++) {
      const v = Math.abs((data[i] - 128) / 128);
      if (v > peak) peak = v;
    }
    const w = Math.max(4, peak * level.width);
    lctx.fillStyle = "#24e0ff"; lctx.fillRect(0, 0, w, level.height);
    lctx.fillStyle = "#ff2fd6"; lctx.fillRect(w, 0, 2, level.height);
  }

  /* ---------------- play / stop ---------------- */
  async function play() {
    if (playing) return;
    const h = await pollHealth();
    if (!h || !h.on_air) return;

    setState("buffering"); btn.textContent = "■  STOP"; btn.classList.add("stop");
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = ctx.createAnalyser(); analyser.fftSize = 256;

    try {
      const p = await fetchProgram(null);
      items = p.items.map(it => ({ ...it, buffer: null }));
      cursor = 0; scheduleCursor = 0; playing = true;
      // decode the very first item so we know something is ready
      await getDecoded(0);
      if (!items.length || items[0].buffer == null) {
        setStatus("no committed audio yet — waiting…"); stop(); return;
      }
      nextWhen = ctx.currentTime + 0.2;
      setState("onair");
      drawLevel();
      hbTimer = setInterval(() => {
        const it = items[cursor]; if (it) sendHeartbeat(it.seq, 0);
      }, 5000);
      fillWindow(p.start_offset_s || 0);
    } catch (e) {
      setStatus("playback error: " + e.message); stop();
    }
  }

  function stop() {
    playing = false;
    if (loopTimer) clearInterval(loopTimer); loopTimer = null;
    if (hbTimer) clearInterval(hbTimer); hbTimer = null;
    if (raf) cancelAnimationFrame(raf); raf = null;
    if (ctx) { try { ctx.close(); } catch (e) {} ctx = null; }
    analyser = null;
    items = []; cursor = 0; scheduleCursor = 0;
    btn.textContent = "▶  PLAY"; btn.classList.remove("stop");
    setState("idle"); lctx.clearRect(0, 0, level.width, level.height);
  }

  btn.addEventListener("click", () => (playing ? stop() : play()));
  startHealth();
})();
