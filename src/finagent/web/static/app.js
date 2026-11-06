// Budget planner: input -> working -> results. The server sends a ready-made
// view model (finagent/web/view.py); this file only draws it.
(function () {
  'use strict';

  var C = 314.16; // circumference of the r=50 pie ring
  var COL = { need: '#000000', want: '#FF7A1A', savings: '#2D6CDF', one_time_set_aside: '#7A5AC8', unallocated: '#B9B8B2' };
  var CARD_BG = { current: '#F4F3F0', other: '#FFFFFF', recommended: '#CDEBF7' };
  var MIN_WORKING_MS = 1200;
  var MAX_LOG = 7; // progress lines kept on screen; older ones scroll away

  var $ = function (id) { return document.getElementById(id); };
  var els = {
    intro: $('intro'), card: $('card'), text: $('situation'), feedback: $('feedback'), snippet: $('snippet'),
    working: $('working'), log: $('log'), results: $('results'), example: $('example'), edit: $('edit'), submit: $('submit'),
    thread: $('thread'), label: $('situation-label'), hint: $('hint'), restart: $('restart'), restartClosed: $('restart-closed'),
    back: $('back')
  };
  var PLACEHOLDER = els.text.placeholder;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function money(n) { return '$' + Math.round(n).toLocaleString('en-US'); }
  function pct(n) { return Math.round(n * 100); }
  function reduceMotion() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; }
  }
  function userId() {
    try {
      var id = sessionStorage.getItem('finagent.user_id');
      if (!id) { id = 'web_' + Math.random().toString(36).slice(2, 10); sessionStorage.setItem('finagent.user_id', id); }
      return id;
    } catch (e) { return 'web'; }
  }
  function resetUserId() {
    try { sessionStorage.removeItem('finagent.user_id'); } catch (e) { /* storage unavailable */ }
  }

  // --- conversation (addition) ---------------------------------------------------
  // The page shows the conversation so far but sends only the newest message: the server's
  // input phase remembers earlier ones for this user_id. Kept in sessionStorage with the id.

  var THREAD_KEY = 'finagent.thread';
  var thread = loadThread();

  function loadThread() {
    try { return JSON.parse(sessionStorage.getItem(THREAD_KEY)) || []; } catch (e) { return []; }
  }
  function saveThread() {
    try { sessionStorage.setItem(THREAD_KEY, JSON.stringify(thread)); } catch (e) { /* storage unavailable */ }
  }

  function renderThread() {
    var started = thread.length > 0;
    var ready = started && !!thread[thread.length - 1].ready;
    els.thread.hidden = !started;
    els.thread.innerHTML = thread.map(function (m) {
      if (m.role === 'user') return '<div class="msg user"><span class="who">You</span>' + esc(m.text) + '</div>';
      var qs = m.questions && m.questions.length
        ? '<ol class="questions">' + m.questions.map(function (q) { return '<li>' + esc(q) + '</li>'; }).join('') + '</ol>' : '';
      return '<div class="msg agent"><span class="who">Planner</span>' + esc(m.text) + qs + '</div>';
    }).join('');
    els.thread.scrollTop = els.thread.scrollHeight;
    els.label.textContent = !started ? 'Your situation' : (ready ? 'Anything to add or change?' : 'Your answer');
    els.text.placeholder = !started ? PLACEHOLDER
      : (ready ? 'For example: my rent goes up to $1,800 in January.' : 'Answer the questions above. Rough numbers are fine.');
    els.submit.textContent = !started ? 'Build my budgets' : (ready ? 'Update my budgets' : 'Send answer');
    els.hint.hidden = started;
    els.example.hidden = started;
    els.restart.hidden = !started;
    // Once a dashboard exists, Edit must be cancellable even if there is nothing to change.
    els.back.hidden = !(loadDashboard() || {}).view;
  }

  function backToDashboard() {
    var saved = loadDashboard();
    if (!saved || !saved.view) return;
    els.text.value = ''; // a draft update the user decided not to send
    showFeedback('');
    if (els.results.innerHTML) setPhase('results');
    else showDashboard(saved);
    window.scrollTo({ top: 0, behavior: reduceMotion() ? 'auto' : 'smooth' });
  }

  // --- saved dashboard (addition) ------------------------------------------------
  // The latest view is kept in sessionStorage so a reload shows it again. After an update,
  // `update` holds a short summary of what changed, shown above the dashboard until accepted.

  var DASHBOARD_KEY = 'finagent.dashboard';

  function loadDashboard() {
    try { return JSON.parse(sessionStorage.getItem(DASHBOARD_KEY)); } catch (e) { return null; }
  }
  function saveDashboard(saved) {
    try {
      if (saved) sessionStorage.setItem(DASHBOARD_KEY, JSON.stringify(saved));
      else sessionStorage.removeItem(DASHBOARD_KEY);
    } catch (e) { /* storage unavailable or full: the dashboard just won't survive a reload */ }
  }

  function updateNote(text) {
    return '<section id="update-note" class="t s12 update-note" role="status">' +
      '<div><span class="note-label">What changed</span><p>' + esc(text) + '</p></div>' +
      '<button type="button" class="primary" id="accept-update">Accept</button></section>';
  }

  function showDashboard(saved) {
    els.results.innerHTML = (saved.update ? updateNote(saved.update) : '') + renderResults(saved.view);
    var accept = $('accept-update');
    if (accept) {
      accept.addEventListener('click', function () {
        saved.update = null;
        saveDashboard(saved);
        var note = $('update-note');
        if (note) note.parentNode.removeChild(note);
      });
    }
    var first = thread.filter(function (m) { return m.role === 'user'; })[0];
    if (first) els.snippet.textContent = first.text; // the story, not the last answer
    setPhase('results');
  }

  function restart() {
    thread = [];
    saveThread();
    saveDashboard(null);
    resetUserId(); // a fresh id means a fresh profile and memory on the server
    els.text.value = '';
    els.results.innerHTML = '';
    showFeedback('');
    renderThread();
    setPhase('input');
    els.text.focus();
  }

  // --- phases ----------------------------------------------------------------

  function setPhase(phase) {
    var input = phase === 'input';
    els.intro.className = input ? 'intro' : 'intro is-gone';
    els.card.className = (input ? 'card is-open' : 'card is-closed') + (thread.length ? ' has-thread' : '');
    els.card.scrollTop = 0; // focusing the text box can scroll the card; don't carry that into the folded note
    els.working.hidden = phase !== 'working';
    els.results.hidden = phase !== 'results';
    els.submit.disabled = phase === 'working';
  }

  function showFeedback(message, questions) {
    var html = message ? '<p class="alert">' + esc(message) + '</p>' : '';
    if (questions && questions.length) {
      html += '<ol class="questions">' + questions.map(function (q) { return '<li>' + esc(q) + '</li>'; }).join('') + '</ol>';
    }
    els.feedback.innerHTML = html;
  }

  function submit(e) {
    e.preventDefault();
    var text = els.text.value.trim();
    if (!text) {
      showFeedback(thread.length ? 'Type your answer first.' : 'Add a few details about your money first, or click Fill in an example.');
      return;
    }
    showFeedback('');
    thread.push({ role: 'user', text: text });
    saveThread();
    renderThread();
    els.text.value = '';
    els.snippet.textContent = text;
    els.log.innerHTML = '';
    setPhase('working');
    var started = Date.now();
    var done = function (data) {
      var wait = reduceMotion() ? 0 : Math.max(0, MIN_WORKING_MS - (Date.now() - started));
      setTimeout(function () { handle(data); }, wait);
    };
    streamPlan(text, done)
      .catch(function () { done({ status: 'error', message: 'Could not reach the planner. Is the server running?' }); });
  }

  // Shows each progress line as the server sends it, then hands the final result to `done`.
  // The server answers with newline-delimited JSON; a plain JSON answer (a bad request) is passed through.
  function streamPlan(text, done) {
    return fetch('/api/plan/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, user_id: userId() })
    }).then(function (r) {
      var ndjson = (r.headers.get('Content-Type') || '').indexOf('application/x-ndjson') === 0;
      if (!ndjson || !r.body) {
        return r.json().catch(function () { return { status: 'error' }; }).then(done);
      }
      var reader = r.body.getReader(), decoder = new TextDecoder(), buffer = '', finished = false;
      function line(raw) {
        if (!raw.trim()) return;
        var msg;
        try { msg = JSON.parse(raw); } catch (e) { return; }
        if (msg.type === 'progress') addProgress(msg);
        else if (msg.type === 'result') { finished = true; done(msg); }
      }
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            line(buffer + decoder.decode());
            if (!finished) done({ status: 'error', message: 'The planner stopped before it finished. Please try again.' });
            return;
          }
          buffer += decoder.decode(chunk.value, { stream: true });
          var lines = buffer.split('\n');
          buffer = lines.pop();
          lines.forEach(line);
          return pump();
        });
      }
      return pump();
    });
  }

  function addProgress(msg) {
    var li = document.createElement('li');
    li.innerHTML = (msg.who ? '<span class="who">' + esc(msg.who) + '</span>' : '') + esc(msg.label) +
      (msg.detail ? '<span class="detail">' + esc(msg.detail.length > 220 ? msg.detail.slice(0, 217) + '…' : msg.detail) + '</span>' : '');
    els.log.appendChild(li);
    while (els.log.children.length > MAX_LOG) els.log.removeChild(els.log.firstChild);
  }

  function handle(data) {
    if (data.status === 'ready' && data.view) {
      thread.push({ role: 'agent', text: data.message || 'Got it. Here are your budgets.', ready: true });
      saveThread();
      renderThread();
      var saved = { view: data.view, update: data.update_summary || null };
      saveDashboard(saved);
      showDashboard(saved);
      window.scrollTo({ top: 0, behavior: reduceMotion() ? 'auto' : 'smooth' });
      return;
    }
    if (data.status === 'needs_clarification') {
      thread.push({ role: 'agent', text: data.message || 'A few more details and we can build your budgets:',
                    questions: data.questions || [] });
    } else {
      // Nothing was recorded on the server, so take the message back out and let the user retry it.
      var last = thread.pop();
      if (last && last.role === 'user') els.text.value = last.text;
      showFeedback(data.message || 'Something went wrong building your plan. Please try again.');
    }
    saveThread();
    renderThread();
    setPhase('input');
    els.text.focus();
  }

  els.card.addEventListener('submit', submit);
  els.text.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit(e);
  });
  els.text.addEventListener('input', function () { if (els.text.value.trim()) showFeedback(''); });
  els.example.addEventListener('click', function () {
    fetch('/api/example').then(function (r) { return r.json(); }).then(function (d) {
      els.text.value = d.text; showFeedback(''); els.text.focus();
    });
  });
  els.edit.addEventListener('click', function () { renderThread(); setPhase('input'); els.text.focus(); });
  els.restart.addEventListener('click', restart);
  els.restartClosed.addEventListener('click', restart);
  els.back.addEventListener('click', backToDashboard);
  renderThread();
  var savedDashboard = loadDashboard();
  if (savedDashboard && savedDashboard.view && thread.length) showDashboard(savedDashboard);

  (function squiggle() {
    var pts = [];
    for (var i = 0; i <= 240; i++) {
      var t = i / 240 * Math.PI * 2, r = 52 + 6 * Math.sin(t * 14);
      pts.push((70 + r * Math.cos(t)).toFixed(2) + ' ' + (70 + r * Math.sin(t)).toFixed(2));
    }
    $('squiggle').setAttribute('d', 'M' + pts.join(' L') + 'Z');
  })();

  // --- results -----------------------------------------------------------------

  var ICON_OK = '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#1A4DB0" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="flex: none; margin-top: 1px"><path d="M5 12.5l4.5 4.5L19 7.5"></path></svg>';
  var ICON_SHORT = '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#A3400A" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="flex: none; margin-top: 1px"><circle cx="12" cy="12" r="9"></circle><path d="M12 7.5v5.5M12 16.5v.01"></path></svg>';
  var ICON_NEUTRAL = '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#45443F" stroke-width="2.4" stroke-linecap="round" style="flex: none; margin-top: 1px"><path d="M6 12h12"></path></svg>';
  var ICON_LOCK = '<svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#000000" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex: none; margin-top: 3px"><rect x="5" y="11" width="14" height="9" rx="2"></rect><path d="M8 11V8a4 4 0 0 1 8 0v3"></path></svg>';

  function sectionTitle(text, rv, extra) {
    return '<div class="s12 reveal ' + rv + '" style="display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-end; gap: 16px; padding: 44px 8px 8px">' +
      '<div><h2 class="serif" style="margin: 0; font-size: 48px; line-height: 1; letter-spacing: -0.01em">' + esc(text) + '</h2>' +
      (extra && extra.sub ? '<p style="margin: 12px 0 0; font-size: 16px; line-height: 1.5; color: #4A4945">' + esc(extra.sub) + '</p>' : '') +
      '</div>' + (extra && extra.right ? extra.right : '') + '</div>';
  }

  // Slice keys any budget actually uses, so empty categories stay out of legends.
  function usedKeys(v) {
    var used = {};
    v.budgets.forEach(function (b) { b.slices.forEach(function (s) { if (s.amount >= 0.5) used[s.key] = true; }); });
    return used;
  }

  function pie(b, used) {
    var total = b.slices.reduce(function (a, s) { return a + s.amount; }, 0);
    var den = Math.max(b.income, total) || 1;
    var pos = 0;
    return b.slices.filter(function (s) { return used[s.key]; }).map(function (s) {
      var len = s.amount / den * C;
      var out = '<circle class="slice" cx="100" cy="100" r="50" style="stroke: ' + COL[s.key] +
        '; stroke-dasharray: ' + len.toFixed(2) + ' ' + C + '; stroke-dashoffset: ' + (-pos).toFixed(2) + '"></circle>';
      pos += len;
      return out;
    }).join('');
  }

  function legend(b, base, used) {
    var den = b.income || 1;
    return b.slices.filter(function (s) { return used[s.key]; }).map(function (s, i) {
      var share = Math.round(s.amount / den * 100);
      var delta = '', color = '#4A4945';
      if (base) {
        var baseSlice = base.slices.filter(function (x) { return x.key === s.key; })[0];
        var d = share - Math.round(baseSlice.amount / (base.income || 1) * 100);
        if (d === 0) delta = 'Same';
        else {
          delta = (d > 0 ? '+' : '−') + Math.abs(d) + ' pts';
          color = d > 0 && s.key === 'savings' ? '#1A4DB0' : (d < 0 && s.key === 'want' ? '#A3400A' : '#45443F');
        }
      }
      return '<div style="display: grid; grid-template-columns: 12px minmax(0, 1fr) auto 42px 70px; align-items: center; gap: 10px; font-size: 15px">' +
        '<span style="width: 12px; height: 12px; border-radius: 50%; background: ' + COL[s.key] + '"></span>' +
        '<span>' + esc(s.label) + '</span>' +
        '<span style="font-weight: 600; text-align: right">' + money(s.amount) + '</span>' +
        '<span style="color: #4A4945; text-align: right">' + share + '%</span>' +
        '<span style="font-size: 13px; font-weight: 600; text-align: right; color: ' + color + '">' + delta + '</span></div>';
    }).join('');
  }

  function budgetCard(b, i, v, used) {
    var bg = b.recommended ? CARD_BG.recommended : (b.id === 'current' ? CARD_BG.current : CARD_BG.other);
    var aria = b.name + ' budget: ' + b.slices.filter(function (s) { return used[s.key]; })
      .map(function (s) { return s.label.toLowerCase() + ' ' + money(s.amount); }).join(', ') + ' a month.';
    var goals = b.goals.map(function (g) {
      var icon = g.ok === true ? ICON_OK : (g.ok === false ? ICON_SHORT : ICON_NEUTRAL);
      return '<div style="display: flex; gap: 10px; align-items: flex-start; font-size: 15px; line-height: 1.4">' + icon +
        '<span><strong style="font-weight: 600">' + esc(g.name) + '</strong> <span style="color: #4A4945">' + esc(g.status) + '</span></span></div>';
    }).join('');
    var miss = b.feasible === false && b.infeasibility
      ? '<details class="miss"><summary>Can’t meet every target</summary><p>' + esc(b.infeasibility) + '</p></details>' : '';
    return '<article class="t s4 reveal r' + (3 + i) + '" style="background: ' + bg + '; display: flex; flex-direction: column">' +
      '<div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; min-height: 30px">' +
      '<h3 style="margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.02em">' + esc(b.name) + '</h3>' +
      (b.recommended ? '<span style="padding: 6px 12px; border-radius: 999px; background: #000000; color: #FFFFFF; font-size: 13px; font-weight: 600">Recommended</span>' : '') +
      '</div>' +
      '<p style="margin: 8px 0 0; font-size: 15px; line-height: 1.5; color: #3F3E3A; min-height: 45px">' + esc(b.description) + '</p>' +
      '<svg class="pie" viewBox="0 0 200 200" role="img" aria-label="' + esc(aria) + '">' + pie(b, used) +
      '<circle class="wipe w' + (i + 1) + '" cx="100" cy="100" r="50" style="stroke: ' + bg + '"></circle></svg>' +
      '<div style="display: flex; flex-direction: column; gap: 10px; font-variant-numeric: tabular-nums">' + legend(b, i ? v.budgets[0] : null, used) + '</div>' +
      (goals ? '<div style="margin-top: 22px; padding-top: 18px; border-top: 1px solid rgba(0,0,0,.12); display: flex; flex-direction: column; gap: 10px">' + goals + '</div>' : '') +
      miss + '</article>';
  }

  function suggestionTiles(v) {
    var tiles = v.suggestions.slice(0, v.note ? 3 : 4).map(function (s, i) {
      return '<section class="t s3 reveal r' + (5 + (i >> 1)) + '" style="background: #FFFFFF; display: flex; flex-direction: column; justify-content: space-between; gap: 28px; min-height: 250px">' +
        '<span style="font-size: 15px; font-weight: 600">' + esc(s.label) + '</span>' +
        '<div><div class="num" style="font-size: 76px">' + money(s.amount) + '</div>' +
        '<p style="margin: 12px 0 0; font-size: 15px; line-height: 1.45; color: #3F3E3A">' + esc(s.note) + '</p></div></section>';
    });
    if (v.note) {
      tiles.push('<section class="t s3 reveal r6" style="background: #000000; color: #FFFFFF; display: flex; flex-direction: column; justify-content: space-between; gap: 28px; min-height: 250px">' +
        '<span style="font-size: 15px; font-weight: 600; color: #BDBCB7">' + esc(v.note.title) + '</span>' +
        '<p class="serif" style="margin: 0; font-size: ' + (v.note.text.length > 140 ? 21 : 30) + 'px; line-height: 1.15">' + esc(v.note.text) + '</p></section>');
    }
    // Stretch a short row so the tiles still fill the width.
    var span = tiles.length ? Math.floor(12 / tiles.length) : 3;
    if (span !== 3) tiles = tiles.map(function (t) { return t.replace('t s3 ', 't s' + span + ' '); });
    return tiles.join('');
  }

  function lineTable(v) {
    var barMax = 1;
    v.groups.forEach(function (g) { g.rows.forEach(function (r) { r.cells.forEach(function (c) { barMax = Math.max(barMax, c.amount); }); }); });
    var w = function (n) { return Math.min(100, n / barMax * 100).toFixed(1) + '%'; };

    var head = '<div role="row" class="trow" style="padding: 16px 0 12px; border-bottom: 2px solid #000000; font-size: 14px; font-weight: 600">' +
      '<span role="columnheader">Item</span>' + v.columns.map(function (c) { return '<span role="columnheader" style="text-align: right">' + esc(c) + '</span>'; }).join('') + '</div>';

    var essentials = '<div role="row" class="trow" style="padding: 18px 0; border-bottom: 1px solid #E6E5E1">' +
      '<span role="rowheader" style="display: flex; gap: 12px; align-items: flex-start">' + ICON_LOCK +
      '<span><span style="display: block; font-size: 17px; font-weight: 600">Essentials</span><span style="display: block; font-size: 14px; line-height: 1.45; color: #4A4945">' + esc(v.essentials.description) + '</span></span></span>' +
      v.essentials.amounts.map(function (a) { return '<span role="cell" style="text-align: right; font-size: 17px; padding-right: 12px">' + money(a) + '</span>'; }).join('') + '</div>';

    var groups = v.groups.map(function (g) {
      var color = COL[g.key];
      var rows = g.rows.map(function (row) {
        var base = row.cells[0].amount;
        var cells = row.cells.map(function (c, idx) {
          var amt;
          if (c.changed) {
            var d = c.amount - base;
            var dir = g.key === 'unallocated' ? 'is-moved' : (d < 0 ? 'is-cut' : 'is-up');
            var delta = c.paused ? 'Paused' : (d > 0 ? '+' : '−') + money(Math.abs(d));
            var tipId = 'tip-' + row.id.replace(/\W+/g, '-') + '-' + idx;
            amt = c.tip
              ? '<button type="button" class="chg ' + dir + '" aria-describedby="' + tipId + '"><span class="amt">' + money(c.amount) + '</span><span class="delta">' + delta + '</span>' +
                '<span class="tip' + (idx === row.cells.length - 1 ? ' end' : '') + '" role="tooltip" id="' + tipId + '">' + esc(c.tip) + '</span></button>'
              : '<span class="chg ' + dir + '" style="cursor: default"><span>' + money(c.amount) + '</span><span class="delta">' + delta + '</span></span>';
          } else {
            amt = '<span style="padding: 8px 12px">' + money(c.amount) + '</span>';
          }
          return '<span role="cell" style="display: flex; flex-direction: column; align-items: flex-end; font-size: 17px">' + amt +
            '<span class="track" aria-hidden="true"><span class="g" style="width: ' + (idx ? w(base) : '0%') + '"></span>' +
            '<span class="f" style="width: ' + w(c.amount) + '; background: ' + color + '"></span></span></span>';
        }).join('');
        return '<div role="row" class="trow" style="padding: 8px 0 12px; border-bottom: 1px solid #E6E5E1"><span role="rowheader" style="font-size: 17px">' + esc(row.name) + '</span>' + cells + '</div>';
      }).join('');
      return '<div role="row" style="display: flex; align-items: center; gap: 10px; padding: 24px 0 6px; font-size: 14px; font-weight: 600; color: #4A4945">' +
        '<span style="width: 12px; height: 12px; border-radius: 50%; background: ' + color + '"></span><span role="rowheader">' + esc(g.name) + '</span></div>' + rows;
    }).join('');

    return '<section class="t s12 reveal r7" style="background: #FFFFFF; padding-top: 12px; overflow-x: auto">' +
      '<div role="table" aria-label="Monthly amounts in each budget" style="min-width: 700px; font-variant-numeric: tabular-nums">' + head + essentials + groups + '</div></section>';
  }

  function points(title, items, icon, rv) {
    if (!items.length) return '';
    return '<section class="t s6 reveal ' + rv + '" style="background: #FFFFFF">' +
      '<span style="font-size: 15px; font-weight: 600">' + esc(title) + '</span>' +
      '<ul class="points">' + items.map(function (s) { return '<li>' + icon + '<span>' + esc(s) + '</span></li>'; }).join('') + '</ul></section>';
  }

  function renderResults(v) {
    var used = usedKeys(v);
    var legendKeys = [['need', 'Needs'], ['want', 'Wants'], ['savings', 'Savings'], ['one_time_set_aside', 'Set aside'], ['unallocated', 'Not assigned']]
      .filter(function (k) { return used[k[0]]; });
    var legendHtml = '<ul style="margin: 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap; gap: 18px; font-size: 15px">' +
      legendKeys.map(function (k) { return '<li style="display: flex; align-items: center; gap: 8px"><span style="width: 14px; height: 14px; border-radius: 50%; background: ' + COL[k[0]] + '"></span>' + k[1] + '</li>'; }).join('') + '</ul>';

    var rate = v.savings.rate;
    var html = '';
    html += '<section class="t s8 reveal r1" style="background: #000000; color: #FFFFFF; display: flex; flex-direction: column; justify-content: space-between; gap: 36px; min-height: 380px">' +
      '<span style="font-size: 15px; font-weight: 600; color: #BDBCB7">Your plan</span><div>' +
      '<h2 class="serif" style="margin: 0; font-size: clamp(48px, 6vw, 72px); line-height: .98; letter-spacing: -0.015em; max-width: 13ch">' + esc(v.headline) + '</h2>' +
      '<p style="margin: 22px 0 0; max-width: 58ch; font-size: 18px; line-height: 1.6; color: #D9D8D3">' + esc(v.summary) + '</p></div></section>';

    html += '<section class="t s4 reveal r2" style="background: #FF7A1A; display: flex; flex-direction: column; justify-content: space-between; gap: 24px; min-height: 380px">' +
      '<span style="font-size: 15px; font-weight: 600">' + esc(v.savings.label) + '</span><div>' +
      '<div class="num" style="font-size: clamp(96px, 10vw, 132px)">' + (rate == null ? '—' : pct(rate) + '<span class="faint">%</span>') + '</div>' +
      '<p style="margin: 16px 0 0; font-size: 17px; line-height: 1.45; font-weight: 500">' + esc(v.savings.note) + '</p></div></section>';

    html += sectionTitle('Three ways to budget ' + money(v.income) + ' a month', 'r3', { right: legendHtml });
    html += v.budgets.map(function (b, i) { return budgetCard(b, i, v, used); }).join('');

    if (v.suggestions.length || v.note) {
      html += sectionTitle('What we suggest', 'r5');
      html += suggestionTiles(v);
    }

    if (v.groups.length) {
      html += sectionTitle('What changes, line by line', 'r7', { sub: 'Grey bars show what you spend today. Hover over or tab to a highlighted amount to see why it changed.' });
      html += lineTable(v);
    }

    if (v.strengths.length || v.risks.length) {
      html += sectionTitle('Good to know', 'r7');
      html += points('Working for you', v.strengths, ICON_OK, 'r7');
      html += points('Keep an eye on', v.risks, ICON_SHORT, 'r7');
    }

    var made = v.meta.model ? 'Advice written by ' + v.meta.model + '; every number is computed, not generated.' : 'Every number is computed by the planner, not generated.';
    var warn = v.meta.warnings.length ? ' Some steps used the rule-based fallback.' : '';
    html += '<p class="s12 reveal r7" style="margin: 20px 8px 0; max-width: 72ch; font-size: 14px; line-height: 1.55; color: #55544F">' +
      'Based on what you’ve told us, with amounts converted to monthly figures and a forecast from ' + esc(v.meta.start_month) + '. ' + esc(made + warn) +
      ' This is general guidance to help you plan, not personalised financial advice.</p>';
    return html;
  }
})();
