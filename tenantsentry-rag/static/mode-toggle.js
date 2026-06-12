/**
 * TenantSentry.ai — DEV / LIVE Mode Toggle
 * Injected into every page via FastAPI middleware. No per-template code needed.
 *
 * This is the single source of truth for runtime mode: api/mode.py,
 * exposed via GET /api/mode and POST /api/admin/mode/toggle. Toggling the
 * mode anywhere flips that shared server-side state, so every other page
 * (this pill, or the Partner Portal's own .mode-toggle) reflects the same
 * mode — on load, and within a few seconds via polling while the page is open.
 *
 * Visual design intentionally matches the Partner Portal's .mode-toggle /
 * .dev-pill component (amber pill + track + sliding thumb for DEV,
 * green for LIVE) so the indicator looks identical across every portal.
 */
(function () {
  'use strict';

  var AMBER = '#f59e0b';
  var GREEN = '#10b981';

  /* ── Inject shared styles once ── */
  function injectStyles() {
    if (document.getElementById('ts-mode-pill-style')) return;
    var style = document.createElement('style');
    style.id = 'ts-mode-pill-style';
    style.textContent =
      '#ts-mode-pill{display:flex;align-items:center;gap:7px;cursor:pointer;' +
      'padding:5px 10px;border-radius:8px;border:1px solid rgba(245,158,11,0.2);' +
      'background:rgba(245,158,11,0.12);transition:all 0.22s cubic-bezier(0.4,0,0.2,1);' +
      'flex-shrink:0;font-family:inherit;}' +
      '#ts-mode-pill:hover{filter:brightness(1.15);}' +
      '#ts-mode-pill.is-live{border-color:rgba(16,185,129,0.2);background:rgba(16,185,129,0.12);}' +
      '#ts-mode-pill .ts-mode-track{position:relative;width:28px;height:15px;' +
      'border-radius:9px;background:rgba(245,158,11,0.25);' +
      'transition:all 0.22s cubic-bezier(0.4,0,0.2,1);flex-shrink:0;}' +
      '#ts-mode-pill.is-live .ts-mode-track{background:rgba(16,185,129,0.3);}' +
      '#ts-mode-pill .ts-mode-thumb{position:absolute;top:1.5px;left:1.5px;' +
      'width:12px;height:12px;border-radius:50%;background:' + AMBER + ';' +
      'transition:all 0.22s cubic-bezier(0.4,0,0.2,1);box-shadow:0 0 6px rgba(245,158,11,0.5);}' +
      '#ts-mode-pill.is-live .ts-mode-thumb{background:' + GREEN + ';' +
      'transform:translateX(13px);box-shadow:0 0 6px rgba(16,185,129,0.5);}' +
      '#ts-mode-pill .ts-mode-label{font-size:9px;font-weight:700;text-transform:uppercase;' +
      'letter-spacing:0.5px;color:' + AMBER + ';white-space:nowrap;}' +
      '#ts-mode-pill.is-live .ts-mode-label{color:' + GREEN + ';}' +
      '#ts-mode-pill.ts-mode-fixed{position:fixed;top:12px;right:16px;z-index:9999;}';
    document.head.appendChild(style);
  }

  /* ── Wait for DOM ── */
  function init() {
    if (document.getElementById('ts-mode-pill')) return; // already injected

    /* Pages with their own built-in DEV/LIVE toggle (e.g. the partner
       portal's Alpine-driven .mode-toggle) already talk to the same
       /api/mode + /api/admin/mode/toggle endpoints — don't inject a
       second one on top of it. */
    if (document.querySelector('.mode-toggle')) return;

    injectStyles();

    /* ── Create pill (same markup/classes as the Partner Portal's
       .mode-toggle, so it's visually identical everywhere) ── */
    var pill = document.createElement('div');
    pill.id = 'ts-mode-pill';
    pill.title = 'DEV/LIVE mode — shared across every TenantSentry portal. Click to toggle.';
    pill.innerHTML =
      '<span class="ts-mode-track"><span class="ts-mode-thumb"></span></span>' +
      '<span class="ts-mode-label" id="ts-mode-text">···</span>';

    pill.addEventListener('click', toggleMode);

    /* ── Insert into nav right-side, before theme button ── */
    var themeBtn = document.getElementById('ts-theme-btn');
    var logo     = document.querySelector('.logo');

    if (themeBtn && themeBtn.parentNode) {
      themeBtn.parentNode.insertBefore(pill, themeBtn);
    } else if (logo && logo.parentNode) {
      /* Pages like /login have no nav bar — sit the pill in the corner
         near the brand logo instead of overlapping content. */
      pill.classList.add('ts-mode-fixed');
      document.body.appendChild(pill);
    } else {
      /* Fallback: fixed position top-right */
      pill.classList.add('ts-mode-fixed');
      document.body.appendChild(pill);
    }

    loadMode();

    /* ── Keep in sync with mode changes made on other portals ── */
    setInterval(loadMode, 8000);
  }

  /* ── Apply visual state ── */
  function applyMode(data) {
    var pill = document.getElementById('ts-mode-pill');
    var txt  = document.getElementById('ts-mode-text');
    if (!pill) return;

    pill.classList.toggle('is-live', !!data.is_live);
    txt.textContent = data.is_dev ? 'Dev' : 'Live';
    pill.title = data.is_dev
      ? 'DEV mode — mock data, no external calls. Click to switch to LIVE (shared across every portal).'
      : 'LIVE mode — real Claude/Supabase/VoyageAI pipeline. Click to switch to DEV (shared across every portal).';
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
    fetch('/api/admin/mode/toggle', { method: 'POST' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) applyMode(d); })
      .catch(function (e) { console.error('[mode-toggle]', e); });
  }

  /* ── Boot ── */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
