// TRC Slave node — frontend JS (vanilla, no framework)
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fmtBytes = (n) => {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(1)} ${u[i]}`;
  };
  const fmtDur = (s) => {
    if (s == null) return "—";
    s = Math.max(0, s | 0);
    const h = (s / 3600) | 0;
    const m = ((s % 3600) / 60) | 0;
    const ss = s % 60;
    return `${h}h ${m}m ${ss}s`;
  };
  const fmtTs = (ts) => {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString("it-IT", { hour12: false });
  };

  let paused = false;
  let logBuffer = [];          // when paused, buffer up to N then drop oldest
  const LOG_BUF_MAX = 1000;

  // --------- API helpers ----------
  async function api(method, path, body) {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
    return r.json();
  }

  // --------- status polling ----------
  let prevDropped = 0;
  async function refreshStatus() {
    try {
      const s = await api("GET", "/api/capture/status");
      const h = await api("GET", "/api/health");

      $("uptime").textContent = fmtDur(h.uptime_s);

      $("capture-active").textContent = s.active ? "ACTIVE" : "IDLE";
      $("capture-active").className = "value badge " + (s.active ? "active" : "idle");
      $("session-id").textContent = s.session_id || "—";

      $("fps-1s").textContent = s.fps_1s.toFixed(0);
      $("fps-60s").textContent = s.fps_60s.toFixed(0);
      $("udp-pps").textContent = s.udp_packets_rx_per_s.toFixed(0);
      $("udp-total").textContent = s.udp_packets_rx.toLocaleString();
      $("udp-bytes").textContent = fmtBytes(s.udp_bytes_rx);

      $("frame-count").textContent = s.frame_count.toLocaleString();
      const dropEl = $("dropped");
      dropEl.textContent = s.dropped_count.toLocaleString();
      dropEl.style.color = s.dropped_count > prevDropped ? "var(--err)" :
                            s.dropped_count > 0 ? "var(--warn)" : "var(--ok)";
      prevDropped = s.dropped_count;

      $("queue").textContent = s.queue_depth.toLocaleString();
      $("parts").textContent = s.parts;
      $("bytes").textContent = fmtBytes(s.bytes_written);
      $("disk-free").textContent = `${(s.disk_free_mb | 0).toLocaleString()} MB`;

      if (s.last_error) {
        $("error-card").classList.remove("hidden");
        $("last-error").textContent = s.last_error;
        $("last-error-ts").textContent = fmtTs(s.last_error_ts);
      } else {
        $("error-card").classList.add("hidden");
      }

      $("last-update").textContent = "updated " + new Date().toLocaleTimeString("it-IT", { hour12: false });
    } catch (e) {
      console.warn("status refresh:", e);
    }
  }

  // --------- log stream ----------
  function appendLog(line) {
    if (paused) {
      logBuffer.push(line);
      if (logBuffer.length > LOG_BUF_MAX) logBuffer.shift();
      return;
    }
    const filter = $("log-level").value;
    if (filter && line.level !== filter) return;
    const ol = $("log-stream");
    const li = document.createElement("li");
    const ts = new Date(line.ts * 1000).toLocaleTimeString("it-IT", { hour12: false });
    li.innerHTML = `<span class="ts">${ts}</span>` +
                   `<span class="lvl ${line.level}">${line.level}</span>` +
                   `<span class="cmp">${escapeHtml(line.component)}</span>` +
                   `<span class="msg">${escapeHtml(line.message)}</span>`;
    ol.appendChild(li);
    while (ol.children.length > 500) ol.removeChild(ol.firstChild);
    ol.scrollTop = ol.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
    }[c]));
  }

  async function loadInitialLogs() {
    try {
      const lines = await api("GET", "/api/logs?lines=80");
      lines.forEach(appendLog);
    } catch (e) {
      console.warn("initial logs:", e);
    }
  }

  // --------- mf4 list ----------
  async function refreshMf4() {
    try {
      const r = await api("GET", "/api/mf4/list");
      const ul = $("mf4-list");
      ul.innerHTML = "";
      if (!r.files.length) {
        const li = document.createElement("li");
        li.innerHTML = `<span class="fname">(nessun file)</span><span class="fsize">${r.log_dir}</span>`;
        ul.appendChild(li);
        return;
      }
      r.files.slice(-50).reverse().forEach((f) => {
        const li = document.createElement("li");
        const ts = new Date(f.mtime * 1000).toLocaleString("it-IT", { hour12: false });
        li.innerHTML = `<a class="fname" href="/api/mf4/${encodeURIComponent(f.name)}" target="_blank">${escapeHtml(f.name)}</a>` +
                       `<span class="fsize">${fmtBytes(f.size_bytes)}</span>` +
                       `<span class="fts">${ts}</span>`;
        ul.appendChild(li);
      });
    } catch (e) {
      console.warn("mf4 list:", e);
    }
  }

  // --------- commands ----------
  async function runCmd(cmd) {
    if (!cmd || !cmd.trim()) return;
    const out = $("cmd-output");
    out.classList.remove("hidden", "error");
    out.textContent = `$ ${cmd}\n…`;
    try {
      const r = await api("POST", "/api/cmd/exec", { cmd });
      let txt = `$ ${cmd}\n`;
      if (r.stdout) txt += r.stdout;
      if (r.stderr) txt += "\n--- stderr ---\n" + r.stderr;
      txt += `\n[rc=${r.rc} t=${(r.elapsed_s * 1000).toFixed(0)}ms]`;
      out.textContent = txt;
      if (!r.ok) out.classList.add("error");
    } catch (e) {
      out.textContent = `$ ${cmd}\n[error: ${e.message}]`;
      out.classList.add("error");
    }
  }

  // --------- capture control ----------
  async function captureCmd(action) {
    try {
      const path = `/api/capture/${action}`;
      const r = await api("POST", path, { reason: "ui" });
      console.log(action, "→", r);
      await refreshStatus();
    } catch (e) {
      alert(`${action}: ${e.message}`);
    }
  }

  // --------- socketio ----------
  function startSocketIO() {
    const url = window.location.origin;
    const sock = io(url + "/slave", {
      transports: ["websocket", "polling"],
      reconnectionDelay: 1000,
      reconnectionDelayMax: 4000,
    });
    sock.on("connect", () => {
      const led = $("connection-led");
      led.className = "led on";
      led.textContent = "online";
    });
    sock.on("disconnect", () => {
      const led = $("connection-led");
      led.className = "led off";
      led.textContent = "offline";
    });
    sock.on("log", (line) => appendLog(line));
    sock.on("frame", (f) => {
      // already shown via stats; could draw a sparkline
    });
    sock.on("snapshot", (s) => {
      appendLog({
        ts: s.ts, level: "INFO", component: "snapshot",
        message: `force_flush ok=${s.ok}`,
      });
    });
    window._sock = sock;
  }

  // --------- wire UI ----------
  document.addEventListener("DOMContentLoaded", () => {
    $("btn-start").addEventListener("click", () => captureCmd("start"));
    $("btn-stop").addEventListener("click", () => captureCmd("stop"));
    $("btn-snapshot").addEventListener("click", () => captureCmd("snapshot"));

    $("cmd-run").addEventListener("click", () => runCmd($("cmd-input").value));
    $("cmd-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") runCmd($("cmd-input").value);
    });
    document.querySelectorAll(".quick").forEach((b) => {
      b.addEventListener("click", () => {
        $("cmd-input").value = b.dataset.cmd;
        runCmd(b.dataset.cmd);
      });
    });

    $("btn-refresh-mf4").addEventListener("click", refreshMf4);

    $("btn-clear").addEventListener("click", () => { $("log-stream").innerHTML = ""; });
    $("btn-pause").addEventListener("click", (e) => {
      paused = !paused;
      e.target.textContent = paused ? "▶ resume" : "⏸ pause";
      if (!paused && logBuffer.length) {
        const drained = logBuffer.splice(0);
        drained.forEach(appendLog);
      }
    });

    refreshStatus();
    loadInitialLogs();
    refreshMf4();
    startSocketIO();

    setInterval(refreshStatus, 1500);
    setInterval(refreshMf4, 20000);
  });
})();
