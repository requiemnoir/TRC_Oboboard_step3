const socket = io();

// Elements
const btnStart = document.getElementById('btn-start');
const btnStop = document.getElementById('btn-stop');
const btnLogStart = document.getElementById('btn-log-start');
const btnLogStop = document.getElementById('btn-log-stop');
const btnLogStartLogger = document.getElementById('btn-log-start-logger');
const btnLogStopLogger = document.getElementById('btn-log-stop-logger');
const modeSelect = document.getElementById('mode-select');
const btnScanToolsScan = document.getElementById('btn-scantools-scan');
const btnScanToolsVagScan = document.getElementById('btn-scantools-vag-scan');
const btnScanToolsVagDoip = document.getElementById('btn-scantools-vag-doip');
const btnScanToolsDoipLinkLocal = document.getElementById('btn-scantools-doip-linklocal');
const btnScanToolsMode06 = document.getElementById('btn-scantools-mode06');
const btnScanToolsSelfTest = document.getElementById('btn-scantools-selftest');
const btnScanToolsDiscovery = document.getElementById('btn-scantools-discovery');
const btnScanToolsClear = document.getElementById('btn-scantools-clear');
const btnScanToolsDoipClear = document.getElementById('btn-scantools-doip-clear');
const btnScanToolsDoipMode06 = document.getElementById('btn-scantools-doip-mode06');
const btnScanToolsClearConsole = document.getElementById('btn-scantools-clear-console');
const btnLiveStart = document.getElementById('btn-live-start');
const btnLiveStop = document.getElementById('btn-live-stop');
const liveStatus = document.getElementById('live-status');
const vehicleRpmEl = document.getElementById('vehicle-rpm');
const vehicleSpeedEl = document.getElementById('vehicle-speed');
const vehicleMilEl = document.getElementById('vehicle-mil');
const vehicleCoolantEl = document.getElementById('vehicle-coolant-temp');
const vehicleSocEl = document.getElementById('vehicle-battery-soc');
const vehicleVoltageEl = document.getElementById('vehicle-battery-voltage');
const vehicleOdometerEl = document.getElementById('vehicle-odometer');
const channelsContainer = document.getElementById('channels-container');
const logTable = document.getElementById('log-table-body');
const scanToolsConsole = document.getElementById('scantools-console');
const scanToolsStatus = document.getElementById('scantools-status');
const scanToolsChannelSelect = document.getElementById('scantools-channel-select');
const btnLogsDeleteAll = document.getElementById('btn-logs-delete-all');
const btnSessionBundleLatest = document.getElementById('btn-session-bundle-latest');
const btnChannelsReset = document.getElementById('btn-channels-reset');

// System indicators (navbar)
const cpuTempEl = document.getElementById('stat-cpu-temp');
const cpuPctEl = document.getElementById('stat-cpu');
const ramPctEl = document.getElementById('stat-ram');
let sysStatsTimer = null;

// Gateway Mirror
const gmEnabledEl = document.getElementById('gm-enabled');
const gmAutostartEl = document.getElementById('gm-autostart');
const gmAutoDiscoverIpEl = document.getElementById('gm-auto-discover-ip');
const gmGatewayIpEl = document.getElementById('gm-gateway-ip');
const gmTargetAddrEl = document.getElementById('gm-target-addr');
const gmTesterAddrEl = document.getElementById('gm-tester-addr');
const gmTargetBusEl = document.getElementById('gm-target-bus');
const gmDestIpEl = document.getElementById('gm-dest-ip');
const gmDestPortEl = document.getElementById('gm-dest-port');
const btnGmRefresh = document.getElementById('btn-gm-refresh');
const btnGmSave = document.getElementById('btn-gm-save');
const btnGmStart = document.getElementById('btn-gm-start');
const btnGmStop = document.getElementById('btn-gm-stop');
const btnGmDiscoverTarget = document.getElementById('btn-gm-discover-target');
const gmStatusEl = document.getElementById('gm-status');
const gmLastRespEl = document.getElementById('gm-last-response');

const gmCanEls = [1,2,3,4,5,6,7,8].map(n => document.getElementById(`gm-can-${n}`));
const gmFlexEls = {
    A: document.getElementById('gm-fr-a'),
    B: document.getElementById('gm-fr-b'),
};
const gmLinEls = [1,2,3].map(n => document.getElementById(`gm-lin-${n}`));

// CAN Trigger (DBC-based)
const canTrChannelEl = document.getElementById('can-tr-channel');
const canTrDbcEl = document.getElementById('can-tr-dbc');
const canTrMsgFilterEl = document.getElementById('can-tr-msg-filter');
const canTrMessageEl = document.getElementById('can-tr-message');
const canTrSigFilterEl = document.getElementById('can-tr-sig-filter');
const canTrSigGlobalEl = document.getElementById('can-tr-sig-global');
const canTrSigGlobalResultsEl = document.getElementById('can-tr-sig-global-results');
const canTrSigGlobalHintEl = document.getElementById('can-tr-sig-global-hint');
const canTrSignalEl = document.getElementById('can-tr-signal');
const canTrStartOpEl = document.getElementById('can-tr-start-op');
const canTrStartValEl = document.getElementById('can-tr-start-val');
const canTrStopOpEl = document.getElementById('can-tr-stop-op');
const canTrStopValEl = document.getElementById('can-tr-stop-val');
const canTrAutoStopEl = document.getElementById('can-tr-auto-stop');
const canTrNoMsgStopEl = document.getElementById('can-tr-no-msg-s');
const btnCanTrApply = document.getElementById('btn-can-tr-apply');
const btnCanTrArm = document.getElementById('btn-can-tr-arm');
const btnCanTrDisarm = document.getElementById('btn-can-tr-disarm');
const canTrStatusEl = document.getElementById('can-trigger-status');
const canTrLastErrorEl = document.getElementById('can-tr-last-error');
let canTrArmedState = false;
let canTrDescribeCache = null;
let canTrSignalIndexCache = null;
let canTrLastError = null;
let canTrDirtyUntilMs = 0;

function _canTrMarkDirty(ms = 4000) {
    try {
        const now = Date.now();
        canTrDirtyUntilMs = Math.max(canTrDirtyUntilMs, now + ms);
    } catch (_) {
        canTrDirtyUntilMs = 0;
    }
}

function _canTrIsDirty() {
    try {
        return Date.now() < canTrDirtyUntilMs;
    } catch (_) {
        return false;
    }
}

// Ethernet Trigger
const ethTrCooldownEl = document.getElementById('eth-tr-cooldown');
const btnEthTrApply = document.getElementById('btn-eth-tr-apply');
const btnEthTrArm = document.getElementById('btn-eth-tr-arm');
const btnEthTrDisarm = document.getElementById('btn-eth-tr-disarm');
const ethTrStatusEl = document.getElementById('eth-trigger-status');
const ethTrLastErrorEl = document.getElementById('eth-tr-last-error');
let ethTrArmedState = false;
let ethTrLastError = null;

// YOLO Trigger (Logger)
const yoloClassesEl = document.getElementById('yolo-classes');
const yoloTriggerStatusEl = document.getElementById('yolo-trigger-status');
const btnYoloArm = document.getElementById('btn-yolo-arm');
const btnYoloDisarm = document.getElementById('btn-yolo-disarm');

const yoloConfEl = document.getElementById('yolo-conf');
const yoloImgSzEl = document.getElementById('yolo-imgsz');
const yoloFpsEl = document.getElementById('yolo-fps');
const yoloCooldownEl = document.getElementById('yolo-cooldown');
const yoloModelEl = document.getElementById('yolo-model');
const btnYoloApply = document.getElementById('btn-yolo-apply');
const btnYoloApplyClasses = document.getElementById('btn-yolo-apply-classes');
const btnYoloTest = document.getElementById('btn-yolo-test');
const yoloLastErrorEl = document.getElementById('yolo-last-error');
const yoloTestOutEl = document.getElementById('yolo-test-output');

let yoloSelectionDirty = false;
let yoloSettingsDirty = false;
let yoloArmedState = false;

// Custom Objects
const customNameEl = document.getElementById('custom-name');
const btnCustomCapture = document.getElementById('btn-custom-capture');
const btnCustomTrain = document.getElementById('btn-custom-train');
const customObjectsEl = document.getElementById('custom-objects');
const customTriggerStatusEl = document.getElementById('custom-trigger-status');
const btnCustomArm = document.getElementById('btn-custom-arm');
const btnCustomDisarm = document.getElementById('btn-custom-disarm');
const customThresholdEl = document.getElementById('custom-threshold');
const customFpsEl = document.getElementById('custom-fps');
const customCooldownEl = document.getElementById('custom-cooldown');
const btnCustomTest = document.getElementById('btn-custom-test');
const customLastErrorEl = document.getElementById('custom-last-error');
const customTestOutEl = document.getElementById('custom-test-output');

let customSelectionDirty = false;

// Trigger Rules
const trEnabledEl = document.getElementById('tr-enabled');
const trModeEl = document.getElementById('tr-mode');
const trWindowEl = document.getElementById('tr-window');
const trCooldownEl = document.getElementById('tr-cooldown');
const trAutoStopEl = document.getElementById('tr-auto-stop');
const trPrerollEl = document.getElementById('tr-preroll');
const videoRecEnabledEl = document.getElementById('video-rec-enabled');
const videoRecStatusEl = document.getElementById('video-rec-status');
const trSrcMotionEl = document.getElementById('tr-src-motion');
const trSrcYoloEl = document.getElementById('tr-src-yolo');
const trSrcCustomEl = document.getElementById('tr-src-custom');
const btnTrApply = document.getElementById('btn-tr-apply');

let saveCfgTimer = null;

// Webcam elements (Logger + Live Data)
const webcamImgLogger = document.getElementById('webcam-img-logger');
const webcamStatusLogger = document.getElementById('webcam-status-logger');
const webcamImgLive = document.getElementById('webcam-img-live');
const webcamStatusLive = document.getElementById('webcam-status-live');

// Timeline Viewer
const timelineModeEl = document.getElementById('timeline-mode');
const timelineSessionEl = document.getElementById('timeline-session');
const btnTimelineLoad = document.getElementById('timeline-load');
const timelineStatusEl = document.getElementById('timeline-status');
const timelineVideoEl = document.getElementById('timeline-video');
const timelineLiveImgEl = document.getElementById('timeline-live');
const timelineChannelEl = document.getElementById('timeline-channel');
const timelineDbcEl = document.getElementById('timeline-dbc');
const timelineSignalsEl = document.getElementById('timeline-signals');
const timelineSignalCountEl = document.getElementById('timeline-signal-count');
const btnTimelineRefreshSignals = document.getElementById('timeline-refresh-signals');
const btnTimelinePlot = document.getElementById('timeline-plot');
const timelinePlotAreaEl = document.getElementById('timeline-plot-area');
const timelineValuesEl = document.getElementById('timeline-values');

let timelineManifest = null;
let timelineReviewBaseEpochS = null;
let timelineReviewFile = null;
let timelinePlotReady = false;
let timelineLiveEnabled = false;
let timelineLiveState = null;
let timelineLiveFlushTimer = null;
let timelineVideoTimeUpdateTimer = null;
let timelineVideoRafId = null;
let timelineReviewTracePairs = null; // [{lineIdx, markerIdx}] for review mode

function _timelineCancelRaf() {
    if (timelineVideoRafId != null) {
        try { cancelAnimationFrame(timelineVideoRafId); } catch (_) {}
        timelineVideoRafId = null;
    }
}

function _timelineNearestIndexSorted(xs, x) {
    // xs must be numeric and (mostly) sorted ascending.
    const n = xs.length;
    if (n <= 1) return 0;
    let lo = 0;
    let hi = n - 1;
    while (lo + 1 < hi) {
        const mid = (lo + hi) >> 1;
        const v = Number(xs[mid]);
        if (Number.isNaN(v) || v < x) lo = mid;
        else hi = mid;
    }
    const vlo = Number(xs[lo]);
    const vhi = Number(xs[hi]);
    if (Number.isNaN(vlo)) return hi;
    if (Number.isNaN(vhi)) return lo;
    return (Math.abs(vhi - x) < Math.abs(vlo - x)) ? hi : lo;
}

async function timelineSyncToVideoTime(tSec) {
    if (!timelinePlotReady || !timelinePlotAreaEl) return;
    const ct = Number(tSec);
    if (!Number.isFinite(ct)) return;

    // Throttle relayout to avoid spamming.
    if (timelineVideoTimeUpdateTimer) return;
    timelineVideoTimeUpdateTimer = setTimeout(async () => {
        timelineVideoTimeUpdateTimer = null;
        try {
            await Plotly.relayout(timelinePlotAreaEl, {
                'shapes[0].x0': ct,
                'shapes[0].x1': ct,
            });
        } catch (_) {
            // ignore
        }

        // Update per-signal marker dots (review mode only)
        try {
            if (timelineReviewTracePairs && Array.isArray(timelinePlotAreaEl.data)) {
                for (const p of timelineReviewTracePairs) {
                    const line = timelinePlotAreaEl.data[p.lineIdx];
                    const mark = timelinePlotAreaEl.data[p.markerIdx];
                    const xs = Array.isArray(line?.x) ? line.x : [];
                    const ys = Array.isArray(line?.y) ? line.y : [];
                    if (!xs.length || !ys.length) continue;
                    let i = 0;
                    try {
                        i = _timelineNearestIndexSorted(xs, ct);
                    } catch (_) {
                        // fallback linear
                        let bestI = 0;
                        let bestD = Infinity;
                        for (let j = 0; j < xs.length; j++) {
                            const dx = Math.abs(Number(xs[j]) - ct);
                            if (dx < bestD) { bestD = dx; bestI = j; }
                        }
                        i = bestI;
                    }
                    const mx = Number(xs[i]);
                    const my = Number(ys[i]);
                    if (!Number.isFinite(mx) || !Number.isFinite(my)) continue;
                    // restyle only this marker trace
                    Plotly.restyle(timelinePlotAreaEl, { x: [[mx]], y: [[my]] }, [p.markerIdx]);
                }
            }
        } catch (_) {
            // ignore
        }

        timelineUpdateValuesAtTime(ct);
    }, 60);
}

function timelineAttachVideoSyncHandlers() {
    if (!timelineVideoEl) return;
    // Reset any previous RAF loop
    _timelineCancelRaf();

    const tick = () => {
        if (!timelineVideoEl) return;
        if (timelineVideoEl.paused || timelineVideoEl.ended) {
            timelineVideoRafId = null;
            return;
        }
        timelineSyncToVideoTime(Number(timelineVideoEl.currentTime || 0));
        timelineVideoRafId = requestAnimationFrame(tick);
    };

    timelineVideoEl.onplay = () => {
        _timelineCancelRaf();
        timelineVideoRafId = requestAnimationFrame(tick);
    };
    timelineVideoEl.onpause = () => _timelineCancelRaf();
    timelineVideoEl.onended = () => _timelineCancelRaf();
    timelineVideoEl.onseeked = () => timelineSyncToVideoTime(Number(timelineVideoEl.currentTime || 0));
    timelineVideoEl.ontimeupdate = () => timelineSyncToVideoTime(Number(timelineVideoEl.currentTime || 0));
}

// State
let availableInterfaces = [];
let availableDBCs = [];
let loggingActive = false;
let appConfigCache = {};
let logStatusTimer = null;

function syncAcqButtons() {
    const isActive = !!loggingActive;
    if (btnLogStart) btnLogStart.disabled = isActive;
    if (btnLogStop) btnLogStop.disabled = !isActive;
    if (btnLogStartLogger) btnLogStartLogger.disabled = isActive;
    if (btnLogStopLogger) btnLogStopLogger.disabled = !isActive;
}

function setLoggingActive(active) {
    loggingActive = !!active;
    syncAcqButtons();
    if (btnLogsDeleteAll) btnLogsDeleteAll.disabled = loggingActive;
    const container = document.getElementById('log-files-list');
    if (container) {
        container.querySelectorAll('.btn-log-delete').forEach(b => {
            b.disabled = loggingActive;
        });
    }
}

async function refreshLoggingStatus() {
    try {
        const res = await fetch('/api/log/status', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json().catch(() => ({}));
        const active = !!data?.active;
        if (active !== !!loggingActive) {
            setLoggingActive(active);
        }
    } catch (e) {
        // ignore
    }
}

function _setBadge(el, text, level) {
    if (!el) return;
    el.textContent = String(text);
    const cls = String(level || 'secondary');
    el.className = `badge text-bg-${cls}`;
}

async function refreshSystemStats() {
    try {
        const res = await fetch('/api/system/stats', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json().catch(() => ({}));
        const t = (data?.cpu_temp_c != null) ? Number(data.cpu_temp_c) : null;
        const cpu = (data?.cpu_percent != null) ? Number(data.cpu_percent) : null;
        const ram = (data?.ram_percent != null) ? Number(data.ram_percent) : null;

        if (cpuTempEl) {
            const txt = (t == null || Number.isNaN(t)) ? 'CPU —°C' : `CPU ${t.toFixed(1)}°C`;
            const level = (t != null && !Number.isNaN(t) && t >= 80) ? 'danger' : ((t != null && !Number.isNaN(t) && t >= 70) ? 'warning' : 'secondary');
            _setBadge(cpuTempEl, txt, level);
        }
        if (cpuPctEl) {
            const txt = (cpu == null || Number.isNaN(cpu)) ? 'CPU —%' : `CPU ${cpu.toFixed(0)}%`;
            const level = (cpu != null && !Number.isNaN(cpu) && cpu >= 90) ? 'danger' : ((cpu != null && !Number.isNaN(cpu) && cpu >= 70) ? 'warning' : 'secondary');
            _setBadge(cpuPctEl, txt, level);
        }
        if (ramPctEl) {
            const txt = (ram == null || Number.isNaN(ram)) ? 'RAM —%' : `RAM ${ram.toFixed(0)}%`;
            const level = (ram != null && !Number.isNaN(ram) && ram >= 90) ? 'danger' : ((ram != null && !Number.isNaN(ram) && ram >= 75) ? 'warning' : 'secondary');
            _setBadge(ramPctEl, txt, level);
        }
    } catch (e) {
        // ignore
    }
}

async function startAcquisitionManual() {
    const formats = getSelectedFormats();
    const res = await fetch('/api/acq/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({formats})
    });
    if (!res.ok) {
        const txt = await res.text();
        throw new Error(`Start acquisition failed (${res.status}): ${txt}`);
    }
    setLoggingActive(true);
    // Confirm final backend state (covers trigger interactions / edge cases)
    refreshLoggingStatus();
}

async function stopAcquisitionManual() {
    const res = await fetch('/api/acq/stop', {method: 'POST'});
    if (!res.ok) {
        const txt = await res.text();
        throw new Error(`Stop acquisition failed (${res.status}): ${txt}`);
    }
    setLoggingActive(false);
    // Confirm final backend state (covers in-flight stop / trigger re-start races)
    refreshLoggingStatus();
}

const COCO80 = [
    'person','bicycle','car','motorcycle','airplane','bus','train','truck','boat','traffic light',
    'fire hydrant','stop sign','parking meter','bench','bird','cat','dog','horse','sheep','cow',
    'elephant','bear','zebra','giraffe','backpack','umbrella','handbag','tie','suitcase','frisbee',
    'skis','snowboard','sports ball','kite','baseball bat','baseball glove','skateboard','surfboard','tennis racket','bottle',
    'wine glass','cup','fork','knife','spoon','bowl','banana','apple','sandwich','orange',
    'broccoli','carrot','hot dog','pizza','donut','cake','chair','couch','potted plant','bed',
    'dining table','toilet','tv','laptop','mouse','remote','keyboard','cell phone','microwave','oven',
    'toaster','sink','refrigerator','book','clock','vase','scissors','teddy bear','hair drier','toothbrush'
];

// Listeners
if (modeSelect) modeSelect.addEventListener('change', () => setMode(modeSelect.value));
if (btnScanToolsScan) btnScanToolsScan.addEventListener('click', () => runScanTools('scan_obd'));
if (btnScanToolsVagScan) btnScanToolsVagScan.addEventListener('click', () => runScanTools('vag_scan_report'));
if (btnScanToolsMode06) btnScanToolsMode06.addEventListener('click', () => runScanTools('mode06'));
if (btnScanToolsSelfTest) btnScanToolsSelfTest.addEventListener('click', () => runScanTools('self_test'));
if (btnScanToolsVagDoip) btnScanToolsVagDoip.addEventListener('click', () => runScanTools('vag_doip_scan_report'));
if (btnScanToolsDoipLinkLocal) btnScanToolsDoipLinkLocal.addEventListener('click', () => runScanTools('doip_recover_network'));
if (btnScanToolsDiscovery) btnScanToolsDiscovery.addEventListener('click', () => runScanTools('discovery'));
if (btnScanToolsClear) btnScanToolsClear.addEventListener('click', () => runScanTools('clear_dtcs'));
if (btnScanToolsDoipClear) btnScanToolsDoipClear.addEventListener('click', () => runScanTools('doip_clear_dtcs'));
if (btnScanToolsDoipMode06) btnScanToolsDoipMode06.addEventListener('click', () => runScanTools('doip_mode06'));
if (btnScanToolsClearConsole) btnScanToolsClearConsole.addEventListener('click', () => {
    if (scanToolsConsole) scanToolsConsole.textContent = '';
});

if (btnLiveStart) btnLiveStart.addEventListener('click', startLiveData);
if (btnLiveStop) btnLiveStop.addEventListener('click', stopLiveData);

if (btnLogsDeleteAll) btnLogsDeleteAll.addEventListener('click', async () => {
    if (loggingActive) {
        try {
            await refreshLoggingStatus();
        } catch (e) {
            // ignore
        }
        if (loggingActive) {
            alert('Stop logging before deleting log files.');
            return;
        }
    }
    if (!confirm('Delete ALL log files? This cannot be undone.')) return;
    try {
        const resp = await fetch('/api/logs', { method: 'DELETE' });
        if (!resp.ok) {
            // If logging is active, offer to stop it and retry.
            if (resp.status === 409) {
                let payload = null;
                try {
                    payload = await resp.json();
                } catch (e) {
                    payload = null;
                }
                const isLoggingBusy = payload?.status === 'busy' && String(payload?.error || '').toLowerCase().includes('logging');
                if (isLoggingBusy) {
                    const ok = confirm('Logging is active. Stop logging and delete all log files?');
                    if (!ok) return;
                    try {
                        await fetch('/api/log/stop', { method: 'POST' });
                    } catch (e) {
                        // ignore
                    }
                    setLoggingActive(false);
                    try {
                        await refreshLoggingStatus();
                    } catch (e) {
                        // ignore
                    }
                    const retry = await fetch('/api/logs', { method: 'DELETE' });
                    if (!retry.ok) {
                        const txt2 = await retry.text();
                        alert(`Delete all failed (${retry.status}): ${txt2}`);
                        return;
                    }
                    await loadLogs();
                    return;
                }
            }

            const txt = await resp.text();
            alert(`Delete all failed (${resp.status}): ${txt}`);
            return;
        }
    } catch (e) {
        console.error(e);
        alert(`Delete all error: ${e}`);
        return;
    }
    await loadLogs();
});

// Charts (Logger mode)
const ctxEl = document.getElementById('loadChart');
const ctx = ctxEl ? ctxEl.getContext('2d') : null;
const loadChart = ctx ? new Chart(ctx, {
    type: 'line',
    data: {
        labels: [],
        datasets: [{
            label: 'Bus Load (%)',
            data: [],
            borderColor: '#198754',
            tension: 0.4
        }]
    },
    options: {
        responsive: true,
        scales: {
            y: { beginAtZero: true, max: 100 }
        },
        animation: false
    }
}) : null;

// Init
async function init() {
    await loadAppConfig();
    await loadInterfaces();
    await loadDBCs();
    restoreChannelRowsFromConfig(appConfigCache);
    await loadLogs();
    initMf4Viewer();
    initTimelineViewer();
    initHealth();
    initPowerControls();
    initProfiles();
    updateScanToolsChannelSelect();
    let initialMode = (modeSelect && modeSelect.value) ? modeSelect.value : 'logger';
    try {
        const u = new URL(window.location.href);
        const qMode = u.searchParams.get('mode');
        if (qMode && ['logger', 'scantools', 'mf4', 'settings'].includes(String(qMode))) {
            initialMode = String(qMode);
            if (modeSelect) modeSelect.value = initialMode;
        }
    } catch (_) {
        // ignore
    }
    setMode(initialMode);
    setLiveUiRunning(false);
    initWebcam();
    initYoloTrigger();
    initCustomObjects();
    initTriggerRules();
    initVideoRecordingToggle();
    initSessionBundle();
    initCanTrigger();
    initEthTrigger();
    initConfigPersistence();
    initGatewayMirrorSettings();
    setLoggingActive(false);

    // Keep UI in sync even when logging is started/stopped by triggers or other clients.
    try {
        await refreshLoggingStatus();
    } catch (e) {
        // ignore
    }
    if (!logStatusTimer) {
        logStatusTimer = setInterval(refreshLoggingStatus, 1200);
    }

    // System stats in navbar
    try {
        await refreshSystemStats();
    } catch (e) {
        // ignore
    }
    if (!sysStatsTimer) {
        sysStatsTimer = setInterval(refreshSystemStats, 2000);
    }
}

function initHealth() {
    const btn = document.getElementById('btn-health-refresh');
    if (btn) {
        btn.addEventListener('click', () => refreshHealth());
    }
}

function initPowerControls() {
    const btnReboot = document.getElementById('btn-system-reboot');
    const btnShutdown = document.getElementById('btn-system-shutdown');
    const modalEl = document.getElementById('powerModal');
    if (!btnReboot && !btnShutdown) return;
    if (!modalEl || typeof bootstrap === 'undefined') return;

    const modal = new bootstrap.Modal(modalEl, { backdrop: 'static' });

    let currentAction = null; // 'reboot' | 'shutdown'

    const setInlineMsg = (t, cls) => {
        const el = document.getElementById('power-inline-msg');
        if (!el) return;
        el.textContent = String(t || '');
        if (cls) el.className = cls;
    };

    const showPw = (show) => {
        const w = document.getElementById('power-pass-wrap');
        if (w) w.style.display = show ? '' : 'none';
    };

    const setModalText = (title, actionLine, msg, danger) => {
        const t = document.getElementById('powerModalTitle');
        const a = document.getElementById('powerModalAction');
        const m = document.getElementById('powerModalMsg');
        const c = document.getElementById('btn-power-confirm');
        if (t) t.textContent = title;
        if (a) a.textContent = actionLine;
        if (m) m.textContent = msg || '';
        if (c) {
            c.className = danger ? 'btn btn-danger' : 'btn btn-warning text-dark';
            c.textContent = 'Confirm';
        }
    };

    const openModal = (action) => {
        currentAction = action;
        const isShutdown = action === 'shutdown';
        showPw(false);
        const pw = document.getElementById('power-password');
        if (pw) pw.value = '';
        setModalText(
            isShutdown ? 'Shutdown Raspberry' : 'Reboot Raspberry',
            isShutdown ? 'Spegnere il Raspberry adesso?' : 'Riavviare il Raspberry adesso?',
            'Verrà eseguito un comando di sistema. Se necessario, verrà richiesta la password sudo.',
            true
        );
        modal.show();
    };

    async function postPower(action, sudoPassword) {
        const body = { action, confirm: true };
        if (sudoPassword) body.sudo_password = String(sudoPassword);
        const r = await fetch('/api/system/power', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
            const msg = j?.error || `HTTP ${r.status}`;
            const needPw = !!j?.need_password;
            return { ok: false, error: msg, need_password: needPw };
        }
        return j;
    }

    const btnConfirm = document.getElementById('btn-power-confirm');
    if (btnConfirm) {
        btnConfirm.addEventListener('click', async () => {
            if (!currentAction) return;
            btnConfirm.disabled = true;
            setInlineMsg('', 'small text-muted align-self-center');
            const pw = document.getElementById('power-password');
            const pwd = (pw && pw.value) ? String(pw.value) : '';

            try {
                const j = await postPower(currentAction, pwd);
                if (!j.ok) {
                    if (j.need_password) {
                        showPw(true);
                        setModalText(
                            currentAction === 'shutdown' ? 'Shutdown Raspberry' : 'Reboot Raspberry',
                            'Permessi insufficienti: inserisci password sudo',
                            String(j.error || 'sudo password required'),
                            true
                        );
                    } else {
                        setModalText(
                            currentAction === 'shutdown' ? 'Shutdown Raspberry' : 'Reboot Raspberry',
                            'Errore',
                            String(j.error || 'Request failed'),
                            true
                        );
                    }
                    return;
                }

                // Scheduled
                setInlineMsg(currentAction === 'shutdown' ? 'Shutdown requested…' : 'Reboot requested…', 'small text-warning align-self-center');
                try { btnReboot && (btnReboot.disabled = true); } catch (_) {}
                try { btnShutdown && (btnShutdown.disabled = true); } catch (_) {}
                modal.hide();
            } catch (e) {
                setModalText(
                    currentAction === 'shutdown' ? 'Shutdown Raspberry' : 'Reboot Raspberry',
                    'Errore',
                    String(e?.message || e),
                    true
                );
            } finally {
                btnConfirm.disabled = false;
            }
        });
    }

    if (btnReboot) btnReboot.addEventListener('click', () => openModal('reboot'));
    if (btnShutdown) btnShutdown.addEventListener('click', () => openModal('shutdown'));
}

function initProfiles() {
    const sel = document.getElementById('profile-select');
    const btn = document.getElementById('btn-profile-apply');
    if (!sel || !btn) return;

    // Restore from config cache if present
    try {
        const p = String(appConfigCache?.profile || '').trim();
        if (p) sel.value = p;
    } catch (_) {}

    btn.addEventListener('click', async () => {
        const profile = String(sel.value || '').trim();
        applyProfileToUi(profile);

        // Persist profile + key toggles immediately (programmatic .checked does not fire change events)
        const ethIf = document.getElementById('eth-interface');
        const ethPcap = document.getElementById('eth-pcap');
        const ethDoip = document.getElementById('eth-doip');
        const ethSomeip = document.getElementById('eth-someip');
        const ethXcp = document.getElementById('eth-xcp');
        const ethIp = document.getElementById('eth-target-ip');

        const patch = {
            profile,
            formats_default: getSelectedFormats(),
            eth_settings: {
                interface: ethIf ? String(ethIf.value || '').trim() : 'lo',
                target_ip: ethIp ? String(ethIp.value || '').trim() : '',
                pcap_enabled: !!ethPcap?.checked,
                doip_enabled: !!ethDoip?.checked,
                someip_enabled: !!ethSomeip?.checked,
                xcp_enabled: !!ethXcp?.checked,
            }
        };

        try {
            await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(patch),
            });
            appConfigCache = { ...(appConfigCache || {}), ...patch };
        } catch (_) {
            // ignore
        }
    });
}

