/**
 * TenantSentry.ai — DEV / LIVE Mode Toggle
 * Injected into every page via FastAPI middleware. No per-template code needed.
 *
 * Creates a single pill button that sits in the top nav.
 * Click toggles between DEV (zero API calls) and LIVE (real pipeline).
 * No admin token required — developer tool only.
 */
(function () {
  'use strict';

  /* ── Wait for DOM ── */
  function init() {
    if (document.getElementById('ts-mode-pill')) return; // already injected

    /* ── Create pill ── */
    var pill = document.createElement('button');
    pill.id = 'ts-mode-pill';
    pill.title = 'Toggle DEV / LIVE mode';
    pill.innerHTML =
      '<span id="ts-mode-dot" style="width:7px;height:7px;border-radius:50%;display:inline-block;flex-shrink:0;margin-right:5px;"></span>' +
      '<span id="ts-mode-text">···</span>';

    Object.assign(pill.style, {
      display:        'inline-flex',
      alignItems:     'center',
      padding:        '0 11px',
      height:         '28px',
      borderRadius:   '20px',
      fontSize:       '11px',
      fontWeight:     '700',
      letterSpacing:  '0.5px',
      cursor:         'pointer',
      border:         'none',
      transition:     'all 0.2s',
      flexShrink:     '0',
      fontFamily:     'inherit',
    });

    pill.addEventListener('click', toggleMode);

    /* ── Insert into nav right-side, before theme button ── */
    var themeBtn = document.getElementById('ts-theme-btn');
    if (themeBtn && themeBtn.parentNode) {
      themeBtn.parentNode.insertBefore(pill, themeBtn);
    } else {
      /* Fallback: fixed position top-right */
      Object.assign(pill.style, {
        position: 'fixed',
        top:      '12px',
        right:    '16px',
        zIndex:   '9999',
      });
      document.body.appendChild(pill);
    }

    loadMode();
  }

  /* ── Apply visual state ── */
  function applyMode(data) {
    var pill = document.getElementById('ts-mode-pill');
    var dot  = document.getElementById('ts-mode-dot');
    var txt  = document.getElementById('ts-mode-text');
    if (!pill) return;

    if (data.is_dev) {
      pill.style.background = 'rgba(245,158,11,0.18)';
      pill.style.color      = '#fbbf24';
      pill.style.boxShadow  = '0 0 0 1px rgba(245,158,11,0.4)';
      dot.style.background  = '#f59e0b';
      txt.textContent       = 'DEV';
    } else {
      pill.style.background = 'rgba(34,197,94,0.14)';
      pill.style.color      = '#4ade80';
      pill.style.boxShadow  = '0 0 0 1px rgba(34,197,94,0.35)';
      dot.style.background  = '#22c55e';
      txt.textContent       = 'LIVE';
    }
  }

  /* ── Fetch current mode ── */
  function loadMode() {
    fetch('/api/mode')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) applyMode(d); })
      .catch(function () {});
  }

  /* ── Toggle ── */
  function toggleMode() {
    fetch('/api/admin/mode/toggle', {
      method:  'POST',
      headers: { 'X-Admin-Token': getAdminToken() },
    })
    .then(function (r) {
      if (r.status === 401) {
        /* Prompt for token once, store it, retry */
        var token = prompt('Enter admin token to switch mode:');
        if (!token) return null;
        localStorage.setItem('ts-admin-token', token);
        return fetch('/api/admin/mode/toggle', {
          method:  'POST',
          headers: { 'X-Admin-Token': token },
        }).then(function (r2) { return r2.ok ? r2.json() : null; });
      }
      return r.ok ? r.json() : null;
    })
    .then(function (d) { if (d) applyMode(d); })
    .catch(function (e) { console.error('[mode-toggle]', e); });
  }

  function getAdminToken() {
    return localStorage.getItem('ts-admin-token') || '';
  }

  /* ── Boot ── */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
