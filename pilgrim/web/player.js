/* Pilgrim Dot Farm — Web Audio client (RADIO.md §9).
   AudioContext created inside PLAY (autoplay). Decodes current+next2 (more
   while that is under a minute of audio), schedules
   sample-accurate gapless joins, proves audio flows via an AnalyserNode meter.
   Production health never interrupts already buffered audio. The station stays on
   air server-side; the client just joins the committed program at "now".

   Phones (RADIO §9.2 "Mobile continuity"): the graph's output goes through ONE
   persistent <audio> element (#sink, a MediaStreamDestination) so iOS/Android
   treat the page as media playback: it keeps running in the background and
   when the phone is locked, obeys the media volume (not the ringer switch),
   and gets lock-screen controls via the Media Session API. An interrupted or
   suspended context (phone call, Siri, another app) is resumed when the
   interruption ends and the page rejoins the live programme.
*/
(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const btn = $("btn-play"), ind = $("indicator"), indLabel = $("ind-label");
  const level = $("level"); const lctx = level.getContext("2d");
  const offairEl = $("offair"), statusEl = $("status");
  const songTitle = $("song-title");
  const songArtist = $("song-artist");
  const coverArt = $("cover-art");
  const dlBtn = $("dl-mp3");
  // Cover art (ALBUM_ART.md Step 8): the placeholder always fills the frame,
  // so the PLAY row never moves. Art is cosmetic: a load error falls back
  // silently and never touches playback, the indicator or OFF AIR logic.
  const ART_PLACEHOLDER = "img/fall.jpg";
  let shownArt = ART_PLACEHOLDER;

  let ctx = null, analyser = null, raf = null;
  // Output sink (RADIO §9.2): the graph feeds #sink through a
  // MediaStreamDestination ("element"); if the element cannot play, the
  // graph connects straight to ctx.destination ("direct", desktop fallback).
  const sinkEl = $("sink");
  let sinkDest = null, sinkMode = "direct";
  // Interruption recovery: Date.now() when the context stopped running
  // (null while running) and how many times it recovered this session.
  let interruptedAt = null, resumedCount = 0, resumeTimer = null;
  // After an interruption at least this long the buffered future is behind
  // the station: rejoin live instead of playing stale audio late.
  const REJOIN_AFTER_S = 5;
  const PLATFORM = (() => {
    const ua = navigator.userAgent || "";
    if (/iPhone|iPad|iPod/.test(ua)
        || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "ios";
    if (/Android/.test(ua)) return "android";
    return "other";
  })();
  let items = [];            // {seq, media_id, type, duration_s, sfx[], buffer}
  let cursor = 0;            // index of the item currently on air
  let scheduleCursor = 0;    // index of the next item to schedule (in order)
  let nextWhen = 0;
  let playing = false;
  let hbTimer = null;
  let session = 0;
  // Programme epoch/revision (TUI.md §5, §9.2). A revision change means
  // already-published future playback moved (e.g. a listener request was
  // spliced into the next song slot): cancel obsolete future sources and
  // refetch the snapshot instead of playing the stale rows.
  let epoch = null, revision = null;
  let programToken = 0;      // invalidates in-flight fillWindow work on reset
  let stateTimer = null;
  // Off air (SPLIT_PLAN §4). The edge/gateway answer 503 {off_air:true} when
  // the origin is unreachable: definitive, stop once buffered audio runs out.
  // Any other failure (network blip, other status, decode error) only ends
  // the session after this long with the buffer dry; any success resets it.
  const OFFAIR_GRACE_MS = 10000;
  let dryFailSince = null;   // performance.now() of the first dry failure
  // A media fetch with no new bytes for this long is abandoned and retried
  // from the origin (decode()).
  const MEDIA_STALL_MS = 8000;
  // Decode window (RADIO §9.2): current + next 2 always; while that holds
  // less than LOOKAHEAD_S of scheduled audio (runs of short talk/liners),
  // keep decoding up to current + MAX_AHEAD so one slow fetch is not a gap.
  const MIN_AHEAD = 3, MAX_AHEAD = 6, LOOKAHEAD_S = 60;
  // Dead-air watchdog (RADIO §9.2). Nothing scheduled at the playhead for this
  // long after the last scheduled audio ended means the pending fetch/decode
  // is lost: production saw a spliced request's song never come back from
  // decode, and the page sat silent with "onair" lit until a reload. Rejoin
  // live through the revision-reset path, record it as an underrun, and back
  // off between attempts so a slow network is not refetched into a thrash.
  const DEAD_AIR_S = 3;
  const REJOIN_GAP_MIN_MS = 5000, REJOIN_GAP_MAX_MS = 30000;
  let rejoinAt = 0, rejoinGap = REJOIN_GAP_MIN_MS;
  let mediaProgressAt = 0;   // performance.now() of the last media byte received
  // A revision reset whose snapshot fetch failed is retried on the next state
  // poll; a program page that came back `reset: true` asks for the same.
  let resetPending = false;
  class OffAirError extends Error {}

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
  function showOffAir() {
    btn.disabled = true; offairEl.classList.remove("hidden"); setState("idle");
  }
  async function pollHealth() {
    try {
      const r = await fetch("/api/health");
      const h = await r.json();
      if (!h.on_air) {
        // While playing, health alone must not interrupt committed audio: the
        // program/media fetches decide (gap test). Buffered audio finishes and
        // fillWindow shows OFF AIR only once a fetch fails with the buffer dry.
        if (playing) return h;
        showOffAir(); setStatus(""); return h;
      }
      offairEl.classList.add("hidden"); btn.disabled = false;
      return h;
    } catch (e) {
      if (playing) return null;
      showOffAir(); return null;
    }
  }
  function startHealth() {
    setInterval(pollHealth, 8000);
    pollHealth();
  }

  /* ---------------- program + decode ---------------- */
  async function fetchProgram(afterSeq, e, r) {
    const params = new URLSearchParams();
    if (afterSeq != null) params.set("after_seq", afterSeq);
    if (e != null) params.set("epoch", e);
    if (r != null) params.set("revision", r);
    const q = params.toString();
    const res = await fetch("/api/station/program" + (q ? "?" + q : ""));
    if (!res.ok) await throwHttp("program", res);
    return res.json();
  }
  /* A 503 whose JSON says off_air is the edge/gateway reporting the origin
     unreachable (SPLIT_PLAN §4); anything else is an ordinary failure. */
  async function throwHttp(what, res) {
    if (res.status === 503) {
      let body = null;
      try { body = await res.json(); } catch (e) { /* not JSON */ }
      if (body && body.off_air === true) throw new OffAirError(what + ": off air");
    }
    throw new Error(what + " HTTP " + res.status);
  }
  /* Fetch a whole body, aborting when no headers or no bytes arrive for
     MEDIA_STALL_MS. A browser can sit on a dead pooled connection for a
     minute or forever (seen in Firefox against CloudFront); without this the
     decode window waits silently instead of retrying (RADIO §9.2). */
  async function fetchBytes(url, what) {
    const ac = new AbortController();
    let timer = null;
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(() => ac.abort(), MEDIA_STALL_MS);
    };
    arm();
    mediaProgressAt = performance.now();
    try {
      const r = await fetch(url, { signal: ac.signal });
      if (!r.ok) await throwHttp(what, r);
      mediaProgressAt = performance.now();
      if (!r.body || !r.body.getReader) return await r.arrayBuffer();
      const reader = r.body.getReader();
      const parts = [];
      let n = 0;
      for (;;) {
        arm();
        const { done, value } = await reader.read();
        if (done) break;
        parts.push(value); n += value.byteLength;
        mediaProgressAt = performance.now();
      }
      const out = new Uint8Array(n);
      let o = 0;
      for (const v of parts) { out.set(v, o); o += v.byteLength; }
      return out.buffer;
    } catch (e) {
      if (ac.signal.aborted) throw new Error(what + " stalled");
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }
  /* Audio comes from CloudFront (`url`, RADIO §8.4). If that fetch fails or
     stalls, retry once from the origin's /api/media/<id> (a different host,
     so a different connection); that route is also the only source for
     items not yet in S3. */
  async function fetchMedia(entry) {
    const origin = "/api/media/" + entry.media_id;
    if (entry.url && entry.url !== origin) {
      try {
        return await fetchBytes(entry.url, "media");
      } catch (e) {
        if (e instanceof OffAirError) throw e;
        return await fetchBytes(origin, "media (origin)");
      }
    }
    return await fetchBytes(origin, "media");
  }
  async function decode(entry, audioContext) {
    return await audioContext.decodeAudioData(await fetchMedia(entry));
  }

  async function getDecoded(i) {
    const it = items[i];
    if (!it) return null;
    if (!it.buffer) it.buffer = await decode(it, ctx);
    preloadArt(it);
    // SFX overlays decode in the background and are never awaited: a slow or
    // failed stinger is skipped, it never delays its host (SFX.md §7.4).
    for (const ov of it.sfx || []) {
      if (ov._loading) continue;
      ov._loading = true;
      const c = ctx;
      decode(ov, c).then(b => {
        if (c !== ctx || (it._scheduled && !it._gain)) return;  // host already done
        ov.buffer = b;
        if (it._scheduled) scheduleOverlay(it, ov);
      }).catch(() => {});
    }
    return it.buffer;
  }

  /* ---------------- output sink + interruptions (RADIO §9.2) ---------------- */
  /* Connect the analyser (every source ends there) to the page's output.
     Called synchronously inside the PLAY gesture so sinkEl.play() is allowed.
     The element path is what keeps phones playing in the background; if it
     cannot play (old browser, policy), fall back to the direct connection. */
  function attachOutput() {
    sinkMode = "direct";
    if (sinkEl && ctx.createMediaStreamDestination && "srcObject" in sinkEl) {
      const c = ctx;
      try {
        sinkDest = ctx.createMediaStreamDestination();
        analyser.connect(sinkDest);
        sinkEl.srcObject = sinkDest.stream;
        sinkMode = "element";
        const p = sinkEl.play();
        if (p && p.catch) p.catch(() => { if (c === ctx) fallbackDirect(); });
        return;
      } catch (e) { fallbackDirect(); return; }
    }
    analyser.connect(ctx.destination);
  }
  function fallbackDirect() {
    if (!ctx || !analyser || sinkMode !== "element") return;
    try { if (sinkDest) analyser.disconnect(sinkDest); } catch (e) {}
    try { sinkEl.srcObject = null; } catch (e) {}
    sinkDest = null; sinkMode = "direct";
    try { analyser.connect(ctx.destination); } catch (e) {}
  }
  function detachOutput() {
    try { if (sinkEl) { sinkEl.pause(); sinkEl.srcObject = null; } } catch (e) {}
    sinkDest = null; sinkMode = "direct";
  }

  /* The context stopped running (iOS "interrupted", or "suspended": a call,
     Siri, another app taking audio focus, the OS pausing the page). Ask for
     it back now and keep asking while the page is visible; the audio clock
     pauses with the context, so buffered sources resume where they left off
     and a long interruption rejoins the live programme (resetProgram). */
  function onCtxState() {
    if (!ctx || !playing) return;
    const s = ctx.state;
    if (s === "running") {
      if (interruptedAt !== null) {
        const gap = (Date.now() - interruptedAt) / 1000;
        interruptedAt = null; resumedCount++;
        stopResumeTimer();
        setStatus("");
        if (gap >= REJOIN_AFTER_S) void resetProgram();
      }
      return;
    }
    if (s === "closed") return;
    if (interruptedAt === null) {
      interruptedAt = Date.now();
      setState("buffering");
      setStatus("audio " + s + " — waiting to resume");
    }
    tryResume();
    if (!resumeTimer) resumeTimer = setInterval(tryResume, 2000);
  }
  function stopResumeTimer() {
    if (resumeTimer) clearInterval(resumeTimer);
    resumeTimer = null;
  }
  function tryResume() {
    if (!ctx || !playing) { stopResumeTimer(); return; }
    if (ctx.state === "running") { onCtxState(); return; }
    if (ctx.state === "closed") { stopResumeTimer(); return; }
    try { const p = ctx.resume(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
    try { if (sinkMode === "element" && sinkEl.paused) { const p = sinkEl.play(); if (p && p.catch) p.catch(() => {}); } } catch (e) {}
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tryResume(); });
  addEventListener("pageshow", tryResume);
  addEventListener("focus", tryResume);

  /* Lock-screen / notification controls and "now playing" (Media Session).
     Pause from the lock screen is STOP (radio has no pause); play restarts. */
  function initMediaSession() {
    if (!("mediaSession" in navigator)) return;
    const ms = navigator.mediaSession;
    const bind = (action, fn) => { try { ms.setActionHandler(action, fn); } catch (e) {} };
    bind("play", () => { if (!playing) void play(); });
    bind("pause", () => { if (playing) stop(); });
    bind("stop", () => { if (playing) stop(); });
    for (const a of ["seekbackward", "seekforward", "seekto", "previoustrack", "nexttrack"]) bind(a, null);
  }
  let msKey = null;
  function setMediaSession(it) {
    if (!("mediaSession" in navigator)) return;
    const named = !!(it && it.type === "song" && it.title && it.artist);
    const key = named ? (it.seq + "|" + (it.art_url || "")) : "station";
    if (key === msKey) return;
    msKey = key;
    try {
      const art = named && it.art_url ? it.art_url : new URL(ART_PLACEHOLDER, location.href).href;
      navigator.mediaSession.metadata = new MediaMetadata({
        title: named ? it.title : "PILGRIM DOT FARM",
        artist: named ? it.artist : "live radio",
        album: "PILGRIM DOT FARM radio",
        artwork: [{ src: art, type: "image/jpeg" }],
      });
    } catch (e) {}
  }
  function setPlaybackState(s) {
    try { if ("mediaSession" in navigator) navigator.mediaSession.playbackState = s; } catch (e) {}
    if (s !== "playing") { msKey = null; try { navigator.mediaSession.metadata = null; } catch (e) {} }
  }

  /* ---------------- scheduling ---------------- */
  function scheduleOne(i, offset, token) {
    const it = items[i];
    const src = ctx.createBufferSource();
    src.buffer = it.buffer;
    const g = ctx.createGain();
    src.connect(g); g.connect(analyser);   // analyser -> output sink (attachOutput)
    src.start(nextWhen, offset);
    it._when = nextWhen;
    it._offset = offset;
    it._src = src;           // held so a revision reset can cancel it
    const end = nextWhen + (it.buffer.duration - offset);
    it._end = end;
    nextWhen = end;
    it._scheduled = true;
    it._gain = g;
    scheduleOverlays(it);
    src.addEventListener("ended", () => {
      const wasScheduled = it._scheduled;
      src.disconnect(); g.disconnect(); src.buffer = null;
      it.buffer = null; it._gain = null; it._src = null;
      for (const ov of it.sfx || []) if (!ov._scheduled) ov.buffer = null;
      // a source cancelled by a revision reset must not advance the playhead
      if (wasScheduled && playing && token === session) { advancePlayhead(); checkDeadAir(); }
    });
    return src;
  }

  /* Stop every scheduled source that has not started yet (revision reset).
     The audible item keeps playing; its remaining future is replaced by the
     new snapshot. */
  function cancelFuture() {
    const now = ctx ? ctx.currentTime : 0;
    const stopSource = (holder) => {
      holder._scheduled = false;
      try { holder._src.stop(); } catch (e) {}
      try { holder._src.disconnect(); } catch (e) {}
      try { if (holder._gain) holder._gain.disconnect(); } catch (e) {}
      holder._src = null; holder._gain = null; holder.buffer = null;
    };
    for (const it of items) {
      if (it._scheduled && it._src && it._when > now + 0.02) stopSource(it);
      for (const ov of it.sfx || []) {
        if (ov._scheduled && ov._src && ov._when > now + 0.02) stopSource(ov);
      }
    }
  }

  /* Cutover case: the current occurrence changed or vanished. Stop everything
     and restart from the snapshot's on-air item (a short fade/gap is expected
     for an edit; normal joins stay sample-contiguous). */
  function stopAllSources() {
    const stopSource = (holder) => {
      holder._scheduled = false;
      try { holder._src.stop(); } catch (e) {}
      try { holder._src.disconnect(); } catch (e) {}
      try { if (holder._gain) holder._gain.disconnect(); } catch (e) {}
      holder._src = null; holder._gain = null;
    };
    for (const it of items) {
      if (it._src) stopSource(it);
      for (const ov of it.sfx || []) if (ov._src) stopSource(ov);
    }
  }

  /* SFX sidecars ride on their host: started at host start + offset_s on the
     same AudioContext clock, gain-staged, stopped at the host's end so they
     can never push a join. The host dips to `duck` under a stinger so two
     full-level sources don't clip. Joining mid-host: a stinger whose beat has
     passed is skipped; a bed picks up where it would be. An overlay decoded
     too late to start on time is dropped. */
  function scheduleOverlays(it) {
    for (const ov of it.sfx || []) if (ov.buffer) scheduleOverlay(it, ov);
  }

  function scheduleOverlay(it, ov) {
    if (ov._scheduled || !ov.buffer || !it._gain) return;
    let when = it._when + (ov.offset_s - it._offset), skip = 0;
    if (ov.offset_s < it._offset) {
      if (ov.kind !== "bed") return;
      when = it._when; skip = it._offset - ov.offset_s;
    }
    if (when < ctx.currentTime + 0.02 || skip >= ov.buffer.duration || when >= it._end) return;
    ov._scheduled = true;
    const s = ctx.createBufferSource(); s.buffer = ov.buffer; s._sfx = true;
    const g = ctx.createGain(); g.gain.value = ov.gain ?? 0.5;
    s.connect(g); g.connect(analyser);
    ov._src = s; ov._gain = g; ov._when = when;
    s.start(when, skip);
    s.stop(it._end);
    s.addEventListener("ended", () => {
      s.disconnect(); g.disconnect(); s.buffer = null; ov.buffer = null;
      ov._src = null; ov._gain = null;
    });
    const level = ov.duck ?? 1;
    if (level >= 1) return;
    const end = Math.min(when + ov.buffer.duration - skip, it._end);
    // overlapping stingers share the first one's dip rather than fight over it
    it._ducks = it._ducks || [];
    if (it._ducks.some(([a, b]) => when < b && end > a)) return;
    it._ducks.push([when, end]);
    const r = 0.04, hg = it._gain.gain;
    hg.setValueAtTime(1, Math.max(when - r, it._when));
    hg.linearRampToValueAtTime(level, when);
    hg.setValueAtTime(level, end);
    hg.linearRampToValueAtTime(1, Math.min(end + r, it._end));
  }

  /* Song label under the meter — the audible item only, driven from the
     audio-clock playhead (RADIO §9.1 / COSMETIC_PATCHING §3). Shows the title
     on one line and the artist on the next for a song with both fields;
     otherwise both lines are empty. Fetching, decoding, or scheduling the
     NEXT song must never announce it early. */
  function clearSongLabel() { songTitle.textContent = ""; songArtist.textContent = ""; }
  function setSongLabel(it) {
    const named = !!(it && it.type === "song" && it.title && it.artist);
    if (named) { songTitle.textContent = it.title; songArtist.textContent = it.artist; }
    else clearSongLabel();
    setCoverArt(named ? it : null, named);
    setDownload(named ? it : null);
    if (playing) setMediaSession(named ? it : null);
    // VOTING.md §4/§5: one hook for the rating row. The detail is the song that
    // is AUDIBLE now (audio clock), never the next one; null when nothing is.
    // rating.js ignores repeats of the same airing.
    document.dispatchEvent(new CustomEvent("pd:nowplaying", {
      detail: named ? { epoch, seq: it.seq, media_id: it.media_id,
                        title: it.title, artist: it.artist,
                        // FIX_RATINGS.md Step 3: the lifetime aggregate rides
                        // on the programme row (RADIO §11), so the rating row
                        // can show it the instant the song is audible.
                        rating_count: it.rating_count || 0,
                        rating_avg: (typeof it.rating_avg === "number") ? it.rating_avg : null }
                    : null,
    }));
  }

  /* DOWNLOAD MP3 (RADIO §9.1): shown only while a named song is audible, so
     you have to listen to get it. The click fetches the same MP3 the player
     streams (CloudFront, origin fallback, stall timeout: fetchMedia) into a
     same-origin blob, because the download attribute is ignored on a
     cross-origin link. A download never touches playback. */
  let dlItem = null, dlBusy = false;
  function setDownload(it) {
    dlItem = it ? { media_id: it.media_id, url: it.url,
                    title: it.title, artist: it.artist } : null;
    if (!dlBusy) dlBtn.hidden = !dlItem;
  }
  function mp3Name(d) {
    const raw = (d.title + " - " + d.artist).replace(/[\u0000-\u001f\u007f\/\\:*?"<>|]+/g, " ");
    const name = raw.replace(/\s+/g, " ").trim().slice(0, 120);
    return (name || "pilgrim-dot-farm") + ".mp3";
  }
  dlBtn.addEventListener("click", async () => {
    if (!dlItem || dlBusy) return;
    const d = dlItem;
    dlBusy = true; dlBtn.disabled = true; dlBtn.textContent = "DOWNLOADING…";
    let label = "DOWNLOAD MP3";
    try {
      const bytes = await fetchMedia(d);
      const href = URL.createObjectURL(new Blob([bytes], { type: "audio/mpeg" }));
      const a = document.createElement("a");
      a.href = href; a.download = mp3Name(d); a.style.display = "none";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 60_000);
    } catch (e) {
      label = "DOWNLOAD FAILED";
    }
    dlBtn.textContent = label;
    dlBtn.disabled = false; dlBusy = false;
    dlBtn.hidden = !dlItem;
    if (label !== "DOWNLOAD MP3") {
      setTimeout(() => { if (!dlBusy) dlBtn.textContent = "DOWNLOAD MP3"; }, 3000);
    }
  });

  /* The cover swaps with the label, from the same playhead decision: an
     audible song with an uploaded art_url shows its cover, anything else
     shows the placeholder. The next song's art is never shown early. */
  function setCoverArt(it, named) {
    const url = (named && it && it.art_url) ? it.art_url : ART_PLACEHOLDER;
    if (url === shownArt) return;
    shownArt = url;
    coverArt.src = url;
    coverArt.alt = named ? (it.title + " - " + it.artist) : "";
  }

  coverArt.addEventListener("error", () => {
    // A dead cover URL is cosmetic: fall back once, silently.
    if (shownArt !== ART_PLACEHOLDER) {
      shownArt = ART_PLACEHOLDER;
      coverArt.src = ART_PLACEHOLDER;
      coverArt.alt = "";
    }
  });

  /* Warm the next covers while the next songs decode (Step 8 preload). */
  function preloadArt(it) {
    if (it && it.art_url && it.type === "song") {
      const img = new Image();
      img.src = it.art_url;
    }
  }

  function advancePlayhead() {
    while (cursor < scheduleCursor && items[cursor]._end <= ctx.currentTime) {
      items[cursor].buffer = null;
      cursor++;
    }
    const it = items[cursor];
    if (it && it._scheduled && it._when <= ctx.currentTime && !it._started) {
      it._started = true;
      rejoinGap = REJOIN_GAP_MIN_MS;   // audio flowed again: watchdog backoff resets
      sendHeartbeat(it.seq, it._offset + ctx.currentTime - it._when);
      setState("onair");
    } else if (playing && !(it && it._scheduled)) {
      // nothing scheduled at the playhead (a fetch/decode still pending): say
      // so instead of leaving "onair" lit over silence
      setState("buffering");
    }
    // label mirrors the on-air item; a gap / buffering / non-song shows ""
    setSongLabel(it && it._started ? it : null);
  }

  /* Dead-air watchdog. Runs from the heartbeat tick, the state poll and each
     source's end, i.e. independently of fillWindow, which is the thing that
     may be stuck in an await that never settles. */
  function checkDeadAir() {
    if (!playing || !ctx) return;
    advancePlayhead();
    const it = items[cursor];
    if (it && it._scheduled && it._end > ctx.currentTime) return;  // audio is scheduled at the playhead
    if (ctx.currentTime - nextWhen < DEAD_AIR_S) return;           // a join may still be in flight
    const now = performance.now();
    if (now - mediaProgressAt < DEAD_AIR_S * 1000) return;        // bytes are still arriving: let it finish
    if (now - rejoinAt < rejoinGap) return;
    rejoinAt = now;
    rejoinGap = Math.min(rejoinGap * 2, REJOIN_GAP_MAX_MS);
    const pending = items[scheduleCursor] || it;
    if (pending) sendHeartbeat(pending.seq, 0, true);  // dead air is recorded, never hidden
    setStatus("waiting for audio: rejoining live");
    void resetProgram();
  }

  /* The audio clock advances the decode window as clips finish. Keep current
     plus next two decoded (up to MAX_AHEAD while under LOOKAHEAD_S of audio)
     and schedule each as soon as it is ready (§9.2). */
  async function fillWindow(startOffset, token, ptoken = programToken) {
    while (playing && token === session && ptoken === programToken) {
      try {
        advancePlayhead();
        if (items.length < cursor + MAX_AHEAD) {
          const lastSeq = items.length ? items[items.length - 1].seq : null;
          const p = await fetchProgram(lastSeq, epoch, revision);
          if (!playing || token !== session || ptoken !== programToken) return;
          if (p.reset) {
            // our cursor is stale (epoch/revision moved under us): the page is
            // a full snapshot, not a continuation. Let the reset path replace
            // the future instead of appending renumbered rows behind it.
            resetPending = true;
          } else {
            for (const it of p.items || []) {
              if (!items.some(x => x.seq === it.seq)) items.push(it);
            }
          }
        }
        while (scheduleCursor < Math.min(items.length, cursor + MIN_AHEAD)
               || (scheduleCursor < Math.min(items.length, cursor + MAX_AHEAD)
                   && nextWhen - ctx.currentTime < LOOKAHEAD_S)) {
          await getDecoded(scheduleCursor);
          if (!playing || token !== session || ptoken !== programToken) return;
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
        dryFailSince = null;   // a full pass succeeded
      } catch (e) {
        if (!playing || token !== session || ptoken !== programToken) return;
        setStatus("waiting for audio: " + e.message);
        if (ctx.currentTime >= nextWhen - 0.05) {
          // Buffer is dry. Off air is definitive; anything else gets a grace
          // window so one blip / 404 / bad decode never ends the session.
          const now = performance.now();
          if (dryFailSince === null) dryFailSince = now;
          if (e instanceof OffAirError || now - dryFailSince >= OFFAIR_GRACE_MS) {
            stop(); showOffAir(); return;
          }
        } else {
          dryFailSince = null;  // still playing buffered audio
        }
        // Back off; a dead origin must not be hammered every 100 ms.
        await new Promise(r => setTimeout(r, 1000));
        continue;
      }
      if (cursor >= scheduleCursor) setState("buffering");
      await new Promise(r => setTimeout(r, 100));
    }
  }

  /* Revision-aware refresh (TUI.md §5, §9.2). A revision change means the
     published future moved — e.g. a listener request was spliced ahead of the
     stock songs. Keep the audible item, cancel the obsolete future sources,
     and reschedule the new snapshot from the audio clock. */
  async function resetProgram() {
    if (!playing || !ctx) return;
    const token = session;
    const before = programToken;
    let p;
    try {
      p = await fetchProgram(null, epoch, revision);
    } catch (e) {
      // Nothing was cancelled yet: the buffered future keeps playing and the
      // next state poll retries. Off air with a dry buffer is definitive.
      if (!playing || token !== session) return;
      if (e instanceof OffAirError && ctx.currentTime >= nextWhen - 0.05) {
        stop(); showOffAir(); return;
      }
      resetPending = true;
      setStatus("program refresh failed: " + e.message);
      return;
    }
    if (!playing || token !== session || before !== programToken) return;
    const ptoken = ++programToken;
    resetPending = false;
    cancelFuture();
    epoch = p.epoch; revision = p.revision;
    const all = p.items || [];
    const cur = items[cursor];
    const onAir = all[0] || null;
    const keepCurrent = !!(cur && onAir && cur.seq === onAir.seq && cur._scheduled
      && cur._when <= ctx.currentTime && cur._end > ctx.currentTime);
    if (keepCurrent) {
      // Keep the audible item; replace only what comes after it.
      const curIdx = all.findIndex(it => it.seq === cur.seq);
      const future = (curIdx >= 0 ? all.slice(curIdx + 1)
                                  : all.filter(it => it.seq > cur.seq))
        .map(it => ({ ...it, buffer: null }));
      items = [cur, ...future];
      cursor = 0;
      scheduleCursor = 1;
      nextWhen = cur._end;
      void fillWindow(0, token, ptoken);
    } else {
      // The current occurrence changed or vanished: cut over to the snapshot.
      stopAllSources();
      items = all.map(it => ({ ...it, buffer: null }));
      cursor = 0;
      scheduleCursor = 0;
      nextWhen = ctx.currentTime + 0.05;
      void fillWindow(p.start_offset_s || 0, token, ptoken);
    }
  }

  async function pollState() {
    if (!playing) return;
    checkDeadAir();
    try {
      const r = await fetch("/api/station/state");
      if (!r.ok) return;
      const st = await r.json();
      if (!playing) return;
      if (st.epoch !== epoch || st.revision !== revision || resetPending) {
        epoch = st.epoch; revision = st.revision;
        await resetProgram();
        return;
      }
      // A cover can be published after the program snapshot was fetched (art
      // never gates playout). The state carries the on-air item's current
      // art URL; swap it in without refetching or rescheduling audio. Only
      // for the item that is audible here: the server's playhead can be on
      // an item this client has not started (never show the next cover early).
      const cur = items[cursor];
      if (cur && cur._started && st.current_seq === cur.seq && st.current_art_url
          && st.current_art_url !== cur.art_url) {
        cur.art_url = st.current_art_url;
        setCoverArt(cur, cur.type === "song");
      }
    } catch (e) { /* keep the current audio; retry on the next poll */ }
  }

  /* ---------------- heartbeat ---------------- */
  function sendHeartbeat(seq, position, underrun = false) {
    const it = items.find(i => i.seq === seq) || {};
    fetch("/api/station/heartbeat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seq, media_id: it.media_id, position, underrun,
                             player_id: String(myId),
                             started_at: ctx ? ctx.currentTime : 0, type: it.type,
                             // playback telemetry (RADIO §9.2): reported, never acted on
                             platform: PLATFORM, ctx_state: ctx ? ctx.state : "closed",
                             hidden: !!document.hidden, sink: sinkMode,
                             resumed: resumedCount })
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
    dryFailSince = null;
    rejoinAt = 0; rejoinGap = REJOIN_GAP_MIN_MS; resetPending = false;
    const token = ++session;
    setState("buffering"); btn.textContent = "■  STOP"; btn.classList.add("stop");

    try {
      // iOS: Web Audio alone runs in the "ambient" session and obeys the
      // ringer switch; ask for the playback session (Audio Session API,
      // Safari 17+; a no-op elsewhere) before the context exists.
      try { if (navigator.audioSession) navigator.audioSession.type = "playback"; } catch (e) {}
      // Create and resume synchronously from the click, before any network await.
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      const resumed = ctx.resume();
      analyser = ctx.createAnalyser(); analyser.fftSize = 256;
      interruptedAt = null; resumedCount = 0;
      attachOutput();                      // sink element play() inside the gesture
      ctx.addEventListener("statechange", onCtxState);
      setPlaybackState("playing");
      setMediaSession(null);
      await resumed;
      if (!playing || token !== session) return;
      const p = await fetchProgram(null);
      if (!playing || token !== session) return;
      epoch = p.epoch; revision = p.revision;
      items = p.items.map(it => ({ ...it, buffer: null }));  // sfx[] rides along
      cursor = 0; scheduleCursor = 0;
      nextWhen = ctx.currentTime + 0.2;
      drawLevel();
      if (bc) bc.postMessage({ kind: "playing", id: myId, at: myStartAt });
      hbTimer = setInterval(() => {
        advancePlayhead();
        const it = items[cursor];
        if (it?._started) sendHeartbeat(it.seq, it._offset + ctx.currentTime - it._when);
        checkDeadAir();
      }, 5000);
      stateTimer = setInterval(pollState, 2000);
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
    if (stateTimer) clearInterval(stateTimer); stateTimer = null;
    programToken++;
    epoch = null; revision = null;
    if (raf) cancelAnimationFrame(raf); raf = null;
    stopResumeTimer(); interruptedAt = null;
    detachOutput();
    setPlaybackState("none");
    if (ctx) { try { ctx.close(); } catch (e) {} ctx = null; }
    analyser = null;
    items = []; cursor = 0; scheduleCursor = 0;
    clearSongLabel();
    setCoverArt(null, false);
    setDownload(null);
    // VOTING.md: STOP closes the rating row immediately.
    document.dispatchEvent(new CustomEvent("pd:nowplaying", { detail: null }));
    btn.textContent = "▶  PLAY"; btn.classList.remove("stop");
    setState("idle"); lctx.clearRect(0, 0, level.width, level.height);
  }

  btn.addEventListener("click", () => (playing ? stop() : play()));
  initMediaSession();
  startHealth();
})();