function applyProfileToUi(profile) {
    // Minimal, safe presets (non-destructive): just set obvious toggles/fields.
    // Channel rows remain unchanged to avoid accidental hardware misconfig.
    const setChecked = (id, v) => {
        const el = document.getElementById(id);
        if (el) el.checked = !!v;
    };
    const setVal = (id, v) => {
        const el = document.getElementById(id);
        if (el) el.value = v;
    };

    if (profile === 'solo_can') {
        setChecked('eth-pcap', false);
        setChecked('eth-doip', false);
        setChecked('eth-someip', false);
        setChecked('eth-xcp', false);
        setChecked('fmt-csv', true);
        setChecked('fmt-txt', true);
        setChecked('fmt-json', false);
        setChecked('fmt-mf4', false);
    } else if (profile === 'can_doip') {
        setChecked('eth-pcap', true);
        setChecked('eth-doip', true);
        setChecked('eth-someip', false);
        setChecked('eth-xcp', false);
        setChecked('fmt-csv', true);
        setChecked('fmt-txt', true);
        setChecked('fmt-json', false);
    } else if (profile === 'solo_mf4_viewer') {
        // Nessun effetto su logging; suggerisce solo la modalità MF4.
        // L’utente può cambiare Mode manualmente.
        setChecked('eth-pcap', false);
        setChecked('eth-doip', false);
        setChecked('eth-someip', false);
        setChecked('eth-xcp', false);
    } else if (profile === 'test_sim') {
        setChecked('eth-pcap', true);
        setChecked('eth-doip', true);
        setChecked('eth-someip', true);
        setChecked('eth-xcp', true);
        setChecked('fmt-csv', true);
        setChecked('fmt-txt', true);
        setChecked('fmt-json', true);
        setChecked('fmt-mf4', true);
        setVal('eth-interface', 'lo');
        setVal('eth-target-ip', '127.0.0.1');
    }
}

async function refreshHealth() {
    const okEl = document.getElementById('health-ok');
    const writableEl = document.getElementById('health-log-writable');
    const diskEl = document.getElementById('health-disk-free');
    const depAsammdfEl = document.getElementById('health-dep-asammdf');
    const depNumpyEl = document.getElementById('health-dep-numpy');
    const errEl = document.getElementById('health-error');

    const setText = (el, text, cls) => {
        if (!el) return;
        el.textContent = text;
        if (cls) el.className = cls;
    };

    try {
        if (errEl) {
            errEl.style.display = 'none';
            errEl.textContent = '';
        }
        const res = await fetch('/api/health', { cache: 'no-store' });
        const data = await res.json();
        const ok = !!data?.ok;
        setText(okEl, ok ? 'OK' : 'NOT OK', ok ? 'text-success' : 'text-danger');

        const writable = !!data?.log_dir_writable;
        setText(writableEl, writable ? 'YES' : 'NO', writable ? 'text-success' : 'text-danger');

        const free = Number(data?.disk?.free_bytes || 0);
        const freeGb = free > 0 ? (free / (1024 ** 3)).toFixed(2) : '—';
        setText(diskEl, free > 0 ? `${freeGb} GB` : '—', 'text-muted');

        const depAsam = !!data?.deps?.asammdf;
        setText(depAsammdfEl, depAsam ? 'OK' : 'MISSING', depAsam ? 'text-success' : 'text-danger');
        const depNp = !!data?.deps?.numpy;
        setText(depNumpyEl, depNp ? 'OK' : 'MISSING', depNp ? 'text-success' : 'text-danger');

        const err = data?.log_dir_writable_error || data?.deps?.numpy_error || data?.deps?.asammdf_error;
        if (!ok && errEl) {
            errEl.textContent = String(err || 'Healthcheck failed');
            errEl.style.display = '';
        }
    } catch (e) {
        setText(okEl, 'ERROR', 'text-danger');
        if (errEl) {
            errEl.textContent = String(e);
            errEl.style.display = '';
        }
    }
}

function getChannelRowsConfig() {
    try {
        const rows = document.querySelectorAll('.channel-row');
        const channels = [];
        rows.forEach(row => {
            const id = row.querySelector('.interface-select')?.value;
            const bitrate = row.querySelector('.bitrate-select')?.value;
            const dbcSel = row.querySelector('.dbc-select');
            const dbcNames = dbcSel
                ? Array.from(dbcSel.selectedOptions || []).map(o => String(o.value || '').trim()).filter(v => v)
                : [];
            const dbc = (dbcNames && dbcNames.length) ? dbcNames[0] : '';
            if (id !== null && id !== undefined && String(id).trim() !== '') {
                channels.push({
                    id: parseInt(String(id), 10),
                    bitrate: parseInt(String(bitrate || '0'), 10) || 0,
                    // Backward compatibility:
                    // - dbc_name: single string (first selected)
                    // - dbc_names: array of selected DBCs
                    dbc_name: String(dbc || '').trim(),
                    dbc_names: Array.isArray(dbcNames) ? dbcNames : [],
                });
            }
        });
        return channels;
    } catch (e) {
        return [];
    }
}

function restoreChannelRowsFromConfig(cfgObj) {
    if (!channelsContainer) return;
    const cfg = (cfgObj && typeof cfgObj === 'object') ? cfgObj : {};
    const list = Array.isArray(cfg?.logger_channels) ? cfg.logger_channels : [];
    channelsContainer.innerHTML = '';
    if (list.length) {
        list.forEach((ch) => {
            addChannelRow({
                id: ch?.id,
                bitrate: ch?.bitrate,
                dbc_name: ch?.dbc_name,
                dbc_names: Array.isArray(ch?.dbc_names) ? ch.dbc_names : null,
            });
        });
    } else {
        addChannelRow();
    }
    // Ensure row indices are consistent
    try {
        document.querySelectorAll('.channel-row .row-idx').forEach((el, idx) => {
            el.textContent = String(idx + 1);
        });
    } catch (e) {
        // ignore
    }
}

function initEthTrigger() {
    if (!ethTrCooldownEl || !btnEthTrArm || !btnEthTrDisarm || !ethTrStatusEl) return;

    const postEthTrigger = async (payload) => {
        const res = await fetch('/api/trigger/eth', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {})
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data?.error || `Ethernet trigger request failed (${res.status})`);
        }
        return data;
    };

    if (btnEthTrApply) btnEthTrApply.addEventListener('click', async () => {
        const payload = {
            ...(ethTrArmedState ? { armed: true } : {}),
            cooldown_s: numVal(ethTrCooldownEl, 2.0),
            formats: getSelectedFormats(),
        };
        try {
            await postEthTrigger(payload);
            ethTrLastError = null;
        } catch (e) {
            ethTrLastError = String(e);
        }
        await refreshEthTrigger();
    });

    if (btnEthTrArm) btnEthTrArm.addEventListener('click', async () => {
        const payload = {
            armed: true,
            cooldown_s: numVal(ethTrCooldownEl, 2.0),
            formats: getSelectedFormats(),
        };
        try {
            await postEthTrigger(payload);
            ethTrLastError = null;
        } catch (e) {
            ethTrLastError = String(e);
        }
        await refreshEthTrigger();
    });

    if (btnEthTrDisarm) btnEthTrDisarm.addEventListener('click', async () => {
        try {
            await postEthTrigger({ armed: false });
            ethTrLastError = null;
        } catch (e) {
            ethTrLastError = String(e);
        }
        await refreshEthTrigger();
    });

    refreshEthTrigger();
    setInterval(refreshEthTrigger, 2000);
}

async function refreshEthTrigger() {
    if (!ethTrStatusEl) return;
    let data = null;
    try {
        const res = await fetch('/api/trigger/eth', { cache: 'no-store' });
        data = await res.json().catch(() => ({}));
    } catch (e) {
        data = { armed: false };
    }

    const armed = !!data?.armed;
    ethTrArmedState = armed;

    if (ethTrCooldownEl && data?.cooldown_s !== undefined && data?.cooldown_s !== null) {
        ethTrCooldownEl.value = String(data.cooldown_s);
    }

    if (ethTrLastErrorEl) {
        const err = ethTrLastError || data?.last_error;
        ethTrLastErrorEl.textContent = err ? String(err) : '—';
        ethTrLastErrorEl.className = err ? 'text-danger' : 'text-muted';
    }

    ethTrStatusEl.textContent = armed ? 'Armed (waiting)' : 'Disarmed';
    ethTrStatusEl.className = armed ? 'text-warning' : 'text-muted';
    if (btnEthTrArm) btnEthTrArm.disabled = armed;
    if (btnEthTrDisarm) btnEthTrDisarm.disabled = !armed;
}

function _clearSelectOptions(sel, placeholderText) {
    if (!sel) return;
    sel.innerHTML = '';
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = placeholderText || 'Select';
    sel.appendChild(opt);
}

function _setSelectOptions(sel, values, placeholderText) {
    _clearSelectOptions(sel, placeholderText);
    if (!sel || !Array.isArray(values)) return;
    values.forEach(v => {
        const opt = document.createElement('option');
        opt.value = String(v);
        opt.textContent = String(v);
        sel.appendChild(opt);
    });
}

function _getConfiguredChannelIds() {
    try {
        const ids = new Set();
        // channel rows exist only in Logger mode; use the interface id as channel id
        document.querySelectorAll('.channel-row .interface-select').forEach(sel => {
            const v = sel && sel.value;
            if (v !== null && v !== undefined && String(v).trim() !== '') {
                const n = parseInt(String(v), 10);
                if (!Number.isNaN(n)) ids.add(n);
            }
        });
        if (!ids.size) ids.add(0);
        return Array.from(ids).sort((a, b) => a - b);
    } catch (e) {
        return [0];
    }
}

function initCanTrigger() {
    if (!canTrDbcEl || !canTrMessageEl || !canTrSignalEl || !btnCanTrArm || !btnCanTrDisarm) return;

    const canTriggerRoot = document.getElementById('can-trigger');
    const bindDirty = (el) => {
        if (!el) return;
        ['focusin', 'pointerdown', 'keydown', 'input', 'change'].forEach(ev => {
            try {
                el.addEventListener(ev, () => _canTrMarkDirty(), { passive: true });
            } catch (_) {}
        });
    };
    [canTrChannelEl, canTrDbcEl, canTrMsgFilterEl, canTrMessageEl, canTrSigFilterEl, canTrSigGlobalEl, canTrSigGlobalResultsEl, canTrSignalEl, canTrStartOpEl, canTrStartValEl, canTrStopOpEl, canTrStopValEl, canTrAutoStopEl, canTrNoMsgStopEl]
        .forEach(bindDirty);

    const wildcardMatch = (text, pattern) => {
        const s = String(text || '').toLowerCase();
        const p = String(pattern || '').trim().toLowerCase();
        if (!p) return true;
        if (!p.includes('*')) return s.includes(p);
        const parts = p.split('*').filter(x => x.length);
        if (!parts.length) return true;
        let pos = 0;
        for (const part of parts) {
            const idx = s.indexOf(part, pos);
            if (idx < 0) return false;
            pos = idx + part.length;
        }
        return true;
    };

    const splitMsgSigQuery = (q) => {
        const raw = String(q || '').trim();
        if (!raw) return { msgQ: '', sigQ: '' };
        const i = raw.indexOf('.');
        if (i > 0 && i < raw.length - 1) {
            return { msgQ: raw.slice(0, i).trim(), sigQ: raw.slice(i + 1).trim() };
        }
        return { msgQ: '', sigQ: raw };
    };

    const clearGlobalSignalResults = () => {
        if (canTrSigGlobalResultsEl) {
            canTrSigGlobalResultsEl.innerHTML = '';
            canTrSigGlobalResultsEl.style.display = 'none';
        }
        if (canTrSigGlobalHintEl) {
            canTrSigGlobalHintEl.textContent = '';
            canTrSigGlobalHintEl.style.display = 'none';
        }
    };

    const buildSignalIndex = () => {
        const out = [];
        const msgs = canTrDescribeCache?.messages;
        if (!Array.isArray(msgs)) return out;
        for (const m of msgs) {
            const mname = String(m?.name || '');
            if (!mname) continue;
            const sigs = Array.isArray(m?.signals) ? m.signals : [];
            for (const s of sigs) {
                const sname = String(s?.name || '');
                if (!sname) continue;
                out.push({ msg: mname, sig: sname });
            }
        }
        return out;
    };

    const renderMessages = () => {
        if (!canTrMessageEl) return;
        const prev = String(canTrMessageEl.value || '');
        _clearSelectOptions(canTrMessageEl, 'Select message');
        const q = String(canTrMsgFilterEl?.value || '').trim().toLowerCase();
        const msgs = Array.isArray(canTrDescribeCache?.messages) ? canTrDescribeCache.messages : [];
        for (const m of msgs) {
            const name = String(m?.name || '');
            if (!name) continue;
            if (q && !wildcardMatch(name, q) && !wildcardMatch(String(m?.comment || ''), q) && !wildcardMatch(String(m?.frame_id ?? ''), q)) {
                continue;
            }
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            canTrMessageEl.appendChild(opt);
        }
        // keep selection if still present; otherwise fall back to placeholder
        if (prev && Array.from(canTrMessageEl.options).some(o => o.value === prev)) {
            canTrMessageEl.value = prev;
        } else {
            canTrMessageEl.value = '';
        }
    };

    const renderSignals = () => {
        _clearSelectOptions(canTrSignalEl, 'Select signal');
        const msg = String(canTrMessageEl.value || '').trim();
        if (!msg || !canTrDescribeCache || !Array.isArray(canTrDescribeCache.messages)) return;
        const m = canTrDescribeCache.messages.find(x => String(x?.name) === msg);
        const q = String(canTrSigFilterEl?.value || '').trim().toLowerCase();
        const sigs = Array.isArray(m?.signals) ? m.signals : [];
        for (const s of sigs) {
            const name = String(s?.name || '');
            if (!name) continue;
            if (q && !wildcardMatch(name, q) && !wildcardMatch(String(s?.comment || ''), q)) continue;
            const opt = document.createElement('option');
            opt.value = name;
            opt.textContent = name;
            canTrSignalEl.appendChild(opt);
        }
    };

    // Populate DBC list from existing /api/dbcs load.
    const refreshDbcSelect = () => {
        const prev = canTrDbcEl.value;
        canTrDbcEl.innerHTML = '';
        const opt0 = document.createElement('option');
        opt0.value = '';
        opt0.textContent = 'Select DBC';
        canTrDbcEl.appendChild(opt0);
        (availableDBCs || []).forEach(n => {
            const opt = document.createElement('option');
            opt.value = String(n);
            opt.textContent = String(n);
            canTrDbcEl.appendChild(opt);
        });
        if (prev) canTrDbcEl.value = prev;
    };
    refreshDbcSelect();

    // Populate channel ids from current configuration
    const refreshChannelSelect = () => {
        const ids = _getConfiguredChannelIds();
        const prev = canTrChannelEl.value;
        canTrChannelEl.innerHTML = '';
        ids.forEach(id => {
            const opt = document.createElement('option');
            opt.value = String(id);
            opt.textContent = String(id);
            canTrChannelEl.appendChild(opt);
        });
        if (prev) canTrChannelEl.value = prev;
    };
    refreshChannelSelect();

    const loadDescribe = async () => {
        canTrDescribeCache = null;
        canTrSignalIndexCache = null;
        clearGlobalSignalResults();
        _clearSelectOptions(canTrMessageEl, 'Select message');
        _clearSelectOptions(canTrSignalEl, 'Select signal');
        const dbc = String(canTrDbcEl.value || '').trim();
        if (!dbc) return;
        try {
            const res = await fetch(`/api/dbc/describe?dbc_name=${encodeURIComponent(dbc)}&include_comments=1`, { cache: 'no-store' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data?.ok) {
                canTrLastError = data?.error || `DBC describe failed (${res.status})`;
                canTrDescribeCache = null;
                return;
            }
            canTrLastError = null;
            canTrDescribeCache = data;
            canTrSignalIndexCache = buildSignalIndex();
            renderMessages();
        } catch (e) {
            canTrLastError = String(e);
            canTrDescribeCache = null;
            canTrSignalIndexCache = null;
        }
    };

    const refreshSignals = () => renderSignals();

    if (canTrDbcEl) canTrDbcEl.addEventListener('change', async () => {
        _canTrMarkDirty();
        if (canTrMsgFilterEl) canTrMsgFilterEl.value = '';
        if (canTrSigFilterEl) canTrSigFilterEl.value = '';
        if (canTrSigGlobalEl) canTrSigGlobalEl.value = '';
        await loadDescribe();
        refreshSignals();
    });
    if (canTrMessageEl) canTrMessageEl.addEventListener('change', () => {
        _canTrMarkDirty();
        refreshSignals();
    });

    if (canTrMsgFilterEl) canTrMsgFilterEl.addEventListener('input', () => {
        _canTrMarkDirty();
        renderMessages();
        refreshSignals();
    });
    if (canTrSigFilterEl) canTrSigFilterEl.addEventListener('input', () => {
        _canTrMarkDirty();
        refreshSignals();
    });

    // Global signal search (auto message)
    let canTrSigSearchTimer = null;
    const runCanTrSignalSearch = async () => {
        if (!canTrSigGlobalEl || !canTrSigGlobalResultsEl) return;
        const qRaw = String(canTrSigGlobalEl.value || '').trim();
        if (!qRaw) {
            clearGlobalSignalResults();
            return;
        }
        const { msgQ, sigQ } = splitMsgSigQuery(qRaw);
        const mq = String(msgQ || '').trim().toLowerCase();
        const sq = String(sigQ || '').trim().toLowerCase();
        const idx = Array.isArray(canTrSignalIndexCache) ? canTrSignalIndexCache : [];
        if (!idx.length) {
            clearGlobalSignalResults();
            if (canTrSigGlobalHintEl) {
                canTrSigGlobalHintEl.textContent = 'Select a DBC first.';
                canTrSigGlobalHintEl.style.display = '';
            }
            return;
        }

        const hits = [];
        for (const h of idx) {
            const m = String(h.msg || '');
            const s = String(h.sig || '');
            const mL = m.toLowerCase();
            const sL = s.toLowerCase();
            const okMsg = mq ? wildcardMatch(mL, mq) : true;
            const okSig = sq ? wildcardMatch(sL, sq) : true;
            if (okMsg && okSig) hits.push({ msg: m, sig: s });
            if (hits.length >= 120) break;
        }

        canTrSigGlobalResultsEl.innerHTML = '';
        for (const h of hits) {
            const opt = document.createElement('option');
            opt.textContent = `${h.sig} — ${h.msg}`;
            opt.dataset.msg = h.msg;
            opt.dataset.sig = h.sig;
            canTrSigGlobalResultsEl.appendChild(opt);
        }
        canTrSigGlobalResultsEl.style.display = hits.length ? '' : 'none';
        if (canTrSigGlobalHintEl) {
            canTrSigGlobalHintEl.textContent = hits.length ? `Showing ${hits.length} matches (type msg.sig to narrow).` : 'No matches.';
            canTrSigGlobalHintEl.style.display = '';
        }
    };

    if (canTrSigGlobalEl) canTrSigGlobalEl.addEventListener('input', () => {
        _canTrMarkDirty();
        if (canTrSigSearchTimer) clearTimeout(canTrSigSearchTimer);
        canTrSigSearchTimer = setTimeout(runCanTrSignalSearch, 180);
    });

    if (canTrSigGlobalResultsEl) canTrSigGlobalResultsEl.addEventListener('change', async () => {
        _canTrMarkDirty();
        const opt = canTrSigGlobalResultsEl.selectedOptions && canTrSigGlobalResultsEl.selectedOptions[0];
        const msgName = opt?.dataset?.msg || '';
        const sigName = opt?.dataset?.sig || '';
        if (!msgName || !sigName) return;

        if (canTrMsgFilterEl) canTrMsgFilterEl.value = '';
        if (canTrSigFilterEl) canTrSigFilterEl.value = '';
        renderMessages();
        canTrMessageEl.value = msgName;
        refreshSignals();
        canTrSignalEl.value = sigName;

        if (canTrSigGlobalHintEl) {
            canTrSigGlobalHintEl.textContent = `Selected: ${msgName}.${sigName}`;
            canTrSigGlobalHintEl.style.display = '';
        }
        // keep results visible for quick re-pick
    });

    const postCanTrigger = async (payload) => {
        const res = await fetch('/api/trigger/can', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {})
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data?.ok === false) {
            throw new Error(data?.error || `CAN trigger request failed (${res.status})`);
        }
        return data;
    };

    if (btnCanTrApply) btnCanTrApply.addEventListener('click', async () => {
        _canTrMarkDirty(6000);
        const formats = getSelectedFormats();
        const payload = {
            ...(canTrArmedState ? { armed: true } : {}),
            channel_id: parseInt(String(canTrChannelEl.value || '0'), 10) || 0,
            dbc_name: String(canTrDbcEl.value || '').trim(),
            message: String(canTrMessageEl.value || '').trim(),
            signal: String(canTrSignalEl.value || '').trim(),
            start_op: String(canTrStartOpEl.value || 'eq').trim(),
            start_value: String(canTrStartValEl.value || '').trim(),
            auto_stop_enabled: !!(canTrAutoStopEl && canTrAutoStopEl.checked),
            no_message_stop_s: canTrNoMsgStopEl ? (parseFloat(String(canTrNoMsgStopEl.value || '0')) || 0) : 0,
            stop_op: String(canTrStopOpEl.value || 'eq').trim(),
            stop_value: String(canTrStopValEl.value || '').trim(),
            formats,
        };
        try {
            await postCanTrigger(payload);
            canTrLastError = null;
            canTrDirtyUntilMs = 0;
        } catch (e) {
            canTrLastError = String(e);
        }
        await refreshCanTrigger();
    });

    if (btnCanTrArm) btnCanTrArm.addEventListener('click', async () => {
        _canTrMarkDirty(6000);
        const formats = getSelectedFormats();
        const payload = {
            armed: true,
            channel_id: parseInt(String(canTrChannelEl.value || '0'), 10) || 0,
            dbc_name: String(canTrDbcEl.value || '').trim(),
            message: String(canTrMessageEl.value || '').trim(),
            signal: String(canTrSignalEl.value || '').trim(),
            start_op: String(canTrStartOpEl.value || 'eq').trim(),
            start_value: String(canTrStartValEl.value || '').trim(),
            auto_stop_enabled: !!(canTrAutoStopEl && canTrAutoStopEl.checked),
            no_message_stop_s: canTrNoMsgStopEl ? (parseFloat(String(canTrNoMsgStopEl.value || '0')) || 0) : 0,
            stop_op: String(canTrStopOpEl.value || 'eq').trim(),
            stop_value: String(canTrStopValEl.value || '').trim(),
            formats,
        };
        if (!payload.dbc_name || !payload.message || !payload.signal) {
            alert('Select DBC, message and signal for CAN trigger.');
            return;
        }
        try {
            await postCanTrigger(payload);
            canTrLastError = null;
            canTrDirtyUntilMs = 0;
        } catch (e) {
            canTrLastError = String(e);
        }
        await refreshCanTrigger();
    });

    if (btnCanTrDisarm) btnCanTrDisarm.addEventListener('click', async () => {
        _canTrMarkDirty(6000);
        try {
            await postCanTrigger({ armed: false });
            canTrLastError = null;
            canTrDirtyUntilMs = 0;
        } catch (e) {
            canTrLastError = String(e);
        }
        await refreshCanTrigger();
    });

    // Initial load of message/signal lists and server state
    loadDescribe().then(() => refreshSignals());
    refreshCanTrigger();
    setInterval(refreshCanTrigger, 2000);

    // Keep channel list in sync with configuration changes
    document.addEventListener('change', (ev) => {
        const t = ev.target;
        if (t && t.classList && t.classList.contains('interface-select')) {
            refreshChannelSelect();
        }
    });
}

