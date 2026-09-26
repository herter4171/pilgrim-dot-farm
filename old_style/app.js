/* MiniMax Music 3 — cheesy windowed UI logic */
(() => {
  "use strict";

  /* ---------------- window manager (drag / resize / focus) ---------------- */
  const wins = [...document.querySelectorAll(".window")];
  wins.forEach(win => {
    const bar = win.querySelector(".win-bar");
    // create resize handle
    const rsz = document.createElement("div");
    rsz.className = "rsz";
    win.appendChild(rsz);

    bar.addEventListener("pointerdown", e => {
      if (e.target.closest("button")) return;
      focusWin(win);
      const r = win.getBoundingClientRect();
      const ox = e.clientX - r.left, oy = e.clientY - r.top;
      const move = ev => {
        win.style.left = Math.max(0, ev.clientX - ox) + "px";
        win.style.top = Math.max(0, ev.clientY - oy) + "px";
      };
      const up = () => { window.removeEventListener("pointermove", move);
                         window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });

    rsz.addEventListener("pointerdown", e => {
      e.stopPropagation();
      focusWin(win);
      const r = win.getBoundingClientRect();
      const ox = e.clientX, oy = e.clientY, ow = r.width, oh = r.height;
      const move = ev => {
        win.style.width = Math.max(260, ow + (ev.clientX - ox)) + "px";
        win.style.height = Math.max(160, oh + (ev.clientY - oy)) + "px";
      };
      const up = () => { window.removeEventListener("pointermove", move);
                         window.removeEventListener("pointerup", up); };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });

    const min = win.querySelector(".w-min");
    min.addEventListener("click", () => win.classList.add("minimized"));
    const close = win.querySelector(".w-close");
    close.addEventListener("click", () => win.classList.add("minimized"));
    win.addEventListener("pointerdown", () => focusWin(win));
  });

  function focusWin(win) {
    wins.forEach(w => w.classList.remove("focus"));
    win.classList.add("focus");
  }

  /* ---------------- cheesy rain canvas ---------------- */
  const cvs = document.getElementById("rain");
  const ctx = cvs.getContext("2d");
  function sizeCvs() { cvs.width = innerWidth; cvs.height = innerHeight; }
  sizeCvs(); addEventListener("resize", sizeCvs);
  const cols = [];
  const nCols = 60;
  for (let i = 0; i < nCols; i++) cols.push(Math.random() * -80);
  setInterval(() => {
    ctx.fillStyle = "rgba(10,1,24,0.12)";
    ctx.fillRect(0, 0, cvs.width, cvs.height);
    ctx.font = "14px monospace";
    for (let i = 0; i < nCols; i++) {
      const ch = "♪♫♩♬♭♮♯0123456789AB".charAt(Math.floor(Math.random()*16));
      ctx.fillStyle = Math.random() > .5 ? "#ff2fd6" : "#24e0ff";
      ctx.fillText(ch, i * (cvs.width / nCols), cols[i] * 18);
      if (cols[i] * 18 > cvs.height && Math.random() > .975) cols[i] = 0;
      cols[i]++;
    }
  }, 90);

  /* ---------------- helpers ---------------- */
  const $ = id => document.getElementById(id);
  async function jfetch(url, opts) {
    const r = await fetch(url, opts);
    const t = await r.json();
    if (!r.ok) throw new Error(t.error || r.statusText);
    return t;
  }
  function ding() {
    try {
      const C = new (window.AudioContext || window.webkitAudioContext)();
      const play = (freq, t0, dur, type = "sine", vol = .5) => {
        const o = C.createOscillator(), g = C.createGain();
        o.type = type; o.frequency.value = freq;
        o.connect(g); g.connect(C.destination);
        const t = C.currentTime + t0;
        g.gain.setValueAtTime(0, t);
        g.gain.linearRampToValueAtTime(vol, t + .02);
        g.gain.exponentialRampToValueAtTime(.001, t + dur);
        o.start(t); o.stop(t + dur + .02);
      };
      play(1046.5, 0, .28, "triangle", .6);   // C6
      play(1318.5, .16, .4, "triangle", .6);  // E6
      play(1568, .32, .55, "triangle", .6);   // G6
    } catch (e) {}
  }

  /* ---------------- song generator ---------------- */
  const btnGen = $("btn-gen"), genStatus = $("gen-status"),
        genProgress = $("gen-progress"), dlZone = $("download-zone");
  let pollTimer = null, curTitle = "", curArtist = "", curId = null;

  $("btn-gen").addEventListener("click", async () => {
    const lyrics = $("lyrics").value;
    const prompt = $("prompt").value;
    if (!lyrics.trim() && !prompt.trim()) {
      genStatus.textContent = "⚠ give me at least some lyrics or a description!";
      return;
    }
    btnGen.disabled = true;
    dlZone.classList.add("hidden");
    curId = null;
    genStatus.textContent = "queued...";
    genProgress.textContent = "⏳";
    try {
      const t = await jfetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lyrics, prompt })
      });
      curId = t.id;
      pollTimer = setInterval(pollJob, 1500);
      pollJob();
    } catch (e) {
      genStatus.textContent = "✖ " + e.message;
      btnGen.disabled = false;
      genProgress.textContent = "";
    }
  });

  async function pollJob() {
    if (!curId) return;
    try {
      const t = await jfetch("/api/status/" + curId);
      if (t.state === "pending") { genStatus.textContent = "waiting in queue..."; }
      else if (t.state === "running") { genStatus.textContent = "🎛 synth-wrangling... (may take a minute)"; }
      else if (t.state === "done") {
        clearInterval(pollTimer); pollTimer = null;
        btnGen.disabled = false;
        genProgress.textContent = "✅ ready";
        genStatus.textContent = "🎉 your jam is ready!";
        curTitle = t.meta.title; curArtist = t.meta.artist; curId = t.id;
        $("dl-meta").textContent = `${t.meta.title} — ${t.meta.artist}` +
          (t.meta.duration ? `  (${t.meta.duration}s)` : "");
        dlZone.classList.remove("hidden");
        ding();
        loadSongs();
      } else if (t.state === "error") {
        clearInterval(pollTimer); pollTimer = null;
        btnGen.disabled = false; genProgress.textContent = "";
        genStatus.textContent = "✖ error: " + (t.error || "generation failed");
      }
    } catch (e) {
      clearInterval(pollTimer); pollTimer = null;
      genStatus.textContent = "✖ " + e.message;
    }
  }

  $("btn-dl").addEventListener("click", () => {
    if (curId) {
      const a = document.createElement("a");
      a.href = "/api/songs/" + curId;
      a.download = (curTitle || curId) + ".wav";
      document.body.appendChild(a); a.click(); a.remove();
    }
  });

  /* ---------------- music player ---------------- */
  const audio = $("audio");
  async function loadSongs() {
    try {
      const t = await jfetch("/api/songs");
      const list = $("song-list");
      list.innerHTML = "";
      if (!t.songs.length) {
        list.innerHTML = '<div class="empty">no jams yet — hit Generate!</div>';
        return;
      }
      t.songs.forEach(s => {
        const item = document.createElement("div");
        item.className = "song-item";
        const left = document.createElement("span");
        left.innerHTML = `<span class="t">${esc(s.title)}</span> <span class="a">— ${esc(s.artist)}</span>`;
        const dl = document.createElement("a");
        dl.className = "dl"; dl.href = "/api/songs/" + s.id; dl.download = (s.title || s.id) + ".wav";
        dl.textContent = "⬇";
        dl.addEventListener("click", e => e.stopPropagation());
        item.appendChild(left); item.appendChild(dl);
        item.addEventListener("click", () => {
          $("np-title").textContent = s.title;
          $("np-artist").textContent = s.artist + (s.duration ? ` · ${s.duration}s` : "");
          audio.src = "/api/songs/" + s.id;
          audio.play();
        });
        list.appendChild(item);
      });
    } catch (e) {}
  }
  function esc(s) { const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s); return d.innerHTML; }

  /* ---------------- chat UI ---------------- */
  const chatLog = $("chat-log"), chatInput = $("chat-input");
  let chatHist = [];
  function addMsg(role, text, reason) {
    const el = document.createElement("div");
    el.className = "msg " + role;
    if (reason) {
      const r = document.createElement("details");
      r.className = "msg reason";
      const sum = document.createElement("summary");
      sum.textContent = "🤖 reasoning…";
      const pre = document.createElement("div");
      pre.textContent = reason;
      r.appendChild(sum); r.appendChild(pre);
      chatLog.appendChild(r);
    }
    el.textContent = text;
    chatLog.appendChild(el);
    chatLog.scrollTop = chatLog.scrollHeight;
    return el;
  }
  function sendChat() {
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    addMsg("user", text);
    chatHist.push({ role: "user", content: text });
    const agent = $("chat-agent").value;
    const model = $("chat-model").value;
    const busy = addMsg("bot", "…");
    busy.textContent = agent === "songwriter"
      ? "⏳ songwriter (qwen38 + minimax skill)..." : "⏳ talking to " + model + "...";
    jfetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent, model, messages: chatHist })
    }).then(t => {
      chatHist.push({ role: "assistant", content: t.content });
      if (t.reasoning) {
        busy.remove();
        addMsg("bot", t.content, t.reasoning);
      } else {
        busy.textContent = t.content;
      }
    }).catch(e => {
      busy.textContent = "✖ " + e.message;
    });
  }
  // keep the skill label honest as you switch agents
  $("chat-agent").addEventListener("change", () => {
    $("skill-tag").textContent =
      $("chat-agent").value === "songwriter"
        ? "minimax-music3-prompting" : "lyrics · minimax";
  });
  $("btn-chat").addEventListener("click", sendChat);
  chatInput.addEventListener("keydown", e => { if (e.key === "Enter") sendChat(); });

  $("btn-send-to-gen").addEventListener("click", () => {
    const m = [...chatLog.querySelectorAll(".msg.bot:not(.msg.reason)")];
    const last = m[m.length - 1];
    if (last) $("lyrics").value = (last.textContent || "").trim();
    focusWin(document.querySelector('[data-win="gen"]'));
  });

  /* ---------------- logs ---------------- */
  let lastLogLen = 0;
  async function pollLogs() {
    try {
      const t = await jfetch("/api/logs?lines=400");
      const pre = $("log-view");
      if (t.lines.length !== lastLogLen) {
        pre.textContent = t.lines.join("\n");
        pre.scrollTop = pre.scrollHeight;
        lastLogLen = t.lines.length;
      }
    } catch (e) {}
  }
  setInterval(pollLogs, 2000);
  pollLogs();

  loadSongs();
})();
