(function _bassRetune() {
  'use strict';

  // ── State ──────────────────────────────────────────────────────────────────

  let _songs = [];
  let _sortKey = 'title';
  let _pendingSong = null;

  // ── DOM refs (resolved after fragment injection) ───────────────────────────

  function $id(id) { return document.getElementById(id); }

  // ── Utilities ──────────────────────────────────────────────────────────────

  function api(path, opts) {
    return fetch('/api/plugins/bass_retune/' + path, opts).then(r => r.json());
  }

  // Bass open-string semitones (E2=28, A2=33, D3=38, G3=43)
  const _BASE_SEMITONES = [28, 33, 38, 43];
  const _NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];

  // List badge: just the resulting open-string note names, e.g. "D A D G"
  function offsetStr(offsets) {
    return offsets.map((o, i) => _NOTE_NAMES[(_BASE_SEMITONES[i] + o + 120) % 12]).join(' ');
  }

  // Modal detail: "D A D G  →  E(-2) A(+0) D(+0) G(+0)"
  function offsetStrDetail(offsets) {
    const eStd = ['E', 'A', 'D', 'G'];
    const current = offsets.map((o, i) => _NOTE_NAMES[(_BASE_SEMITONES[i] + o + 120) % 12]).join(' ');
    const detail  = offsets.map((o, i) => eStd[i] + '(' + (o >= 0 ? '+' : '') + o + ')').join(' ');
    return current + '  →  ' + detail;
  }

  function _esc(str) {
    return (str || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _spinner() {
    return '<svg class="inline-block w-3 h-3 mr-1 animate-spin text-white" fill="none" viewBox="0 0 24 24">' +
      '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>' +
      '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/></svg>';
  }

  // ── Patch status strip ─────────────────────────────────────────────────────

  function _showPatchStrip(state, message, showButton) {
    const el = $id('patch-status');
    if (!el) return;

    if (state === 'hidden') {
      el.className = 'hidden';
      el.innerHTML = '';
      return;
    }

    const styles = {
      warning: 'flex items-center gap-3 px-3 py-2 rounded-lg mb-3 text-xs bg-yellow-950/40 border border-yellow-800/50 text-yellow-400',
      success: 'flex items-center gap-3 px-3 py-2 rounded-lg mb-3 text-xs bg-green-950/40 border border-green-800/50 text-green-400',
      error:   'flex items-center gap-3 px-3 py-2 rounded-lg mb-3 text-xs bg-red-950/40 border border-red-800/50 text-red-400',
      ok:      'flex items-center gap-3 px-3 py-2 rounded-lg mb-3 text-xs text-gray-500',
    };
    el.className = styles[state] || styles.ok;

    const msg = document.createElement('span');
    msg.className = 'flex-1';
    msg.textContent = message;
    el.innerHTML = '';
    el.appendChild(msg);

    if (showButton) {
      const btn = document.createElement('button');
      btn.className = 'px-2 py-1 rounded text-xs font-medium bg-accent hover:bg-accent/80 text-white transition';
      btn.textContent = 'Apply Patch';
      btn.addEventListener('click', () => patchCore(btn));
      el.appendChild(btn);
    }
  }

  // ── Health check ───────────────────────────────────────────────────────────

  async function checkHealth() {
    try {
      const data = await api('health');
      if (!data.song_py_ok) {
        if (data.is_elevated) {
          _showPatchStrip('warning', '\u26a0 Colored indications in Tab View of string changes post-retune requires a one-time patch after app updates.', true);
        } else {
          _showPatchStrip('warning', '\u26a0 Colored indications in Tab View of string changes post-retune requires a one-time patch. Restart Slopsmith as Administrator to apply it.', false);
        }
      } else {
        _showPatchStrip('hidden');
      }
    } catch (err) {
      console.warn('[bass_retune] health check failed:', err);
    }
  }

  // ── Patch core ─────────────────────────────────────────────────────────────

  async function patchCore(btn) {
    const origText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = _spinner() + 'Patching\u2026';

    try {
      const data = await api('patch_core', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });

      if (data.already_present && !data.patched) {
        console.warn('[bass_retune] patch_core: already_present but health reported false');
        _showPatchStrip('ok', '\u2139 Field already present. Try restarting Slopsmith.');
      } else if (data.patched) {
        _showPatchStrip('success', '\u2713 Patched. Restart Slopsmith to apply.');
      } else {
        console.error('[bass_retune] patch_core failed:', data.error);
        _showPatchStrip('error', '\u2715 Auto-patch failed \u2014 manual patch required. See console for details.');
      }
    } catch (err) {
      console.error('[bass_retune] patch_core request error:', err);
      _showPatchStrip('error', '\u2715 Patch request failed. See console for details.');
      btn.disabled = false;
      btn.textContent = origText;
    }
  }

  // ── Song list ──────────────────────────────────────────────────────────────

  function renderList() {
    const $list = $id('song-list');
    if (!$list) return;

    if (!_songs.length) {
      $list.innerHTML = '<div class="text-center text-gray-500 text-sm py-10">No eligible songs found.</div>';
      return;
    }

    const sorted = _songs.slice().sort((a, b) => {
      if (_sortKey === 'title') return (a.title || '').localeCompare(b.title || '');
      return (b.rowid || 0) - (a.rowid || 0);
    });

    $list.innerHTML = '';
    for (const song of sorted) {
      const card = document.createElement('div');
      card.className = 'flex items-center gap-3 bg-dark-800 border border-gray-800 rounded-xl px-4 py-3';

      card.innerHTML =
        `<div class="flex-1 min-w-0">` +
          `<div class="font-medium text-gray-200 truncate">${_esc(song.title)}</div>` +
          `<div class="text-xs text-gray-500 mt-0.5">${_esc(song.artist)}</div>` +
        `</div>` +
        `<span class="font-mono text-xs text-yellow-500 border border-gray-700 bg-dark-700 rounded px-2 py-0.5 whitespace-nowrap">${_esc(offsetStr(song.offsets))}</span>`;

      const btn = document.createElement('button');
      btn.className = 'px-3 py-1.5 rounded-lg text-sm font-medium bg-accent hover:bg-accent/80 text-white transition whitespace-nowrap';
      btn.textContent = 'Add E-Std';
      btn.addEventListener('click', () => openModal(song));
      card.appendChild(btn);

      $list.appendChild(card);
    }
  }

  async function loadPluginSongs() {
    const $list = $id('song-list');
    if ($list) $list.innerHTML =
      '<div class="text-center text-gray-500 text-sm py-10">' +
      '<svg class="inline-block w-4 h-4 mr-2 animate-spin text-accent" fill="none" viewBox="0 0 24 24">' +
      '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>' +
      '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/></svg>' +
      'Scanning library\u2026</div>';
    try {
      const data = await api('songs');
      _songs = (data && data.songs) || [];
      renderList();
    } catch (err) {
      if ($list) $list.innerHTML =
        `<div class="text-center text-red-400 text-sm py-10">Failed to load songs: ${_esc(err.message)}</div>`;
    }
  }

  // ── Modal ──────────────────────────────────────────────────────────────────

  async function openModal(song) {
    _pendingSong = song;
    const $modal = $id('modal');
    const $label = $id('modal-song-label');
    const $info  = $id('modal-info');
    const $warn  = $id('modal-warning');
    const $warnBody = $id('modal-warning-body');
    const $noteList = $id('modal-note-list');
    const $confirm  = $id('modal-confirm');
    const $status   = $id('modal-status');

    if ($label) $label.textContent = `${song.title}${song.artist ? ' \u2014 ' + song.artist : ''}`;
    if ($info) $info.innerHTML =
      `<div class="flex justify-between items-baseline py-1.5 border-b border-gray-800 text-sm gap-4">` +
      `<span class="text-gray-500 shrink-0">Current tuning</span>` +
      `<span class="font-medium text-gray-200 font-mono text-right">${_esc(offsetStrDetail(song.offsets))}</span></div>`;
    if ($warn) $warn.classList.add('hidden');
    if ($status) { $status.textContent = ''; $status.className = 'text-xs text-gray-500 mt-3 min-h-[18px]'; }
    if ($confirm) { $confirm.disabled = false; $confirm.textContent = 'Add E-Std Arrangement'; }
    if ($modal) $modal.classList.remove('hidden');

    try {
      const check = await api('check?filename=' + encodeURIComponent(song.filename));
      if (!check.eligible) {
        if ($status) { $status.className = 'text-xs text-red-400 mt-3 min-h-[18px]'; $status.textContent = check.reason || 'Not eligible.'; }
        if ($confirm) $confirm.disabled = true;
        return;
      }
      const arr = (check.arrangements || [])[0] || {};
      const count = arr.octave_up_count || 0;
      if (count > 0 && $warn) {
        $warn.classList.remove('hidden');
        if ($warnBody) $warnBody.textContent = `${count} note${count !== 1 ? 's' : ''} will be shifted up an octave to fit E standard tuning.`;
        if ($noteList) $noteList.innerHTML = (arr.octave_up_notes || []).map(n =>
          `<div>t=${n.time}s str=${n.string} fret=${n.fret} (${n.section || '\u2014'})</div>`
        ).join('');
      }
    } catch (err) {
      console.warn('[bass_retune] pre-flight check failed:', err);
    }
  }

  function closeModal() {
    const $modal = $id('modal');
    if ($modal) $modal.classList.add('hidden');
    _pendingSong = null;
  }

  async function confirmConvert() {
    if (!_pendingSong) return;
    const song = _pendingSong;
    const $confirm = $id('modal-confirm');
    const $status  = $id('modal-status');

    if ($confirm) { $confirm.disabled = true; $confirm.innerHTML = _spinner() + 'Converting\u2026'; }
    if ($status)  { $status.textContent = ''; $status.className = 'text-xs text-gray-500 mt-3 min-h-[18px]'; }

    try {
      const result = await api('convert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: song.filename }),
      });

      if (result.success) {
        if ($status) { $status.className = 'text-xs text-green-400 mt-3 min-h-[18px]'; }
        const warns = (result.warnings || []).length;
        if ($status) $status.textContent = '\u2713 Done.' + (warns ? ` ${warns} warning(s) \u2014 check console.` : '');
        if (warns) console.warn('[bass_retune] convert warnings:', result.warnings);
        _songs = _songs.filter(s => s.filename !== song.filename);
        renderList();
        setTimeout(closeModal, 1800);
      } else {
        if ($status) { $status.className = 'text-xs text-red-400 mt-3 min-h-[18px]'; $status.textContent = '\u2715 ' + (result.error || 'Conversion failed.'); }
        if ($confirm) { $confirm.disabled = false; $confirm.textContent = 'Add E-Std Arrangement'; }
      }
    } catch (err) {
      if ($status) { $status.className = 'text-xs text-red-400 mt-3 min-h-[18px]'; $status.textContent = '\u2715 ' + err.message; }
      if ($confirm) { $confirm.disabled = false; $confirm.textContent = 'Add E-Std Arrangement'; }
    }
  }

  // ── Sort buttons ───────────────────────────────────────────────────────────

  function _setSortActive(key) {
    _sortKey = key;
    const $t = $id('sort-title');
    const $r = $id('sort-recent');
    const active   = 'px-3 py-1 rounded text-xs font-medium border border-accent bg-accent text-white transition sort-btn';
    const inactive = 'px-3 py-1 rounded text-xs font-medium border border-gray-700 bg-dark-700 text-gray-400 hover:text-white hover:bg-dark-600 transition sort-btn';
    if ($t) $t.className = key === 'title'  ? active : inactive;
    if ($r) $r.className = key === 'recent' ? active : inactive;
    renderList();
  }

  // ── Wire events (use delegation since DOM is injected async) ───────────────

  document.addEventListener('click', function (e) {
    const id = e.target.id || (e.target.closest('[id]') || {}).id;
    if (id === 'sort-title')   { e.preventDefault(); _setSortActive('title'); }
    if (id === 'sort-recent')  { e.preventDefault(); _setSortActive('recent'); }
    if (id === 'modal-cancel') { e.preventDefault(); closeModal(); }
    if (id === 'modal-confirm'){ e.preventDefault(); confirmConvert(); }
    if (e.target.id === 'modal' ) closeModal();
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Tab View beat coloring
  // ═══════════════════════════════════════════════════════════════════════════
  //
  // Confirmed DOM structure (alphaTab 1.8, Page layout):
  //   - Beat groups are <g class="bN"> where N is GLOBALLY sequential across
  //     the entire score — b0, b1, b2, ... with no per-measure reset.
  //   - Each global beat index N has EXACTLY TWO <g class="bN"> elements
  //     (one per stave — standard notation + tab stave), so count = 2×beats.
  //   - There are NO measure group elements with class mN or similar.
  //   - All other <g> elements have class "at" (alphaTab internal nodes).
  //
  // The flags map from /api/plugins/bass_retune/flags uses the same global
  // beat index N, so we can directly query g.bN and add our CSS class.
  //
  // Cache slot mismatch:
  //   _compute_flags_from_psarc() in routes.py determines arr_idx from the
  //   manifest JSON entry order, which does NOT necessarily match the
  //   arrangement_index in bundle.songInfo (slopsmith's own list index).
  //   The debug_flags endpoint confirmed arr_idx=0 has the 91 flags, but
  //   /flags?arrangement=0,1,2 all returned {} — meaning the cache file
  //   was written but _read_flags_cache is reading a different path, OR
  //   the cache wasn't flushed to disk before the query.
  //
  //   Workaround: probe all arrangement slots 0-3 and use the first one
  //   with non-empty flags. Cache the winner per filename so we only probe
  //   once per song load. Additionally expose a backend endpoint to re-write
  //   the cache on demand via debug_flags (which recomputes live).
  //
  // ── Stylesheet ─────────────────────────────────────────────────────────────

  (function _injectStyles() {
    if (document.getElementById('bass-retune-tab-styles')) return;
    const style = document.createElement('style');
    style.id = 'bass-retune-tab-styles';
    // Target all SVG shape elements inside a flagged beat group.
    // octaveUp  → amber: the note was moved up an octave to fit E standard.
    // stringDown → cyan: the note was remapped to a lower string, same pitch.
    //
    // We use !important because alphaTab sets inline fill="" on many paths
    // and its own stylesheet has specificity over class-only selectors.
    style.textContent = [
      'g.br-octave-up path, g.br-octave-up rect, g.br-octave-up circle,',
      'g.br-octave-up ellipse, g.br-octave-up text { fill: #b45309 !important; }',
      'g.br-octave-up line, g.br-octave-up polyline, g.br-octave-up polygon',
      '{ stroke: #b45309 !important; }',
      'g.br-string-down path, g.br-string-down rect, g.br-string-down circle,',
      'g.br-string-down ellipse, g.br-string-down text { fill: #0e7490 !important; }',
      'g.br-string-down line, g.br-string-down polyline, g.br-string-down polygon',
      '{ stroke: #0e7490 !important; }',
    ].join('\n');
    document.head.appendChild(style);
  })();

  // ── Flags fetching ──────────────────────────────────────────────────────────
  //
  // The cache file is keyed by (filename, arr_idx) where arr_idx comes from
  // _compute_flags_from_psarc's manifest scan — NOT from bundle.songInfo
  // .arrangement_index. We don't know which slot has the flags until we probe.
  // Probe slots 0-3 in parallel; use whichever returns a non-empty map.
  // Cache the result in _flagsCache so subsequent renders for the same song
  // don't re-probe.

  // _flagsCache: filename → { flags: {beatIdx:flag}, probed: true }
  //              filename → { flags: {}, probed: true }  (no flags found)
  const _flagsCache = new Map();

  async function _fetchFlagsForFile(filename) {
    if (_flagsCache.has(filename)) return _flagsCache.get(filename).flags;

    // Probe slots sequentially until a non-empty result is found.
    // No fixed cap on slot count — songs with multiple bass arrangements
    // can push the E-Std index arbitrarily high. Stop at 10 as a sanity
    // ceiling against malformed cache data causing runaway requests.
    const MAX_SLOTS = 10;
    let found = {};
    for (let i = 0; i < MAX_SLOTS; i++) {
      let result;
      try {
        result = await fetch('/api/plugins/bass_retune/flags?filename=' +
          encodeURIComponent(filename) + '&arrangement=' + i)
          .then(r => r.json());
      } catch (_) {
        break;
      }
      const f = (result && result.flags) ? result.flags : {};
      if (Object.keys(f).length > 0) {
        found = f;
        console.log('[bass_retune] flags found at arrangement slot', i,
                    '—', Object.keys(f).length, 'flagged beats for', filename);
        break;
      }
    }

    if (!Object.keys(found).length) {
      console.log('[bass_retune] no flags in any slot for', filename,
                  '— not a Bass E-Std arrangement or cache missing');
    }

    _flagsCache.set(filename, { flags: found });
    return found;
  }

  // ── DOM coloring ────────────────────────────────────────────────────────────
  //
  // alphaTab renders each beat as two <g class="bN"> elements (notation + tab
  // stave). We collect all of them, deduplicate by beat index to get a sorted
  // list of unique indices, then look up each in flagsMap.
  //
  // We do NOT deduplicate the actual elements — we color ALL elements sharing
  // a given bN class so both staves (notation row and tab row) get colored.

  function _applyFlagClasses(atMount, flagsMap) {
    if (!atMount) return;
    if (!flagsMap || !Object.keys(flagsMap).length) return;

    // Search both the light DOM and any shadow root.
    const roots = [atMount];
    if (atMount.shadowRoot) roots.push(atMount.shadowRoot);

    let applied = 0;

    for (const root of roots) {
      // Collect every <g> with a class matching exactly /^b\d+$/.
      // className.baseVal gives the raw SVG class string.
      const allGs = root.querySelectorAll('g[class]');
      for (const g of allGs) {
        const cls = g.className && g.className.baseVal !== undefined
          ? g.className.baseVal
          : (typeof g.className === 'string' ? g.className : '');

        if (!/^b\d+$/.test(cls)) continue;   // only pure "bN" elements

        // Only color the tab-stave beat group (has a direct <text> child).
        // The notation-stave group has a nested <g class="at"> instead.
        if (!g.querySelector(':scope > text')) continue;

        const beatIdx = cls.slice(1);         // strip leading "b" → "42"

        // Always clear stale classes first (safe after re-renders).
        g.classList.remove('br-octave-up', 'br-string-down');

        const flag = flagsMap[beatIdx];
        if (flag === 'octaveUp') {
          g.classList.add('br-octave-up');
          applied++;
        } else if (flag === 'stringDown') {
          g.classList.add('br-string-down');
          applied++;
        }
      }
    }

    if (applied > 0) {
      console.log('[bass_retune] colored', applied, 'beats (MO path)');
    }
  }

  // ── Factory hook ────────────────────────────────────────────────────────────
  //
  // window.slopsmithViz_tabview is the factory registered by tabview's
  // screen.js. We wrap it so every instance notifies us when its DOM changes.
  //
  // Inside each factory instance, _tvApi, _tvAtMount, and _tvReady are
  // closure-private. We observe the rendered DOM via MutationObserver on
  // the .tabview-at div, which fires whenever alphaTab mutates the SVG
  // (i.e., after renderFinished). The observer is debounced at 120ms.
  //
  // Song-change detection: we shadow tabview's own draw() logic — compare
  // bundle.songInfo.filename + arrangement_index each frame, and re-fetch
  // flags when the pair changes.

  const _POLL_MS  = 200;
  const _POLL_MAX = 75;   // 15 seconds

  function _hookTabviewFactory() {
    let _polls = 0;

    function _tryHook() {
      const orig = window.slopsmithViz_tabview;
      if (typeof orig !== 'function') {
        if (++_polls < _POLL_MAX) setTimeout(_tryHook, _POLL_MS);
        else console.warn('[bass_retune] slopsmithViz_tabview never appeared; tab coloring disabled');
        return;
      }
      if (orig._brWrapped) return;   // already wrapped (hot-reload guard)

      function _wrappedFactory() {
        const inst = orig.apply(this, arguments);
        if (!inst || typeof inst.init !== 'function') return inst;

        // Per-instance state.
        let _observer    = null;
        let _debounce    = null;
        let _curFile     = null;
        let _curArr      = null;
        let _curFlags    = null;   // flags map currently applied
        let _loadGen     = 0;      // incremented each _loadFlags call to cancel stale _tryColor loops
        // Stable instance id for logging (tabview uses numeric ids internally,
        // but we can't read them, so use a timestamp-based one).
        const _iid = 'br' + (++_hookTabviewFactory._seq);

        // Find the most recently added .tabview-at div. Under splitscreen
        // there may be several; the last one belongs to the newest instance.
        function _findMount() {
          const all = document.querySelectorAll('.tabview-at');
          return all.length ? all[all.length - 1] : null;
        }

        function _color(mount) {
          if (!_curFlags) return;
          if ((window.slopsmith?.currentSong?.arrangement || '') !== 'Bass E-Std') return;
          _applyFlagClasses(mount || _findMount(), _curFlags);
        }

        function _onMutation() {
          clearTimeout(_debounce);
          _debounce = setTimeout(function () {
            _color(_findMount());
          }, 300);
        }

        function _observe(mount) {
          if (_observer) { _observer.disconnect(); _observer = null; }
          if (!mount) return;
          _observer = new MutationObserver(_onMutation);
          _observer.observe(mount, { childList: true, subtree: true });
        }

        async function _loadFlags(filename, mount) {
          _curFlags = null;
          const myGen = ++_loadGen;  // cancel any in-flight _tryColor from a previous call
          // Strip stale classes immediately so a re-render doesn't flash old colors.
          if (mount) {
            mount.querySelectorAll('.br-octave-up, .br-string-down')
                 .forEach(g => g.classList.remove('br-octave-up', 'br-string-down'));
          }
          const flags = await _fetchFlagsForFile(filename);
          if (myGen !== _loadGen) return;  // superseded by a newer _loadFlags call
          _curFlags = flags;
          // alphaTab may not have rendered bN elements yet — or the MutationObserver
          // may have already fired before the fetch resolved. Retry until we
          // successfully color at least one beat (meaning both flags AND bN elements
          // are present), then the MutationObserver handles scroll re-renders.
          let attempts = 0;
          const MAX_ATTEMPTS = 120;  // 30s at 250ms intervals
          function _tryColor() {
            if (myGen !== _loadGen) return;  // superseded — stop retrying
            const m = _findMount();
            if (!m || !_curFlags) return;
            // Check arrangement name here (not at fetch time) to avoid the race
            // where currentSong.arrangement isn't populated yet at init.
            // Only 'Bass E-Std' arrangements should be colored — exact match.
            // Retry if empty (currentSong not ready) or if still on a Bass
            // variant mid-transition (e.g. briefly showing "Bass" before
            // "Bass E-Std" lands). Only suppress permanently for non-Bass names.
            const arrName = window.slopsmith?.currentSong?.arrangement || '';
            if (arrName === '') {
              if (++attempts < MAX_ATTEMPTS) setTimeout(_tryColor, 250);
              return;
            }
            if (arrName !== 'Bass E-Std') {
              if (arrName.includes('Bass') && ++attempts < MAX_ATTEMPTS) {
                setTimeout(_tryColor, 250);
                return;
              }
              console.log('[bass_retune] arrangement "' + arrName + '" — coloring suppressed');
              return;
            }
            const allGs = m.querySelectorAll('g[class]');
            let colored = 0;
            for (const g of allGs) {
              const cls = g.className && g.className.baseVal !== undefined
                ? g.className.baseVal : (typeof g.className === 'string' ? g.className : '');
              if (!/^b\d+$/.test(cls)) continue;
              // Only color the tab-stave beat group (has a direct <text> child
              // with the fret number). The notation-stave group contains a nested
              // <g class="at"> instead — skip it to avoid coloring note icons.
              const directText = g.querySelector(':scope > text');
              if (!directText) continue;
              g.classList.remove('br-octave-up', 'br-string-down');
              const flag = _curFlags[cls.slice(1)];
              if (flag === 'octaveUp') { g.classList.add('br-octave-up'); colored++; }
              else if (flag === 'stringDown') { g.classList.add('br-string-down'); colored++; }
            }
            if (colored > 0) {
              console.log('[bass_retune] colored', colored, 'beats');
              if (!_observer) _observe(m);
            } else if (++attempts < MAX_ATTEMPTS) {
              setTimeout(_tryColor, 250);
            }
          }
          // Attach observer as soon as .tabview-at exists — poll until it
          // appears. alphaTab recreates the div on each init so we must
          // re-observe after every _loadFlags call, not just at init time.
          // _tryColor handles coloring; the observer handles scroll re-renders.
          (function _attachWhenReady(gen, att) {
            if (gen !== _loadGen) return;
            const m = _findMount();
            if (m) {
              _observe(m);
            } else if (att < 60) {
              setTimeout(function() { _attachWhenReady(gen, att + 1); }, 250);
            }
          })(myGen, 0);
          _tryColor();
        }

        // ── Filename extraction ───────────────────────────────────────
        // bundle.songInfo.filename is not populated by slopsmith core for all
        // songs. Fall back to audio_url derivation, then window.slopsmith
        // .currentSong.filename (populated for songs without audio_url).
        function _filenameFromBundle(b) {
          const si = (b && b.songInfo) || {};
          if (typeof si.filename === 'string' && si.filename) return decodeURIComponent(si.filename);
          // window.slopsmith.currentSong.filename is the authoritative source —
          // prefer it over audio_url derivation since the audio file may use
          // underscores while the psarc has spaces (or vice versa).
          const csf = window.slopsmith && window.slopsmith.currentSong &&
                      window.slopsmith.currentSong.filename;
          if (typeof csf === 'string' && csf) return decodeURIComponent(csf);
          if (typeof si.audio_url === 'string' && si.audio_url) {
            const m = si.audio_url.match(/\/audio\/audio_(.+)\.mp3$/i);
            if (m) return decodeURIComponent(m[1]) + '.psarc';
          }
          return null;
        }

        // ── Wrap init ────────────────────────────────────────────────
        const origInit = inst.init.bind(inst);
        inst.init = function (canvas, bundle) {
          origInit(canvas, bundle);
          const si = (bundle && bundle.songInfo) || {};
          _curFile = _filenameFromBundle(bundle);
          _curArr  = Number.isInteger(si.arrangement_index) ? si.arrangement_index : 0;

          // Delay slightly so tabview has time to create the .tabview-at div.
          setTimeout(function () {
            const mount = _findMount();
            if (mount) _observe(mount);
            // Always start _loadFlags even if mount is null — _tryColor will
            // poll until the DOM lands and will attach the observer itself.
            if (_curFile) _loadFlags(_curFile, mount);
          }, 150);
        };

        // ── Wrap draw ────────────────────────────────────────────────
        // draw() is called every rAF. We only act on song/arr changes.
        const origDraw = inst.draw.bind(inst);
        inst.draw = function (bundle) {
          origDraw(bundle);
          if (!bundle) return;
          const si  = bundle.songInfo || {};
          const fn  = _filenameFromBundle(bundle);
          const arr = Number.isInteger(si.arrangement_index) ? si.arrangement_index : 0;
          if (fn && (fn !== _curFile || arr !== _curArr)) {
            _curFile = fn;
            _curArr  = arr;
            _curFlags = null;  // clear immediately so MO doesn't re-apply stale colors
            // Invalidate cache entry so we re-probe arrangement slots for
            // the new song (different songs may have flags at different slots).
            _flagsCache.delete(fn);
            const mount = _findMount();
            _observe(mount);
            _loadFlags(fn, mount);
          }
        };

        // ── Wrap destroy ─────────────────────────────────────────────
        const origDestroy = inst.destroy.bind(inst);
        inst.destroy = function () {
          if (_observer) { _observer.disconnect(); _observer = null; }
          clearTimeout(_debounce);
          _curFlags = null;
          origDestroy();
        };

        console.log('[bass_retune] tabview instance', _iid, 'hooked');
        return inst;
      }

      _wrappedFactory._brWrapped = true;
      window.slopsmithViz_tabview = _wrappedFactory;
      console.log('[bass_retune] tabview factory wrapped for beat coloring');
    }

    _tryHook();
  }
  _hookTabviewFactory._seq = 0;

  // ── Init ───────────────────────────────────────────────────────────────────

  loadPluginSongs();
  checkHealth();
  _hookTabviewFactory();

})();