async function refreshCanTrigger() {
    if (!canTrStatusEl) return;
    let data = null;
    try {
        const res = await fetch('/api/trigger/can', { cache: 'no-store' });
        data = await res.json().catch(() => ({}));
    } catch (e) {
        data = { armed: false };
    }

    const armed = !!data?.armed;
    canTrArmedState = armed;

    if (canTrLastErrorEl) {
        const err = canTrLastError || data?.last_error;
        canTrLastErrorEl.textContent = err ? String(err) : '—';
        canTrLastErrorEl.className = err ? 'text-danger' : 'text-muted';
    }

    if (canTrStatusEl) {
        canTrStatusEl.textContent = armed ? 'Armed (waiting)' : 'Disarmed';
        canTrStatusEl.className = armed ? 'text-warning' : 'text-muted';
    }
    if (btnCanTrArm) btnCanTrArm.disabled = armed;
    if (btnCanTrDisarm) btnCanTrDisarm.disabled = !armed;

    // Don't overwrite user selections while they're interacting with the form.
    try {
        const root = document.getElementById('can-trigger');
        const ae = document.activeElement;
        const activeInside = !!(root && ae && root.contains(ae));
        if (_canTrIsDirty() || activeInside) {
            return;
        }
    } catch (_) {
        // ignore
    }

    // Apply server state into UI (best-effort)
    try {
        // Ensure filters don't hide server-selected values.
        if (canTrMsgFilterEl) canTrMsgFilterEl.value = '';
        if (canTrSigFilterEl) canTrSigFilterEl.value = '';
        if (canTrSigGlobalEl) canTrSigGlobalEl.value = '';
        if (canTrSigGlobalResultsEl) canTrSigGlobalResultsEl.style.display = 'none';
        if (canTrSigGlobalHintEl) canTrSigGlobalHintEl.style.display = 'none';

        if (canTrChannelEl && data?.channel_id !== undefined && data?.channel_id !== null) {
            canTrChannelEl.value = String(data.channel_id);
        }
        if (canTrDbcEl && typeof data?.dbc_name === 'string') {
            if (canTrDbcEl.value !== data.dbc_name) {
                canTrDbcEl.value = data.dbc_name;
                canTrDescribeCache = null;
                canTrSignalIndexCache = null;
                await (async () => {
                    const dbc = String(canTrDbcEl.value || '').trim();
                    if (!dbc) return;
                    const res = await fetch(`/api/dbc/describe?dbc_name=${encodeURIComponent(dbc)}&include_comments=1`, { cache: 'no-store' });
                    const j = await res.json().catch(() => ({}));
                    if (res.ok && j?.ok) {
                        canTrDescribeCache = j;
                        const names = Array.isArray(j?.messages) ? j.messages.map(m => m?.name).filter(Boolean) : [];
                        _setSelectOptions(canTrMessageEl, names, 'Select message');
                        // rebuild index (minimal)
                        try {
                            const msgs = Array.isArray(j?.messages) ? j.messages : [];
                            const out = [];
                            for (const m of msgs) {
                                const mn = String(m?.name || '');
                                if (!mn) continue;
                                const sigs = Array.isArray(m?.signals) ? m.signals : [];
                                for (const s of sigs) {
                                    const sn = String(s?.name || '');
                                    if (!sn) continue;
                                    out.push({ msg: mn, sig: sn });
                                }
                            }
                            canTrSignalIndexCache = out;
                        } catch (_) {
                            canTrSignalIndexCache = null;
                        }
                    }
                })();
            }
        }
        if (canTrMessageEl && typeof data?.message === 'string') {
            canTrMessageEl.value = data.message;
            // refresh signals
            if (canTrDescribeCache && Array.isArray(canTrDescribeCache.messages)) {
                const m = canTrDescribeCache.messages.find(x => String(x?.name) === String(data.message));
                const sigs = Array.isArray(m?.signals) ? m.signals.map(s => s?.name).filter(Boolean) : [];
                _setSelectOptions(canTrSignalEl, sigs, 'Select signal');
            }
        }
        if (canTrSignalEl && typeof data?.signal === 'string') canTrSignalEl.value = data.signal;
        if (canTrStartOpEl && typeof data?.start_op === 'string') canTrStartOpEl.value = data.start_op;
        if (canTrStopOpEl && typeof data?.stop_op === 'string') canTrStopOpEl.value = data.stop_op;
        if (canTrStartValEl && data?.start_value !== undefined) canTrStartValEl.value = String(data.start_value);
        if (canTrStopValEl && data?.stop_value !== undefined) canTrStopValEl.value = String(data.stop_value);
        if (canTrAutoStopEl && data?.auto_stop_enabled !== undefined) canTrAutoStopEl.checked = !!data.auto_stop_enabled;
        if (canTrNoMsgStopEl && data?.no_message_stop_s !== undefined && data?.no_message_stop_s !== null) {
            canTrNoMsgStopEl.value = String(data.no_message_stop_s);
        }
    } catch (e) {
        // ignore
    }
}

async function loadAppConfig() {
    let data = null;
    try {
        const res = await fetch('/api/config', { cache: 'no-store' });
        data = await res.json();
    } catch (e) {
        data = null;
    }
    const cfg = data?.config && typeof data.config === 'object' ? data.config : {};
    appConfigCache = cfg;
    // Default formats
    if (Array.isArray(cfg?.formats_default)) {
        const set = new Set(cfg.formats_default.map(s => String(s).toLowerCase()));
        const map = {
            'csv': 'fmt-csv',
            'txt': 'fmt-txt',
            'json': 'fmt-json',
            'mf4': 'fmt-mf4',
        };
        Object.entries(map).forEach(([fmt, id]) => {
            const el = document.getElementById(id);
            if (el) el.checked = set.has(fmt);
        });
    }

    // MF4: include decoded channels
    try {
        const el = document.getElementById('mf4-include-decoded');
        if (el) {
            // Default ON if missing (legacy behavior).
            el.checked = (cfg?.mf4_include_decoded !== undefined) ? !!cfg.mf4_include_decoded : true;
        }
    } catch (e) {
        // ignore
    }

    // Restore Ethernet settings (standalone)
    try {
        const es = (cfg && typeof cfg.eth_settings === 'object') ? cfg.eth_settings : null;
        if (es) {
            const ethIf = document.getElementById('eth-interface');
            const ethPcap = document.getElementById('eth-pcap');
            const ethDoip = document.getElementById('eth-doip');
            const ethSomeip = document.getElementById('eth-someip');
            const ethXcp = document.getElementById('eth-xcp');
            const ethIp = document.getElementById('eth-target-ip');
            if (ethIf && typeof es.interface === 'string') ethIf.value = es.interface;
            if (ethIp && typeof es.target_ip === 'string') ethIp.value = es.target_ip;
            if (ethPcap && es.pcap_enabled !== undefined) ethPcap.checked = !!es.pcap_enabled;
            if (ethDoip && es.doip_enabled !== undefined) ethDoip.checked = !!es.doip_enabled;
            if (ethSomeip && es.someip_enabled !== undefined) ethSomeip.checked = !!es.someip_enabled;
            if (ethXcp && es.xcp_enabled !== undefined) ethXcp.checked = !!es.xcp_enabled;
        }
    } catch (e) {
        // ignore
    }

    // Enforce system mode coherence in the UI (prevents accidental drift)
    try {
        const smRaw = (cfg && typeof cfg.system_mode === 'string') ? cfg.system_mode : '';
        const sm = String(smRaw || '').trim().toLowerCase();
        const es = (cfg && typeof cfg.eth_settings === 'object') ? cfg.eth_settings : null;
        const iface = es && typeof es.interface === 'string' ? es.interface : '';
        const inferred = sm || ((String(iface || '').trim() === 'lo') ? 'simulation' : 'real');

        const ethIf = document.getElementById('eth-interface');
        const ethPcap = document.getElementById('eth-pcap');
        if (ethIf && ethPcap && (sm === 'real' || sm === 'simulation')) {
            if (inferred === 'simulation') {
                ethIf.value = 'lo';
                ethPcap.checked = true;
            } else {
                ethIf.value = 'eth0';
                ethPcap.checked = false;
            }
            ethIf.disabled = true;
            ethPcap.disabled = true;
        } else {
            // Unlocked (legacy / no system_mode)
            if (ethIf) ethIf.disabled = false;
            if (ethPcap) ethPcap.disabled = false;
        }
    } catch (e) {
        // ignore
    }

    // Restore storage/output directory
    try {
        const storage = (cfg && typeof cfg.storage === 'object') ? cfg.storage : null;
        const outDirEl = document.getElementById('storage-output-dir');
        if (outDirEl) outDirEl.value = (storage && typeof storage.output_dir === 'string') ? storage.output_dir : '';
    } catch (e) {
        // ignore
    }
    // Restore MF4 chunk size (MB)
    try {
        const el = document.getElementById('mf4-chunk-size-mb');
        if (el) {
            const v = (cfg && cfg.mf4_chunk_size_mb !== undefined && cfg.mf4_chunk_size_mb !== null) ? Number(cfg.mf4_chunk_size_mb) : 100;
            if (Number.isFinite(v) && v > 0) el.value = String(Math.round(v));
            else el.value = '100';
            try { el.classList.remove('is-invalid'); } catch (e) { /* ignore */ }
        }
        const msg = document.getElementById('mf4-chunk-size-mb-msg');
        if (msg) msg.textContent = '';
    } catch (e) {
        // ignore
    }
}

function debounceSaveConfig(patch) {
    if (saveCfgTimer) clearTimeout(saveCfgTimer);
    saveCfgTimer = setTimeout(async () => {
        try {
            await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(patch)
            });
        } catch (e) {
            // ignore
        }
    }, 400);
}

function initConfigPersistence() {
    // Persist default formats
    ['fmt-csv', 'fmt-txt', 'fmt-json', 'fmt-mf4'].forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('change', () => {
            debounceSaveConfig({ formats_default: getSelectedFormats() });
        });
    });

    // Persist MF4 decoded-channel selection
    const mf4Dec = document.getElementById('mf4-include-decoded');
    if (mf4Dec) {
        mf4Dec.addEventListener('change', () => {
            debounceSaveConfig({ mf4_include_decoded: !!mf4Dec.checked });
        });
    }
    // Persist MF4 chunk size (MB)
    const mf4ChunkMb = document.getElementById('mf4-chunk-size-mb');
    const mf4ChunkMbInvalid = document.getElementById('mf4-chunk-size-mb-invalid');
    const mf4ChunkMbMsg = document.getElementById('mf4-chunk-size-mb-msg');
    let mf4ChunkMsgTimer = null;

    const setMf4ChunkValidity = (isValid, message) => {
        if (!mf4ChunkMb) return;
        try {
            if (isValid) mf4ChunkMb.classList.remove('is-invalid');
            else mf4ChunkMb.classList.add('is-invalid');
        } catch (e) {
            // ignore
        }

        if (mf4ChunkMbInvalid) {
            if (!isValid && message) mf4ChunkMbInvalid.textContent = String(message);
            else mf4ChunkMbInvalid.textContent = 'Inserisci un valore valido (1–4096 MB).';
        }
    };

    const setMf4ChunkMsg = (text) => {
        if (!mf4ChunkMbMsg) return;
        if (mf4ChunkMsgTimer) {
            try { clearTimeout(mf4ChunkMsgTimer); } catch (e) { /* ignore */ }
            mf4ChunkMsgTimer = null;
        }
        mf4ChunkMbMsg.textContent = String(text || '');
        if (text) {
            mf4ChunkMsgTimer = setTimeout(() => {
                mf4ChunkMbMsg.textContent = '';
                mf4ChunkMsgTimer = null;
            }, 3000);
        }
    };

    const persistMf4ChunkMb = () => {
        if (!mf4ChunkMb) return;
        const raw = String(mf4ChunkMb.value || '').trim();
        if (!raw) {
            setMf4ChunkValidity(false, 'Inserisci un valore (1–4096 MB).');
            return;
        }
        const v = Number(raw);
        if (!Number.isFinite(v)) {
            setMf4ChunkValidity(false, 'Valore non valido. Inserisci un numero (1–4096 MB).');
            return;
        }

        const rounded = Math.round(v);
        const clamped = Math.max(1, Math.min(4096, rounded));

        if (clamped !== rounded) {
            try { mf4ChunkMb.value = String(clamped); } catch (e) { /* ignore */ }
            setMf4ChunkMsg(`Valore limitato a ${clamped} MB.`);
        } else {
            setMf4ChunkMsg('');
        }

        setMf4ChunkValidity(true);
        debounceSaveConfig({ mf4_chunk_size_mb: clamped });
    };
    if (mf4ChunkMb) {
        mf4ChunkMb.addEventListener('change', persistMf4ChunkMb);
        mf4ChunkMb.addEventListener('input', persistMf4ChunkMb);
    }

    // Persist channel configuration (interfaces/bitrates/DBCs)
    if (channelsContainer) {
        channelsContainer.addEventListener('change', (ev) => {
            const t = ev.target;
            if (!t || !t.classList) return;
            if (t.classList.contains('interface-select') || t.classList.contains('bitrate-select') || t.classList.contains('dbc-select')) {
                debounceSaveConfig({ logger_channels: getChannelRowsConfig() });
            }
        });
    }

    if (btnChannelsReset) {
        btnChannelsReset.addEventListener('click', async () => {
            try {
                await fetch('/api/config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ logger_channels: [] })
                });
            } catch (e) {
                // ignore
            }
            appConfigCache = { ...(appConfigCache || {}), logger_channels: [] };
            restoreChannelRowsFromConfig(appConfigCache);
            updateScanToolsChannelSelect();
        });
    }

    // Persist Ethernet settings for standalone mode
    const ethIf = document.getElementById('eth-interface');
    const ethPcap = document.getElementById('eth-pcap');
    const ethDoip = document.getElementById('eth-doip');
    const ethSomeip = document.getElementById('eth-someip');
    const ethXcp = document.getElementById('eth-xcp');
    const ethIp = document.getElementById('eth-target-ip');
    const persistEth = () => {
        debounceSaveConfig({
            eth_settings: {
                interface: ethIf ? String(ethIf.value || '').trim() : 'lo',
                target_ip: ethIp ? String(ethIp.value || '').trim() : '',
                pcap_enabled: !!ethPcap?.checked,
                doip_enabled: !!ethDoip?.checked,
                someip_enabled: !!ethSomeip?.checked,
                xcp_enabled: !!ethXcp?.checked,
            }
        });
    };
    [ethIf, ethPcap, ethDoip, ethSomeip, ethXcp, ethIp].forEach((el) => {
        if (!el) return;
        el.addEventListener('change', persistEth);
        el.addEventListener('input', persistEth);
    });

    // Persist storage/output directory
    const outDirEl = document.getElementById('storage-output-dir');
    const persistStorage = () => {
        debounceSaveConfig({
            storage: {
                output_dir: outDirEl ? String(outDirEl.value || '').trim() : ''
            }
        });
    };
    if (outDirEl) {
        outDirEl.addEventListener('change', persistStorage);
        outDirEl.addEventListener('input', persistStorage);
    }
}

function initSessionBundle() {
    if (!btnSessionBundleLatest) return;
    btnSessionBundleLatest.addEventListener('click', async () => {
        try {
            const res = await fetch('/api/sessions', { cache: 'no-store' });
            const data = await res.json();
            const sessions = Array.isArray(data?.sessions) ? data.sessions : [];
            if (!sessions.length) {
                alert('No sessions found yet. Start/stop a log first.');
                return;
            }
            const base = sessions[0]?.base;
            if (!base) {
                alert('No session base found.');
                return;
            }
            window.location.href = `/api/session/bundle?base=${encodeURIComponent(base)}`;
        } catch (e) {
            alert(`Bundle error: ${e}`);
        }
    });
}

function timelineSetStatus(text, isError = false) {
    if (!timelineStatusEl) return;
    timelineStatusEl.textContent = String(text || '');
    timelineStatusEl.classList.toggle('text-danger', !!isError);
    timelineStatusEl.classList.toggle('text-muted', !isError);
}

function timelineGetMode() {
    return timelineModeEl ? String(timelineModeEl.value || 'review') : 'review';
}

function timelineGetSelectedSignals() {
    if (!timelineSignalsEl) return [];
    const out = [];
    Array.from(timelineSignalsEl.selectedOptions || []).forEach(o => {
        const v = String(o.value || '').trim();
        if (v) out.push(v);
    });
    return out;
}

function timelineEnforceMaxSignals(maxN = 10) {
    if (!timelineSignalsEl) return;
    const sel = Array.from(timelineSignalsEl.selectedOptions || []);
    if (sel.length <= maxN) return;

    // Keep first maxN selected.
    const keep = new Set(sel.slice(0, maxN).map(o => o.value));
    Array.from(timelineSignalsEl.options || []).forEach(o => {
        if (o.selected && !keep.has(o.value)) o.selected = false;
    });
}

function timelineUpdateSignalCount() {
    if (!timelineSignalCountEl) return;
    const n = timelineGetSelectedSignals().length;
    timelineSignalCountEl.textContent = `${n} / 10`;
}

function timelineGetSelectedDbcs() {
    // Same semantics as MF4 viewer: if placeholder "" is selected => Auto.
    const out = { auto: true, dbcs: [] };
    if (!timelineDbcEl) return out;

    const selected = Array.from(timelineDbcEl.selectedOptions || []).map(o => String(o.value || ''));
    if (selected.includes('')) {
        return { auto: true, dbcs: [] };
    }
    const dbcs = selected.map(s => s.trim()).filter(Boolean);
    return { auto: false, dbcs };
}

async function timelineLoadSessions() {
    if (!timelineSessionEl) return;
    timelineSessionEl.innerHTML = '';
    const ph = document.createElement('option');
    ph.value = '';
    ph.text = 'Select a session...';
    ph.disabled = true;
    ph.selected = true;
    timelineSessionEl.appendChild(ph);

    try {
        const res = await fetch('/api/sessions', { cache: 'no-store' });
        const data = await res.json().catch(() => ({}));
        const sessions = Array.isArray(data?.sessions) ? data.sessions : [];
        sessions.forEach(s => {
            const base = String(s?.base || '').trim();
            if (!base) return;
            const opt = document.createElement('option');
            opt.value = base;
            opt.text = base;
            timelineSessionEl.appendChild(opt);
        });
    } catch (e) {
        // ignore
    }
}

function timelineFillDbcSelect() {
    if (!timelineDbcEl) return;
    const prev = new Set(Array.from(timelineDbcEl.selectedOptions || []).map(o => String(o.value || '')));
    timelineDbcEl.innerHTML = '';
    const autoOpt = document.createElement('option');
    autoOpt.value = '';
    autoOpt.text = 'Auto (use config mapping per channel)';
    autoOpt.selected = prev.size === 0 || prev.has('');
    timelineDbcEl.appendChild(autoOpt);
    (availableDBCs || []).forEach(d => {
        const name = String(d || '').trim();
        if (!name) return;
        const opt = document.createElement('option');
        opt.value = name;
        opt.text = name;
        opt.selected = prev.has(name);
        timelineDbcEl.appendChild(opt);
    });
}

async function timelineFillChannelSelectForFile(mf4File) {
    if (!timelineChannelEl) return;
    timelineChannelEl.innerHTML = '';

    const anyOpt = document.createElement('option');
    anyOpt.value = '';
    anyOpt.text = 'Any';
    anyOpt.selected = true;
    timelineChannelEl.appendChild(anyOpt);

    if (!mf4File) return;
    try {
        const res = await fetch(`/api/mf4/raw_channels?file=${encodeURIComponent(mf4File)}`, { cache: 'no-store' });
        const data = await res.json().catch(() => ({}));
        const chans = Array.isArray(data?.channels) ? data.channels : [];
        chans.forEach(ch => {
            const id = String(ch?.id ?? '').trim();
            if (!id) return;
            const opt = document.createElement('option');
            opt.value = id;
            opt.text = `${id}${ch?.count != null ? ` (${ch.count})` : ''}`;
            timelineChannelEl.appendChild(opt);
        });
    } catch (e) {
        // ignore
    }
}

function timelineSetVideoModeReview() {
    if (timelineLiveImgEl) timelineLiveImgEl.style.display = 'none';
    if (timelineVideoEl) timelineVideoEl.style.display = 'block';
    timelineLiveEnabled = false;
    timelineLiveState = null;
    if (timelineLiveFlushTimer) {
        clearInterval(timelineLiveFlushTimer);
        timelineLiveFlushTimer = null;
    }
    _timelineCancelRaf();
}

function timelineSetVideoModeLive() {
    if (timelineVideoEl) {
        timelineVideoEl.pause?.();
        timelineVideoEl.removeAttribute('src');
        timelineVideoEl.load?.();
        timelineVideoEl.style.display = 'none';
    }
    if (timelineLiveImgEl) {
        timelineLiveImgEl.style.display = '';
        timelineLiveImgEl.src = `/api/camera/stream?t=${Date.now()}`;
    }
    timelineLiveEnabled = true;
    timelineReviewTracePairs = null;
    _timelineCancelRaf();
}

