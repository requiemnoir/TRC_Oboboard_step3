(function () {
    /* ═══════════════════════════════════════════════
     * §1  CAROUSEL NAVIGATION
     * ═══════════════════════════════════════════════ */
    const track = document.getElementById('carousel-track');
    const navLeft = document.getElementById('nav-left');
    const navRight = document.getElementById('nav-right');
    const dots = document.querySelectorAll('.page-dots .dot');
    const TOTAL_PAGES = 3;
    let currentPage = 0;

    function goToPage(idx) {
        idx = Math.max(0, Math.min(TOTAL_PAGES - 1, idx));
        currentPage = idx;
        track.style.transform = `translateX(-${idx * (100 / TOTAL_PAGES)}%)`;
        navLeft.disabled = idx === 0;
        navRight.disabled = idx === TOTAL_PAGES - 1;
        dots.forEach((d, i) => d.classList.toggle('active', i === idx));
    }

    navLeft.addEventListener('click', () => goToPage(currentPage - 1));
    navRight.addEventListener('click', () => goToPage(currentPage + 1));
    dots.forEach((d) => d.addEventListener('click', () => goToPage(Number(d.dataset.page))));

    /* Swipe support for touch screens */
    let touchStartX = 0;
    let touchStartY = 0;
    document.addEventListener('touchstart', (e) => {
        touchStartX = e.changedTouches[0].screenX;
        touchStartY = e.changedTouches[0].screenY;
    }, { passive: true });
    document.addEventListener('touchend', (e) => {
        /* Don't swipe pages when interacting with scrollable settings or OSK */
        if (e.target.closest('#settings-body') || e.target.closest('#osk')) return;
        const dx = e.changedTouches[0].screenX - touchStartX;
        const dy = e.changedTouches[0].screenY - touchStartY;
        if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
            goToPage(currentPage + (dx < 0 ? 1 : -1));
        }
    }, { passive: true });

    /* Keyboard arrows */
    document.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowLeft') goToPage(currentPage - 1);
        if (e.key === 'ArrowRight') goToPage(currentPage + 1);
    });

    goToPage(0);

    /* ═══════════════════════════════════════════════
     * §2  PAGE 0 — RECORDING CONTROL
     * ═══════════════════════════════════════════════ */
    const stateEl = document.getElementById('recording-state');
    const uptimeEl = document.getElementById('recording-uptime');
    const uptimeGroupEl = document.getElementById('uptime-group');
    const kl15ToggleButton = document.getElementById('kl15-toggle-button');
    const kl15StatusText = document.getElementById('kl15-status-text');
    const manualStartButton = document.getElementById('manual-start-button');
    const manualStopButton = document.getElementById('manual-stop-button');
    const manualStatusText = document.getElementById('manual-status-text');

    let latestStatus = {};
    let latestKl15 = {};
    let requestInFlight = false;

    function formatDuration(totalSeconds) {
        const safeSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
        const hours = Math.floor(safeSeconds / 3600);
        const minutes = Math.floor((safeSeconds % 3600) / 60);
        const seconds = safeSeconds % 60;
        return [hours, minutes, seconds].map((v) => String(v).padStart(2, '0')).join(':');
    }

    function setBusyState(active) {
        requestInFlight = active;
        if (kl15ToggleButton) kl15ToggleButton.disabled = active;
        if (manualStartButton) manualStartButton.disabled = active;
        if (manualStopButton) manualStopButton.disabled = active;
    }

    function setManualStatus(text) {
        if (manualStatusText) manualStatusText.textContent = text;
    }

    function updateActionButtons() {
        const recording = !!latestStatus.recording;
        const stopping = !!latestStatus.stopping;
        const kl15Enabled = !!latestKl15.enabled;
        if (manualStartButton) manualStartButton.disabled = requestInFlight || recording || stopping;
        if (manualStopButton) manualStopButton.disabled = requestInFlight || (!recording && !stopping);
        if (kl15ToggleButton) {
            kl15ToggleButton.disabled = requestInFlight || stopping;
            kl15ToggleButton.textContent = kl15Enabled ? 'Disable KL_15 Recording' : 'Enable KL_15 Recording';
        }
    }

    function renderState() {
        const recording = !!latestStatus.recording;
        const stopping = !!latestStatus.stopping;
        const startedSource = latestStatus.started_source ? String(latestStatus.started_source).replace(/_/g, ' ') : null;
        const kl15Enabled = !!latestKl15.enabled;
        const kl15Detected = !!latestKl15.detected;

        if (stateEl) {
            stateEl.textContent = stopping ? 'Stopping…' : recording ? 'Recording' : 'Idle';
            stateEl.classList.toggle('recording', recording && !stopping);
            stateEl.classList.toggle('idle', !recording && !stopping);
            stateEl.classList.toggle('stopping', stopping);
        }
        if (uptimeGroupEl) uptimeGroupEl.hidden = !recording;

        if (kl15StatusText) {
            kl15StatusText.textContent = kl15Enabled
                ? (kl15Detected ? 'Enabled. Ignition detected.' : 'Enabled. Waiting for ignition.')
                : 'Disabled.';
        }

        if (manualStatusText) {
            if (stopping) setManualStatus('Stopping recording…');
            else if (recording) setManualStatus(startedSource ? `Running: ${startedSource}.` : 'Recording is running.');
            else if (kl15Enabled) setManualStatus('Idle. KL_15 auto-record enabled.');
            else setManualStatus('Idle.');
        }
        updateActionButtons();
    }

    function updateUptimeClock() {
        if (!latestStatus || !latestStatus.recording || !latestStatus.started_at_ms) {
            if (uptimeEl) uptimeEl.textContent = '00:00:00';
            return;
        }
        const elapsed = Math.max(0, (Date.now() - Number(latestStatus.started_at_ms)) / 1000);
        if (uptimeEl) uptimeEl.textContent = formatDuration(elapsed);
    }

    async function refreshStatus() {
        try {
            const [dispR, logR, kl15R] = await Promise.all([
                fetch('/api/display/status', { cache: 'no-store' }),
                fetch('/api/log/status', { cache: 'no-store' }),
                fetch('/api/kl15', { cache: 'no-store' }),
            ]);
            if (!dispR.ok || !logR.ok || !kl15R.ok) throw new Error('status fetch');
            const disp = await dispR.json();
            const log = await logR.json();
            const kl15 = await kl15R.json();
            latestStatus = { ...(disp || {}), stopping: !!log.stopping };
            latestKl15 = {
                enabled: !!kl15?.config?.enabled,
                detected: !!kl15?.state?.detected,
                recording: !!kl15?.state?.recording,
            };
            renderState();
            updateUptimeClock();
        } catch (_) {
            setManualStatus('Unable to load recorder status.');
            updateActionButtons();
        }
    }

    async function postJson(url, body) {
        const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        if (!r.ok && r.status !== 202) throw new Error(`HTTP ${r.status}`);
        return r;
    }

    if (kl15ToggleButton) {
        kl15ToggleButton.addEventListener('click', async () => {
            const next = !latestKl15.enabled;
            setBusyState(true);
            try { await postJson('/api/kl15', { enabled: next }); await refreshStatus(); }
            catch (_) { setManualStatus('Unable to change KL_15 recording.'); }
            finally { setBusyState(false); renderState(); }
        });
    }
    if (manualStartButton) {
        manualStartButton.addEventListener('click', async () => {
            setBusyState(true); setManualStatus('Starting recording…');
            try { await postJson('/api/log/start', {}); await refreshStatus(); }
            catch (_) { setManualStatus('Unable to start recording.'); }
            finally { setBusyState(false); renderState(); }
        });
    }
    if (manualStopButton) {
        manualStopButton.addEventListener('click', async () => {
            setBusyState(true); setManualStatus('Stopping recording…');
            try {
                if (latestKl15.enabled) await postJson('/api/kl15', { enabled: false });
                await postJson('/api/log/stop', {});
                await refreshStatus();
            } catch (_) { setManualStatus('Unable to stop recording.'); }
            finally { setBusyState(false); renderState(); }
        });
    }

    renderState();
    refreshStatus();
    setInterval(refreshStatus, 2000);
    setInterval(updateUptimeClock, 1000);

    /* ═══════════════════════════════════════════════
     * §3  PAGE 1 — MF4 CONVERTER & PLOT
     * ═══════════════════════════════════════════════ */
    const mf4FileSelect = document.getElementById('mf4-file-select');
    const mf4RefreshBtn = document.getElementById('mf4-refresh-btn');
    const mf4ConvertBtn = document.getElementById('mf4-convert-btn');
    const mf4ConvertStatus = document.getElementById('mf4-convert-status');
    const mf4DecodedSelect = document.getElementById('mf4-decoded-select');
    const mf4SignalSelect = document.getElementById('mf4-signal-select');
    const mf4PlotBtn = document.getElementById('mf4-plot-btn');
    const mf4ChartEl = document.getElementById('mf4-chart');

    /* Signal search overlay elements */
    const signalPickerBtn = document.getElementById('signal-picker-btn');
    const signalOverlay = document.getElementById('signal-search-overlay');
    const signalSearchInput = document.getElementById('signal-search-input');
    const signalSearchList = document.getElementById('signal-search-list');
    const signalSearchClose = document.getElementById('signal-search-close');

    /* Chart overlay elements */
    const chartOverlay = document.getElementById('chart-overlay');
    const chartOverlayTitle = document.getElementById('chart-overlay-title');
    const chartOverlayClose = document.getElementById('chart-overlay-close');

    let mf4Converting = false;
    /* Signal keys are "Label.Signal" — store the full group data for decoded_data calls */
    let mf4SignalGroups = [];   // [{message, signals: [{key, unit}]}]
    let mf4CurrentDbc = '';     // DBC used for the decoded file
    let allSignalItems = [];    // [{value, label}] full list for search

    function populateSelect(sel, items, placeholder) {
        sel.innerHTML = '';
        const ph = document.createElement('option');
        ph.value = '';
        ph.textContent = placeholder;
        sel.appendChild(ph);
        (items || []).forEach((item) => {
            const val = typeof item === 'object' ? (item.value || item.name || '') : item;
            const label = typeof item === 'object' ? (item.label || item.name || val) : item;
            const o = document.createElement('option');
            o.value = val;
            o.textContent = label.length > 45 ? '…' + label.slice(-43) : label;
            sel.appendChild(o);
        });
    }

    async function loadMf4Files() {
        try {
            /* Raw files (no exports) */
            const rRaw = await fetch('/api/mf4/files', { cache: 'no-store' });
            if (!rRaw.ok) throw new Error('fetch files');
            const rawArr = await rRaw.json();   // direct array of {name, label, bucket, …}

            /* Exported / decoded files */
            const rExp = await fetch('/api/mf4/files?include_exports=true', { cache: 'no-store' });
            let expArr = [];
            if (rExp.ok) {
                expArr = await rExp.json();
            }

            const rawItems = (rawArr || [])
                .filter((f) => f.bucket === 'logs')
                .map((f) => ({ value: f.name, label: f.label || f.name }));
            const expItems = (expArr || [])
                .filter((f) => f.bucket === 'exports' || (f.name || '').includes('decoded'))
                .map((f) => ({ value: f.name, label: f.label || f.name }));

            populateSelect(mf4FileSelect, rawItems, '— select raw file —');
            populateSelect(mf4DecodedSelect, expItems, '— convert or pick exported —');
        } catch (e) {
            populateSelect(mf4FileSelect, [], '— error loading files —');
            populateSelect(mf4DecodedSelect, [], '— error loading files —');
        }
    }

    if (mf4FileSelect) {
        mf4FileSelect.addEventListener('change', () => {
            const hasFile = !!mf4FileSelect.value;
            mf4ConvertBtn.disabled = !hasFile || mf4Converting;
            mf4ConvertStatus.textContent = hasFile ? 'Ready to convert.' : 'Select a raw file first.';
        });
    }

    if (mf4RefreshBtn) {
        mf4RefreshBtn.addEventListener('click', () => loadMf4Files());
    }

    if (mf4ConvertBtn) {
        mf4ConvertBtn.addEventListener('click', async () => {
            const file = mf4FileSelect.value;
            if (!file) return;
            mf4Converting = true;
            mf4ConvertBtn.disabled = true;
            mf4ConvertStatus.textContent = 'Converting… this may take a moment.';
            try {
                const r = await postJson('/api/mf4/export_decoded_mf4', { file: file });
                if (r.ok) {
                    mf4ConvertStatus.textContent = 'Conversion complete! Refreshing files…';
                    await loadMf4Files();
                    mf4ConvertStatus.textContent = 'Done. Select the decoded file to plot.';
                } else {
                    const err = await r.json().catch(() => ({}));
                    mf4ConvertStatus.textContent = 'Conversion failed: ' + (err.error || 'unknown error');
                }
            } catch (e) {
                mf4ConvertStatus.textContent = 'Conversion failed: ' + e.message;
            } finally {
                mf4Converting = false;
                mf4ConvertBtn.disabled = !mf4FileSelect.value;
            }
        });
    }

    if (mf4DecodedSelect) {
        mf4DecodedSelect.addEventListener('change', async () => {
            const file = mf4DecodedSelect.value;
            mf4SignalSelect.disabled = true;
            mf4PlotBtn.disabled = true;
            mf4SignalGroups = [];
            allSignalItems = [];
            mf4SignalSelect.value = '';
            signalPickerBtn.textContent = '— select decoded file first —';
            signalPickerBtn.disabled = true;
            if (!file) return;
            signalPickerBtn.textContent = '— loading signals… —';
            try {
                /* Try /api/mf4/signals first (works on already-decoded MF4 files) */
                const rSig = await fetch('/api/mf4/signals?file=' + encodeURIComponent(file), { cache: 'no-store' });
                if (rSig.ok) {
                    const dSig = await rSig.json();
                    const sigNames = dSig.signals || [];
                    if (sigNames.length > 0) {
                        allSignalItems = sigNames.map((s) => ({ value: s, label: s }));
                        populateSelect(mf4SignalSelect, allSignalItems, '— select signal —');
                        mf4SignalSelect.disabled = false;
                        mf4SignalSelect.dataset.mode = 'direct';
                        signalPickerBtn.textContent = '— tap to search signals (' + allSignalItems.length + ') —';
                        signalPickerBtn.disabled = false;
                        return;
                    }
                }

                /* Fallback: /api/mf4/decoded_signals with auto DBC */
                const rDec = await fetch('/api/mf4/decoded_signals?file=' + encodeURIComponent(file) + '&auto=true', { cache: 'no-store' });
                if (!rDec.ok) {
                    const errJ = await rDec.json().catch(() => ({}));
                    throw new Error(errJ.error || 'unable to list signals');
                }
                const dDec = await rDec.json();
                mf4SignalGroups = dDec.groups || [];
                allSignalItems = [];
                for (const grp of mf4SignalGroups) {
                    for (const s of (grp.signals || [])) {
                        const key = s.key || '';
                        const unit = s.unit ? ` (${s.unit})` : '';
                        allSignalItems.push({ value: key, label: key + unit });
                    }
                }
                populateSelect(mf4SignalSelect, allSignalItems, '— select signal —');
                mf4SignalSelect.disabled = allSignalItems.length === 0;
                mf4SignalSelect.dataset.mode = 'decoded';
                signalPickerBtn.textContent = allSignalItems.length
                    ? '— tap to search signals (' + allSignalItems.length + ') —'
                    : '— no signals found —';
                signalPickerBtn.disabled = allSignalItems.length === 0;
            } catch (e) {
                signalPickerBtn.textContent = '— ' + e.message + ' —';
                signalPickerBtn.disabled = true;
            }
        });
    }

    /* ─── Signal search overlay logic ─── */
    function renderSignalList(filter) {
        signalSearchList.innerHTML = '';
        const q = (filter || '').toLowerCase().trim();
        let matches;
        if (!q) {
            matches = allSignalItems;
        } else if (q.includes('*')) {
            /* MDA-style wildcard: * matches any sequence of characters */
            const pat = q.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
            try {
                const re = new RegExp(pat);
                matches = allSignalItems.filter((it) => re.test(it.label.toLowerCase()));
            } catch (_) {
                matches = allSignalItems.filter((it) => it.label.toLowerCase().includes(q));
            }
        } else {
            matches = allSignalItems.filter((it) => it.label.toLowerCase().includes(q));
        }

        if (matches.length === 0) {
            const li = document.createElement('li');
            li.className = 'signal-list-empty';
            li.textContent = q ? 'No signals match "' + filter + '"' : 'No signals available';
            signalSearchList.appendChild(li);
            return;
        }
        /* Limit rendered items for performance on large signal lists */
        const show = matches.slice(0, 200);
        for (const item of show) {
            const li = document.createElement('li');
            li.textContent = item.label;
            li.dataset.value = item.value;
            if (item.value === mf4SignalSelect.value) li.classList.add('selected');
            li.addEventListener('click', () => {
                mf4SignalSelect.value = item.value;
                signalPickerBtn.textContent = item.label;
                mf4PlotBtn.disabled = false;
                closeSignalSearch();
            });
            signalSearchList.appendChild(li);
        }
        if (matches.length > 200) {
            const li = document.createElement('li');
            li.className = 'signal-list-empty';
            li.textContent = '… and ' + (matches.length - 200) + ' more. Refine your search.';
            signalSearchList.appendChild(li);
        }
    }

    function openSignalSearch() {
        signalOverlay.hidden = false;
        signalSearchInput.value = '';
        renderSignalList('');
        showOSK(signalSearchInput);
    }

    function closeSignalSearch() {
        hideOSK();
        signalOverlay.hidden = true;
    }

    if (signalPickerBtn) {
        signalPickerBtn.addEventListener('click', () => {
            if (!signalPickerBtn.disabled) openSignalSearch();
        });
    }
    if (signalSearchClose) {
        signalSearchClose.addEventListener('click', closeSignalSearch);
    }
    /* input event still works even though we set value programmatically */
    if (signalSearchInput) {
        signalSearchInput.addEventListener('input', () => {
            renderSignalList(signalSearchInput.value);
        });
    }
    /* Close on backdrop click (but not on osk area) */
    if (signalOverlay) {
        signalOverlay.addEventListener('click', (e) => {
            if (e.target === signalOverlay) closeSignalSearch();
        });
    }

    /* ─── On-screen keyboard (OSK) — shared across pages ─── */
    const osk = document.getElementById('osk');
    let oskShift = false;
    let oskSymbols = false;
    let oskTargetInput = null;  /* the <input> the OSK types into */

    const symbolMap = {
        'q': '!', 'w': '@', 'e': '#', 'r': '$', 't': '%',
        'y': '^', 'u': '&', 'i': '*', 'o': '(', 'p': ')',
        'a': '-', 's': '+', 'd': '=', 'f': '[', 'g': ']',
        'h': '{', 'j': '}', 'k': '|', 'l': '\\',
        'z': '~', 'x': '`', 'c': '<', 'v': '>', 'b': '/',
        'n': ':', 'm': ';',
    };

    function showOSK(targetInput) {
        if (!osk) return;
        oskTargetInput = targetInput || null;
        osk.hidden = false;
        document.body.classList.add('osk-visible');
        /* If inside signal search, adjust overlay */
        if (signalOverlay && !signalOverlay.hidden) {
            signalOverlay.classList.add('osk-active');
        }
        oskShift = false;
        oskSymbols = false;
        updateOSKLabels();
    }

    function hideOSK() {
        if (!osk) return;
        osk.hidden = true;
        oskTargetInput = null;
        document.body.classList.remove('osk-visible');
        if (signalOverlay) signalOverlay.classList.remove('osk-active');
    }

    function updateOSKLabels() {
        if (!osk) return;
        osk.classList.toggle('osk-shift', oskShift && !oskSymbols);
        osk.querySelectorAll('.osk-key[data-key]').forEach((btn) => {
            const base = btn.dataset.key;
            if (base === ' ' || base === '_' || base === '.') return;
            if (oskSymbols && symbolMap[base]) {
                btn.textContent = symbolMap[base];
            } else if (oskShift) {
                btn.textContent = base.toUpperCase();
            } else {
                btn.textContent = base;
            }
        });
        /* highlight symbol button */
        const symBtn = osk.querySelector('[data-action="symbols"]');
        if (symBtn) {
            symBtn.style.background = oskSymbols ? 'rgba(240,178,77,0.3)' : '';
            symBtn.style.color = oskSymbols ? 'var(--accent-strong)' : '';
        }
    }

    function oskType(char) {
        if (!oskTargetInput) return;
        oskTargetInput.value += char;
        oskTargetInput.dispatchEvent(new Event('input', { bubbles: true }));
        /* auto-reset shift after one character */
        if (oskShift && !oskSymbols) {
            oskShift = false;
            updateOSKLabels();
        }
    }

    if (osk) {
        osk.addEventListener('click', (e) => {
            const btn = e.target.closest('.osk-key');
            if (!btn) return;
            e.preventDefault();
            e.stopPropagation();

            const action = btn.dataset.action;
            if (action === 'backspace') {
                if (oskTargetInput) {
                    oskTargetInput.value = oskTargetInput.value.slice(0, -1);
                    oskTargetInput.dispatchEvent(new Event('input', { bubbles: true }));
                }
                return;
            }
            if (action === 'clear') {
                if (oskTargetInput) {
                    oskTargetInput.value = '';
                    oskTargetInput.dispatchEvent(new Event('input', { bubbles: true }));
                }
                return;
            }
            if (action === 'done') {
                hideOSK();
                document.querySelectorAll('#settings-body .osk-input').forEach((i) => i.classList.remove('osk-focused'));
                return;
            }
            if (action === 'shift') {
                oskShift = !oskShift;
                oskSymbols = false;
                updateOSKLabels();
                return;
            }
            if (action === 'symbols') {
                oskSymbols = !oskSymbols;
                oskShift = false;
                updateOSKLabels();
                return;
            }

            const base = btn.dataset.key;
            if (base == null) return;

            if (base === ' ' || base === '_' || base === '.' || base === '*') {
                oskType(base);
            } else if (oskSymbols && symbolMap[base]) {
                oskType(symbolMap[base]);
            } else if (oskShift) {
                oskType(base.toUpperCase());
            } else {
                oskType(base);
            }
        });
    }

    if (mf4PlotBtn) {
        mf4PlotBtn.addEventListener('click', async () => {
            const file = mf4DecodedSelect.value;
            const signal = mf4SignalSelect.value;
            if (!file || !signal) return;
            mf4PlotBtn.disabled = true;
            mf4PlotBtn.textContent = 'Loading…';
            try {
                let timestamps = [];
                let values = [];
                let plotLabel = signal;
                const mode = mf4SignalSelect.dataset.mode || 'direct';

                if (mode === 'direct') {
                    const r = await postJson('/api/mf4/data', { file: file, signals: [signal], max_points: 800 });
                    if (!r.ok) throw new Error('fetch data');
                    const data = await r.json();
                    if (data.series && data.series.length > 0) {
                        timestamps = data.series[0].t || [];
                        values = data.series[0].y || [];
                        plotLabel = data.series[0].name || signal;
                    } else if (data.data && data.data[signal]) {
                        timestamps = data.data[signal].t || data.data[signal].timestamps || [];
                        values = data.data[signal].y || data.data[signal].values || [];
                    } else {
                        throw new Error('no data returned');
                    }
                } else {
                    const r = await postJson('/api/mf4/decoded_data', {
                        file: file, signals: [signal], auto: true, max_points: 800,
                    });
                    if (!r.ok) {
                        const errJ = await r.json().catch(() => ({}));
                        throw new Error(errJ.error || 'fetch data failed');
                    }
                    const data = await r.json();
                    const series = data.series || [];
                    const match = series.find((s) => s.name === signal) || series[0];
                    if (!match || !match.t || match.t.length === 0) throw new Error('no data for signal');
                    timestamps = match.t;
                    values = match.y;
                    plotLabel = match.name || signal;
                }

                if (!timestamps.length) throw new Error('no data');

                /* Open fullscreen chart overlay */
                chartOverlay.hidden = false;
                chartOverlayTitle.textContent = plotLabel;

                const trace = {
                    x: timestamps,
                    y: values,
                    type: 'scattergl',
                    mode: 'lines',
                    line: { color: '#f0b24d', width: 1.5 },
                    name: plotLabel,
                };
                const layout = {
                    paper_bgcolor: 'rgba(0,0,0,0)',
                    plot_bgcolor: 'rgba(20,16,12,0.6)',
                    font: { color: '#d1c3aa', size: 11 },
                    margin: { l: 52, r: 16, t: 10, b: 38 },
                    xaxis: { title: 's', gridcolor: 'rgba(255,244,220,0.08)', zeroline: false },
                    yaxis: { title: plotLabel, gridcolor: 'rgba(255,244,220,0.08)', zeroline: false },
                    showlegend: false,
                };
                const cfg = { responsive: true, displayModeBar: false };
                if (typeof Plotly !== 'undefined') {
                    Plotly.newPlot(mf4ChartEl, [trace], layout, cfg);
                    /* Resize after overlay is visible */
                    setTimeout(() => Plotly.Plots.resize(mf4ChartEl), 60);
                }
            } catch (e) {
                chartOverlay.hidden = false;
                chartOverlayTitle.textContent = 'Error';
                mf4ChartEl.innerHTML = '<p style="color:var(--danger-strong);padding:1.5rem;text-align:center;font-size:1rem;">Failed to plot: ' +
                    e.message.replace(/</g, '&lt;') + '</p>';
            } finally {
                mf4PlotBtn.disabled = false;
                mf4PlotBtn.textContent = 'Plot Signal';
            }
        });
    }

    /* Chart overlay close */
    function closeChartOverlay() {
        chartOverlay.hidden = true;
        if (typeof Plotly !== 'undefined') {
            try { Plotly.purge(mf4ChartEl); } catch (_) {}
        }
        mf4ChartEl.innerHTML = '';
    }
    if (chartOverlayClose) {
        chartOverlayClose.addEventListener('click', closeChartOverlay);
    }
    if (chartOverlay) {
        chartOverlay.addEventListener('click', (e) => {
            if (e.target === chartOverlay) closeChartOverlay();
        });
    }

    /* Load file list when page first appears */
    loadMf4Files();

    /* ═══════════════════════════════════════════════
     * §4  PAGE 2 — SETTINGS
     * ═══════════════════════════════════════════════ */
    /* ── MF4 flat fields ── */
    const mf4Fields = {
        'cfg-mf4-chunk':     'mf4_chunk_size_mb',
        'cfg-mf4-part-time': 'mf4_part_time_limit_min',
        'cfg-mf4-flush':     'mf4_flush_interval_mb',
    };

    /* ── Ethernet fields (nested under eth_settings) ── */
    const ethFields = {
        'cfg-eth-interface':  'interface',
        'cfg-eth-target-ip':  'target_ip',
        'cfg-eth-pcap':       'pcap_enabled',
        'cfg-eth-someip':     'someip_enabled',
        'cfg-eth-doip':       'doip_enabled',
        'cfg-eth-xcp':        'xcp_enabled',
    };

    /* ── Gateway Mirror fields (nested under gateway_mirror) ── */
    const gmFields = {
        'cfg-gm-enabled':      'enabled',
        'cfg-gm-autostart':    'autostart',
        'cfg-gm-autodiscover': 'auto_discover_ip',
        'cfg-gm-gateway-ip':   'gateway_ip',
        'cfg-gm-dest-ip':      'dest_ip',
        'cfg-gm-dest-port':    'dest_port',
        'cfg-gm-target-addr':  'target_addr',
        'cfg-gm-tester-addr':  'tester_logical_address',
        'cfg-gm-target-bus':   'target_bus',
    };
    /* Array fields that need CSV ↔ array conversion */
    const gmArrayFields = {
        'cfg-gm-can':     'can',
        'cfg-gm-flexray': 'flexray',
        'cfg-gm-lin':     'lin',
    };

    const cfgSaveBtn = document.getElementById('cfg-save-btn');
    const cfgStatusEl = document.getElementById('cfg-status');

    function setFieldValue(elId, val) {
        const el = document.getElementById(elId);
        if (!el || val === undefined) return;
        if (el.type === 'checkbox') el.checked = !!val;
        else el.value = val;
    }

    function getFieldValue(el) {
        if (el.type === 'checkbox') return el.checked;
        if (el.type === 'number') return Number(el.value);
        return el.value;
    }

    async function loadConfig() {
        try {
            const r = await fetch('/api/config', { cache: 'no-store' });
            if (!r.ok) throw new Error('load config');
            const raw = await r.json();
            const cfg = (typeof raw.config === 'object' && raw.config) || raw;

            /* MF4 flat */
            for (const [elId, key] of Object.entries(mf4Fields)) {
                setFieldValue(elId, cfg[key]);
            }

            /* Ethernet */
            const es = (typeof cfg.eth_settings === 'object' && cfg.eth_settings) || {};
            for (const [elId, key] of Object.entries(ethFields)) {
                setFieldValue(elId, es[key]);
            }

            /* Gateway Mirror */
            const gm = (typeof cfg.gateway_mirror === 'object' && cfg.gateway_mirror) || {};
            for (const [elId, key] of Object.entries(gmFields)) {
                setFieldValue(elId, gm[key]);
            }
            for (const [elId, key] of Object.entries(gmArrayFields)) {
                const arr = Array.isArray(gm[key]) ? gm[key] : [];
                setFieldValue(elId, arr.join(', '));
            }
        } catch (_) {
            if (cfgStatusEl) cfgStatusEl.textContent = 'Failed to load settings.';
        }
    }

    if (cfgSaveBtn) {
        cfgSaveBtn.addEventListener('click', async () => {
            cfgSaveBtn.disabled = true;
            if (cfgStatusEl) cfgStatusEl.textContent = 'Saving…';
            const payload = {};

            /* MF4 flat */
            for (const [elId, key] of Object.entries(mf4Fields)) {
                const el = document.getElementById(elId);
                if (el) payload[key] = getFieldValue(el);
            }

            /* Ethernet (nested) */
            const ethPayload = {};
            for (const [elId, key] of Object.entries(ethFields)) {
                const el = document.getElementById(elId);
                if (el) ethPayload[key] = getFieldValue(el);
            }
            payload.eth_settings = ethPayload;

            /* Gateway Mirror (nested) */
            const gmPayload = {};
            for (const [elId, key] of Object.entries(gmFields)) {
                const el = document.getElementById(elId);
                if (el) gmPayload[key] = getFieldValue(el);
            }
            for (const [elId, key] of Object.entries(gmArrayFields)) {
                const el = document.getElementById(elId);
                if (el) {
                    const raw = (el.value || '').trim();
                    if (!raw) {
                        gmPayload[key] = [];
                    } else if (key === 'can' || key === 'lin') {
                        gmPayload[key] = raw.split(/[,\s]+/).filter(Boolean).map(Number);
                    } else {
                        gmPayload[key] = raw.split(/[,\s]+/).filter(Boolean);
                    }
                }
            }
            payload.gateway_mirror = gmPayload;

            try {
                const r = await postJson('/api/config', payload);
                if (r.ok) {
                    if (cfgStatusEl) cfgStatusEl.textContent = 'Settings saved.';
                } else {
                    const err = await r.json().catch(() => ({}));
                    if (cfgStatusEl) cfgStatusEl.textContent = 'Save failed: ' + (err.error || 'unknown');
                }
            } catch (e) {
                if (cfgStatusEl) cfgStatusEl.textContent = 'Save failed: ' + e.message;
            } finally {
                cfgSaveBtn.disabled = false;
                setTimeout(() => { if (cfgStatusEl) cfgStatusEl.textContent = ''; }, 4000);
            }
        });
    }

    /* ── OSK for settings text inputs ── */
    /* Use pointerup (works on touch + mouse) and stop propagation so the
       carousel swipe handler doesn't eat the tap. */
    document.querySelectorAll('#settings-body .osk-input').forEach((inp) => {
        inp.addEventListener('pointerup', (e) => {
            e.stopPropagation();
            /* highlight the focused input */
            document.querySelectorAll('#settings-body .osk-input').forEach((i) => i.classList.remove('osk-focused'));
            inp.classList.add('osk-focused');
            showOSK(inp);
        });
        /* Prevent default touch behavior that might steal focus */
        inp.addEventListener('touchend', (e) => {
            e.stopPropagation();
        });
    });

    /* Dismiss OSK when tapping a non-input area on the settings page */
    document.getElementById('settings-body')?.addEventListener('pointerup', (e) => {
        if (!e.target.closest('.osk-input') && !e.target.closest('#osk')) {
            if (oskTargetInput && (signalOverlay ? signalOverlay.hidden : true)) {
                hideOSK();
                document.querySelectorAll('#settings-body .osk-input').forEach((i) => i.classList.remove('osk-focused'));
            }
        }
    });

    loadConfig();
})();