(() => {
  function initNavbarDropdowns() {
    const nav = document.querySelector('nav.navbar');
    if (!nav || !window.bootstrap || !window.bootstrap.Dropdown) return;

    const toggles = nav.querySelectorAll('[data-bs-toggle="dropdown"]');
    toggles.forEach((toggle) => {
      try {
        window.bootstrap.Dropdown.getOrCreateInstance(toggle);
      } catch (_) {
        // ignore
      }
    });
  }

  function setActiveNavButton() {
    const nav = document.querySelector('nav.navbar');
    if (!nav) return;
    const path = window.location.pathname || '/';

    // Mark active top-level link
    const links = nav.querySelectorAll('a.nav-link[href^="/"], a.dropdown-item[href^="/"]');
    let activeEl = null;
    for (const a of links) {
      const href = a.getAttribute('href') || '';
      if (href === path) {
        activeEl = a;
        break;
      }
    }

    if (activeEl) {
      if (activeEl.classList.contains('dropdown-item')) {
        activeEl.classList.add('active');
        // Also mark parent dropdown toggle as active
        const dd = activeEl.closest('.dropdown');
        const toggle = dd ? dd.querySelector('.nav-link.dropdown-toggle') : null;
        if (toggle) toggle.classList.add('active');
      } else {
        activeEl.classList.add('active');
      }
    }
  }

  async function refreshSystemStatsOnce() {
    const cpuTempEl = document.getElementById('stat-cpu-temp');
    const cpuPctEl = document.getElementById('stat-cpu');
    const ramPctEl = document.getElementById('stat-ram');
    if (!cpuTempEl && !cpuPctEl && !ramPctEl) return;

    try {
      const res = await fetch('/api/system/stats', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json().catch(() => ({}));
      const t = (data && data.cpu_temp_c != null) ? Number(data.cpu_temp_c) : null;
      const cpu = (data && data.cpu_percent != null) ? Number(data.cpu_percent) : null;
      const ram = (data && data.ram_percent != null) ? Number(data.ram_percent) : null;

      if (cpuTempEl) cpuTempEl.textContent = (t == null || Number.isNaN(t)) ? 'CPU —°C' : `CPU ${t.toFixed(1)}°C`;
      if (cpuPctEl) cpuPctEl.textContent = (cpu == null || Number.isNaN(cpu)) ? 'CPU —%' : `CPU ${cpu.toFixed(0)}%`;
      if (ramPctEl) ramPctEl.textContent = (ram == null || Number.isNaN(ram)) ? 'RAM —%' : `RAM ${ram.toFixed(0)}%`;

      const cs = document.getElementById('connection-status');
      if (cs && (window.location.pathname || '/') !== '/') {
        cs.textContent = 'OK';
        cs.classList.add('text-success');
      }
    } catch (_) {
      const cs = document.getElementById('connection-status');
      if (cs && (window.location.pathname || '/') !== '/') {
        cs.textContent = 'ERR';
        cs.classList.remove('text-success');
        cs.classList.add('text-danger');
      }
    }
  }

  function wireModeSelectRedirect() {
    const sel = document.getElementById('mode-select');
    if (!sel) return;
    const path = window.location.pathname || '/';
    if (path === '/') return; // index page handles mode switching internally
    sel.addEventListener('change', () => {
      const v = String(sel.value || 'logger');
      window.location.href = '/?mode=' + encodeURIComponent(v);
    });
  }

  function init() {
    const path = window.location.pathname || '/';
    try { initNavbarDropdowns(); } catch (_) {}
    try { setActiveNavButton(); } catch (_) {}
    try { wireModeSelectRedirect(); } catch (_) {}
    try {
      // Keep this lightweight: stats only.
      // On the index page, app.js already refreshes these; avoid double polling.
      if (path !== '/') {
        refreshSystemStatsOnce();
        setInterval(refreshSystemStatsOnce, 2000);
      }
    } catch (_) {}
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