async function timelineLoadSelectedSession() {
    if (!timelineSessionEl) return;
    const base = String(timelineSessionEl.value || '').trim();
    if (!base) return;

    timelineSetStatus('Loading session...');
    timelineManifest = null;
    timelineReviewBaseEpochS = null;
    timelineReviewFile = null;
    timelinePlotReady = false;
    if (btnTimelinePlot) btnTimelinePlot.disabled = true;
    if (timelineSignalsEl) timelineSignalsEl.innerHTML = '';
    timelineUpdateSignalCount();
    if (timelineValuesEl) timelineValuesEl.textContent = '';

    try {
        const res = await fetch(`/api/session/manifest?base=${encodeURIComponent(base)}`, { cache: 'no-store' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data?.ok) {
            throw new Error(data?.error || `HTTP ${res.status}`);
        }
        timelineManifest = data;
        timelineReviewFile = data?.files?.mf4 || null;

        // Determine a base epoch for sync.
        const ve = (data?.video?.start_epoch_s_effective != null) ? Number(data.video.start_epoch_s_effective) : null;
        timelineReviewBaseEpochS = (ve != null && !Number.isNaN(ve)) ? ve : null;

        if (timelineGetMode() === 'live') {
            timelineSetVideoModeLive();
        } else {
            timelineSetVideoModeReview();
            const mp4 = data?.files?.mp4;
            if (timelineVideoEl && mp4) {
                timelineVideoEl.src = `/api/video/file?base=${encodeURIComponent(base)}&t=${Date.now()}`;
                timelineVideoEl.load();
            }
        }

        await timelineFillChannelSelectForFile(timelineReviewFile);
        await timelineRefreshSignals();

        const syncSrc = String(data?.video?.sync_source || '').trim();
        if (timelineReviewBaseEpochS == null) {
            timelineSetStatus('Loaded. Missing sync timestamp; start a new session for perfect video sync.', true);
        } else if (syncSrc && syncSrc !== 'meta') {
            timelineSetStatus('Loaded. Sync is approximate (no video_start metadata).', true);
        } else {
            timelineSetStatus('Loaded');
        }
    } catch (e) {
        console.error(e);
        timelineSetStatus(`Load error: ${e}`, true);
    }
}

async function timelineRefreshSignals() {
    if (!timelineSignalsEl) return;
    timelineSignalsEl.innerHTML = '';
    if (btnTimelinePlot) btnTimelinePlot.disabled = true;

    const mode = timelineGetMode();
    const { auto, dbcs } = timelineGetSelectedDbcs();
    const chRaw = timelineChannelEl ? String(timelineChannelEl.value || '').trim() : '';
    const ch = chRaw === '' ? null : Number(chRaw);

    if (mode === 'review') {
        if (!timelineReviewFile) {
            timelineSetStatus('No MF4 for this session', true);
            return;
        }

        const qs = new URLSearchParams();
        qs.set('file', timelineReviewFile);
        if (ch !== null && !Number.isNaN(ch)) qs.set('channel', String(ch));
        if (auto && dbcs.length === 0) {
            qs.set('auto', '1');
        } else {
            dbcs.forEach(d => qs.append('dbc', d));
        }
        try {
            const decoded = await mf4FetchJson(`/api/mf4/decoded_signals?${qs.toString()}`);
            if (!decoded?.ok || !Array.isArray(decoded.groups)) {
                timelineSetStatus(decoded?.error ? String(decoded.error) : 'Failed to load decoded signals', true);
                return;
            }
            decoded.groups.forEach((g) => {
                const msg = String(g?.message || '').trim();
                const sg = Array.isArray(g?.signals) ? g.signals : [];
                if (!msg || sg.length === 0) return;
                const og = document.createElement('optgroup');
                og.label = msg;
                sg.forEach((s) => {
                    const key = String(s?.key || '').trim();
                    if (!key) return;
                    const parts = key.split('.', 2);
                    const sigLabel = (parts.length === 2) ? parts[1] : key;
                    const opt = document.createElement('option');
                    opt.value = key;
                    opt.text = sigLabel;
                    opt.title = key;
                    og.appendChild(opt);
                });
                timelineSignalsEl.appendChild(og);
            });
            timelineUpdateSignalCount();
            timelineSetStatus('Signals loaded');
        } catch (e) {
            console.error(e);
            timelineSetStatus(`Signals error: ${e}`, true);
        }
        return;
    }

    // Live mode: list from selected DBC(s) (or config mapping via Auto is not supported here).
    if (auto && dbcs.length === 0) {
        timelineSetStatus('Live: select one or more DBCs (Auto not supported)', true);
        return;
    }
    const keys = new Set();
    for (const dbc of dbcs) {
        try {
            const r = await fetch(`/api/dbc/describe?dbc_name=${encodeURIComponent(dbc)}`, { cache: 'no-store' });
            const j = await r.json().catch(() => ({}));
            if (!j?.ok || !Array.isArray(j?.messages)) continue;
            (j.messages || []).forEach(m => {
                const msg = String(m?.name || '').trim();
                const sigs = Array.isArray(m?.signals) ? m.signals : [];
                if (!msg) return;
                sigs.forEach(s => {
                    const sn = String(s?.name || '').trim();
                    if (!sn) return;
                    keys.add(`${msg}.${sn}`);
                });
            });
        } catch (e) {
            // ignore
        }
    }
    Array.from(keys).sort().forEach(k => {
        const opt = document.createElement('option');
        opt.value = k;
        opt.text = k;
        timelineSignalsEl.appendChild(opt);
    });
    timelineUpdateSignalCount();
    timelineSetStatus('Signals loaded');
}

async function timelinePlot() {
    const mode = timelineGetMode();
    const selected = timelineGetSelectedSignals();
    if (selected.length === 0) {
        timelineSetStatus('Select at least one signal', true);
        return;
    }
    if (selected.length > 10) {
        timelineEnforceMaxSignals(10);
    }

    if (!timelinePlotAreaEl || typeof Plotly === 'undefined') {
        timelineSetStatus('Plotly not available', true);
        return;
    }

    if (mode === 'review') {
        if (!timelineManifest || !timelineReviewFile) {
            timelineSetStatus('Load a session first', true);
            return;
        }
        const baseEpoch = timelineReviewBaseEpochS;
        if (baseEpoch == null || Number.isNaN(Number(baseEpoch))) {
            timelineSetStatus('Missing sync timestamp for this session. Start a new session and retry.', true);
            return;
        }

        const { auto, dbcs } = timelineGetSelectedDbcs();
        const chRaw = timelineChannelEl ? String(timelineChannelEl.value || '').trim() : '';
        const ch = (chRaw === '') ? null : Number(chRaw);

        timelineSetStatus('Fetching data...');
        try {
            const info = await mf4GetFileInfo(timelineReviewFile);
            const isRawCan = (info && info.ok && String(info.kind) === 'raw_can');
            const wantDecoded = isRawCan && (Array.isArray(dbcs) && dbcs.length > 0 || !!auto);

            const payloadDecoded = {
                file: timelineReviewFile,
                dbcs,
                auto,
                channel: (ch === null || Number.isNaN(ch)) ? null : ch,
                signals: selected,
                max_points: 6000,
                t_mode: 'abs',
            };
            const payloadDirect = {
                file: timelineReviewFile,
                signals: selected,
                max_points: 6000,
                t_mode: 'abs',
            };

            const payload = wantDecoded ? payloadDecoded : payloadDirect;
            // If video duration known, window to [start, start+duration].
            if (timelineVideoEl && timelineVideoEl.duration && Number.isFinite(timelineVideoEl.duration)) {
                payload.start_abs_s = baseEpoch;
                payload.end_abs_s = baseEpoch + Number(timelineVideoEl.duration);
            }

            let j = null;
            try {
                j = await mf4FetchJson(wantDecoded ? '/api/mf4/decoded_data' : '/api/mf4/data', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            } catch (e) {
                const msg = String(e || '');
                // Auto-recover: measured/decoded MF4 has no raw CAN table.
                if (wantDecoded && msg.includes('mf4 non contiene tabella CAN raw')) {
                    timelineSetStatus('MF4 “misurato/decodificato”: plot dei canali diretti (no RAW CAN).');
                    j = await mf4FetchJson('/api/mf4/data', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payloadDirect),
                    });
                } else {
                    throw e;
                }
            }
            const series = Array.isArray(j?.series) ? j.series : [];
            const traces = [];
            const pairs = [];
            series.forEach(s => {
                const name = String(s?.name || '').trim();
                const t = Array.isArray(s?.t) ? s.t : [];
                const y = Array.isArray(s?.y) ? s.y : [];
                if (!name || t.length === 0 || y.length === 0) return;
                const x = t.map(v => Number(v) - Number(baseEpoch));
                const lineIdx = traces.length;
                traces.push({
                    name,
                    x,
                    y,
                    mode: 'lines',
                    type: 'scattergl',
                });

                // Add a marker dot that we will move with the video playhead.
                const markerIdx = traces.length;
                traces.push({
                    name: `${name} •`,
                    x: [0],
                    y: [Number(y[0])],
                    mode: 'markers',
                    type: 'scattergl',
                    marker: { size: 7 },
                    hoverinfo: 'skip',
                    showlegend: false,
                });
                pairs.push({ lineIdx, markerIdx });
            });

            const layout = {
                margin: { l: 50, r: 10, t: 10, b: 40 },
                xaxis: { title: 't (s) from video start', zeroline: false },
                yaxis: { zeroline: false },
                legend: { orientation: 'h' },
                shapes: [{
                    type: 'line',
                    x0: (timelineVideoEl && Number.isFinite(timelineVideoEl.currentTime)) ? Number(timelineVideoEl.currentTime) : 0,
                    x1: (timelineVideoEl && Number.isFinite(timelineVideoEl.currentTime)) ? Number(timelineVideoEl.currentTime) : 0,
                    y0: 0,
                    y1: 1,
                    xref: 'x',
                    yref: 'paper',
                    line: { color: '#ffc107', width: 2 },
                }],
            };
            const config = { responsive: true, displaylogo: false };
            await Plotly.newPlot(timelinePlotAreaEl, traces, layout, config);
            timelinePlotReady = true;
            timelineReviewTracePairs = pairs;
            timelineSetStatus('Plotted');

            // Keep plot cursor + marker dots synced to video time.
            timelineAttachVideoSyncHandlers();
            if (timelineVideoEl) {
                timelineSyncToVideoTime(Number(timelineVideoEl.currentTime || 0));
            }

            // Allow clicking the plot to seek the video.
            try {
                if (timelinePlotAreaEl && typeof timelinePlotAreaEl.on === 'function' && timelineVideoEl) {
                    timelinePlotAreaEl.removeAllListeners?.('plotly_click');
                    timelinePlotAreaEl.on('plotly_click', (ev) => {
                        try {
                            const pt = ev?.points?.[0];
                            const x = (pt && pt.x != null) ? Number(pt.x) : null;
                            if (x == null || !Number.isFinite(x)) return;
                            timelineVideoEl.currentTime = Math.max(0, x);
                            timelineVideoEl.pause?.();
                            timelineSyncToVideoTime(Number(timelineVideoEl.currentTime || 0));
                        } catch (_) {
                            // ignore
                        }
                    });
                }
            } catch (_) {
                // ignore
            }
        } catch (e) {
            console.error(e);
            timelineSetStatus(`Plot error: ${e}`, true);
        }
        return;
    }

    // Live mode
    timelineSetStatus('Live plotting...');
    timelineSetVideoModeLive();
    timelineLiveState = {
        t0: Date.now() / 1000.0,
        keys: selected.slice(0, 10),
        traces: new Map(),
        pending: new Map(),
    };

    const traces = timelineLiveState.keys.map((k) => ({ name: k, x: [], y: [], mode: 'lines', type: 'scattergl' }));
    timelineLiveState.keys.forEach((k, idx) => timelineLiveState.traces.set(k, idx));

    await Plotly.newPlot(
        timelinePlotAreaEl,
        traces,
        {
            margin: { l: 50, r: 10, t: 10, b: 40 },
            xaxis: { title: 't (s) from live start', zeroline: false },
            yaxis: { zeroline: false },
            legend: { orientation: 'h' },
        },
        { responsive: true, displaylogo: false }
    );
    timelinePlotReady = true;

    if (!timelineLiveFlushTimer) {
        timelineLiveFlushTimer = setInterval(() => {
            timelineFlushLive();
        }, 250);
    }
}

function timelineUpdateValuesAtTime(tSec) {
    if (!timelineValuesEl || !timelinePlotAreaEl) return;
    const gd = timelinePlotAreaEl;
    const data = gd?.data;
    if (!Array.isArray(data) || data.length === 0) return;
    const lines = [];
    data.forEach(tr => {
        const name = String(tr?.name || '').trim();
        const x = Array.isArray(tr?.x) ? tr.x : [];
        const y = Array.isArray(tr?.y) ? tr.y : [];
        if (!name || x.length === 0 || y.length === 0) return;
        // nearest point
        let bestI = 0;
        let bestD = Infinity;
        for (let i = 0; i < x.length; i++) {
            const dx = Math.abs(Number(x[i]) - Number(tSec));
            if (dx < bestD) {
                bestD = dx;
                bestI = i;
            }
        }
        const val = y[bestI];
        lines.push(`${name}: ${Number(val).toFixed(3)}`);
    });
    timelineValuesEl.textContent = lines.join('   |   ');
}

function timelineOnBusFrame(frame) {
    if (!timelineLiveEnabled || !timelineLiveState || !frame) return;
    if (!frame.decoded || !frame.decoded.name || !frame.decoded.signals) return;
    const msg = String(frame.decoded.name || '').trim();
    const sigs = frame.decoded.signals || {};
    const tsMs = Number(frame.timestamp || 0);
    const t = (tsMs / 1000.0) - Number(timelineLiveState.t0 || 0);
    if (!Number.isFinite(t)) return;

    for (const key of (timelineLiveState.keys || [])) {
        const parts = String(key).split('.', 2);
        if (parts.length !== 2) continue;
        const m = parts[0];
        const s = parts[1];
        if (m !== msg) continue;
        const v = sigs[s];
        const fv = (typeof v === 'boolean') ? (v ? 1.0 : 0.0) : Number(v);
        if (!Number.isFinite(fv)) continue;

        const arr = timelineLiveState.pending.get(key) || [];
        arr.push([t, fv]);
        timelineLiveState.pending.set(key, arr);
    }
}

function timelineFlushLive() {
    if (!timelineLiveEnabled || !timelineLiveState || !timelinePlotReady || !timelinePlotAreaEl) return;
    const updates = [];
    const traceIdx = [];
    for (const [key, pts] of (timelineLiveState.pending.entries() || [])) {
        if (!pts || pts.length === 0) continue;
        const idx = timelineLiveState.traces.get(key);
        if (idx == null) continue;
        const xs = [];
        const ys = [];
        pts.forEach(p => {
            xs.push(p[0]);
            ys.push(p[1]);
        });
        updates.push({ x: [xs], y: [ys] });
        traceIdx.push(idx);
    }
    timelineLiveState.pending.clear();
    if (updates.length === 0) return;

    // Merge updates into a single extendTraces call.
    const x = [];
    const y = [];
    const idxs = [];
    updates.forEach((u, i) => {
        x.push(u.x[0]);
        y.push(u.y[0]);
        idxs.push(traceIdx[i]);
    });
    try {
        Plotly.extendTraces(timelinePlotAreaEl, { x, y }, idxs, 2500);
    } catch (_) {
        // ignore
    }
}

function initTimelineViewer() {
    if (!timelineModeEl || !timelineSessionEl || !btnTimelineLoad) return;

    // Video diagnostics (helps when the MP4 exists but the browser can't decode/play it)
    if (timelineVideoEl) {
        try {
            timelineVideoEl.preload = 'metadata';
        } catch (_) {
            // ignore
        }
        timelineVideoEl.addEventListener('loadedmetadata', () => {
            try {
                const d = Number(timelineVideoEl.duration);
                if (Number.isFinite(d) && d > 0) {
                    timelineSetStatus(`Video loaded (${d.toFixed(1)}s)`);
                }
            } catch (_) {
                // ignore
            }
        });
        timelineVideoEl.addEventListener('error', () => {
            try {
                const code = timelineVideoEl.error ? timelineVideoEl.error.code : 0;
                const map = {
                    1: 'MEDIA_ERR_ABORTED',
                    2: 'MEDIA_ERR_NETWORK',
                    3: 'MEDIA_ERR_DECODE',
                    4: 'MEDIA_ERR_SRC_NOT_SUPPORTED',
                };
                const label = map[code] || `MEDIA_ERR_${code || 'UNKNOWN'}`;
                const src = String(timelineVideoEl.currentSrc || timelineVideoEl.src || '');
                timelineSetStatus(`Video error: ${label}${src ? ` (src: ${src})` : ''}`, true);
            } catch (e) {
                timelineSetStatus(`Video error: ${e}`, true);
            }
        });
    }

    timelineFillDbcSelect();
    timelineLoadSessions();
    timelineUpdateSignalCount();

    const applyModeUi = () => {
        const mode = timelineGetMode();
        if (mode === 'live') {
            timelineSetVideoModeLive();
            timelineSetStatus('Live mode');
        } else {
            timelineSetVideoModeReview();
            timelineSetStatus('Review mode');
        }
    };

    timelineModeEl.addEventListener('change', async () => {
        applyModeUi();
        await timelineRefreshSignals();
    });

    if (timelineSessionEl) {
        timelineSessionEl.addEventListener('change', async () => {
            await timelineLoadSelectedSession();
        });
    }

    btnTimelineLoad.addEventListener('click', async () => {
        await timelineLoadSelectedSession();
    });

    if (timelineDbcEl) {
        timelineDbcEl.addEventListener('change', async () => {
            await timelineRefreshSignals();
        });
    }
    if (timelineChannelEl) {
        timelineChannelEl.addEventListener('change', async () => {
            await timelineRefreshSignals();
        });
    }
    if (btnTimelineRefreshSignals) {
        btnTimelineRefreshSignals.addEventListener('click', async () => {
            await timelineRefreshSignals();
        });
    }
    if (timelineSignalsEl) {
        timelineSignalsEl.addEventListener('change', () => {
            timelineEnforceMaxSignals(10);
            timelineUpdateSignalCount();
            if (btnTimelinePlot) btnTimelinePlot.disabled = (timelineGetSelectedSignals().length === 0);
        });
    }
    if (btnTimelinePlot) {
        btnTimelinePlot.addEventListener('click', async () => {
            await timelinePlot();
        });
    }

    applyModeUi();
}

function initTriggerRules() {
    if (!btnTrApply) return;

    const getSelectedTriggerRuleFormats = () => {
        const formats = [];
        if (document.getElementById('tr-fmt-csv')?.checked) formats.push('csv');
        if (document.getElementById('tr-fmt-txt')?.checked) formats.push('txt');
        if (document.getElementById('tr-fmt-json')?.checked) formats.push('json');
        if (document.getElementById('tr-fmt-mf4')?.checked) formats.push('mf4');
        return formats;
    };

    const setSelectedTriggerRuleFormats = (formats) => {
        const set = new Set((Array.isArray(formats) ? formats : []).map(f => String(f).toLowerCase()));
        const csv = document.getElementById('tr-fmt-csv');
        const txt = document.getElementById('tr-fmt-txt');
        const json = document.getElementById('tr-fmt-json');
        const mf4 = document.getElementById('tr-fmt-mf4');
        if (csv) csv.checked = set.has('csv');
        if (txt) txt.checked = set.has('txt');
        if (json) json.checked = set.has('json');
        if (mf4) mf4.checked = set.has('mf4');
    };

    const refresh = async () => {
        let data = null;
        try {
            const res = await fetch('/api/trigger/rules', { cache: 'no-store' });
            data = await res.json();
        } catch (e) {
            data = null;
        }
        if (!data) return;

        if (trEnabledEl) trEnabledEl.checked = !!data.enabled;
        if (trModeEl && data.mode) trModeEl.value = String(data.mode);
        if (trWindowEl && data.window_s != null) trWindowEl.value = String(data.window_s);
        if (trCooldownEl && data.cooldown_s != null) trCooldownEl.value = String(data.cooldown_s);
        if (trAutoStopEl && data.auto_stop_s != null) trAutoStopEl.value = String(data.auto_stop_s);
        if (trPrerollEl && data.video_preroll_s != null) trPrerollEl.value = String(data.video_preroll_s);

        const src = new Set((data.sources || []).map(s => String(s).toLowerCase()));
        if (trSrcMotionEl) trSrcMotionEl.checked = src.has('motion');
        if (trSrcYoloEl) trSrcYoloEl.checked = src.has('yolo');
        if (trSrcCustomEl) trSrcCustomEl.checked = src.has('custom');

        if (Array.isArray(data.formats)) {
            setSelectedTriggerRuleFormats(data.formats);
        }
    };

    btnTrApply.addEventListener('click', async () => {
        const sources = [];
        if (trSrcMotionEl?.checked) sources.push('motion');
        if (trSrcYoloEl?.checked) sources.push('yolo');
        if (trSrcCustomEl?.checked) sources.push('custom');
        if (!sources.length) {
            alert('Select at least one source.');
            return;
        }
        const payload = {
            enabled: !!trEnabledEl?.checked,
            mode: trModeEl ? String(trModeEl.value) : 'any',
            sources,
            window_s: numVal(trWindowEl, 2.0),
            cooldown_s: numVal(trCooldownEl, 2.0),
            auto_stop_s: numVal(trAutoStopEl, 5.0),
            video_preroll_s: numVal(trPrerollEl, 0.0),
            formats: getSelectedTriggerRuleFormats(),
        };
        try {
            const res = await fetch('/api/trigger/rules', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!res.ok) {
                const txt = await res.text();
                alert(`Apply rules failed (${res.status}): ${txt}`);
                return;
            }
        } catch (e) {
            alert(`Apply rules error: ${e}`);
            return;
        }
        await refresh();
    });

    refresh();
}

function initVideoRecordingToggle() {
    if (!videoRecEnabledEl) return;

    const setStatus = (text) => {
        if (videoRecStatusEl) videoRecStatusEl.textContent = text;
    };

    const applyUiState = (enabled) => {
        videoRecEnabledEl.checked = !!enabled;
        setStatus(enabled ? 'Video recording enabled (MP4 will be saved).' : 'Video recording disabled (no MP4 will be saved).');
    };

    const refresh = async () => {
        setStatus('Loading video recording setting…');
        try {
            const resp = await fetch('/api/video/recording', { cache: 'no-store' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            applyUiState(!!data.enabled);
        } catch (e) {
            console.warn('Failed to load /api/video/recording', e);
            setStatus('Unable to load video recording setting.');
        }
    };

    videoRecEnabledEl.addEventListener('change', async () => {
        const desired = !!videoRecEnabledEl.checked;
        setStatus('Saving…');
        try {
            const resp = await fetch('/api/video/recording', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: desired })
            });
            if (!resp.ok) {
                const txt = await resp.text();
                throw new Error(`HTTP ${resp.status}: ${txt}`);
            }
            const data = await resp.json();
            applyUiState(!!data.enabled);
        } catch (e) {
            console.warn('Failed to save /api/video/recording', e);
            await refresh();
        }
    }, { passive: true });

    refresh();
}

function numVal(el, fallback) {
    const v = el ? parseFloat(el.value) : NaN;
    return Number.isFinite(v) ? v : fallback;
}

function intVal(el, fallback) {
    const v = el ? parseInt(el.value, 10) : NaN;
    return Number.isFinite(v) ? v : fallback;
}

function getSelectedFormats() {
    const formats = [];
    if (document.getElementById('fmt-csv')?.checked) formats.push('csv');
    if (document.getElementById('fmt-txt')?.checked) formats.push('txt');
    if (document.getElementById('fmt-json')?.checked) formats.push('json');
    if (document.getElementById('fmt-mf4')?.checked) formats.push('mf4');
    return formats.length ? formats : ['csv', 'txt'];
}

function initYoloTrigger() {
    if (yoloClassesEl) {
        yoloClassesEl.innerHTML = '';
        COCO80.forEach((name, idx) => {
            const id = `yolo-cls-${idx}`;
            const row = document.createElement('div');
            row.className = 'form-check';
            row.innerHTML = `
                <input class="form-check-input yolo-cls" type="checkbox" id="${id}" value="${name}">
                <label class="form-check-label" for="${id}">${name}</label>
            `;
            yoloClassesEl.appendChild(row);
        });

        yoloClassesEl.querySelectorAll('input.yolo-cls').forEach((cb) => {
            cb.addEventListener('change', () => {
                yoloSelectionDirty = true;
            });
        });
    }

    [yoloConfEl, yoloImgSzEl, yoloFpsEl, yoloCooldownEl, yoloModelEl].forEach((el) => {
        if (!el) return;
        el.addEventListener('input', () => {
            yoloSettingsDirty = true;
        });
    });

    const applyYoloConfig = async () => {
        const selected = yoloClassesEl
            ? Array.from(yoloClassesEl.querySelectorAll('input.yolo-cls:checked')).map(cb => cb.value)
            : [];
        const payload = {
            // If currently armed, send armed=true to explicitly re-arm (clears manual-stop latch server-side).
            ...(yoloArmedState ? { armed: true } : {}),
            classes: selected,
            conf: numVal(yoloConfEl, 0.5),
            imgsz: intVal(yoloImgSzEl, 320),
            fps: numVal(yoloFpsEl, 1.0),
            cooldown_s: numVal(yoloCooldownEl, 2.0),
            model: yoloModelEl ? String(yoloModelEl.value || 'yolov8n.pt').trim() : 'yolov8n.pt',
        };
        try {
            const res = await fetch('/api/trigger/yolo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!res.ok) {
                const txt = await res.text();
                alert(`YOLO apply failed (${res.status}): ${txt}`);
                return;
            }
        } catch (e) {
            alert(`YOLO apply error: ${e}`);
            return;
        }
        yoloSelectionDirty = false;
        yoloSettingsDirty = false;
        await refreshYoloTrigger();
    };

    if (btnYoloApply) btnYoloApply.addEventListener('click', applyYoloConfig);
    if (btnYoloApplyClasses) btnYoloApplyClasses.addEventListener('click', applyYoloConfig);

    if (btnYoloTest) btnYoloTest.addEventListener('click', async () => {
        const selected = yoloClassesEl
            ? Array.from(yoloClassesEl.querySelectorAll('input.yolo-cls:checked')).map(cb => cb.value)
            : [];
        const payload = {
            classes: selected,
            conf: numVal(yoloConfEl, 0.5),
            imgsz: intVal(yoloImgSzEl, 320),
            model: yoloModelEl ? String(yoloModelEl.value || 'yolov8n.pt').trim() : 'yolov8n.pt',
        };
        if (yoloTestOutEl) yoloTestOutEl.textContent = 'Running...';
        try {
            const res = await fetch('/api/yolo/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data?.ok) {
                const err = data?.error || (await res.text().catch(() => 'failed'));
                if (yoloTestOutEl) yoloTestOutEl.textContent = `ERROR: ${err}`;
                return;
            }
            const det = Array.isArray(data?.detections) ? data.detections : [];
            const top = det.slice(0, 10);
            const summary = {
                ok: data.ok,
                model: data.model,
                infer_ms: data.infer_ms,
                count: data.count,
                top: top,
            };
            if (yoloTestOutEl) yoloTestOutEl.textContent = JSON.stringify(summary, null, 2);
        } catch (e) {
            if (yoloTestOutEl) yoloTestOutEl.textContent = `ERROR: ${e}`;
        }
    });

    if (btnYoloArm) btnYoloArm.addEventListener('click', async () => {
        const selected = yoloClassesEl
            ? Array.from(yoloClassesEl.querySelectorAll('input.yolo-cls:checked')).map(cb => cb.value)
            : [];
        if (!selected.length) {
            alert('Select one or more objects for YOLO trigger.');
            return;
        }
        const formats = getSelectedFormats();
        try {
            const res = await fetch('/api/trigger/yolo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    armed: true,
                    classes: selected,
                    formats,
                    conf: numVal(yoloConfEl, 0.5),
                    imgsz: intVal(yoloImgSzEl, 320),
                    fps: numVal(yoloFpsEl, 1.0),
                    cooldown_s: numVal(yoloCooldownEl, 2.0),
                    model: yoloModelEl ? String(yoloModelEl.value || 'yolov8n.pt').trim() : 'yolov8n.pt',
                })
            });
            if (!res.ok) {
                const txt = await res.text();
                alert(`YOLO arm failed (${res.status}): ${txt}`);
                return;
            }
        } catch (e) {
            alert(`YOLO arm error: ${e}`);
            return;
        }
        yoloSelectionDirty = false;
        await refreshYoloTrigger();
    });

    if (btnYoloDisarm) btnYoloDisarm.addEventListener('click', async () => {
        try {
            await fetch('/api/trigger/yolo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ armed: false })
            });
        } catch (e) {
            // ignore
        }
        yoloSelectionDirty = false;
        await refreshYoloTrigger();
    });

    refreshYoloTrigger();
    setInterval(refreshYoloTrigger, 2000);
}

