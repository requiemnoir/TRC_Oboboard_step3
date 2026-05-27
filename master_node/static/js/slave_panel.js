// TRC Master · slave_panel.js — vanilla, polls the master's PROXIED API
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
    return `${(s/3600|0)}h ${((s%3600)/60|0)}m ${s%60}s`;
  };
  const fmtTs = (ts) => ts ? new Date(ts*1000).toLocaleTimeString("it-IT", {hour12:false}) : "—";

  const PREFIX = "/slave-node";   // matches Flask blueprint url_prefix

  async function api(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(PREFIX + path, opts);
    const txt = await r.text();
    try { return { ok: r.ok, status: r.status, body: JSON.parse(txt) }; }
    catch { return { ok: r.ok, status: r.status, body: txt }; }
  }

  let lastDropped = 0;
  let paused = false;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
    }[c]));
  }

  function setLed(state) {
    const led = $("slave-led");
    led.className = "led " + state;
    led.textContent = state === "on" ? "slave online"
                    : state === "warn" ? "slave degraded"
                    : "slave offline";
  }

  async function refresh() {
    // Health
    const h = await api("GET", "/api/health");
    if (!h.ok) { setLed("off"); $("last-update").textContent = `slave unreachable @ ${fmtTs(Date.now()/1000)}`; return; }
    setLed("on");
    $("slave-host").textContent = h.body.hostname || "—";
    $("slave-uptime").textContent = fmtDur(h.body.uptime_s);
    $("slave-git").textContent = (h.body.branch || "?") + " @ " + (h.body.git_sha || "?");

    // Status
    const s = await api("GET", "/api/status");
    if (!s.ok) return;
    const st = s.body;
    const b = $("capture-state");
    b.textContent = st.active ? "ACTIVE" : "IDLE";
    b.className = "badge " + (st.active ? "active" : "idle");
    $("fps-1s").textContent = st.fps_1s.toFixed(0);
    $("fps-60s").textContent = st.fps_60s.toFixed(0);
    $("udp-pps").textContent = st.udp_packets_rx_per_s.toFixed(0);
    $("udp-total").textContent = (st.udp_packets_rx || 0).toLocaleString();
    $("udp-bytes").textContent = fmtBytes(st.udp_bytes_rx);
    $("frame-count").textContent = (st.frame_count || 0).toLocaleString();

    const dropEl = $("dropped");
    dropEl.textContent = (st.dropped_count || 0).toLocaleString();
    if (st.dropped_count > lastDropped) {
      dropEl.style.color = "var(--err)";
      setLed("warn");
    } else if (st.dropped_count > 0) {
      dropEl.style.color = "var(--warn)";
    } else {
      dropEl.style.color = "var(--ok)";
    }
    lastDropped = st.dropped_count || 0;

    $("queue").textContent = st.queue_depth || 0;
    $("parts").textContent = st.parts || 0;
    $("bytes").textContent = fmtBytes(st.bytes_written);
    $("disk-free").textContent = `${(st.disk_free_mb|0).toLocaleString()} MB`;

    if (st.last_error) {
      $("error-card").classList.remove("hidden");
      $("last-error").textContent = st.last_error;
      $("last-error-ts").textContent = fmtTs(st.last_error_ts);
    } else {
      $("error-card").classList.add("hidden");
    }

    $("last-update").textContent = "updated " + new Date().toLocaleTimeString("it-IT", {hour12:false});
  }

  async function refreshLogs() {
    if (paused) return;
    const lvl = $("log-level").value;
    const q = "?lines=80" + (lvl ? "&level=" + lvl : "");
    const r = await api("GET", "/api/logs" + q);
    if (!r.ok || !Array.isArray(r.body)) return;
    const ol = $("log-stream");
    ol.innerHTML = "";
    r.body.forEach((line) => {
      const li = document.createElement("li");
      const ts = new Date(line.ts*1000).toLocaleTimeString("it-IT", {hour12:false});
      li.innerHTML = `<span class="ts">${ts}</span>` +
                     `<span class="lvl ${line.level}">${line.level}</span>` +
                     `<span class="cmp">${escapeHtml(line.component)}</span>` +
                     `<span class="msg">${escapeHtml(line.message)}</span>`;
      ol.appendChild(li);
    });
    ol.scrollTop = ol.scrollHeight;
  }

  async function refreshMf4() {
    const r = await api("GET", "/api/mf4");
    const ul = $("mf4-list"); ul.innerHTML = "";
    if (!r.ok || !r.body.files) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="fname">(slave unreachable o nessun file)</span>`;
      ul.appendChild(li); return;
    }
    if (!r.body.files.length) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="fname">(nessun MF4)</span><span class="fsize">${escapeHtml(r.body.log_dir)}</span>`;
      ul.appendChild(li); return;
    }
    r.body.files.slice(-50).reverse().forEach((f) => {
      const li = document.createElement("li");
      const ts = new Date(f.mtime*1000).toLocaleString("it-IT", {hour12:false});
      li.innerHTML = `<a class="fname" href="${PREFIX}/api/mf4/${encodeURIComponent(f.name)}" target="_blank">${escapeHtml(f.name)}</a>` +
                     `<span class="fsize">${fmtBytes(f.size_bytes)}</span>` +
                     `<span class="fts">${ts}</span>`;
      ul.appendChild(li);
    });
  }

  async function runCmd(cmd) {
    if (!cmd || !cmd.trim()) return;
    const out = $("cmd-output");
    out.classList.remove("hidden", "error");
    out.textContent = `[slave] $ ${cmd}\n…`;
    const r = await api("POST", "/api/cmd", { cmd });
    if (!r.ok) {
      out.classList.add("error");
      out.textContent = `[error ${r.status}] ${JSON.stringify(r.body, null, 2)}`;
      return;
    }
    let txt = `[slave] $ ${cmd}\n`;
    if (r.body.stdout) txt += r.body.stdout;
    if (r.body.stderr) txt += "\n--- stderr ---\n" + r.body.stderr;
    txt += `\n[rc=${r.body.rc} t=${(r.body.elapsed_s*1000).toFixed(0)}ms]`;
    out.textContent = txt;
    if (!r.body.ok) out.classList.add("error");
  }

  async function captureCmd(action) {
    const r = await api("POST", "/api/" + action, { reason: "master-ui" });
    if (!r.ok) {
      alert(`${action}: ${r.status} ${JSON.stringify(r.body)}`);
    }
    await refresh();
  }

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

    $("btn-refresh-logs").addEventListener("click", refreshLogs);
    $("btn-refresh-mf4").addEventListener("click", refreshMf4);
    $("btn-pause-logs").addEventListener("click", (e) => {
      paused = !paused;
      e.target.textContent = paused ? "▶ resume" : "⏸ pause";
    });
    $("log-level").addEventListener("change", refreshLogs);

    refresh(); refreshLogs(); refreshMf4();
    setInterval(refresh, 1500);
    setInterval(refreshLogs, 3000);
    setInterval(refreshMf4, 25000);
  });
})();
