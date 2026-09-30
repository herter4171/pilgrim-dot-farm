/* Pilgrim Dot Farm — Web Audio client (RADIO.md §9).
   AudioContext created inside PLAY (autoplay). Decodes current+next2, schedules
   sample-accurate gapless joins, proves audio flows via an AnalyserNode meter.
   Production health never interrupts already buffered audio. The station stays on
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
  let hbTimer = null;
  let session = 0;

  /* Cross-tab single-listener guard: if this page is open in more than one
     tab/browser, we want audio ONLY when the user presses PLAY, and only from
     the tab they most recently pressed it in. BroadcastChannel tells other
     open tabs to stop so we never get doubled foreground+background audio. */
  const bc = "BroadcastChannel" in window ? new BroadcastChannel("pilgrim-radio") : null;
  const myId = (Math.random() * 1e9) | 0;
  let myStartAt = 0;
  if (bc) {
    bc.onmessage = (ev) => {
      const d = ev.data || {};
      if (d.kind === "playing" && d.id !== myId && playing && d.at > myStartAt) {
        setStatus("stopped:" + (d.reason || "started in another tab"));
        stop(true);
      }
    };
  }

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
        if (playing) return h;
        btn.disabled = true; offairEl.classList.remove("hidden"); setState("idle");
        setStatus(""); return h;
      }
      offairEl.classList.add("hidden"); btn.disabled = false;
      // rotation = what can air now; inventory.song.have = unaired new songs
      const rot = h.rotation || {}, inv = h.inventory || {};
      setStatus(`songs ${rot.song||0} (${inv.song?.have||0} new) · ` +
                `spots ${rot.commercial||0} · liners ${rot.liner||0} · ` +
                `dj ${rot.dj_talk||0} · news ${rot.news||0}`);
      return h;
    } catch (e) {
      if (playing) return null;
      btn.disabled = true; offairEl.classList.remove("hidden"); setState("idle");
      return null;
    }
  }
  function startHealth() {
    setInterval(pollHealth, 8000);
    pollHealth();
  }

  /* ---------------- program + decode ---------------- */
  async function fetchProgram(afterSeq) {
    const url = afterSeq == null ? "/api/station/program" : "/api/station/program?after_seq=" + afterSeq;
    const r = await fetch(url);
    if (!r.ok) throw new Error("program HTTP " + r.status);
    return r.json();
  }
  async function decode(mediaId, audioContext) {
    const r = await fetch("/api/media/" + mediaId);
    if (!r.ok) throw new Error("media HTTP " + r.status);
    return await audioContext.decodeAudioData(await r.arrayBuffer());
  }

  async function getDecoded(i) {
    const it = items[i];
    if (!it) return null;
    if (!it.buffer) it.buffer = await decode(it.media_id, ctx);
    return it.buffer;
  }

  /* ---------------- scheduling ---------------- */
  function scheduleOne(i, offset, token) {
    const it = items[i];
    const src = ctx.createBufferSource();
    src.buffer = it.buffer;
    const g = ctx.createGain();
    src.connect(g); g.connect(analyser); analyser.connect(ctx.destination);
    src.start(nextWhen, offset);
    it._when = nextWhen;
    it._offset = offset;
    const end = nextWhen + (it.buffer.duration - offset);
    it._end = end;
    nextWhen = end;
    it._scheduled = true;
    src.addEventListener("ended", () => {
      src.disconnect(); g.disconnect(); src.buffer = null;
      it.buffer = null;
      if (playing && token === session) advancePlayhead();
    });
    return src;
  }

  function advancePlayhead() {
    while (cursor < scheduleCursor && items[cursor]._end <= ctx.currentTime) {
      items[cursor].buffer = null;
      cursor++;
    }
    const it = items[cursor];
    if (it && it._scheduled && it._when <= ctx.currentTime && !it._started) {
      it._started = true;
      sendHeartbeat(it.seq, it._offset + ctx.currentTime - it._when);
      setState("onair");
    }
  }

  /* The audio clock advances the decode window as clips finish. Keep current
     plus next two decoded and schedule each as soon as it is ready (§9.2). */
  async function fillWindow(startOffset, token) {
    while (playing && token === session) {
      try {
        advancePlayhead();
        if (items.length < cursor + 3) {
          const lastSeq = items.length ? items[items.length - 1].seq : null;
          const p = await fetchProgram(lastSeq);
          if (!playing || token !== session) return;
          for (const it of p.items || []) {
            if (!items.some(x => x.seq === it.seq)) items.push(it);
          }
        }
        while (scheduleCursor < Math.min(items.length, cursor + 3)) {
          await getDecoded(scheduleCursor);
          if (!playing || token !== session) return;
          // A slow fetch must not schedule a source in the past. Record the
          // underrun honestly, then resume as soon as rendered audio is ready.
          if (nextWhen < ctx.currentTime) {
            if (scheduleCursor > 0) sendHeartbeat(items[scheduleCursor].seq, 0, true);
            nextWhen = ctx.currentTime + 0.05;
          }
          const offset = startOffset || 0;
          if (offset < items[scheduleCursor].buffer.duration) {
            scheduleOne(scheduleCursor, offset, token);
          } else {
            // A stale join offset can land just beyond a decoded file's end.
            items[scheduleCursor]._end = ctx.currentTime;
            items[scheduleCursor].buffer = null;
          }
          startOffset = null;
          scheduleCursor++;
          advancePlayhead();
        }
      } catch (e) {
        if (!playing || token !== session) return;
        setStatus("waiting for audio: " + e.message);
      }
      if (cursor >= scheduleCursor) setState("buffering");
      await new Promise(r => setTimeout(r, 100));
    }
  }

  /* ---------------- heartbeat ---------------- */
  function sendHeartbeat(seq, position, underrun = false) {
    const it = items.find(i => i.seq === seq) || {};
    fetch("/api/station/heartbeat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seq, media_id: it.media_id, position, underrun,
                             started_at: ctx ? ctx.currentTime : 0, type: it.type })
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
    playing = true;
    myStartAt = Date.now();
    const token = ++session;
    setState("buffering"); btn.textContent = "■  STOP"; btn.classList.add("stop");

    try {
      // Create and resume synchronously from the click, before any network await.
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      const resumed = ctx.resume();
      analyser = ctx.createAnalyser(); analyser.fftSize = 256;
      await resumed;
      if (!playing || token !== session) return;
      const p = await fetchProgram(null);
      if (!playing || token !== session) return;
      items = p.items.map(it => ({ ...it, buffer: null }));
      cursor = 0; scheduleCursor = 0;
      nextWhen = ctx.currentTime + 0.2;
      drawLevel();
      if (bc) bc.postMessage({ kind: "playing", id: myId, at: myStartAt });
      hbTimer = setInterval(() => {
        advancePlayhead();
        const it = items[cursor];
        if (it?._started) sendHeartbeat(it.seq, it._offset + ctx.currentTime - it._when);
      }, 5000);
      void fillWindow(p.start_offset_s || 0, token);
    } catch (e) {
      if (token !== session) return;
      setStatus("playback error: " + e.message); stop();
    }
  }

  function stop(remote = false) {
    playing = false;
    session++;
    if (!remote && bc) bc.postMessage({ kind: "stopped", id: myId, at: Date.now() });
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