async function refreshYoloTrigger() {
    if (!yoloTriggerStatusEl && !btnYoloArm && !btnYoloDisarm) return;
    let data = null;
    let logStatus = null;
    try {
        const res = await fetch('/api/trigger/yolo', { cache: 'no-store' });
        data = await res.json();
    } catch (e) {
        data = { armed: false, classes: [] };
    }

    try {
        const res2 = await fetch('/api/log/status', { cache: 'no-store' });
        logStatus = await res2.json();
    } catch (e) {
        logStatus = { active: false };
    }

    const armed = !!data?.armed;
    const latched = !!data?.latched;
    const recording = !!logStatus?.active;

    // Keep a local copy so other actions (like Apply) can re-arm when needed.
    yoloArmedState = armed;

    if (yoloLastErrorEl) {
        const err = data?.last_error;
        yoloLastErrorEl.textContent = err ? String(err) : '—';
        yoloLastErrorEl.className = err ? 'text-danger' : 'text-muted';
    }
    if (yoloTriggerStatusEl) {
        if (recording) {
            yoloTriggerStatusEl.textContent = 'Recording';
            yoloTriggerStatusEl.className = 'text-danger';
        } else {
            if (armed && latched) {
                yoloTriggerStatusEl.textContent = 'Armed (latched — press Arm/Apply)';
                yoloTriggerStatusEl.className = 'text-danger';
            } else {
                yoloTriggerStatusEl.textContent = armed ? 'Armed (waiting)' : 'Disarmed';
                yoloTriggerStatusEl.className = armed ? 'text-warning' : 'text-muted';
            }
        }
    }
    // Allow "Arm" even when already armed if we're latched (manual Stop requires re-arm).
    if (btnYoloArm) btnYoloArm.disabled = armed && !latched;
    if (btnYoloDisarm) btnYoloDisarm.disabled = !armed;

    // Apply selected options from server state
    if (!yoloSelectionDirty && yoloClassesEl && Array.isArray(data?.classes)) {
        const set = new Set(data.classes.map(s => String(s).toLowerCase()));
        yoloClassesEl.querySelectorAll('input.yolo-cls').forEach((cb) => {
            cb.checked = set.has(String(cb.value).toLowerCase());
        });
    }

    // Apply settings from server state
    if (!yoloSettingsDirty) {
        if (yoloConfEl && data?.conf != null) yoloConfEl.value = String(data.conf);
        if (yoloImgSzEl && data?.imgsz != null) yoloImgSzEl.value = String(data.imgsz);
        if (yoloFpsEl && data?.fps != null) yoloFpsEl.value = String(data.fps);
        if (yoloCooldownEl && data?.cooldown_s != null) yoloCooldownEl.value = String(data.cooldown_s);
        if (yoloModelEl && data?.model != null) yoloModelEl.value = String(data.model);
    }
}

function initCustomObjects() {
    if (btnCustomCapture) btnCustomCapture.addEventListener('click', async () => {
        const name = customNameEl ? String(customNameEl.value || '').trim() : '';
        if (!name) {
            alert('Enter an object name.');
            return;
        }
        try {
            const res = await fetch('/api/custom/objects/capture', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data?.ok) {
                alert(`Capture failed: ${data?.error || res.status}`);
                return;
            }
        } catch (e) {
            alert(`Capture error: ${e}`);
            return;
        }
        await refreshCustomObjects();
    });

    if (btnCustomTrain) btnCustomTrain.addEventListener('click', async () => {
        const name = customNameEl ? String(customNameEl.value || '').trim() : '';
        if (!name) {
            alert('Enter the object name to train.');
            return;
        }
        try {
            const res = await fetch('/api/custom/objects/train', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data?.ok) {
                alert(`Train failed: ${data?.error || res.status}`);
                return;
            }
        } catch (e) {
            alert(`Train error: ${e}`);
            return;
        }
        await refreshCustomObjects();
    });

    if (btnCustomTest) btnCustomTest.addEventListener('click', async () => {
        const selected = customObjectsEl
            ? Array.from(customObjectsEl.querySelectorAll('input.custom-obj:checked')).map(cb => cb.value)
            : [];
        const payload = {
            objects: selected,
            threshold: intVal(customThresholdEl, 20),
        };
        if (customTestOutEl) customTestOutEl.textContent = 'Running...';
        try {
            const res = await fetch('/api/custom/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data?.ok) {
                const err = data?.error || (await res.text().catch(() => 'failed'));
                if (customTestOutEl) customTestOutEl.textContent = `ERROR: ${err}`;
                return;
            }
            if (customTestOutEl) customTestOutEl.textContent = JSON.stringify(data.result, null, 2);
        } catch (e) {
            if (customTestOutEl) customTestOutEl.textContent = `ERROR: ${e}`;
        }
    });

    [customThresholdEl, customFpsEl, customCooldownEl].forEach((el) => {
        if (!el) return;
        el.addEventListener('input', () => {
            // do nothing; values are read on arm/apply
        });
    });

    if (customObjectsEl) {
        customObjectsEl.addEventListener('change', () => {
            customSelectionDirty = true;
        });
    }

    if (btnCustomArm) btnCustomArm.addEventListener('click', async () => {
        const selected = customObjectsEl
            ? Array.from(customObjectsEl.querySelectorAll('input.custom-obj:checked')).map(cb => cb.value)
            : [];
        if (!selected.length) {
            alert('Select at least one trained custom object.');
            return;
        }
        const formats = getSelectedFormats();
        const payload = {
            armed: true,
            objects: selected,
            formats,
            threshold: intVal(customThresholdEl, 20),
            fps: numVal(customFpsEl, 1.0),
            cooldown_s: numVal(customCooldownEl, 2.0),
        };
        try {
            const res = await fetch('/api/trigger/custom', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!res.ok) {
                const txt = await res.text();
                alert(`Custom arm failed (${res.status}): ${txt}`);
                return;
            }
        } catch (e) {
            alert(`Custom arm error: ${e}`);
            return;
        }
        customSelectionDirty = false;
        await refreshCustomTrigger();
    });

    if (btnCustomDisarm) btnCustomDisarm.addEventListener('click', async () => {
        try {
            await fetch('/api/trigger/custom', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ armed: false })
            });
        } catch (e) {
            // ignore
        }
        customSelectionDirty = false;
        await refreshCustomTrigger();
    });

    refreshCustomObjects();
    refreshCustomTrigger();
    setInterval(refreshCustomTrigger, 2000);
}

async function refreshCustomObjects() {
    if (!customObjectsEl) return;
    let data = null;
    try {
        const res = await fetch('/api/custom/objects', { cache: 'no-store' });
        data = await res.json();
    } catch (e) {
        data = { ok: false, objects: [] };
    }

    const objects = Array.isArray(data?.objects) ? data.objects : [];
    customObjectsEl.innerHTML = '';
    if (!objects.length) {
        customObjectsEl.innerHTML = '<div class="text-muted small">No custom objects yet.</div>';
        return;
    }

    objects.forEach((o, idx) => {
        const name = o?.name;
        const trained = !!o?.trained;
        const count = o?.sample_count ?? 0;
        const id = `custom-obj-${idx}`;
        const row = document.createElement('div');
        row.className = 'form-check';
        row.innerHTML = `
            <input class="form-check-input custom-obj" type="checkbox" id="${id}" value="${name}" ${trained ? '' : 'disabled'}>
            <label class="form-check-label" for="${id}">${name} <span class="text-muted small">(${count} photos${trained ? '' : ', not trained'})</span></label>
        `;
        customObjectsEl.appendChild(row);
    });
}

async function refreshCustomTrigger() {
    let data = null;
    try {
        const res = await fetch('/api/trigger/custom', { cache: 'no-store' });
        data = await res.json();
    } catch (e) {
        data = { armed: false, objects: [] };
    }

    let logStatus = null;
    try {
        const res2 = await fetch('/api/log/status', { cache: 'no-store' });
        logStatus = await res2.json();
    } catch (e) {
        logStatus = { active: false };
    }

    const armed = !!data?.armed;
    const recording = !!logStatus?.active;
    if (customTriggerStatusEl) {
        if (recording) {
            customTriggerStatusEl.textContent = 'Recording';
            customTriggerStatusEl.className = 'text-danger';
        } else {
            customTriggerStatusEl.textContent = armed ? 'Armed (waiting)' : 'Disarmed';
            customTriggerStatusEl.className = armed ? 'text-warning' : 'text-muted';
        }
    }
    if (btnCustomArm) btnCustomArm.disabled = armed;
    if (btnCustomDisarm) btnCustomDisarm.disabled = !armed;

    if (customLastErrorEl) {
        const err = data?.last_error;
        customLastErrorEl.textContent = err ? String(err) : '—';
        customLastErrorEl.className = err ? 'text-danger' : 'text-muted';
    }

    if (!customSelectionDirty && customObjectsEl && Array.isArray(data?.objects)) {
        const set = new Set(data.objects.map(s => String(s).toLowerCase()));
        customObjectsEl.querySelectorAll('input.custom-obj').forEach((cb) => {
            cb.checked = set.has(String(cb.value).toLowerCase());
        });
    }

    if (customThresholdEl && data?.threshold != null) customThresholdEl.value = String(data.threshold);
    if (customFpsEl && data?.fps != null) customFpsEl.value = String(data.fps);
    if (customCooldownEl && data?.cooldown_s != null) customCooldownEl.value = String(data.cooldown_s);
}

function initWebcam() {
    // Set MJPEG stream source once; same shared backend stream for all clients.
    const url = `/api/camera/stream?t=${Date.now()}`;
    if (webcamImgLogger) webcamImgLogger.src = url;
    if (webcamImgLive) webcamImgLive.src = url;

    // Poll camera status
    const update = async () => {
        let data = null;
        try {
            const res = await fetch('/api/camera/status', { cache: 'no-store' });
            data = await res.json();
        } catch (e) {
            data = { available: false, connected: false, last_error: String(e) };
        }

        const connected = !!data?.connected;
        const available = !!data?.available;
        const label = !available
            ? 'Unavailable'
            : connected
                ? 'Connected'
                : 'Disconnected';

        const extra = (data?.last_error && !connected) ? ` (${data.last_error})` : '';
        const text = label + extra;

        const cls = !available
            ? 'text-danger'
            : connected
                ? 'text-success'
                : 'text-danger';

        if (webcamStatusLogger) {
            webcamStatusLogger.textContent = text;
            webcamStatusLogger.className = cls;
        }
        if (webcamStatusLive) {
            webcamStatusLive.textContent = text;
            webcamStatusLive.className = cls;
        }
    };

    update();
    setInterval(update, 2000);
}

function setMode(mode) {
    const showLogger = mode === 'logger';
    const showScanTools = mode === 'scantools';
    const showMf4 = mode === 'mf4';
    const showSettings = mode === 'settings';

    // Elements can belong to multiple modes (e.g., shared log management panels).
    // Show an element if it matches ANY active mode class.
    const all = document.querySelectorAll('.mode-logger, .mode-scantools, .mode-mf4, .mode-settings');
    all.forEach(el => {
        const want = (
            (el.classList.contains('mode-logger') && showLogger) ||
            (el.classList.contains('mode-scantools') && showScanTools) ||
            (el.classList.contains('mode-mf4') && showMf4) ||
            (el.classList.contains('mode-settings') && showSettings)
        );
        el.style.display = want ? '' : 'none';
    });

    if (showMf4) {
        mf4EnsureLoaded();
    }

    if (showSettings) {
        try { refreshHealth(); } catch (_) {}
        try { pdxEnsureLoaded(); } catch (_) {}
        try { gmEnsureLoaded(); } catch (_) {}
    }
}

// -----------------------------
// Gateway Mirror (Settings)
// -----------------------------

let gmLoadedOnce = false;

function gmSetStatus(text, isError=false) {
    if (!gmStatusEl) return;
    gmStatusEl.textContent = String(text || '');
    gmStatusEl.className = isError ? 'text-danger' : 'text-muted';
}

function gmSetResponse(obj) {
    if (!gmLastRespEl) return;
    if (obj === null || obj === undefined) {
        gmLastRespEl.textContent = '—';
        return;
    }
    try {
        gmLastRespEl.textContent = (typeof obj === 'string') ? obj : JSON.stringify(obj, null, 2);
    } catch (_) {
        gmLastRespEl.textContent = String(obj);
    }
}

function gmUiToConfig() {
    const can = [];
    gmCanEls.forEach((el, idx) => {
        if (el && el.checked) can.push(idx + 1);
    });
    const flexray = [];
    if (gmFlexEls.A && gmFlexEls.A.checked) flexray.push('A');
    if (gmFlexEls.B && gmFlexEls.B.checked) flexray.push('B');
    const lin = [];
    gmLinEls.forEach((el, idx) => {
        if (el && el.checked) lin.push(idx + 1);
    });

    return {
        enabled: !!gmEnabledEl?.checked,
        autostart: !!gmAutostartEl?.checked,
        auto_discover_ip: !!gmAutoDiscoverIpEl?.checked,
        gateway_ip: String(gmGatewayIpEl?.value || '').trim(),
        target_addr: String(gmTargetAddrEl?.value || '').trim(),
        tester_logical_address: String(gmTesterAddrEl?.value || '').trim(),
        target_bus: String(gmTargetBusEl?.value || 'ethernet').trim() || 'ethernet',
        dest_ip: String(gmDestIpEl?.value || '').trim(),
        dest_port: (gmDestPortEl && String(gmDestPortEl.value || '').trim() !== '') ? Number(gmDestPortEl.value) : 0,
        can,
        flexray,
        lin,
    };
}

function gmConfigToUi(cfg) {
    const c = (cfg && typeof cfg === 'object') ? cfg : {};
    if (gmEnabledEl) gmEnabledEl.checked = !!c.enabled;
    if (gmAutostartEl) gmAutostartEl.checked = !!c.autostart;
    if (gmAutoDiscoverIpEl) gmAutoDiscoverIpEl.checked = (c.auto_discover_ip !== false);
    if (gmGatewayIpEl) gmGatewayIpEl.value = String(c.gateway_ip || '');
    if (gmTargetAddrEl) gmTargetAddrEl.value = String(c.target_addr || '');
    if (gmTesterAddrEl) gmTesterAddrEl.value = String(c.tester_logical_address || '0x0E00');
    if (gmTargetBusEl) gmTargetBusEl.value = String(c.target_bus || 'ethernet');
    if (gmDestIpEl) gmDestIpEl.value = String(c.dest_ip || '');
    if (gmDestPortEl) gmDestPortEl.value = (c.dest_port !== undefined && c.dest_port !== null) ? String(c.dest_port) : '';

    const canSet = new Set(Array.isArray(c.can) ? c.can.map(x => Number(x)) : []);
    gmCanEls.forEach((el, idx) => {
        if (el) el.checked = canSet.has(idx + 1);
    });

    const frSet = new Set(Array.isArray(c.flexray) ? c.flexray.map(x => String(x).toUpperCase()) : []);
    if (gmFlexEls.A) gmFlexEls.A.checked = frSet.has('A');
    if (gmFlexEls.B) gmFlexEls.B.checked = frSet.has('B');

    const linSet = new Set(Array.isArray(c.lin) ? c.lin.map(x => Number(x)) : []);
    gmLinEls.forEach((el, idx) => {
        if (el) el.checked = linSet.has(idx + 1);
    });
}

async function gmLoadConfig() {
    gmSetStatus('Loading…');
    try {
        const res = await fetch('/api/gateway/mirror/config', { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok || !data?.ok) {
            gmSetStatus(data?.error || `Load failed (${res.status})`, true);
            return;
        }
        gmConfigToUi(data.config);
        gmSetStatus('Ready');
    } catch (e) {
        gmSetStatus(String(e), true);
    }
}

async function gmSaveConfig() {
    gmSetStatus('Saving…');
    try {
        const cfg = gmUiToConfig();
        const res = await fetch('/api/gateway/mirror/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: cfg })
        });
        const data = await res.json();
        if (!res.ok || !data?.ok) {
            gmSetStatus(data?.error || `Save failed (${res.status})`, true);
            gmSetResponse(data);
            return;
        }
        gmConfigToUi(data.config);
        gmSetStatus('Saved');
        gmSetResponse(data);
    } catch (e) {
        gmSetStatus(String(e), true);
    }
}

async function gmStart() {
    gmSetStatus('Starting…');
    gmSetResponse(null);
    try {
        // Persist UI values first so Start matches what you see.
        await gmSaveConfig();
        const res = await fetch('/api/gateway/mirror/start', { method: 'POST' });
        const data = await res.json();
        gmSetResponse(data);
        if (data?.ok) {
            // Refresh UI fields that the backend may have auto-discovered
            if (data.target_addr && gmTargetAddrEl) gmTargetAddrEl.value = String(data.target_addr);
            if (data.gateway_ip && gmGatewayIpEl && !String(gmGatewayIpEl.value || '').trim()) {
                gmGatewayIpEl.value = String(data.gateway_ip);
            }
            gmSetStatus('Running (enabled)');
        } else {
            gmSetStatus(data?.error || 'Start failed', true);
        }
    } catch (e) {
        gmSetStatus(String(e), true);
    }
}

async function gmStop() {
    if (!confirm('Stop the Gateway Mirror?\nYou can restart it afterwards with Start Mirror.')) return;
    gmSetStatus('Stopping…');
    gmSetResponse(null);
    try {
        const res = await fetch('/api/gateway/mirror/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
        });
        const data = await res.json();
        gmSetResponse(data);
        if (data?.ok) {
            gmSetStatus('Stopped (disabled)');
            if (data.target_addr && gmTargetAddrEl) gmTargetAddrEl.value = String(data.target_addr);
        } else {
            gmSetStatus(data?.error || 'Stop failed', true);
        }
    } catch (e) {
        gmSetStatus(String(e), true);
    }
}

async function gmDiscoverTargetAddr() {
    gmSetStatus('Discovering target address…');
    gmSetResponse(null);
    try {
        const body = {
            gateway_ip: String(gmGatewayIpEl?.value || '').trim(),
            auto_discover_ip: !!gmAutoDiscoverIpEl?.checked,
            tester_logical_address: String(gmTesterAddrEl?.value || '').trim(),
        };
        const res = await fetch('/api/gateway/mirror/discover_target_addr', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        gmSetResponse(data);
        if (!res.ok || !data?.ok) {
            gmSetStatus(data?.error || `Discovery failed (${res.status})`, true);
            return;
        }

        if (data.gateway_ip && gmGatewayIpEl) {
            // Always update — auto-discovery may have resolved a different address.
            gmGatewayIpEl.value = String(data.gateway_ip);
        }
        if (data.target_addr && gmTargetAddrEl) {
            gmTargetAddrEl.value = String(data.target_addr);
        }

        // Persist what we discovered.
        await gmSaveConfig();
        gmSetStatus('Target address discovered');
    } catch (e) {
        gmSetStatus(String(e), true);
    }
}

function gmEnsureLoaded() {
    if (gmLoadedOnce) return;
    gmLoadedOnce = true;

    if (btnGmRefresh) btnGmRefresh.onclick = async () => {
        gmSetResponse(null);
        await gmLoadConfig();
    };
    if (btnGmSave) btnGmSave.onclick = async () => {
        await gmSaveConfig();
    };
    if (btnGmStart) btnGmStart.onclick = async () => {
        await gmStart();
    };
    if (btnGmStop) btnGmStop.onclick = async () => {
        await gmStop();
    };

    if (btnGmDiscoverTarget) btnGmDiscoverTarget.onclick = async () => {
        await gmDiscoverTargetAddr();
    };

    gmLoadConfig();
}

function initGatewayMirrorSettings() {
    // Lazy-loaded on Settings mode; keep init for symmetry.
    // (No-op here because setMode triggers gmEnsureLoaded.)
}

// -----------------------------
// Projects (PDX)
// -----------------------------

let pdxLoadedOnce = false;

function pdxSetStatus(text) {
    const el = document.getElementById('pdx-status');
    if (el) el.textContent = String(text || '');
}

function pdxSetReport(obj) {
    const el = document.getElementById('pdx-report');
    if (!el) return;
    if (obj === null || obj === undefined) {
        el.textContent = '—';
        return;
    }
    try {
        el.textContent = (typeof obj === 'string') ? obj : JSON.stringify(obj, null, 2);
    } catch (_) {
        el.textContent = String(obj);
    }
}

async function loadPdxProjects() {
    const select = document.getElementById('pdx-select');
    const activeEl = document.getElementById('pdx-active');
    if (!select) return;

    pdxSetStatus('Loading…');
    try {
        const res = await fetch('/api/projects/pdx/list', { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok || !data?.ok) {
            pdxSetStatus(data?.error || `List failed (${res.status})`);
            return;
        }

        const items = Array.isArray(data.items) ? data.items : [];
        const active = (typeof data.active === 'string') ? data.active : '';

        const prev = select.value;
        select.innerHTML = '';
        if (!items.length) {
            const opt = document.createElement('option');
            opt.value = '';
            opt.disabled = true;
            opt.selected = true;
            opt.textContent = 'No PDX uploaded';
            select.appendChild(opt);
        } else {
            const opt0 = document.createElement('option');
            opt0.value = '';
            opt0.textContent = 'Select PDX…';
            select.appendChild(opt0);
            items.forEach(it => {
                const fn = String(it?.filename || '').trim();
                if (!fn) return;
                const opt = document.createElement('option');
                opt.value = fn;
                const cnt = it?.counts;
                const suffix = cnt ? ` (DTC:${cnt.dtcs ?? 0}, Prot:${cnt.protocols ?? 0})` : '';
                opt.textContent = fn + suffix;
                select.appendChild(opt);
            });
        }

        if (activeEl) activeEl.textContent = active || '—';
        if (active && items.some(it => it?.filename === active)) {
            select.value = active;
        } else if (prev && items.some(it => it?.filename === prev)) {
            select.value = prev;
        }

        pdxSetStatus('Ready');
    } catch (e) {
        pdxSetStatus(String(e));
    }
}

function pdxEnsureLoaded() {
    if (pdxLoadedOnce) return;
    pdxLoadedOnce = true;

    const upload = document.getElementById('pdx-upload');
    const refresh = document.getElementById('btn-pdx-refresh');
    const activate = document.getElementById('btn-pdx-activate');
    const reportBtn = document.getElementById('btn-pdx-report');
    const select = document.getElementById('pdx-select');

    if (refresh) refresh.onclick = async () => {
        pdxSetReport(null);
        await loadPdxProjects();
    };

    if (upload) upload.onchange = async (e) => {
        const files = e.target.files;
        if (!files || files.length === 0) return;
        pdxSetReport(null);

        for (let i = 0; i < files.length; i++) {
            const f = files[i];
            if (!f) continue;
            pdxSetStatus(`Importing ${f.name}…`);
            const formData = new FormData();
            formData.append('file', f);
            const res = await fetch('/api/projects/pdx/import', { method: 'POST', body: formData });
            let data = null;
            try { data = await res.json(); } catch (_) { data = null; }
            if (!res.ok || !data?.ok) {
                pdxSetStatus(data?.error || `Import failed (${res.status})`);
                return;
            }
            // Show the last imported report.
            pdxSetReport(data);
        }

        pdxSetStatus('Import complete');
        await loadPdxProjects();
    };

    if (activate) activate.onclick = async () => {
        const filename = String(select?.value || '').trim();
        if (!filename) {
            pdxSetStatus('Select a PDX first');
            return;
        }
        pdxSetStatus(`Activating ${filename}…`);
        pdxSetReport(null);
        try {
            const res = await fetch('/api/projects/pdx/select', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename })
            });
            const data = await res.json();
            if (!res.ok || !data?.ok) {
                pdxSetStatus(data?.error || `Activate failed (${res.status})`);
                return;
            }
            pdxSetStatus('Active project updated');
            await loadPdxProjects();
        } catch (e) {
            pdxSetStatus(String(e));
        }
    };

    if (reportBtn) reportBtn.onclick = async () => {
        const filename = String(select?.value || '').trim();
        if (!filename) {
            pdxSetStatus('Select a PDX first');
            return;
        }
        pdxSetStatus(`Loading report for ${filename}…`);
        try {
            const res = await fetch(`/api/projects/pdx/report?filename=${encodeURIComponent(filename)}`, { cache: 'no-store' });
            const data = await res.json();
            if (!res.ok || !data?.ok) {
                pdxSetStatus(data?.error || `Report failed (${res.status})`);
                return;
            }
            pdxSetReport(data);
            pdxSetStatus('Ready');
        } catch (e) {
            pdxSetStatus(String(e));
        }
    };

    loadPdxProjects();
}

let mf4ViewerInitialized = false;
let mf4FilesLoadedOnce = false;

let mf4SignalCache = null; // [{value,text,group}]
let mf4MessageCache = null; // [{value,text}]
let mf4DecodedGroupsCache = null; // [{message, signals:[{key,unit}]}]

let mf4FileInfoCache = new Map();

const MF4_LS_KEY = 'kvbm_mf4_viewer_state_v1';

// Track what is currently plotted so we can append/merge or open a second plot.
const mf4PlotState = {
    plot1: { seriesByName: new Map(), axisMode: 'single' },
    plot2: { seriesByName: new Map(), axisMode: 'single' },
};

function mf4LoadState() {
    try {
        const raw = localStorage.getItem(MF4_LS_KEY);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        return (parsed && typeof parsed === 'object') ? parsed : {};
    } catch (_) {
        return {};
    }
}

function mf4SaveState(patch) {
    try {
        const prev = mf4LoadState();
        const next = { ...prev, ...patch };
        localStorage.setItem(MF4_LS_KEY, JSON.stringify(next));
    } catch (_) {
        // ignore
    }
}

function initMf4Viewer() {
    if (mf4ViewerInitialized) return;
    mf4ViewerInitialized = true;

    const refreshBtn = document.getElementById('mf4-refresh-files');
    const fileSelect = document.getElementById('mf4-file-select');
    const dbcSelect = document.getElementById('mf4-dbc-select');
    const chSelect = document.getElementById('mf4-channel-select');
    const messagesWrap = document.getElementById('mf4-messages-wrap');
    const messagesSelect = document.getElementById('mf4-messages-select');
    const messageFilter = document.getElementById('mf4-message-filter');
    const messagesAllBtn = document.getElementById('mf4-messages-all');
    const messagesNoneBtn = document.getElementById('mf4-messages-none');
    const signalsSelect = document.getElementById('mf4-signals-select');
    const plotBtn = document.getElementById('mf4-plot');
    const exportBtn = document.getElementById('mf4-export-decoded');
    const selectAllBtn = document.getElementById('mf4-select-all');
    const axisMode = document.getElementById('mf4-axis-mode');
    const startInput = document.getElementById('mf4-start-s');
    const endInput = document.getElementById('mf4-end-s');
    const maxPointsInput = document.getElementById('mf4-max-points');
    const signalFilter = document.getElementById('mf4-signal-filter');
    const uploadBtn = document.getElementById('mf4-upload-btn');

    // Restore persisted UI state
    const st = mf4LoadState();
    if (axisMode && (st.axis_mode === 'single' || st.axis_mode === 'multi')) axisMode.value = st.axis_mode;
    if (startInput && st.start_s !== undefined && st.start_s !== null && String(st.start_s) !== '') startInput.value = String(st.start_s);
    if (endInput && st.end_s !== undefined && st.end_s !== null && String(st.end_s) !== '') endInput.value = String(st.end_s);
    if (maxPointsInput && Number.isFinite(parseInt(st.max_points, 10))) maxPointsInput.value = String(parseInt(st.max_points, 10));
    if (signalFilter && st.signal_filter !== undefined && st.signal_filter !== null) signalFilter.value = String(st.signal_filter);
    if (messageFilter && st.message_filter !== undefined && st.message_filter !== null) messageFilter.value = String(st.message_filter);
    // DBC multi-select state is restored in mf4LoadDbcs(); channel is restored when channels list is loaded.

    if (refreshBtn) refreshBtn.addEventListener('click', async () => {
        mf4FilesLoadedOnce = false;
        await mf4EnsureLoaded();
    });

    if (dbcSelect) dbcSelect.addEventListener('change', async () => {
        // If any non-empty DBC is selected, de-select the "Auto" option.
        try {
            const selected = Array.from(dbcSelect.selectedOptions || []).map(o => String(o.value || ''));
            if (selected.some(v => v !== '')) {
                Array.from(dbcSelect.options).forEach(o => { if (o.value === '') o.selected = false; });
            }
        } catch (_) {}

        const { auto, dbcs, arxml, fibex } = mf4GetSelectedDbcs();
        mf4SaveState({ dbcs, dbc: dbcs[0] || '', auto, signals: [], messages: [] });
        await mf4LoadSignalsForSelectedFile();
    });

    if (chSelect) chSelect.addEventListener('change', async () => {
        const ch = mf4GetSelectedChannel();
        mf4SaveState({ channel: (ch === null ? '' : String(ch)), signals: [], messages: [] });
        await mf4LoadSignalsForSelectedFile();
    });

    if (axisMode) axisMode.addEventListener('change', () => {
        mf4SaveState({ axis_mode: axisMode.value });
    });
    if (startInput) startInput.addEventListener('change', () => mf4SaveState({ start_s: startInput.value }));
    if (endInput) endInput.addEventListener('change', () => mf4SaveState({ end_s: endInput.value }));
    if (maxPointsInput) maxPointsInput.addEventListener('change', () => mf4SaveState({ max_points: maxPointsInput.value }));

    if (signalFilter) signalFilter.addEventListener('input', () => {
        mf4SaveState({ signal_filter: signalFilter.value });
        mf4ApplySignalFilter();
    });

    if (signalFilter) signalFilter.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            // Convenience: press Enter to plot if ready.
            const canPlot = (mf4GetSelectedSignals().length > 0) && String(fileSelect?.value || '').trim();
            if (canPlot) {
                e.preventDefault();
                plotBtn?.click();
            }
        }
    });

    if (fileSelect) fileSelect.addEventListener('change', async () => {
        mf4SaveState({ file: fileSelect.value, signals: [], messages: [] });
        await mf4LoadSignalsForSelectedFile();
    });

    if (messageFilter) messageFilter.addEventListener('input', () => {
        mf4SaveState({ message_filter: messageFilter.value });
        mf4ApplyMessageFilter();
    });

    if (messagesAllBtn) messagesAllBtn.addEventListener('click', () => {
        mf4SelectAllMessages();
    });

    if (messagesNoneBtn) messagesNoneBtn.addEventListener('click', () => {
        mf4SelectNoMessages();
    });

    if (messagesSelect) messagesSelect.addEventListener('change', () => {
        mf4SaveState({ messages: mf4GetSelectedMessages(), messages_none: false, signals: [] });
        mf4RebuildSignalsFromDecodedCache();
    });

    if (signalsSelect) signalsSelect.addEventListener('change', () => {
        mf4SaveState({ signals: mf4GetSelectedSignals() });
        mf4ApplySignalFilter();
        const sel = mf4GetSelectedSignals();
        if (plotBtn) plotBtn.disabled = (sel.length === 0);
        if (exportBtn) exportBtn.disabled = (sel.filter(s => String(s).includes('.')).length === 0);
    });

    if (plotBtn) plotBtn.addEventListener('click', async () => {
        await mf4PlotSelectedSignals();
    });

    if (selectAllBtn) selectAllBtn.addEventListener('click', () => {
        mf4SelectAllSignals();
    });

    if (exportBtn) exportBtn.addEventListener('click', async () => {
        await mf4ExportDecodedMf4();
    });

    if (uploadBtn) uploadBtn.addEventListener('click', async () => {
        await mf4UploadSelectedFile();
    });
}

function mf4SelectAllSignals() {
    const signalsSelect = document.getElementById('mf4-signals-select');
    const signalFilter = document.getElementById('mf4-signal-filter');
    const plotBtn = document.getElementById('mf4-plot');
    const exportBtn = document.getElementById('mf4-export-decoded');
    const selectAllBtn = document.getElementById('mf4-select-all');
    if (!signalsSelect) return;

    // Clear filter so "all" truly means all signals.
    if (signalFilter) {
        signalFilter.value = '';
        mf4SaveState({ signal_filter: '' });
    }
    mf4RenderSignalsFiltered('');

    const opts = Array.from(signalsSelect.options);
    opts.forEach(o => { o.selected = true; });

    const sel = mf4GetSelectedSignals();
    mf4SaveState({ signals: sel });

    if (plotBtn) plotBtn.disabled = (sel.length === 0);
    if (exportBtn) exportBtn.disabled = (sel.filter(s => String(s).includes('.')).length === 0);
    if (selectAllBtn) selectAllBtn.disabled = (opts.length === 0);
}

async function mf4ExportDecodedMf4() {
    const fileSelect = document.getElementById('mf4-file-select');
    const exportBtn = document.getElementById('mf4-export-decoded');
    const startInput = document.getElementById('mf4-start-s');
    const endInput = document.getElementById('mf4-end-s');
    if (!fileSelect) return;

    const file = String(fileSelect.value || '').trim();
    const signals = mf4GetSelectedSignals().filter(s => String(s).includes('.'));
    if (!file) {
        mf4SetStatus('Select an MF4 file', true);
        return;
    }
    if (signals.length === 0) {
        mf4SetStatus('Select one or more decoded signals (Message.Signal) to export', true);
        return;
    }

    const { auto, dbcs, arxml, fibex } = mf4GetSelectedDbcs();
    const ch = mf4GetSelectedChannel();

    let start_s = null;
    let end_s = null;
    try {
        const v = String(startInput?.value || '').trim();
        start_s = v === '' ? null : parseFloat(v);
        if (start_s !== null && (!Number.isFinite(start_s) || start_s < 0)) start_s = null;
    } catch (_) { start_s = null; }
    try {
        const v = String(endInput?.value || '').trim();
        end_s = v === '' ? null : parseFloat(v);
        if (end_s !== null && (!Number.isFinite(end_s) || end_s < 0)) end_s = null;
    } catch (_) { end_s = null; }

    mf4SetStatus('Exporting decoded MF4...');
    if (exportBtn) exportBtn.disabled = true;

    try {
        const body = {
            file,
            dbcs,
            auto: !!auto,
            arxml: !!arxml,
            fibex: fibex || '',
            channel: (ch === null ? null : ch),
            signals,
            start_s,
            end_s,
        };

        const r = await fetch('/api/mf4/export_decoded_mf4', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        const ct = String(r.headers.get('content-type') || '');
        if (!r.ok) {
            if (ct.includes('application/json')) {
                const err = await r.json();
                throw new Error(err?.error ? String(err.error) : `HTTP ${r.status} ${r.statusText}`);
            }
            const txt = await r.text();
            throw new Error(txt || `HTTP ${r.status} ${r.statusText}`);
        }

        const blob = await r.blob();
        let name = 'decoded.mf4';
        try {
            const cd = String(r.headers.get('content-disposition') || '');
            const m = cd.match(/filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i);
            if (m) {
                name = decodeURIComponent(m[1] || m[2] || name);
            }
        } catch (_) {}

        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);

        mf4SetStatus(`Export ready: ${name}`);
    } catch (e) {
        console.error(e);
        mf4SetStatus(`Export error: ${e}`, true);
    } finally {
        const sel = mf4GetSelectedSignals();
        if (exportBtn) exportBtn.disabled = (sel.filter(s => String(s).includes('.')).length === 0);
    }
}

function mf4UpdateSignalCount(visible, total) {
    const el = document.getElementById('mf4-signal-count');
    if (!el) return;
    el.textContent = `${visible} / ${total}`;
}

function mf4UpdateMessageCount(visible, total) {
    const el = document.getElementById('mf4-message-count');
    if (!el) return;
    el.textContent = `${visible} / ${total}`;
}

function mf4SetMessagesVisible(visible) {
    const wrap = document.getElementById('mf4-messages-wrap');
    if (!wrap) return;
    wrap.style.display = visible ? '' : 'none';
}

function mf4GetSelectedMessages() {
    const sel = document.getElementById('mf4-messages-select');
    if (!sel) return [];
    return Array.from(sel.selectedOptions || []).map(o => String(o.value || '')).filter(Boolean);
}

function mf4BuildMessageCacheFromGroups(groups) {
    try {
        const msgs = (Array.isArray(groups) ? groups : [])
            .map(g => String(g?.message || '').trim())
            .filter(Boolean);
        mf4MessageCache = msgs.map(m => ({ value: m, text: m }));
    } catch (_) {
        mf4MessageCache = [];
    }
}

function mf4RenderMessagesFiltered(queryText) {
    const sel = document.getElementById('mf4-messages-select');
    if (!sel) return;

    const cache = Array.isArray(mf4MessageCache) ? mf4MessageCache : [];
    const normalize = (s) => String(s || '')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, ' ')
        .trim();
    const qNorm = normalize(queryText);
    const tokens = qNorm ? qNorm.split(/\s+/).filter(Boolean) : [];

    const selected = new Set(mf4GetSelectedMessages());
    const frag = document.createDocumentFragment();

    let visible = 0;
    const total = cache.length;

    cache.forEach((it) => {
        const val = String(it.value || '');
        if (!val) return;
        const hay = normalize(`${it.value || ''} ${it.text || ''}`);
        const match = (tokens.length === 0) ? true : tokens.every(t => hay.includes(t));
        const show = match || selected.has(val);
        if (!show) return;

        const opt = document.createElement('option');
        opt.value = val;
        opt.text = String(it.text || val);
        opt.selected = selected.has(val);
        frag.appendChild(opt);
        visible += 1;
    });

    sel.innerHTML = '';
    sel.appendChild(frag);
    mf4UpdateMessageCount(visible, total);
}

function mf4ApplyMessageFilter() {
    const input = document.getElementById('mf4-message-filter');
    mf4RenderMessagesFiltered(String(input?.value || '').trim());
}

function mf4SelectAllMessages() {
    const sel = document.getElementById('mf4-messages-select');
    if (!sel) return;

    // Clear filter so "all" truly means all messages.
    const input = document.getElementById('mf4-message-filter');
    if (input) {
        input.value = '';
        mf4SaveState({ message_filter: '' });
    }
    mf4RenderMessagesFiltered('');

    Array.from(sel.options || []).forEach(o => { o.selected = true; });
    mf4SaveState({ messages: mf4GetSelectedMessages(), messages_none: false, signals: [] });
    mf4RebuildSignalsFromDecodedCache();
}

function mf4SelectNoMessages() {
    const sel = document.getElementById('mf4-messages-select');
    if (!sel) return;
    Array.from(sel.options || []).forEach(o => { o.selected = false; });
    mf4SaveState({ messages: [], messages_none: true, signals: [] });
    mf4RebuildSignalsFromDecodedCache();
}

function mf4RebuildSignalsFromDecodedCache() {
    return mf4RebuildSignalsFromDecodedCacheEx(false);
}

function mf4RebuildSignalsFromDecodedCachePreserve() {
    return mf4RebuildSignalsFromDecodedCacheEx(true);
}

function mf4RebuildSignalsFromDecodedCacheEx(preserveSelection) {
    preserveSelection = !!preserveSelection;
    const groups = Array.isArray(mf4DecodedGroupsCache) ? mf4DecodedGroupsCache : null;
    if (!groups) return;

    const signalsSelect = document.getElementById('mf4-signals-select');
    const plotBtn = document.getElementById('mf4-plot');
    const exportBtn = document.getElementById('mf4-export-decoded');
    const selectAllBtn = document.getElementById('mf4-select-all');
    if (!signalsSelect) return;

    let noneMode = false;
    try {
        const st = mf4LoadState();
        noneMode = !!st.messages_none;
    } catch (_) {
        noneMode = false;
    }
    const selectedMessages = new Set(mf4GetSelectedMessages());
    const filterActive = noneMode || (selectedMessages.size > 0);
    const frag = document.createDocumentFragment();

    groups.forEach((g) => {
        const msg = String(g?.message || '').trim();
        const sg = Array.isArray(g?.signals) ? g.signals : [];
        if (!msg || sg.length === 0) return;
        if (filterActive && !selectedMessages.has(msg)) return;

        const og = document.createElement('optgroup');
        og.label = msg;
        sg.forEach((s) => {
            const key = String(s?.key || '').trim();
            if (!key) return;
            const parts = key.split('.', 2);
            const sigLabel = (parts.length === 2) ? parts[1] : key;
            const opt = document.createElement('option');
            opt.value = key;
            opt.text = sigLabel;
            opt.title = key;
            og.appendChild(opt);
        });
        frag.appendChild(og);
    });

    signalsSelect.innerHTML = '';
    signalsSelect.appendChild(frag);

    let keep = new Set();
    if (preserveSelection) {
        try {
            const st = mf4LoadState();
            const last = Array.isArray(st.signals) ? st.signals.map(x => String(x || '').trim()).filter(Boolean) : [];
            if (last.length) {
                keep = new Set(last);
            } else {
                keep = new Set(mf4GetSelectedSignals());
            }
        } catch (_) {
            keep = new Set(mf4GetSelectedSignals());
        }
    }

    if (preserveSelection && keep && keep.size > 0) {
        try {
            Array.from(signalsSelect.options).forEach(o => {
                o.selected = keep.has(String(o.value || ''));
            });
        } catch (_) {
            // ignore
        }
        mf4SaveState({ signals: mf4GetSelectedSignals() });
    } else if (!preserveSelection) {
        // Reset signal selection when message selection changes.
        mf4SaveState({ signals: [] });
    }

    // Rebuild caches + apply existing signal filter.
    mf4BuildSignalCache();
    mf4ApplySignalFilter();

    const selSignals = mf4GetSelectedSignals();
    if (plotBtn) plotBtn.disabled = (selSignals.length === 0);
    if (exportBtn) exportBtn.disabled = (selSignals.filter(s => String(s).includes('.')).length === 0);
    if (selectAllBtn) selectAllBtn.disabled = !(signalsSelect && signalsSelect.options && signalsSelect.options.length > 0);
}

function mf4BuildSignalCache() {
    const signalsSelect = document.getElementById('mf4-signals-select');
    if (!signalsSelect) {
        mf4SignalCache = null;
        return;
    }
    const opts = Array.from(signalsSelect.options);
    mf4SignalCache = opts.map(o => {
        const group = o.parentElement && o.parentElement.tagName === 'OPTGROUP'
            ? String(o.parentElement.label || '')
            : '';
        return {
            value: String(o.value || ''),
            text: String(o.text || ''),
            group,
        };
    });
}

function mf4RenderSignalsFiltered(queryText) {
    const signalsSelect = document.getElementById('mf4-signals-select');
    if (!signalsSelect) return;

    if (!Array.isArray(mf4SignalCache)) {
        mf4BuildSignalCache();
    }
    const cache = Array.isArray(mf4SignalCache) ? mf4SignalCache : [];

    const normalize = (s) => String(s || '')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, ' ')
        .trim();
    const qNorm = normalize(queryText);
    const tokens = qNorm ? qNorm.split(/\s+/).filter(Boolean) : [];

    const selected = new Set(mf4GetSelectedSignals());

    const frag = document.createDocumentFragment();
    const ogMap = new Map();

    let visible = 0;
    const total = cache.length;

    const getOrCreateGroup = (label) => {
        const key = String(label || '');
        if (!key) return null;
        if (ogMap.has(key)) return ogMap.get(key);
        const og = document.createElement('optgroup');
        og.label = key;
        ogMap.set(key, og);
        frag.appendChild(og);
        return og;
    };

    cache.forEach((it) => {
        const val = String(it.value || '');
        if (!val) return;

        const hay = normalize(`${it.value || ''} ${it.text || ''} ${it.group || ''}`);
        const match = (tokens.length === 0) ? true : tokens.every(t => hay.includes(t));
        const show = match || selected.has(val);
        if (!show) return;

        const opt = document.createElement('option');
        opt.value = val;
        opt.text = String(it.text || val);
        opt.selected = selected.has(val);
        visible += 1;

        const og = getOrCreateGroup(it.group);
        if (og) {
            og.appendChild(opt);
        } else {
            frag.appendChild(opt);
        }
    });

    signalsSelect.innerHTML = '';
    signalsSelect.appendChild(frag);
    mf4UpdateSignalCount(visible, total);
}

function mf4ApplySignalFilter() {
    const signalsSelect = document.getElementById('mf4-signals-select');
    const signalFilter = document.getElementById('mf4-signal-filter');
    if (!signalsSelect) return;

    const rawQ = String(signalFilter?.value || '').trim();
    mf4RenderSignalsFiltered(rawQ);
}

function mf4SetStatus(text, isError = false) {
    const el = document.getElementById('mf4-status');
    if (!el) return;
    el.textContent = text;
    el.className = isError ? 'mt-2 text-danger small' : 'mt-2 text-muted small';
}

async function mf4GetFileInfo(file) {
    const f = String(file || '').trim();
    if (!f) return null;
    try {
        if (mf4FileInfoCache && mf4FileInfoCache.has(f)) return mf4FileInfoCache.get(f);
    } catch (_) {
        // ignore
    }
    try {
        const info = await mf4FetchJson(`/api/mf4/info?file=${encodeURIComponent(f)}`);
        try { mf4FileInfoCache.set(f, info); } catch (_) {}
        return info;
    } catch (_) {
        return null;
    }
}

async function mf4EnsureLoaded() {
    if (mf4FilesLoadedOnce) return;
    await mf4LoadDbcs();
    await mf4LoadFiles();
    mf4FilesLoadedOnce = true;
}

async function mf4UploadSelectedFile() {
    const input = document.getElementById('mf4-upload-input');
    const btn = document.getElementById('mf4-upload-btn');
    const fileSelect = document.getElementById('mf4-file-select');
    if (!input) return;

    const f = input.files && input.files[0] ? input.files[0] : null;
    if (!f) {
        mf4SetStatus('Select an MF4 file to upload', true);
        return;
    }
    const name = String(f.name || '');
    if (!name.toLowerCase().match(/\.(mf4|mdf|dat)$/i)) {
        mf4SetStatus('Only .mf4, .mdf, .dat files are allowed', true);
        return;
    }

    const fd = new FormData();
    fd.append('file', f, f.name);

    try {
        if (btn) btn.disabled = true;
        mf4SetStatus('Uploading MF4...');

        const res = await mf4FetchJson('/api/mf4/upload', {
            method: 'POST',
            body: fd,
        });

        if (!res?.ok) {
            mf4SetStatus(res?.error ? String(res.error) : 'Upload failed', true);
            return;
        }

        const uploadedName = String(res.name || '').trim();
        mf4SetStatus(`Upload complete (${uploadedName}). Parsing file...`);
        // Small delay to allow UI to render status
        await new Promise(r => setTimeout(r, 100));

        await mf4LoadFiles();

        if (uploadedName && fileSelect && Array.from(fileSelect.options).some(o => o.value === uploadedName)) {
            fileSelect.value = uploadedName;
            input.value = '';
            await mf4LoadSignalsForSelectedFile();
            mf4SetStatus(`Uploaded & Loaded: ${uploadedName}`);
        } else {
            mf4SetStatus('Uploaded. Refresh list to select the file');
        }
    } catch (e) {
        console.error(e);
        mf4SetStatus(`Upload error: ${e}`, true);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function mf4LoadDbcs() {
    const dbcSelect = document.getElementById('mf4-dbc-select');
    if (!dbcSelect) return;

    try {
        // Fetch DBC, FIBEX, and ARXML databases in parallel
        const [dbcs, fibexs, arxmls] = await Promise.all([
            mf4FetchJson('/api/dbcs'),
            mf4FetchJson('/api/fibexs').catch(() => []),
            mf4FetchJson('/api/arxmls').catch(() => []),
        ]);

        const prev = Array.from(dbcSelect.selectedOptions || []).map(o => String(o.value || '').trim());
        dbcSelect.innerHTML = '';

        const optNone = document.createElement('option');
        optNone.value = '';
        optNone.text = 'Auto (use config mapping per channel)';
        dbcSelect.appendChild(optNone);

        // DBC files
        const list = Array.isArray(dbcs) ? dbcs : [];
        list.forEach((d) => {
            const name = String(d?.name || d || '').trim();
            if (!name) return;
            const opt = document.createElement('option');
            opt.value = 'dbc:' + name;
            opt.text = '[DBC] ' + name;
            dbcSelect.appendChild(opt);
        });

        // ARXML files
        const arxmlList = Array.isArray(arxmls) ? arxmls : [];
        if (arxmlList.length > 0) {
            const opt = document.createElement('option');
            opt.value = 'arxml:__all__';
            opt.text = '[ARXML] Auto (catalogo caricato)';
            dbcSelect.appendChild(opt);
        }

        // FIBEX files
        const fibexList = Array.isArray(fibexs) ? fibexs : [];
        fibexList.forEach((f) => {
            const name = String(f || '').trim();
            if (!name) return;
            const opt = document.createElement('option');
            opt.value = 'fibex:' + name;
            opt.text = '[FIBEX] ' + name;
            dbcSelect.appendChild(opt);
        });

        const st = mf4LoadState();
        const savedList = Array.isArray(st.dbcs) ? st.dbcs.map(x => String(x || '').trim()).filter(Boolean) : [];
        const savedSingle = String(st.dbc || '').trim();
        const exists = (v) => Array.from(dbcSelect.options).some(o => o.value === v);

        let toSelect = [];
        if (savedList.length) {
            toSelect = savedList.filter(exists);
        } else if (savedSingle && exists(savedSingle)) {
            toSelect = [savedSingle];
        } else if (Array.isArray(prev) && prev.length) {
            toSelect = prev.filter(exists);
        } else if (list.length === 1 && !arxmlList.length && !fibexList.length) {
            toSelect = ['dbc:' + String(list[0]?.name || list[0] || '')].filter(Boolean);
        }

        if (toSelect.length) {
            Array.from(dbcSelect.options).forEach(o => { o.selected = toSelect.includes(o.value); });
            mf4SaveState({ dbcs: toSelect, dbc: toSelect[0] || '' });
        } else {
            // Auto
            Array.from(dbcSelect.options).forEach(o => { o.selected = (o.value === ''); });
            mf4SaveState({ dbcs: [], dbc: '' });
        }
    } catch (e) {
        console.error(e);
    }
}

async function mf4LoadChannelsForSelectedFile(file) {
    const chSelect = document.getElementById('mf4-channel-select');
    if (!chSelect) return;

    try {
        chSelect.innerHTML = '';
        const optAll = document.createElement('option');
        optAll.value = '';
        optAll.text = 'All channels';
        chSelect.appendChild(optAll);

        const res = await mf4FetchJson(`/api/mf4/raw_channels?file=${encodeURIComponent(file)}`);
        const chans = Array.isArray(res?.channels) ? res.channels : [];
        chans.forEach((c) => {
            const v = String(c);
            if (!v) return;
            const opt = document.createElement('option');
            opt.value = v;
            opt.text = `CH ${v}`;
            chSelect.appendChild(opt);
        });

        const st = mf4LoadState();
        const saved = String(st.channel ?? '').trim();
        if (saved !== '' && Array.from(chSelect.options).some(o => o.value === saved)) {
            chSelect.value = saved;
        } else {
            chSelect.value = '';
        }
    } catch (e) {
        console.error(e);
        // keep All
    }
}

function mf4GetSelectedDbcs() {
    const dbcSelect = document.getElementById('mf4-dbc-select');
    if (!dbcSelect) return { auto: true, dbcs: [], arxml: false, fibex: '' };
    const values = Array.from(dbcSelect.selectedOptions || []).map(o => String(o.value || '').trim());
    if (values.includes('') || values.length === 0) {
        return { auto: true, dbcs: [], arxml: false, fibex: '' };
    }

    // Parse prefixed values: "dbc:name", "arxml:__all__", "fibex:name"
    const dbcs = [];
    let arxml = false;
    let fibex = '';
    for (const v of values) {
        if (v.startsWith('arxml:')) {
            arxml = true;
        } else if (v.startsWith('fibex:')) {
            fibex = v.substring(6);
        } else if (v.startsWith('dbc:')) {
            dbcs.push(v.substring(4));
        } else {
            // Legacy: plain DBC name (backward compat)
            dbcs.push(v);
        }
    }
    return { auto: false, dbcs: dbcs.filter(Boolean), arxml, fibex };
}

function mf4GetSelectedChannel() {
    const chSelect = document.getElementById('mf4-channel-select');
    const v = String(chSelect?.value ?? '').trim();
    if (!v) return null;
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : null;
}

async function mf4LoadFiles() {
    const fileSelect = document.getElementById('mf4-file-select');
    const signalsSelect = document.getElementById('mf4-signals-select');
    const plotBtn = document.getElementById('mf4-plot');
    if (plotBtn) plotBtn.disabled = true;
    if (signalsSelect) signalsSelect.innerHTML = '';

    mf4SetStatus('Loading MF4 files...');

    try {
        if (mf4FileInfoCache) mf4FileInfoCache = new Map();
    } catch (_) {
        mf4FileInfoCache = new Map();
    }

    try {
        const files = await mf4FetchJson('/api/mf4/files');

        if (!fileSelect) return;
        fileSelect.innerHTML = '';

        if (!Array.isArray(files) || files.length === 0) {
            const opt = document.createElement('option');
            opt.value = '';
            opt.disabled = true;
            opt.selected = true;
            opt.text = 'No MF4 files found';
            fileSelect.appendChild(opt);
            mf4SetStatus('No MF4 files found');
            return;
        }

        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.disabled = true;
        placeholder.selected = true;
        placeholder.text = 'Select MF4 file...';
        fileSelect.appendChild(placeholder);

        files.forEach(f => {
            const name = String(f?.name || '').trim();
            if (!name) return;
            const opt = document.createElement('option');
            opt.value = name;
            opt.text = name;
            fileSelect.appendChild(opt);
        });

        // Restore last selected file if present
        const st = mf4LoadState();
        const lastFile = String(st.file || '').trim();
        if (lastFile && Array.from(fileSelect.options).some(o => o.value === lastFile)) {
            fileSelect.value = lastFile;
            await mf4LoadSignalsForSelectedFile();
        }

        mf4SetStatus(fileSelect.value ? 'Select one or more signals' : 'Select an MF4 file');
    } catch (e) {
        console.error(e);
        mf4SetStatus(`MF4 files error: ${e}`, true);
    }
}

async function mf4LoadSignalsForSelectedFile() {
    const fileSelect = document.getElementById('mf4-file-select');
    const dbcSelect = document.getElementById('mf4-dbc-select');
    const chSelect = document.getElementById('mf4-channel-select');
    const messagesWrap = document.getElementById('mf4-messages-wrap');
    const messagesSelect = document.getElementById('mf4-messages-select');
    const messageFilter = document.getElementById('mf4-message-filter');
    const signalsSelect = document.getElementById('mf4-signals-select');
    const plotBtn = document.getElementById('mf4-plot');
    const exportBtn = document.getElementById('mf4-export-decoded');
    const selectAllBtn = document.getElementById('mf4-select-all');
    if (plotBtn) plotBtn.disabled = true;
    if (!fileSelect || !signalsSelect) return;

    const file = String(fileSelect.value || '').trim();
    if (!file) return;

    await mf4LoadChannelsForSelectedFile(file);

    const info = await mf4GetFileInfo(file);
    const isRawCan = (info && info.ok && String(info.kind) === 'raw_can');
    if (dbcSelect) dbcSelect.disabled = !isRawCan;
    if (chSelect) chSelect.disabled = !isRawCan;

    // Messages UI is only meaningful for decoded view (raw CAN + DBC).
    mf4SetMessagesVisible(false);
    mf4DecodedGroupsCache = null;
    mf4MessageCache = null;
    if (messagesSelect) messagesSelect.innerHTML = '';
    mf4UpdateMessageCount(0, 0);

    const { auto, dbcs, arxml, fibex } = mf4GetSelectedDbcs();
    const ch = mf4GetSelectedChannel();
    if (!isRawCan && (dbcs.length || arxml || fibex || !auto)) {
        // Non-raw MF4: ignore DBC/channel decode mode
        if (dbcSelect) Array.from(dbcSelect.options).forEach(o => { o.selected = (o.value === ''); });
        if (chSelect) chSelect.value = '';
        mf4SaveState({ dbcs: [], dbc: '', channel: '' });
    } else {
        mf4SaveState({ dbcs, dbc: dbcs[0] || '', channel: (ch === null ? '' : String(ch)) });
    }

    signalsSelect.innerHTML = '';
    if (info && info.ok && !isRawCan) {
        mf4SetStatus('MF4 “misurato”: DBC decoding disabilitato (uso canali diretti).');
    } else {
        mf4SetStatus('Loading signals...');
    }

    try {
        if (isRawCan && (dbcs.length > 0 || arxml || fibex || auto)) {
            const qs = new URLSearchParams();
            qs.set('file', file);
            if (ch !== null) qs.set('channel', String(ch));
            if (arxml) {
                qs.set('arxml', '1');
            } else if (fibex) {
                qs.set('fibex', fibex);
            } else if (auto && dbcs.length === 0) {
                qs.set('auto', '1');
            } else {
                dbcs.forEach(d => qs.append('dbc', d));
            }
            const decoded = await mf4FetchJson(`/api/mf4/decoded_signals?${qs.toString()}`);
            if (!decoded?.ok || !Array.isArray(decoded.groups)) {
                mf4SetStatus(decoded?.error ? String(decoded.error) : 'Failed to load decoded signals', true);
                return;
            }
            if (decoded.groups.length === 0) {
                mf4SetStatus('No decoded messages found in this MF4 (try a different database or log file)', true);
                mf4UpdateSignalCount(0, 0);
                return;
            }

            // Populate Messages selector from decoded groups.
            mf4DecodedGroupsCache = decoded.groups;
            mf4BuildMessageCacheFromGroups(decoded.groups);
            mf4SetMessagesVisible(true);

            if (messagesSelect) {
                messagesSelect.innerHTML = '';
                const st = mf4LoadState();
                const saved = Array.isArray(st.messages) ? st.messages.map(x => String(x || '').trim()).filter(Boolean) : [];
                const savedSet = new Set(saved);
                const savedNone = !!st.messages_none;

                const cache = Array.isArray(mf4MessageCache) ? mf4MessageCache : [];
                cache.forEach((it) => {
                    const v = String(it.value || '').trim();
                    if (!v) return;
                    const opt = document.createElement('option');
                    opt.value = v;
                    opt.text = String(it.text || v);
                    opt.selected = savedNone ? false : (saved.length ? savedSet.has(v) : true);
                    messagesSelect.appendChild(opt);
                });

                // Apply optional message filter (keeps selected visible).
                mf4ApplyMessageFilter();
                mf4SaveState({ messages: mf4GetSelectedMessages(), messages_none: savedNone && (mf4GetSelectedMessages().length === 0) });
            }

            // Build Signals list only from selected messages.
            mf4RebuildSignalsFromDecodedCachePreserve();
            // Note: mf4RebuildSignalsFromDecodedCachePreserve() already rebuilds caches + applies signal filter.
            mf4SetStatus('Select one or more signals');
            return;
        } else {
            // Fallback: raw MF4 channels
            const data = await mf4FetchJson(`/api/mf4/signals?file=${encodeURIComponent(file)}`);
            if (!data?.ok) {
                mf4SetStatus(data?.error ? String(data.error) : 'Failed to load signals', true);
                return;
            }

            const signals = Array.isArray(data.signals) ? data.signals : [];
            if (signals.length === 0) {
                mf4SetStatus('No signals found');
                mf4UpdateSignalCount(0, 0);
                return;
            }

            signals.forEach(name => {
                const opt = document.createElement('option');
                opt.value = String(name);
                opt.text = String(name);
                signalsSelect.appendChild(opt);
            });
        }

        // Restore last signal selection (best-effort)
        const st = mf4LoadState();
        const last = Array.isArray(st.signals) ? st.signals.map(x => String(x)) : [];
        if (last.length > 0) {
            const wanted = new Set(last);
            Array.from(signalsSelect.options).forEach(o => {
                o.selected = wanted.has(o.value);
            });
        }

        // Cache full list for reliable filtering (some browsers don't respect option.hidden).
        mf4BuildSignalCache();

        mf4ApplySignalFilter();

        const sel = mf4GetSelectedSignals();
        if (plotBtn) plotBtn.disabled = (sel.length === 0);
        if (exportBtn) exportBtn.disabled = (sel.filter(s => String(s).includes('.')).length === 0);
        if (selectAllBtn) selectAllBtn.disabled = !(signalsSelect && signalsSelect.options && signalsSelect.options.length > 0);
        mf4SetStatus('Select one or more signals');
    } catch (e) {
        console.error(e);
        mf4SetStatus(`Signals error: ${e}`, true);
    }
}

function mf4GetSelectedSignals() {
    const signalsSelect = document.getElementById('mf4-signals-select');
    if (!signalsSelect) return [];
    return Array.from(signalsSelect.selectedOptions).map(o => String(o.value)).filter(Boolean);
}

function mf4GetAxisMode() {
    const axis = document.getElementById('mf4-axis-mode');
    const v = String(axis?.value || 'single');
    return (v === 'multi') ? 'multi' : 'single';
}

async function mf4FetchJson(url, options) {
    const r = await fetch(url, options);
    const ct = String(r.headers.get('content-type') || '');
    let payload = null;
    if (ct.includes('application/json')) {
        payload = await r.json();
    } else {
        const txt = await r.text();
        const preview = String(txt || '').slice(0, 400);
        throw new Error(`HTTP ${r.status} ${r.statusText}${preview ? `: ${preview}` : ''}`);
    }
    if (!r.ok) {
        const msg = payload?.error ? String(payload.error) : `HTTP ${r.status} ${r.statusText}`;
        throw new Error(msg);
    }
    return payload;
}

async function mf4PlotSelectedSignals() {
    const fileSelect = document.getElementById('mf4-file-select');
    const dbcSelect = document.getElementById('mf4-dbc-select');
    const chSelect = document.getElementById('mf4-channel-select');
    const plotDiv = document.getElementById('mf4-plot-area');
    const plot2Wrap = document.getElementById('mf4-plot-area-2-wrap');
    const plotDiv2 = document.getElementById('mf4-plot-area-2');
    const plotBtn = document.getElementById('mf4-plot');
    const startInput = document.getElementById('mf4-start-s');
    const endInput = document.getElementById('mf4-end-s');
    const maxPointsInput = document.getElementById('mf4-max-points');
    if (!fileSelect || !plotDiv) return;

    const file = String(fileSelect.value || '').trim();
    const signals = mf4GetSelectedSignals();
    if (!file || signals.length === 0) return;

    const { auto, dbcs, arxml, fibex } = mf4GetSelectedDbcs();
    const ch = mf4GetSelectedChannel();

    if (!window.Plotly) {
        mf4SetStatus('Plotly not available (check network access)', true);
        return;
    }

    const axisMode = mf4GetAxisMode();
    const maxSignalsForMultiAxis = 6;
    const effectiveAxisMode = (axisMode === 'multi' && signals.length <= maxSignalsForMultiAxis) ? 'multi' : 'single';

    let start_s = null;
    let end_s = null;
    let max_points = 5000;
    try {
        const v = String(startInput?.value || '').trim();
        start_s = v === '' ? null : parseFloat(v);
        if (start_s !== null && (!Number.isFinite(start_s) || start_s < 0)) start_s = null;
    } catch (_) {
        start_s = null;
    }
    try {
        const v = String(endInput?.value || '').trim();
        end_s = v === '' ? null : parseFloat(v);
        if (end_s !== null && (!Number.isFinite(end_s) || end_s < 0)) end_s = null;
    } catch (_) {
        end_s = null;
    }
    try {
        const v = parseInt(String(maxPointsInput?.value || '5000'), 10);
        max_points = Number.isFinite(v) ? v : 5000;
    } catch (_) {
        max_points = 5000;
    }
    max_points = Math.max(100, Math.min(max_points, 20000));

    mf4SaveState({ file, signals, start_s: startInput?.value ?? '', end_s: endInput?.value ?? '', max_points });

    mf4SetStatus('Loading data...');
    if (plotBtn) plotBtn.disabled = true;

    try {
        const info = await mf4GetFileInfo(file);
        const isRawCan = (info && info.ok && String(info.kind) === 'raw_can');
        const useDecoded = isRawCan && (dbcs.length > 0 || arxml || fibex || auto) && signals.some(s => String(s).includes('.'));
        const url = useDecoded ? '/api/mf4/decoded_data' : '/api/mf4/data';
        const body = useDecoded
            ? { file, dbcs, auto: !!auto, arxml: !!arxml, fibex: fibex || '', channel: (ch === null ? null : ch), signals, start_s, end_s, max_points }
            : { file, signals, start_s, end_s, max_points };

        let data = null;
        try {
            data = await mf4FetchJson(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } catch (e) {
            const msg = String(e || '');
            // Auto-recover: measured/decoded MF4 (ETAS/INCA style) has no raw CAN table.
            // In that case, plot MF4 channels directly.
            if (useDecoded && msg.includes('mf4 non contiene tabella CAN raw')) {
                mf4SetStatus('MF4 “misurato/decodificato”: plot dei canali diretti (no RAW CAN).');
                data = await mf4FetchJson('/api/mf4/data', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file, signals, start_s, end_s, max_points }),
                });
            } else {
                throw e;
            }
        }
        if (!data?.ok) {
            mf4SetStatus(data?.error ? String(data.error) : 'Failed to load MF4 data', true);
            return;
        }

        const series = Array.isArray(data.series) ? data.series : [];
        if (series.length === 0) {
            mf4SetStatus('No numeric data for selected signals', true);
            try { window.Plotly.purge(plotDiv); } catch (_) {}
            return;
        }

        const incomingByName = new Map();
        series.forEach((s) => {
            const k = String(s?.name || '').trim();
            if (!k) return;
            incomingByName.set(k, s);
        });

        const plot1HasAny = mf4PlotState.plot1.seriesByName.size > 0;
        const hasNewComparedToPlot1 = plot1HasAny
            ? Array.from(incomingByName.keys()).some(k => !mf4PlotState.plot1.seriesByName.has(k))
            : false;

        // Decide where to render.
        // Default: overwrite plot1 (so users can replace/remove by re-plotting).
        // If plot1 already exists and selection adds new signals, ask user:
        //  - add into plot1 (optionally multi-axis), or
        //  - open in plot2.
        let target = 'plot1';
        let mode = 'overwrite'; // overwrite|append
        let forceMultiAxis = false;
        if (plot1HasAny && hasNewComparedToPlot1) {
            const addToExisting = window.confirm(
                'Hai già un grafico.\n\nOK = aggiungi i nuovi segnali al grafico esistente\nAnnulla = apri un secondo grafico'
            );
            if (addToExisting) {
                target = 'plot1';
                mode = 'append';
                forceMultiAxis = window.confirm('Vuoi usare una scala separata (multi-axis) nel grafico esistente?');
            } else {
                target = 'plot2';
                mode = 'overwrite';
            }
        }

        const renderPlot = async (plotEl, seriesToRender, axisModeToUse) => {
            const traces = [];
            const layout = {
                margin: { l: 60, r: 60, t: 20, b: 50 },
                xaxis: { title: 't [s]' },
                yaxis: { title: '' },
                legend: { orientation: 'h' },
            };

            // If plotting a single categorical/enum signal, show y tick labels.
            try {
                if (axisModeToUse !== 'multi' && Array.isArray(seriesToRender) && seriesToRender.length === 1) {
                    const s0 = seriesToRender[0] || {};
                    const cats = Array.isArray(s0.categories) ? s0.categories : null;
                    if (cats && cats.length > 0) {
                        layout.yaxis.tickmode = 'array';
                        layout.yaxis.tickvals = cats.map((_, i) => i);
                        layout.yaxis.ticktext = cats;
                    }
                }
            } catch (_) {
                // ignore
            }

            if (axisModeToUse === 'multi') {
                const unitKeys = [];
                const unitToAxisIndex = {};
                const axisTitleForUnit = (u) => (u ? String(u) : '');

                seriesToRender.forEach((s) => {
                    const unit = String(s.unit || '').trim();
                    const key = unit || '__no_unit__';
                    if (unitToAxisIndex[key] === undefined) {
                        unitToAxisIndex[key] = unitKeys.length;
                        unitKeys.push(key);
                    }
                });

                unitKeys.forEach((key, idx) => {
                    const axisKey = idx === 0 ? 'yaxis' : `yaxis${idx + 1}`;
                    const unit = key === '__no_unit__' ? '' : key;
                    if (idx === 0) {
                        layout[axisKey] = { title: axisTitleForUnit(unit) };
                    } else {
                        const pos = Math.max(0.72, 1.0 - (idx - 1) * 0.06);
                        layout[axisKey] = {
                            title: axisTitleForUnit(unit),
                            overlaying: 'y',
                            side: 'right',
                            position: pos,
                            showgrid: false,
                            zeroline: false,
                        };
                    }
                });

                seriesToRender.forEach((s) => {
                    const unit = String(s.unit || '').trim();
                    const key = unit || '__no_unit__';
                    const axisIdx = unitToAxisIndex[key] || 0;
                    const yaxisName = axisIdx === 0 ? 'y' : `y${axisIdx + 1}`;
                    const label = unit ? `${s.name} [${unit}]` : String(s.name);
                    const isCat = !!s.categorical || Array.isArray(s.text);
                    const hover = isCat && Array.isArray(s.text) && s.text.length === (Array.isArray(s.t) ? s.t.length : 0)
                        ? { text: s.text, hovertemplate: '%{text}<br>t=%{x:.3f}<extra></extra>' }
                        : {};
                    traces.push({
                        type: 'scatter',
                        mode: 'lines',
                        name: label,
                        x: s.t,
                        y: s.y,
                        yaxis: yaxisName,
                        ...(isCat ? { line: { shape: 'hv' } } : {}),
                        ...hover,
                    });
                });
            } else {
                seriesToRender.forEach((s) => {
                    const unit = String(s.unit || '').trim();
                    const name = unit ? `${s.name} [${unit}]` : String(s.name);
                    const isCat = !!s.categorical || Array.isArray(s.text);
                    const hover = isCat && Array.isArray(s.text) && s.text.length === (Array.isArray(s.t) ? s.t.length : 0)
                        ? { text: s.text, hovertemplate: '%{text}<br>t=%{x:.3f}<extra></extra>' }
                        : {};
                    traces.push({
                        type: 'scatter',
                        mode: 'lines',
                        name,
                        x: s.t,
                        y: s.y,
                        ...(isCat ? { line: { shape: 'hv' } } : {}),
                        ...hover,
                    });
                });
            }

            await window.Plotly.newPlot(plotEl, traces, layout, {
                responsive: true,
                displaylogo: false,
            });
        };

        if (target === 'plot2') {
            mf4PlotState.plot2.seriesByName = new Map(incomingByName);
            mf4PlotState.plot2.axisMode = effectiveAxisMode;
            if (plot2Wrap) plot2Wrap.style.display = '';
            if (plotDiv2) {
                await renderPlot(plotDiv2, Array.from(mf4PlotState.plot2.seriesByName.values()), mf4PlotState.plot2.axisMode);
            }
            mf4SetStatus('Interactive plot ready (second plot)');
            return;
        }

        // plot1
        if (mode === 'overwrite' || !plot1HasAny) {
            mf4PlotState.plot1.seriesByName = new Map(incomingByName);
        } else {
            incomingByName.forEach((v, k) => mf4PlotState.plot1.seriesByName.set(k, v));
        }
        mf4PlotState.plot1.axisMode = forceMultiAxis ? 'multi' : effectiveAxisMode;

        await renderPlot(plotDiv, Array.from(mf4PlotState.plot1.seriesByName.values()), mf4PlotState.plot1.axisMode);

        mf4SetStatus(mf4PlotState.plot1.axisMode === 'multi'
            ? 'Interactive plot ready (multi axis)'
            : 'Interactive plot ready');
    } catch (e) {
        console.error(e);
        mf4SetStatus(`Plot error: ${e}`, true);
    } finally {
        if (plotBtn) plotBtn.disabled = (mf4GetSelectedSignals().length === 0);
    }
}

function getSelectedChannelId() {
    const val = scanToolsChannelSelect?.value;
    if (!val) return null;
    const parsed = parseInt(val, 10);
    return Number.isFinite(parsed) ? parsed : null;
}

function updateScanToolsChannelSelect() {
    if (!scanToolsChannelSelect) return;

    const selectedBefore = scanToolsChannelSelect.value;
    const selections = Array.from(document.querySelectorAll('.channel-row .interface-select'))
        .map(sel => ({ value: sel.value, label: sel.options[sel.selectedIndex]?.text || sel.value }))
        .filter(x => x.value);

    scanToolsChannelSelect.innerHTML = '';

    if (selections.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.text = 'Select a channel from Channel Configuration';
        scanToolsChannelSelect.appendChild(opt);
        return;
    }

    selections.forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s.value;
        opt.text = s.label;
        scanToolsChannelSelect.appendChild(opt);
    });

    if (selectedBefore && selections.some(s => s.value === selectedBefore)) {
        scanToolsChannelSelect.value = selectedBefore;
    } else {
        scanToolsChannelSelect.value = selections[0].value;
    }
}

function appendScanLog(line) {
    if (!scanToolsConsole) return;
    if (!scanToolsConsole.textContent || scanToolsConsole.textContent === 'Waiting...') {
        scanToolsConsole.textContent = '';
    }
    scanToolsConsole.textContent += line + '\n';
    scanToolsConsole.scrollTop = scanToolsConsole.scrollHeight;
}

function setLiveUiRunning(running) {
    if (btnLiveStart) btnLiveStart.disabled = !!running;
    if (btnLiveStop) btnLiveStop.disabled = !running;
    if (liveStatus) {
        liveStatus.textContent = running ? 'Running' : 'Stopped';
        liveStatus.className = running ? 'text-warning small' : 'text-muted small';
    }
}

function _isChannelNotActiveError(data) {
    const msg = String(data?.error || data?.status || '').toLowerCase();
    return msg.includes('channel not active');
}

async function _waitForChannelActive(channelId, timeoutMs = 12000) {
    const t0 = Date.now();
    let dynamicTimeoutMs = timeoutMs;
    while ((Date.now() - t0) < dynamicTimeoutMs) {
        try {
            const res = await fetch('/api/runtime/status', { cache: 'no-store' });
            if (res.ok) {
                const st = await res.json();
                const bus = st?.bus || {};
                // On real CAN hardware, opening a channel can sometimes take longer than a few seconds.
                // If the backend reports that it is starting, extend the timeout to avoid false negatives.
                if (bus?.starting && dynamicTimeoutMs < 25000) {
                    dynamicTimeoutMs = 25000;
                }
                if (bus?.running && dynamicTimeoutMs < 20000) {
                    dynamicTimeoutMs = 20000;
                }
                const chans = st?.bus?.channels;
                if (Array.isArray(chans) && chans.map(x => parseInt(x, 10)).includes(parseInt(channelId, 10))) {
                    return true;
                }
            }
        } catch (e) {
            // ignore
        }
        await new Promise(r => setTimeout(r, 400));
    }
    return false;
}

async function _ensureBusStartedForSelectedChannel(channelId) {
    // Start the bus using the current Channel Configuration.
    const channels = getChannelRowsConfig();
    if (!Array.isArray(channels) || channels.length === 0) {
        throw new Error('No channels configured. Add a channel in Channel Configuration first.');
    }

    await fetch('/api/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channels })
    });

    const ok = await _waitForChannelActive(channelId);
    if (!ok) {
        throw new Error('Bus start timed out (channel still inactive).');
    }
}

async function startLiveData() {
    const channelId = getSelectedChannelId();
    // DoIP does not require a CAN channel — allow starting without one.
    const useDoip = (channelId === null);
    setLiveUiRunning(true);
    try {
        const doStart = async () => {
            const body = useDoip
                ? { channel_id: 0, interval_s: 1.0, transport: 'doip' }
                : { channel_id: channelId, interval_s: 0.2 };
            const res = await fetch('/api/scantools/live/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            const data = await res.json();
            return { res, data };
        };

        let { res, data } = await doStart();
        if (!useDoip && !(res.ok && data.status === 'started') && _isChannelNotActiveError(data)) {
            appendScanLog('Channel not active. Auto-starting Bus System…');
            await _ensureBusStartedForSelectedChannel(channelId);
            ({ res, data } = await doStart());
        }

        if (res.ok && data.status === 'started') {
            const t = data.transport || (useDoip ? 'doip' : 'can');
            appendScanLog(`Live Data started (${t.toUpperCase()})`);
        } else {
            const msg = data?.error || data?.status || 'failed';
            setLiveUiRunning(false);
            appendScanLog(`ERROR (live): ${msg}`);
            alert(`Live Data start failed: ${msg}`);
        }
    } catch (e) {
        setLiveUiRunning(false);
        appendScanLog(`ERROR (live): ${e}`);
        alert('Live Data start failed (network error).');
    }
}

async function stopLiveData() {
    try {
        await fetch('/api/scantools/live/stop', { method: 'POST' });
    } catch (e) {
        // ignore
    } finally {
        setLiveUiRunning(false);
    }
}

async function runScanTools(action) {
    let channelId = getSelectedChannelId();
    if (channelId === null) {
        if (action === 'vag_doip_scan_report' || action === 'self_test' || action === 'doip_recover_network' || action === 'doip_clear_dtcs' || action === 'doip_mode06') {
            channelId = 0;
        } else {
            alert('Select a CAN Channel in the ScanTools panel (it is populated from Channel Configuration).');
            return;
        }
    }

    if (scanToolsStatus) {
        const extra = (action === 'vag_doip_scan_report' || action === 'self_test' || action === 'doip_recover_network' || action === 'doip_clear_dtcs' || action === 'doip_mode06') ? '' : ` (channel ${channelId})`;
        scanToolsStatus.textContent = `Running: ${action}${extra}...`;
        scanToolsStatus.className = 'mt-2 text-warning small';
    }

    try {
        const doRun = async () => {
            const res = await fetch('/api/scantools/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ channel_id: channelId, action })
            });
            const data = await res.json();
            return { res, data };
        };

        let { res, data } = await doRun();
        if (!(res.ok && data.status === 'started') && _isChannelNotActiveError(data)) {
            appendScanLog('Channel not active. Auto-starting Bus System…');
            await _ensureBusStartedForSelectedChannel(channelId);
            ({ res, data } = await doRun());
        }

        if (res.ok && data.status === 'started') {
            appendScanLog(`== ${action} started ==`);
            if (scanToolsStatus) {
                scanToolsStatus.textContent = 'Started. Watch output below.';
                scanToolsStatus.className = 'mt-2 text-success small';
            }
        } else {
            const msg = data?.error || data?.status || 'failed';
            appendScanLog(`ERROR: ${msg}`);
            if (scanToolsStatus) {
                scanToolsStatus.textContent = `Failed: ${msg}`;
                scanToolsStatus.className = 'mt-2 text-danger small';
            }
        }
    } catch (e) {
        console.error(e);
        appendScanLog(`ERROR: ${e}`);
        if (scanToolsStatus) {
            scanToolsStatus.textContent = 'Error starting ScanTools.';
            scanToolsStatus.className = 'mt-2 text-danger small';
        }
    }
}

async function loadInterfaces() {
    const res = await fetch('/api/interfaces');
    availableInterfaces = await res.json();
}

async function loadDBCs() {
    const res = await fetch('/api/dbcs');
    availableDBCs = await res.json();
    updateAllDBCSelects();
}

async function loadLogs() {
    const container = document.getElementById('log-files-list');
    container.innerHTML = '<div class="list-group-item text-center text-muted">Loading...</div>';
    
    try {
        const res = await fetch('/api/logs');
        const files = await res.json();
        
        container.innerHTML = '';
        if (files.length === 0) {
            container.innerHTML = '<div class="list-group-item text-center text-muted">No logs found</div>';
            return;
        }

        const formatSize = (bytes) => {
            const n = Number(bytes);
            if (!Number.isFinite(n) || n <= 0) return '0 B';
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let v = n;
            let u = 0;
            while (v >= 1024 && u < units.length - 1) {
                v /= 1024;
                u += 1;
            }
            return `${v.toFixed(v >= 10 || u === 0 ? 0 : 1)} ${units[u]}`;
        };

        files.forEach(f => {
            const item = document.createElement('div');
            item.className = 'list-group-item d-flex justify-content-between align-items-center p-2';
            const sizeBytes = (f?.size_total != null) ? f.size_total : f.size;
            const sizeStr = formatSize(sizeBytes);
            const isMf4 = String(f.name || '').toLowerCase().endsWith('.mf4');
            const isTmpMf4 = String(f.name || '').toLowerCase().endsWith('.tmp.mf4');
            const partCount = Number.isFinite(Number(f?.part_count)) ? Number(f.part_count) : 0;
            const partSuffix = (partCount > 1) ? ` (${partCount} parts)` : '';
            
            item.innerHTML = `
                <div class="text-truncate me-2" title="${f.name}">
                    <small class="fw-bold d-block">${f.name}</small>
                    <small class="text-muted">${sizeStr}${partSuffix}</small>
                </div>
                <div class="d-flex gap-2">
                    ${isMf4 && !isTmpMf4 ? `
                    <button type="button" class="btn btn-sm btn-outline-secondary btn-mf4-decode" data-filename="${encodeURIComponent(f.name)}" title="Decode MF4 to CSV (offline)">
                        CSV
                    </button>
                    ` : ''}
                    <a href="/api/logs/${encodeURIComponent(f.name)}" class="btn btn-sm btn-outline-primary" download="${f.name}" title="Download">
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-download" viewBox="0 0 16 16">
                            <path d="M.5 9.9a.5.5 0 0 1 .5.5v2.5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2.5a.5.5 0 0 1 1 0v2.5a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2v-2.5a.5.5 0 0 1 .5-.5z"/>
                            <path d="M7.646 11.854a.5.5 0 0 0 .708 0l3-3a.5.5 0 0 0-.708-.708L8.5 10.293V1.5a.5.5 0 0 0-1 0v8.793L5.354 8.146a.5.5 0 1 0-.708.708l3 3z"/>
                        </svg>
                    </a>
                    <button type="button" class="btn btn-sm btn-outline-danger btn-log-delete" data-filename="${encodeURIComponent(f.name)}" title="Delete">
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-trash" viewBox="0 0 16 16">
                            <path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0V6z"/>
                            <path d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1v1zM4.118 4 4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4H4.118zM2.5 3h11V2h-11v1z"/>
                        </svg>
                    </button>
                </div>
            `;
            container.appendChild(item);
        });

        // Bind delete buttons
        container.querySelectorAll('.btn-log-delete').forEach(btn => {
            btn.addEventListener('click', async () => {
                const encName = btn.getAttribute('data-filename');
                const name = decodeURIComponent(encName || '');
                if (!name) return;
                if (!confirm(`Delete log file "${name}"?`)) return;

                // Re-sync local state (stop/start can happen in another tab / trigger).
                try {
                    await refreshLoggingStatus();
                } catch (e) {
                    // ignore
                }

                const ensureStopped = async () => {
                    try {
                        await refreshLoggingStatus();
                    } catch (e) {}
                    if (!loggingActive) return true;
                    const ok = confirm('Logging is active. Stop logging and delete this file?');
                    if (!ok) return false;
                    try {
                        await fetch('/api/log/stop', { method: 'POST' });
                    } catch (e) {
                        // ignore
                    }
                    try {
                        await refreshLoggingStatus();
                    } catch (e) {}
                    return !loggingActive;
                };

                if (!(await ensureStopped())) {
                    alert('Stop logging before deleting log files.');
                    return;
                }
                try {
                    const resp = await fetch(`/api/logs/${encodeURIComponent(name)}`, { method: 'DELETE' });
                    if (!resp.ok) {
                        if (resp.status === 409) {
                            // Backend says logging is still active; offer stop+retry.
                            const ok = await ensureStopped();
                            if (!ok) {
                                alert('Stop logging before deleting log files.');
                                return;
                            }
                            const retry = await fetch(`/api/logs/${encodeURIComponent(name)}`, { method: 'DELETE' });
                            if (!retry.ok) {
                                const txt2 = await retry.text();
                                alert(`Delete failed (${retry.status}): ${txt2}`);
                                return;
                            }
                        } else {
                            const txt = await resp.text();
                            alert(`Delete failed (${resp.status}): ${txt}`);
                            return;
                        }
                    }
                } catch (e) {
                    console.error(e);
                    alert(`Delete error: ${e}`);
                    return;
                }
                await loadLogs();
            });
        });

        // Bind MF4 decode buttons
        container.querySelectorAll('.btn-mf4-decode').forEach(btn => {
            btn.addEventListener('click', async () => {
                const encName = btn.getAttribute('data-filename');
                const name = decodeURIComponent(encName || '');
                if (!name) return;

                try {
                    if (!Array.isArray(availableDBCs) || availableDBCs.length === 0) {
                        await loadDBCs();
                    }
                } catch (e) {
                    // ignore
                }

                const dbcNames = (Array.isArray(availableDBCs) ? availableDBCs : [])
                    .map(x => (typeof x === 'string' ? x : (x && x.name ? String(x.name) : '')))
                    .map(s => String(s || '').trim())
                    .filter(Boolean);

                if (!dbcNames.length) {
                    alert('No DBCs available. Upload a DBC first.');
                    return;
                }

                const def = dbcNames[0];
                const raw = prompt(
                    'Decode MF4 to CSV. Enter DBC name(s) (comma-separated).\nExample: ' + def,
                    def
                );
                if (raw === null) return;
                const picked = String(raw || '')
                    .split(',')
                    .map(s => String(s || '').trim())
                    .filter(Boolean);
                if (!picked.length) return;

                // Trigger a download (streaming CSV)
                const qs = new URLSearchParams();
                qs.set('file', name);
                picked.forEach(d => qs.append('dbc', d));
                const url = `/api/mf4/decode_csv?${qs.toString()}`;
                try {
                    const w = window.open(url, '_blank');
                    if (!w) {
                        window.location.href = url;
                    }
                } catch (e) {
                    window.location.href = url;
                }
            });
        });

        // Apply current logging state to new buttons
        setLoggingActive(loggingActive);
    } catch (e) {
        console.error(e);
        container.innerHTML = '<div class="list-group-item text-center text-danger">Error loading logs</div>';
    }
}

// UI Helpers
function addChannelRow(initial) {
    const tpl = document.getElementById('channel-row-template');
    const clone = tpl.content.cloneNode(true);
    const idx = channelsContainer.children.length + 1;
    
    clone.querySelector('.row-idx').innerText = idx;
    
    // Populate Interfaces
    const ifSelect = clone.querySelector('.interface-select');
    availableInterfaces.forEach(iface => {
        const opt = document.createElement('option');
        opt.value = iface.id;
        opt.text = `${iface.name} (${iface.upc})`;
        ifSelect.appendChild(opt);
    });
    ifSelect.addEventListener('change', updateScanToolsChannelSelect);

    // Apply initial interface selection if provided
    try {
        if (initial && initial.id !== undefined && initial.id !== null) {
            ifSelect.value = String(initial.id);
        }
    } catch (e) {
        // ignore
    }

    // Populate DBCs first, then apply the saved selection.
    // (Rebuilding <option>s can reset the selected value if we set it too early.)
    const dbcSelect = clone.querySelector('.dbc-select');
    populateDBCSelect(dbcSelect);

    // Quick actions for multi-select DBC
    try {
        const btnAll = clone.querySelector('.dbc-select-all');
        const btnNone = clone.querySelector('.dbc-select-none');
        if (btnAll && dbcSelect) {
            btnAll.addEventListener('click', () => {
                try {
                    Array.from(dbcSelect.options || []).forEach(opt => {
                        const val = String(opt.value || '').trim();
                        opt.selected = !!val;
                    });
                } catch (e) {
                    // ignore
                }
                debounceSaveConfig({ logger_channels: getChannelRowsConfig() });
            });
        }
        if (btnNone && dbcSelect) {
            btnNone.addEventListener('click', () => {
                try {
                    Array.from(dbcSelect.options || []).forEach(opt => {
                        opt.selected = false;
                    });
                } catch (e) {
                    // ignore
                }
                debounceSaveConfig({ logger_channels: getChannelRowsConfig() });
            });
        }
    } catch (e) {
        // ignore
    }

    // Apply initial bitrate/dbc if provided
    try {
        const br = clone.querySelector('.bitrate-select');
        if (br && initial && initial.bitrate !== undefined && initial.bitrate !== null) {
            br.value = String(initial.bitrate);
        }
        if (dbcSelect && initial) {
            let desired = [];
            if (Array.isArray(initial.dbc_names)) {
                desired = initial.dbc_names.map(v => String(v || '').trim()).filter(v => v);
            } else if (typeof initial.dbc_name === 'string') {
                const v = String(initial.dbc_name || '').trim();
                desired = v ? [v] : [];
            }
            applySelectedDbcs(dbcSelect, desired);
        }
    } catch (e) {
        // ignore
    }

    channelsContainer.appendChild(clone);
    updateScanToolsChannelSelect();

    // Auto-save on structural change
    debounceSaveConfig({ logger_channels: getChannelRowsConfig() });
}

function applySelectedDbcs(select, desiredValues) {
    if (!select) return;
    const desired = Array.isArray(desiredValues) ? desiredValues.map(v => String(v || '').trim()).filter(v => v) : [];
    const desiredSet = new Set(desired);
    try {
        Array.from(select.options || []).forEach(opt => {
            const val = String(opt.value || '').trim();
            if (!val) {
                opt.selected = false;
                return;
            }
            opt.selected = desiredSet.has(val);
        });
    } catch (e) {
        // ignore
    }
}

function removeChannelRow(btn) {
    btn.closest('.channel-row').remove();
    updateScanToolsChannelSelect();

    // Auto-save on structural change
    debounceSaveConfig({ logger_channels: getChannelRowsConfig() });
}

function populateDBCSelect(select) {
    if (!select) return;
    const prevSelected = Array.from(select.selectedOptions || []).map(o => String(o.value || '').trim()).filter(v => v);

    // Keep first option (No DBC placeholder)
    while (select.options.length > 1) {
        select.remove(1);
    }
    availableDBCs.forEach(dbc => {
        const opt = document.createElement('option');
        opt.value = dbc;
        opt.text = dbc;
        select.appendChild(opt);
    });

    // Preserve previous selection(s) when possible
    applySelectedDbcs(select, prevSelected);
}

function updateAllDBCSelects() {
    document.querySelectorAll('.dbc-select').forEach(populateDBCSelect);
}

// Controls
if (btnStart) btnStart.onclick = async () => {
    const channels = (getChannelRowsConfig() || []).map(ch => ({
        ...ch,
        type: 'CAN', // Default to CAN for now
    }));

    if (channels.length === 0) {
        alert("Please select at least one interface.");
        return;
    }

    const res = await fetch('/api/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({channels})
    });
    
    const data = await res.json();
    if(data.status === 'started') {
        btnStart.disabled = true;
        btnStop.disabled = false;
        document.querySelectorAll('.channel-row select').forEach(s => s.disabled = true);
        document.querySelectorAll('.channel-row .btn-outline-danger').forEach(b => b.disabled = true); // Disable remove buttons
    }
};

if (btnStop) btnStop.onclick = async () => {
    await fetch('/api/stop', {method: 'POST'});
    if (btnStart) btnStart.disabled = false;
    if (btnStop) btnStop.disabled = true;
    document.querySelectorAll('.channel-row select').forEach(s => s.disabled = false);
    document.querySelectorAll('.channel-row .btn-outline-danger').forEach(b => b.disabled = false);
};

if (btnLogStart) btnLogStart.onclick = async () => {
    try {
        await startAcquisitionManual();
    } catch (e) {
        console.error(e);
        alert(String(e?.message || e));
    }
};

if (btnLogStop) btnLogStop.onclick = async () => {
    try {
        await stopAcquisitionManual();
    } catch (e) {
        console.error(e);
        alert(String(e?.message || e));
    }
};

if (btnLogStartLogger) {
    btnLogStartLogger.onclick = async () => {
        try {
            await startAcquisitionManual();
        } catch (e) {
            console.error(e);
            alert(String(e?.message || e));
        }
    };
}

if (btnLogStopLogger) {
    btnLogStopLogger.onclick = async () => {
        try {
            await stopAcquisitionManual();
        } catch (e) {
            console.error(e);
            alert(String(e?.message || e));
        }
    };
}

// File Uploads
const dbcUploadEl = document.getElementById('dbc-upload');
if (dbcUploadEl) dbcUploadEl.onchange = async (e) => {
    const files = e.target.files;
    if (files.length === 0) return;

    const bad = [];
    const failed = [];
    let okCount = 0;
    for (let i = 0; i < files.length; i++) {
        const f = files[i];
        const name = String(f?.name || '');
        if (!name.toLowerCase().endsWith('.dbc')) {
            bad.push(name || '(unnamed)');
            continue;
        }
        const formData = new FormData();
        formData.append('file', f);
        const res = await fetch('/api/upload_dbc', {method: 'POST', body: formData});
        if (!res.ok) {
            let err = '';
            try { err = String((await res.json())?.error || ''); } catch (_) { err = ''; }
            failed.push(`${name}${err ? `: ${err}` : ''}`);
            continue;
        }
        okCount += 1;
    }

    const parts = [];
    if (okCount) parts.push(`${okCount} DBC uploaded`);
    if (bad.length) parts.push(`skipped (not .dbc): ${bad.join(', ')}`);
    if (failed.length) parts.push(`failed: ${failed.join(', ')}`);
    alert(parts.join('\n'));
    await loadDBCs();
};

const fibexUploadEl = document.getElementById('fibex-upload');
if (fibexUploadEl) {
    fibexUploadEl.onchange = async (e) => {
        const files = e.target.files;
        if (files.length === 0) return;

        const allowed = ['.xml', '.fibex', '.arxml'];
        const bad = [];
        const failed = [];
        let okCount = 0;

        for (let i = 0; i < files.length; i++) {
            const f = files[i];
            const name = String(f?.name || '');
            const lower = name.toLowerCase();
            if (!allowed.some(ext => lower.endsWith(ext))) {
                bad.push(name || '(unnamed)');
                continue;
            }
            const formData = new FormData();
            formData.append('file', f);
            const res = await fetch('/api/upload_fibex', {method: 'POST', body: formData});
            if (!res.ok) {
                let err = '';
                try { err = String((await res.json())?.error || ''); } catch (_) { err = ''; }
                failed.push(`${name}${err ? `: ${err}` : ''}`);
                continue;
            }
            okCount += 1;
        }

        const parts = [];
        if (okCount) parts.push(`${okCount} FIBEX uploaded`);
        if (bad.length) parts.push(`skipped (bad ext): ${bad.join(', ')}`);
        if (failed.length) parts.push(`failed: ${failed.join(', ')}`);
        alert(parts.join('\n'));
    };
}

// ── ARXML (AUTOSAR) Upload & Catalog ──────────────────────────────────────

async function arxmlRefreshCatalog() {
    const infoDiv = document.getElementById('arxml-catalog-info');
    if (!infoDiv) return;
    try {
        const res = await fetch('/api/arxml/catalog');
        const data = await res.json();
        if (!data.ok && !data.catalog) {
            infoDiv.style.display = 'none';
            return;
        }
        const cat = data.catalog || {};
        infoDiv.style.display = '';
        const pduC = document.getElementById('arxml-pdu-count');
        const frameC = document.getElementById('arxml-frame-count');
        const mirrorC = document.getElementById('arxml-mirror-count');
        const someipC = document.getElementById('arxml-someip-count');
        const socketC = document.getElementById('arxml-socket-count');
        if (pduC) pduC.textContent = cat.pdu_count || 0;
        if (frameC) frameC.textContent = cat.frame_count || 0;
        if (mirrorC) mirrorC.textContent = cat.mirror_channel_count || 0;
        if (someipC) someipC.textContent = cat.someip_method_count || 0;
        if (socketC) socketC.textContent = cat.socket_connection_count || 0;

        // Show errors
        const errDiv = document.getElementById('arxml-errors');
        if (errDiv) {
            const errs = cat.errors || [];
            if (errs.length) {
                errDiv.style.display = '';
                errDiv.textContent = errs.join('; ');
            } else {
                errDiv.style.display = 'none';
            }
        }

        // Show file list
        const flist = document.getElementById('arxml-files-list');
        if (flist) {
            const fres = await fetch('/api/arxml/files');
            const fdata = await fres.json();
            const files = (fdata.files || []);
            if (files.length === 0) {
                flist.innerHTML = '<span class="text-muted">No ARXML files uploaded</span>';
                infoDiv.style.display = 'none';
            } else {
                flist.innerHTML = files.map(f =>
                    `<span class="badge bg-secondary me-1 mb-1">${f} <a href="#" class="text-danger ms-1 arxml-del-btn" data-name="${f}" title="Delete">&times;</a></span>`
                ).join('');
                // Attach delete handlers
                flist.querySelectorAll('.arxml-del-btn').forEach(btn => {
                    btn.onclick = async (e) => {
                        e.preventDefault();
                        const name = btn.getAttribute('data-name');
                        if (!confirm(`Eliminare ${name}?`)) return;
                        await fetch('/api/arxml/delete', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({filename: name}),
                        });
                        await arxmlRefreshCatalog();
                    };
                });
            }
        }
    } catch (e) {
        console.warn('ARXML catalog refresh failed:', e);
    }
}

const arxmlUploadEl = document.getElementById('arxml-upload');
if (arxmlUploadEl) {
    arxmlUploadEl.onchange = async (e) => {
        const files = e.target.files;
        if (!files || files.length === 0) return;

        const bad = [];
        const failed = [];
        let okCount = 0;
        let lastCatalog = {};

        for (let i = 0; i < files.length; i++) {
            const f = files[i];
            const name = String(f?.name || '');
            if (!name.toLowerCase().endsWith('.arxml')) {
                bad.push(name || '(unnamed)');
                continue;
            }
            const formData = new FormData();
            formData.append('file', f);
            try {
                const res = await fetch('/api/upload_arxml', {method: 'POST', body: formData});
                const rj = await res.json();
                if (!res.ok) {
                    failed.push(`${name}: ${rj.error || 'error'}`);
                    continue;
                }
                okCount += (rj.uploaded_count || 1);
                lastCatalog = rj.catalog || {};
                if (rj.errors && rj.errors.length) {
                    failed.push(...rj.errors.map(e => `${name}: ${e}`));
                }
            } catch (err) {
                failed.push(`${name}: ${err}`);
            }
        }

        const parts = [];
        if (okCount) parts.push(`${okCount} ARXML uploaded`);
        if (bad.length) parts.push(`Skipped (not .arxml): ${bad.join(', ')}`);
        if (failed.length) parts.push(`Errors: ${failed.join(', ')}`);

        const summary = lastCatalog;
        if (summary && summary.pdu_count !== undefined) {
            parts.push(`\nCatalog: ${summary.pdu_count} PDUs, ${summary.frame_count} frames, ${summary.mirror_channel_count} mirror channels`);
        }
        alert(parts.join('\n'));
        arxmlUploadEl.value = '';
        await arxmlRefreshCatalog();
    };
}

const btnArxmlReload = document.getElementById('btn-arxml-reload');
if (btnArxmlReload) {
    btnArxmlReload.onclick = async () => {
        try {
            await fetch('/api/arxml/reload', {method: 'POST'});
        } catch (_) {}
        await arxmlRefreshCatalog();
    };
}

// Load ARXML catalog on startup
arxmlRefreshCatalog();


// Ethernet Controls
const btnEthStart = document.getElementById('btn-eth-start');
const btnEthStop = document.getElementById('btn-eth-stop');

if (btnEthStart) btnEthStart.onclick = async () => {
    const ethIf = document.getElementById('eth-interface');
    const ethPcap = document.getElementById('eth-pcap');
    const ethDoip = document.getElementById('eth-doip');
    const ethSomeip = document.getElementById('eth-someip');
    const ethXcp = document.getElementById('eth-xcp');
    const ethIp = document.getElementById('eth-target-ip');
    if (!ethIf || !ethPcap || !ethDoip || !ethSomeip || !ethXcp || !ethIp) return;

    const config = {
        interface: ethIf.value,
        pcap_enabled: ethPcap.checked,
        doip_enabled: ethDoip.checked,
        someip_enabled: ethSomeip.checked,
        xcp_enabled: ethXcp.checked,
        doip_ip: ethIp.value,
        xcp_ip: ethIp.value,
        xcp_port: 5555
    };

    await fetch('/api/eth/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(config)
    });
    
    if (btnEthStart) btnEthStart.disabled = true;
    if (btnEthStop) btnEthStop.disabled = false;
};

if (btnEthStop) btnEthStop.onclick = async () => {
    await fetch('/api/eth/stop', {method: 'POST'});
    if (btnEthStart) btnEthStart.disabled = false;
    if (btnEthStop) btnEthStop.disabled = true;
};

// Socket Events
socket.on('connect', () => {
    const el = document.getElementById('connection-status');
    if (!el) return;
    el.innerText = 'Connected';
    el.classList.add('text-success');
});

socket.on('eth_stats', (stats) => {
    const pps = document.getElementById('eth-pps');
    const mbps = document.getElementById('eth-mbps');
    if (pps) pps.innerText = Number(stats?.pps ?? 0).toFixed(0);
    if (mbps) mbps.innerText = Number(stats?.mbps ?? 0).toFixed(2);
});

socket.on('eth_packet', (pkt) => {
    if (!logTable) return;
    const row = document.createElement('tr');
    row.classList.add('table-info'); // Distinguish Ethernet
    
    let info = pkt.summary;
    if(pkt.layers) info += ` [${pkt.layers}]`;

    row.innerHTML = `
        <td>${(pkt.timestamp).toFixed(3)}</td>
        <td>ETH</td>
        <td>-</td>
        <td>${info}</td>
        <td>${pkt.length}</td>
        <td class="font-monospace text-truncate" style="max-width: 200px;">${pkt.payload_hex}</td>
    `;

    logTable.prepend(row);
    if (logTable.children.length > 50) {
        logTable.lastElementChild.remove();
    }
});

socket.on('bus_stats', (stats) => {
    const load = document.getElementById('stat-load');
    const errs = document.getElementById('stat-errors');
    const up = document.getElementById('stat-uptime');
    if (load) load.innerText = `${stats?.bus_load ?? 0}%`;
    if (errs) errs.innerText = String(stats?.errors ?? 0);
    if (up) up.innerText = `${stats?.uptime ?? 0}s`;

    // Update Chart
    if (loadChart) {
        const now = new Date().toLocaleTimeString();
        if (loadChart.data.labels.length > 20) {
            loadChart.data.labels.shift();
            loadChart.data.datasets[0].data.shift();
        }
        loadChart.data.labels.push(now);
        loadChart.data.datasets[0].data.push(stats.bus_load);
        loadChart.update();
    }
});

socket.on('scan_log', (payload) => {
    if (!payload) return;
    if (typeof payload === 'string') {
        appendScanLog(payload);
        return;
    }
    if (payload.line) appendScanLog(payload.line);
});

socket.on('vehicle_data', (payload) => {
    if (!payload) return;
    const rpm = payload.rpm;
    const speed = payload.speed_kph;
    const milOn = payload.mil_on;
    const milDtcCount = payload.mil_dtc_count;

    if (vehicleRpmEl) {
        vehicleRpmEl.textContent = (rpm === null || rpm === undefined) ? '--' : Math.round(rpm).toString();
    }
    if (vehicleSpeedEl) {
        vehicleSpeedEl.textContent = (speed === null || speed === undefined) ? '--' : Math.round(speed).toString();
    }

    if (vehicleMilEl) {
        // Reset badge color
        vehicleMilEl.classList.remove('bg-secondary', 'bg-success', 'bg-danger', 'bg-warning');

        if (milOn === null || milOn === undefined) {
            vehicleMilEl.classList.add('bg-secondary');
            vehicleMilEl.textContent = 'MIL: --';
        } else if (milOn === true) {
            vehicleMilEl.classList.add('bg-danger');
            const c = (milDtcCount === null || milDtcCount === undefined) ? null : Number(milDtcCount);
            vehicleMilEl.textContent = (c === null || Number.isNaN(c)) ? 'MIL: ON' : `MIL: ON (${c})`;
        } else {
            vehicleMilEl.classList.add('bg-success');
            vehicleMilEl.textContent = 'MIL: OFF';
        }
    }

    // Extra fields from DID probing (EV/hybrid + general)
    const coolant = payload.coolant_temp;
    const soc = payload.battery_soc;
    const voltage = payload.battery_voltage;
    const odo = payload.odometer;

    if (vehicleCoolantEl) {
        vehicleCoolantEl.textContent = (coolant === null || coolant === undefined) ? '--' : Math.round(coolant).toString();
    }
    if (vehicleSocEl) {
        vehicleSocEl.textContent = (soc === null || soc === undefined) ? '--' : soc.toFixed(1);
    }
    if (vehicleVoltageEl) {
        vehicleVoltageEl.textContent = (voltage === null || voltage === undefined) ? '--' : voltage.toFixed(1);
    }
    if (vehicleOdometerEl) {
        vehicleOdometerEl.textContent = (odo === null || odo === undefined) ? '--' : Math.round(odo).toLocaleString();
    }
});

socket.on('bus_data', (frame) => {
    try {
        timelineOnBusFrame(frame);
    } catch (e) {
        // ignore
    }

    // Log table is only present on the main dashboard.
    if (!logTable) return;

    const row = document.createElement('tr');
    
    // Format Data
    let dataStr = "";
    if (Array.isArray(frame.data)) {
        dataStr = frame.data.map(b => b.toString(16).padStart(2, '0')).join(' ');
    }

    let name = "";
    if (frame.decoded) {
        name = frame.decoded.name;
    }

    row.innerHTML = `
        <td>${(frame.timestamp / 1000).toFixed(3)}</td>
        <td>${frame.channel}</td>
        <td>0x${frame.id.toString(16).toUpperCase()}</td>
        <td>${name}</td>
        <td>${frame.dlc}</td>
        <td class="font-monospace">${dataStr}</td>
    `;

    logTable.prepend(row);
    if (logTable.children.length > 50) {
        logTable.lastElementChild.remove();
    }
});

// Start
async function initTimelinePage() {
    await loadAppConfig();
    await loadDBCs();
    initTimelineViewer();

    // Navbar system stats
    try {
        await refreshSystemStats();
    } catch (_) {
        // ignore
    }
    if (!sysStatsTimer) {
        sysStatsTimer = setInterval(refreshSystemStats, 2000);
    }
}

(async () => {
    try {
        const page = (typeof window !== 'undefined' && window.KVBM_PAGE) ? String(window.KVBM_PAGE) : 'index';
        if (page === 'timeline') {
            await initTimelinePage();
        } else {
            await init();
        }
    } catch (e) {
        console.error('Boot error', e);
    }
})();
