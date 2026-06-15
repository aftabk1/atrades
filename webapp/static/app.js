// ── State ──────────────────────────────────────────────────────────────────
let historyChart  = null;
let currentDate   = todayStr();
let fallbackSymbols = [];
let symbolFilter    = '';
let refreshTimer    = null;
let countdown     = 15 * 60; // seconds

// ── Security helpers ───────────────────────────────────────────────────────
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _getCsrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)a1t_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

// ── Utilities ──────────────────────────────────────────────────────────────
function todayStr() {
  return new Date().toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
}
function fmt(n, dec=2) {
  if (n == null) return '—';
  return Number(n).toFixed(dec);
}
function fmtDollar(n) {
  if (n == null) return '—';
  return '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0});
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  return d.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', hour12:false});
}

// ── Tab switching ──────────────────────────────────────────────────────────
let _configEverLoaded = false;
function switchTab(name, el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'trades')    loadLiveTrades();
  if (name === 'today')     loadDashboard(currentDate);
  if (name === 'history')   loadHistory();
  if (name === 'watchlist') loadWatchlist(currentDate);
  if (name === 'config' && !_configEverLoaded) { _configEverLoaded = true; loadConfig(); }
}

// ── Dashboard (today / selected date) ─────────────────────────────────────
async function loadDashboard(dateStr) {
  currentDate = dateStr;
  document.getElementById('date-picker').value = dateStr;
  document.getElementById('refresh-btn').innerHTML = '<span class="spin"></span>';
  try {
    const [res, top5Res] = await Promise.all([
      fetch('/api/dashboard?date=' + dateStr),
      fetch('/api/scan/top5?date=' + dateStr),
    ]);
    const data  = await res.json();
    const top5  = await top5Res.json();
    renderDashboard(data);
    renderRadar(top5.candidates || []);
  } catch(e) {
    console.error(e);
  } finally {
    document.getElementById('refresh-btn').innerHTML = '&#8635; Refresh';
  }
}

function renderRadar(candidates) {
  const rb = document.getElementById('radar-body');
  const card = document.getElementById('radar-card');
  if (!candidates || candidates.length === 0) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';
  rb.innerHTML = candidates.map((c, i) => `
    <tr>
      <td class="muted">${i+1}</td>
      <td><strong>${esc(c.symbol)}</strong></td>
      <td>
        <div class="score-wrap">
          <span>${fmt(c.score,0)}</span>
          <div class="score-bar"><div class="score-fill" style="width:${c.score}%;background:var(--muted)"></div></div>
        </div>
      </td>
      <td class="num mono">${c.current_price ? '$'+fmt(c.current_price) : '—'}</td>
      <td class="num ${c.rsi > 70 ? 'yellow' : ''}">${fmt(c.rsi,0)}</td>
      <td class="num ${c.rs_vs_spy > 0 ? 'green' : 'red'}">${c.rs_vs_spy > 0 ? '+' : ''}${fmt(c.rs_vs_spy,1)}%</td>
      <td class="num ${c.volume_ratio >= 1.5 ? 'green' : c.volume_ratio < 0.8 ? 'muted' : ''}">${fmt(c.volume_ratio,1)}x</td>
      <td>${c.is_trap ? '<span class="trap-badge">TRAP</span>' : '<span class="muted">—</span>'}</td>
    </tr>`).join('');
}

function renderDashboard(d) {
  const regimeMap = {
    'BULL_TREND':      ['bull',  'BULL TREND'],
    'BEAR_TREND':      ['bear',  'BEAR TREND'],
    'SIDEWAYS':        ['side',  'SIDEWAYS'],
    'HIGH_VOLATILITY': ['hvol',  'HIGH VOL'],
  };
  const [cls, label] = regimeMap[d.regime] || ['side', d.regime || 'UNKNOWN'];
  document.getElementById('regime-dot').className  = 'regime-dot ' + cls;
  document.getElementById('regime-text').textContent =
    label + (d.adx ? '  ADX ' + fmt(d.adx, 1) : '') +
    (d.spy_above_200ma ? '  SPY > 200MA' : '');

  document.getElementById('stat-scanned').textContent    = d.symbols_scanned || '—';
  document.getElementById('stat-candidates').textContent = d.candidates_found || '0';
  document.getElementById('stat-trades').textContent     = d.trades_placed    || '0';
  document.getElementById('stat-scans').textContent      = d.scan_count       || '0';
  document.getElementById('stat-adx').textContent        = d.adx ? fmt(d.adx, 1) : '—';
  document.getElementById('stat-mult').textContent       = d.score_multiplier ? fmt(d.score_multiplier, 2) + 'x' : '—';

  const cb = document.getElementById('candidates-body');
  if (!d.candidates || d.candidates.length === 0) {
    cb.innerHTML = '<tr><td colspan="14" class="empty muted">No candidates found for this date</td></tr>';
  } else {
    cb.innerHTML = d.candidates.map((c, i) => `
      <tr>
        <td class="muted">${i+1}</td>
        <td><strong>${c.symbol}</strong></td>
        <td>
          <div class="score-wrap">
            <span>${fmt(c.score,0)}</span>
            <div class="score-bar"><div class="score-fill" style="width:${c.score}%"></div></div>
          </div>
        </td>
        <td class="num mono">$${fmt(c.entry)}</td>
        <td class="num mono red">$${fmt(c.stop)}</td>
        <td class="num mono green">$${fmt(c.target)}</td>
        <td class="num mono muted">$${fmt(c.trail_atr)}</td>
        <td class="num">${c.partial_shares||'—'}+${c.trail_shares||'—'}</td>
        <td class="num">${fmt(c.risk_reward,1)}x</td>
        <td class="num">${fmtDollar(c.dollar_risk)}</td>
        <td class="num ${c.rsi > 70 ? 'yellow' : ''}">${fmt(c.rsi,0)}</td>
        <td class="num ${c.rs_vs_spy > 0 ? 'green' : 'red'}">${c.rs_vs_spy > 0 ? '+' : ''}${fmt(c.rs_vs_spy,1)}%</td>
        <td class="num ${c.volume_ratio >= 1.5 ? 'green' : c.volume_ratio < 0.8 ? 'muted' : ''}">${fmt(c.volume_ratio,1)}x</td>
        <td class="num ${c.gap_pct >= 8 ? 'green' : 'muted'}">${c.gap_pct ? (c.gap_pct > 0 ? '+' : '') + fmt(c.gap_pct,1) + '%' : '—'}</td>
        <td>${c.is_trap ? '<span class="trap-badge">TRAP</span>' : '<span class="muted">—</span>'}</td>
      </tr>`).join('');
  }

  const tb = document.getElementById('trades-body');
  if (!d.trades || d.trades.length === 0) {
    tb.innerHTML = '<tr><td colspan="8" class="empty muted">No trades placed this date</td></tr>';
  } else {
    tb.innerHTML = d.trades.map(t => `
      <tr>
        <td class="muted">${fmtTime(t.ts)}</td>
        <td><strong>${t.symbol}</strong></td>
        <td class="num">${t.shares}</td>
        <td class="num mono">${t.fill_price ? '$' + fmt(t.fill_price) : '<span class="muted">$' + fmt(t.entry) + '</span>'}</td>
        <td class="num mono red">$${fmt(t.stop_loss)}</td>
        <td class="num mono green">${t.partial_target ? '$' + fmt(t.partial_target) : '—'}</td>
        <td class="num">${fmt(t.score,0)}</td>
        <td>${statusBadge(t.status)}</td>
      </tr>`).join('');
  }
}

// ── Live Positions ─────────────────────────────────────────────────────────
async function loadPositions() {
  try {
    const res  = await fetch('/api/positions');
    const data = await res.json();
    const banner = document.getElementById('closed-banner');
    if (!data.market_open && data.next_open) {
      const next = new Date(data.next_open).toLocaleString('en-US', {timeZone:'America/New_York',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      banner.style.display = 'block';
      banner.textContent   = 'Market closed — next open ' + next + ' ET';
    } else {
      banner.style.display = 'none';
    }
    const pb = document.getElementById('positions-body');
    if (data.error) {
      pb.innerHTML = `<tr><td colspan="7" class="empty muted">Alpaca error: ${esc(data.error)}</td></tr>`;
      return;
    }
    if (!data.positions || data.positions.length === 0) {
      pb.innerHTML = '<tr><td colspan="7" class="empty muted">No open positions</td></tr>';
      return;
    }
    pb.innerHTML = data.positions.map(p => {
      const plCls = p.unrealized_pl >= 0 ? 'green' : 'red';
      const sign  = p.unrealized_pl >= 0 ? '+' : '';
      return `
        <tr>
          <td><strong>${p.symbol}</strong></td>
          <td class="num">${p.qty}</td>
          <td class="num mono">$${fmt(p.avg_entry)}</td>
          <td class="num mono">$${fmt(p.current)}</td>
          <td class="num">${fmtDollar(p.market_value)}</td>
          <td class="num ${plCls}">${sign}${fmtDollar(p.unrealized_pl)}</td>
          <td class="num ${plCls}">${sign}${fmt(p.unrealized_plpc,2)}%</td>
        </tr>`;
    }).join('');
  } catch(e) {
    document.getElementById('positions-body').innerHTML =
      '<tr><td colspan="7" class="empty muted">Could not load positions</td></tr>';
  }
}

// ── Watchlist (pre-breakout setups) ────────────────────────────────────────
async function loadWatchlist(dateStr) {
  const url = dateStr ? `/api/scan/setups?date=${dateStr}` : '/api/scan/setups';
  try {
    const res  = await fetch(url);
    const data = await res.json();
    const candidates = data.candidates || [];

    // Badge
    const badge = document.getElementById('watchlist-badge');
    if (candidates.length > 0) {
      badge.textContent = candidates.length;
      badge.style.display = 'inline';
    } else {
      badge.style.display = 'none';
    }

    // Stats bar
    document.getElementById('wl-count').textContent = candidates.length || '0';
    if (candidates.length > 0) {
      const avgScore = candidates.reduce((s, c) => s + (c.score || 0), 0) / candidates.length;
      const avgProx  = candidates.reduce((s, c) => s + (c.proximity_20d || 0), 0) / candidates.length;
      document.getElementById('wl-avg-score').textContent    = avgScore.toFixed(1);
      document.getElementById('wl-avg-proximity').textContent = avgProx.toFixed(1) + '%';
    } else {
      document.getElementById('wl-avg-score').textContent    = '–';
      document.getElementById('wl-avg-proximity').textContent = '–';
    }

    const empty = document.getElementById('watchlist-empty');
    const wrap  = document.getElementById('watchlist-table-wrap');
    const tbody = document.getElementById('watchlist-tbody');

    if (candidates.length === 0) {
      empty.style.display = '';
      wrap.style.display  = 'none';
      return;
    }
    empty.style.display = 'none';
    wrap.style.display  = '';

    tbody.innerHTML = candidates.map(c => {
      const proxColor = (c.proximity_20d || 0) > -1.5 ? 'var(--green)' : 'inherit';
      return `
        <tr>
          <td><strong>${c.symbol}</strong></td>
          <td>${(c.score || 0).toFixed(0)}</td>
          <td>$${(c.current_price || 0).toFixed(2)}</td>
          <td style="color:${proxColor}">${(c.proximity_20d || 0).toFixed(1)}%</td>
          <td>${(c.vcp_contractions || 0) > 0 ? '&#10003;' : '–'}</td>
          <td>${c.consolidation ? '&#10003;' : '–'}</td>
          <td>${c.higher_lows   ? '&#10003;' : '–'}</td>
          <td>${(c.rs_vs_spy || 0).toFixed(1)}%</td>
          <td>$${(c.stop || 0).toFixed(2)}</td>
          <td>$${(c.target || 0).toFixed(2)}</td>
          <td>${c.shares || 0}</td>
          <td>$${(c.dollar_risk || 0).toFixed(0)}</td>
          <td>${(c.risk_reward || 0).toFixed(1)}x</td>
        </tr>`;
    }).join('');
  } catch(e) { console.error('loadWatchlist:', e); }
}

// ── History ────────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const [histRes, closedRes] = await Promise.all([
      fetch('/api/history?days=30'),
      fetch('/api/closed-trades?days=30'),
    ]);
    const rows   = await histRes.json();
    const closed = await closedRes.json();
    renderHistory(rows);
    renderClosedTrades(closed.trades || [], 'closed-history-body', true);
  } catch(e) { console.error(e); }
}

function renderHistory(rows) {
  const labels   = rows.map(r => r.date).reverse();
  const candData = rows.map(r => r.candidates_found || 0).reverse();
  const tradData = rows.map(r => r.trades_placed    || 0).reverse();
  if (historyChart) historyChart.destroy();
  const ctx = document.getElementById('history-chart').getContext('2d');
  historyChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label:'Candidates', data:candData, backgroundColor:'rgba(6,182,212,.35)', borderColor:'rgba(6,182,212,.8)', borderWidth:1, borderRadius:3 },
        { label:'Trades',     data:tradData, backgroundColor:'rgba(99,102,241,.5)',  borderColor:'rgba(99,102,241,.9)', borderWidth:1, borderRadius:3 },
      ],
    },
    options: {
      responsive:true, maintainAspectRatio:false,
      plugins: {
        legend:  { labels:{ color:'#94a3b8', boxWidth:12, font:{size:11} } },
        tooltip: { backgroundColor:'#1a2236', titleColor:'#e2e8f0', bodyColor:'#94a3b8' },
      },
      scales: {
        x: { ticks:{color:'#64748b',font:{size:10}}, grid:{color:'#1f2d45'} },
        y: { ticks:{color:'#64748b',font:{size:10}}, grid:{color:'#1f2d45'}, beginAtZero:true },
      },
    },
  });
  const regimePill = r => {
    if (!r) return '<span class="muted">—</span>';
    const map = { BULL_TREND:'bull-pill', BEAR_TREND:'bear-pill', SIDEWAYS:'side-pill', HIGH_VOLATILITY:'side-pill' };
    const lbl = { BULL_TREND:'BULL', BEAR_TREND:'BEAR', SIDEWAYS:'SIDE', HIGH_VOLATILITY:'HVOL' };
    return `<span class="regime-pill ${map[r]||'side-pill'}">${lbl[r]||r}</span>`;
  };
  const hb = document.getElementById('history-body');
  if (!rows || rows.length === 0) {
    hb.innerHTML = '<tr><td colspan="7" class="empty muted">No history yet — run the scanner to populate data</td></tr>';
    return;
  }
  hb.innerHTML = rows.map(r => `
    <tr style="cursor:pointer" data-date="${esc(r.date)}" title="Click to show candidates">
      <td>${r.date} <span style="color:var(--muted);font-size:10px;" id="hist-arrow-${r.date}">&#9654;</span></td>
      <td>${regimePill(r.regime)}</td>
      <td class="num">${r.adx ? fmt(r.adx,1) : '—'}</td>
      <td class="num">${r.symbols_scanned || '—'}</td>
      <td class="num ${(r.candidates_found||0) > 0 ? 'cyan' : 'muted'}">${r.candidates_found || 0}</td>
      <td class="num ${(r.trades_placed||0) > 0 ? 'green' : 'muted'}">${r.trades_placed || 0}</td>
      <td class="num">${r.score_multiplier ? fmt(r.score_multiplier,2)+'x' : '—'}</td>
    </tr>
    <tr id="hist-detail-${r.date}" style="display:none">
      <td colspan="7" style="padding:0;background:var(--surface2)">
        <div id="hist-detail-inner-${r.date}" style="padding:10px 14px;"></div>
      </td>
    </tr>`).join('');
}

const _histOpen = {};
async function toggleHistoryDetail(date, srcRow) {
  const detailRow  = document.getElementById(`hist-detail-${date}`);
  const inner      = document.getElementById(`hist-detail-inner-${date}`);
  const arrow      = document.getElementById(`hist-arrow-${date}`);
  const isOpen     = _histOpen[date];

  if (isOpen) {
    detailRow.style.display = 'none';
    arrow.innerHTML = '&#9654;';
    _histOpen[date] = false;
    return;
  }

  detailRow.style.display = '';
  arrow.innerHTML = '&#9660;';
  _histOpen[date] = true;
  inner.innerHTML = '<span class="muted" style="font-size:12px;"><span class="spin"></span> Loading…</span>';

  try {
    const res  = await fetch(`/api/dashboard?date=${date}`);
    const data = await res.json();
    const cands = data.candidates || [];
    if (!cands.length) {
      inner.innerHTML = '<span class="muted" style="font-size:12px;">No candidates for this date.</span>';
      return;
    }
    const scanLabel = (data.scan_count||1) > 1
      ? `${cands.length} unique candidate${cands.length>1?'s':''} across ${data.scan_count} scans`
      : `${cands.length} candidate${cands.length>1?'s':''} · 1 scan run`;
    const tableId = `hist-cands-${date}`;
    inner.innerHTML = `
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">${scanLabel}</div>
      <div style="overflow-x:auto">
      <table id="${tableId}" style="font-size:12px;width:100%">
        <thead>
          <tr>
            <th>#</th><th>Symbol</th><th>Score</th>
            <th class="num">Entry</th><th class="num">Now&nbsp;$</th>
            <th class="num">Stop</th><th class="num">Target</th>
            <th class="num">R:R</th><th class="num">$&nbsp;Risk</th>
            <th class="num">RSI</th><th class="num">RS/SPY</th><th class="num">Vol</th><th>Trap</th>
          </tr>
        </thead>
        <tbody>
          ${cands.map((c,i) => `
            <tr>
              <td class="muted">${i+1}</td>
              <td><strong>${c.symbol}</strong></td>
              <td>
                <div class="score-wrap">
                  <span>${fmt(c.score,0)}</span>
                  <div class="score-bar"><div class="score-fill" style="width:${c.score}%"></div></div>
                </div>
              </td>
              <td class="num mono">$${fmt(c.entry)}</td>
              <td class="num mono" id="now-${tableId}-${c.symbol}"><span class="muted" style="font-size:10px;">…</span></td>
              <td class="num mono red">$${fmt(c.stop)}</td>
              <td class="num mono green">$${fmt(c.target)}</td>
              <td class="num">${fmt(c.risk_reward,1)}x</td>
              <td class="num">${fmtDollar(c.dollar_risk)}</td>
              <td class="num ${c.rsi>70?'yellow':''}">${fmt(c.rsi,0)}</td>
              <td class="num ${c.rs_vs_spy>0?'green':'red'}">${c.rs_vs_spy>0?'+':''}${fmt(c.rs_vs_spy,1)}%</td>
              <td class="num ${c.volume_ratio>=1.5?'green':''}">${fmt(c.volume_ratio,1)}x</td>
              <td>${c.is_trap?'<span class="trap-badge">TRAP</span>':'<span class="muted">—</span>'}</td>
            </tr>`).join('')}
        </tbody>
      </table></div>`;

    // Fetch live prices and fill in the Now $ cells
    const syms = cands.map(c => c.symbol).join(',');
    fetch(`/api/prices?symbols=${syms}`)
      .then(r => r.json())
      .then(prices => {
        cands.forEach(c => {
          const cell = document.getElementById(`now-${tableId}-${c.symbol}`);
          if (!cell) return;
          const price = prices[c.symbol];
          if (price == null) { cell.innerHTML = '<span class="muted">—</span>'; return; }
          const pct  = c.entry ? ((price - c.entry) / c.entry * 100) : null;
          const cls  = pct == null ? '' : pct >= 0 ? 'green' : 'red';
          const sign = pct == null ? '' : pct >= 0 ? '▲' : '▼';
          cell.innerHTML = `<span class="${cls}">$${fmt(price)}</span>`
            + (pct != null ? `<br><span style="font-size:10px;" class="${cls}">${sign}${Math.abs(pct).toFixed(1)}%</span>` : '');
        });
      })
      .catch(() => {
        cands.forEach(c => {
          const cell = document.getElementById(`now-${tableId}-${c.symbol}`);
          if (cell) cell.innerHTML = '<span class="muted">—</span>';
        });
      });
  } catch(e) {
    inner.innerHTML = '<span class="muted" style="font-size:12px;">Failed to load candidates.</span>';
  }
}

function jumpToDate(d) {
  switchTab('today', document.getElementById('tab-btn-today'));
  loadDashboard(d);
}

// ── Live Trades tab ────────────────────────────────────────────────────────
let _perfDays = 90;

async function loadLiveTrades() {
  document.getElementById('live-trades-body').innerHTML =
    '<tr><td colspan="12" class="empty"><span class="spin"></span></td></tr>';
  try {
    const [liveRes, todayRes, perfRes, acctRes, sellsRes] = await Promise.all([
      fetch('/api/live-trades'),
      fetch('/api/dashboard?date=' + todayStr()),
      fetch('/api/performance?days=90'),
      fetch('/api/account'),
      fetch('/api/recent-sells'),
    ]);
    const live  = await liveRes.json();
    const today = await todayRes.json();
    const perf  = await perfRes.json();
    const acct  = await acctRes.json();
    const sells = await sellsRes.json();
    renderLiveTrades(live.trades || []);
    renderTradesToday(today.trades || []);
    renderPortfolio(acct, perf);
    renderRecentSells(sells.sells || []);
    // update badge on tab button
    const n = (live.trades || []).length;
    const badge = document.getElementById('open-trades-badge');
    badge.textContent = n;
    badge.style.display = n > 0 ? '' : 'none';
  } catch(e) {
    document.getElementById('live-trades-body').innerHTML =
      '<tr><td colspan="12" class="empty muted">Error loading trades</td></tr>';
  }
}

async function loadPerf(days) {
  _perfDays = days;
  // update period button styles
  [30, 90, 365].forEach(d => {
    const btn = document.getElementById(d === 365 ? 'perf-btn-all' : `perf-btn-${d}`);
    if (btn) btn.classList.toggle('active', d === days);
  });
  try {
    const perf = await fetch('/api/performance?days=' + days).then(r => r.json());
    renderPerf(perf);
  } catch(e) {}
}

function statusBadge(s) {
  const map = {
    open:         ['var(--cyan)',   'OPEN'],
    partial_exit: ['var(--green)',  'PARTIAL'],
    closed:       ['var(--muted)',  'CLOSED'],
    fill_timeout: ['var(--yellow)', 'TIMEOUT'],
  };
  const [color, label] = map[s] || ['var(--muted)', s || '—'];
  return `<span style="font-size:10px;font-weight:700;color:${color};letter-spacing:.04em;">${label}</span>`;
}

function rColor(r) {
  if (r == null) return '';
  if (r >= 1)    return 'green';
  if (r >= 0)    return '';
  return 'red';
}

function renderLiveTrades(trades) {
  const tb   = document.getElementById('live-trades-body');
  const foot = document.getElementById('live-trades-foot');
  if (!trades.length) {
    tb.innerHTML = '<tr><td colspan="12" class="empty muted">No open positions</td></tr>';
    foot.style.display = 'none';
    return;
  }

  // Summary footer
  const totalPL  = trades.reduce((s, t) => s + (t.unrealized_pl || 0), 0);
  const validR   = trades.filter(t => t.unrealized_r != null);
  const avgR     = validR.length ? validR.reduce((s, t) => s + t.unrealized_r, 0) / validR.length : null;
  const plCls    = totalPL > 0 ? 'green' : totalPL < 0 ? 'red' : '';
  document.getElementById('lts-count').textContent = `${trades.length} position${trades.length > 1 ? 's' : ''}`;
  document.getElementById('lts-pl').innerHTML =
    `<span class="${plCls}">${totalPL >= 0 ? '+' : ''}$${fmt(Math.abs(totalPL))} total P&amp;L</span>`;
  document.getElementById('lts-r').innerHTML = avgR != null
    ? `<span class="${rColor(avgR)}">${avgR >= 0 ? '+' : ''}${fmt(avgR, 2)}R avg</span>` : '—';
  foot.style.display = '';
  tb.innerHTML = trades.map(t => {
    const pl    = t.unrealized_pl;
    const plCls = pl > 0 ? 'green' : pl < 0 ? 'red' : '';
    const rVal  = t.unrealized_r;
    const rStr  = rVal != null ? (rVal >= 0 ? '+' : '') + fmt(rVal, 2) + 'R' : '—';
    const noDb  = !t.in_db ? ' title="Position not in DB — placed outside runner"' : '';
    return `<tr${noDb}>
      <td><strong>${t.symbol}</strong>${!t.in_db ? ' <span style="color:var(--yellow);font-size:10px;">⚠</span>' : ''}</td>
      <td class="num">${t.qty}</td>
      <td class="num mono">$${fmt(t.fill_price)}</td>
      <td class="num mono">$${fmt(t.current_price)}</td>
      <td class="num ${plCls}">${pl >= 0 ? '+' : ''}$${fmt(Math.abs(pl))} (${pl >= 0 ? '+' : ''}${fmt(t.unrealized_plpc, 1)}%)</td>
      <td class="num ${rColor(rVal)}" style="font-weight:700">${rStr}</td>
      <td class="num mono red">${t.stop_loss ? '$' + fmt(t.stop_loss) : '—'}</td>
      <td class="num ${t.stop_dist_pct != null && t.stop_dist_pct < 5 ? 'yellow' : 'muted'}">${t.stop_dist_pct != null ? fmt(t.stop_dist_pct, 1) + '%' : '—'}</td>
      <td class="num mono green">${t.partial_target ? '$' + fmt(t.partial_target) : '—'}</td>
      <td class="num muted">${t.target_dist_pct != null ? '+' + fmt(t.target_dist_pct, 1) + '%' : '—'}</td>
      <td class="num muted">${t.days_held}d</td>
      <td>${statusBadge(t.status)}</td>
      <td><button class="btn-sm btn-danger close-pos-btn" data-symbol="${esc(t.symbol)}" data-order-id="${esc(t.buy_order_id||'')}">Close</button></td>
    </tr>`;
  }).join('');
}

function renderTradesToday(trades) {
  const tb = document.getElementById('trades-today-body');
  if (!trades.length) {
    tb.innerHTML = '<tr><td colspan="8" class="empty muted">No trades placed today</td></tr>';
    return;
  }
  tb.innerHTML = trades.map(t => `
    <tr>
      <td class="muted">${fmtTime(t.ts)}</td>
      <td><strong>${t.symbol}</strong></td>
      <td class="num">${t.shares}</td>
      <td class="num mono">${t.fill_price ? '$' + fmt(t.fill_price) : '<span class="muted">$' + fmt(t.entry) + '</span>'}</td>
      <td class="num mono red">$${fmt(t.stop_loss)}</td>
      <td class="num mono green">${t.partial_target ? '$' + fmt(t.partial_target) : '—'}</td>
      <td class="num">${fmt(t.score, 0)}</td>
      <td>${statusBadge(t.status)}</td>
    </tr>`).join('');
}

function renderClosedTrades(trades, tbodyId, showDate) {
  const tb = document.getElementById(tbodyId);
  if (!tb) return;
  const cols = showDate ? 8 : 7;
  if (!trades || !trades.length) {
    tb.innerHTML = `<tr><td colspan="${cols}" class="empty muted">No closed trades</td></tr>`;
    return;
  }
  const EXIT_LABELS = {
    trailing_stop: 'Trail Stop', stop_loss: 'Stop Loss', manual_close: 'Manual',
    pme_exit: 'PME Exit', partial_exit: 'Partial', unknown: 'Unknown',
  };
  tb.innerHTML = trades.map(t => {
    const pnl    = (t.exit_price && t.fill_price && t.shares)
                   ? (t.exit_price - t.fill_price) * t.shares : null;
    const pnlCls = pnl == null ? '' : pnl >= 0 ? 'green' : 'red';
    const pnlStr = pnl == null ? '—' : (pnl >= 0 ? '+' : '') + fmtDollar(Math.abs(pnl));
    const r      = t.actual_r;
    const rCls   = r == null ? '' : r >= 0 ? 'green' : 'red';
    const rStr   = r == null ? '—' : (r >= 0 ? '+' : '') + fmt(r, 2) + 'R';
    const reason = EXIT_LABELS[t.exit_reason] || (t.exit_reason || '—');
    const dateCol = showDate ? `<td class="muted">${(t.exit_ts||'').slice(0,10)}</td>` : '';
    return `<tr>
      ${dateCol}
      <td><strong>${t.symbol}</strong></td>
      <td class="num mono">$${fmt(t.fill_price)}</td>
      <td class="num mono">$${fmt(t.exit_price)}</td>
      <td class="num ${pnlCls}" style="font-weight:600;">${pnlStr}</td>
      <td class="num ${rCls}">${rStr}</td>
      <td class="num muted">${t.hold_days ?? '—'}d</td>
      <td class="muted">${reason}</td>
    </tr>`;
  }).join('');
}

function renderRecentSells(sells) {
  const tb = document.getElementById('recent-sells-body');
  if (!tb) return;
  if (!sells || !sells.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty muted">No sell orders found</td></tr>';
    return;
  }
  tb.innerHTML = sells.map(s => {
    const pnlCls = s.pnl == null ? '' : s.pnl >= 0 ? 'green' : 'red';
    const pnlStr = s.pnl == null ? '—' : (s.pnl >= 0 ? '+' : '') + fmtDollar(Math.abs(s.pnl));
    const dateStr = s.date || (s.ts ? s.ts.slice(0, 10) : '—');
    return `<tr>
      <td class="muted">${dateStr}</td>
      <td><strong>${s.symbol}</strong></td>
      <td class="num">${s.qty % 1 === 0 ? s.qty : fmt(s.qty, 2)}</td>
      <td class="num mono muted">${s.avg_cost ? '$' + fmt(s.avg_cost) : '—'}</td>
      <td class="num mono">$${fmt(s.sell_price)}</td>
      <td class="num ${pnlCls}" style="font-weight:600;">${pnlStr}</td>
    </tr>`;
  }).join('');
}

function renderPortfolio(acct, perf) {
  const modeEl = document.getElementById('portfolio-mode-label');
  const gridEl = document.getElementById('portfolio-kpi-grid');
  if (!acct || acct.error) {
    if (modeEl) modeEl.textContent = acct && acct.error ? 'Alpaca error' : 'unavailable';
    if (gridEl) gridEl.innerHTML = `<div class="kpi"><div class="kpi-label">Status</div><div class="kpi-value muted" style="font-size:13px;">${esc(acct && acct.error ? acct.error : 'Could not load')}</div></div>`;
    return;
  }
  if (modeEl) modeEl.textContent = (acct.is_paper ? 'paper account' : 'LIVE account');
  const todayPnlCls = acct.today_pnl >= 0 ? 'green' : 'red';
  const unrCls      = acct.unrealized_pl >= 0 ? 'green' : 'red';
  const realCls     = (acct.realized_pnl || 0) >= 0 ? 'green' : 'red';
  const sign = v => v >= 0 ? '+' : '';

  // Win rate from perf data
  let winRateHtml = '';
  if (perf && perf.total > 0) {
    const wrCls = (perf.win_rate || 0) >= 50 ? 'green' : 'red';
    winRateHtml = `
    <div class="kpi">
      <div class="kpi-label">Win Rate</div>
      <div class="kpi-value ${wrCls}">${fmt(perf.win_rate, 1)}%</div>
      <div class="kpi-sub">${perf.wins}W / ${perf.losses}L</div>
    </div>`;
  }

  if (gridEl) gridEl.innerHTML = `
    <div class="kpi highlight">
      <div class="kpi-label">Equity</div>
      <div class="kpi-value cyan">${fmtDollar(acct.equity)}</div>
      <div class="kpi-sub">portfolio value</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Cash</div>
      <div class="kpi-value">${fmtDollar(acct.cash)}</div>
      <div class="kpi-sub">available</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Unrealized P&amp;L</div>
      <div class="kpi-value ${unrCls}">${sign(acct.unrealized_pl)}${fmtDollar(acct.unrealized_pl)}</div>
      <div class="kpi-sub">${acct.open_positions_count} open position${acct.open_positions_count !== 1 ? 's' : ''}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Realized P&amp;L</div>
      <div class="kpi-value ${realCls}">${sign(acct.realized_pnl)}${fmtDollar(acct.realized_pnl)}</div>
      <div class="kpi-sub">all closed trades</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Open Exposure</div>
      <div class="kpi-value">${fmtDollar(acct.open_exposure)}</div>
      <div class="kpi-sub">${fmt(acct.open_exposure_pct,1)}% of equity</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Buying Power</div>
      <div class="kpi-value">${fmtDollar(acct.buying_power)}</div>
      <div class="kpi-sub">&nbsp;</div>
    </div>
    ${winRateHtml}`;
}

function renderPerf(p) {
  const el = document.getElementById('perf-card-body');
  const daysLabel = _perfDays >= 365 ? 'all time' : `last ${_perfDays} days`;
  if (!p || p.total === 0) {
    el.innerHTML = `<div style="padding:16px;"><span class="muted">No closed trades (${daysLabel}).</span></div>`;
    return;
  }

  const wr   = p.win_rate      != null ? fmt(p.win_rate, 1) + '%'      : '—';
  const hold = p.avg_hold_days != null ? fmt(p.avg_hold_days, 1) + 'd' : '—';
  const pnl  = p.total_pnl_dollars;
  const pnlCls = pnl != null ? (pnl >= 0 ? 'green' : 'red') : '';
  const pnlStr = pnl != null ? (pnl >= 0 ? '+' : '') + fmtDollar(Math.abs(pnl)) : '—';

  // Exit breakdown bars
  const reasons = p.by_exit_reason || {};
  const total   = p.total || 1;
  const maxCount = Math.max(...Object.values(reasons).map(v => v.count || 0), 1);
  const EXIT_STYLES = {
    'target':       { color: '#22c55e', label: 'Target hit' },
    'target_hit':   { color: '#22c55e', label: 'Target hit' },
    'trailing':     { color: '#3b82f6', label: 'Trailing stop' },
    'trailing_stop':{ color: '#3b82f6', label: 'Trailing stop' },
    'stop':         { color: '#ef4444', label: 'Stop loss' },
    'stop_loss':    { color: '#ef4444', label: 'Stop loss' },
    'timeout':      { color: '#eab308', label: 'Timeout' },
  };
  const barRows = Object.entries(reasons).map(([reason, v]) => {
    const style  = EXIT_STYLES[reason] || { color: '#64748b', label: reason };
    const pct    = Math.round(v.count / maxCount * 100);
    const avgR   = v.avg_r != null ? v.avg_r : null;
    const rCls   = avgR != null ? (avgR >= 0 ? 'green' : 'red') : 'muted';
    const rStr   = avgR != null ? (avgR >= 0 ? '+' : '') + fmt(avgR, 1) + 'R avg' : '—';
    return `<div class="bar-row">
      <div class="bar-label">${style.label}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%;background:${style.color};"></div></div>
      <div class="bar-count">${v.count}</div>
      <div class="bar-r ${rCls}">${rStr}</div>
    </div>`;
  }).join('');

  // Recent trades table
  const recentRows = p.recent && p.recent.length ? [...p.recent].reverse().map(r => {
    const plVal = r.fill_price && r.exit_price && r.shares
      ? (r.exit_price - r.fill_price) * r.shares : null;
    const plCls = r.actual_r >= 0 ? 'green' : 'red';
    const plStr = plVal != null
      ? `<span class="${plCls}">${plVal >= 0 ? '+' : ''}${fmtDollar(Math.abs(plVal))}</span>`
      : '—';
    return `<tr>
      <td><strong>${r.symbol}</strong></td>
      <td class="muted">${r.exit_date || ''}</td>
      <td class="num mono">$${fmt(r.fill_price)}</td>
      <td class="num mono">$${fmt(r.exit_price)}</td>
      <td class="num">${plStr}</td>
      <td class="num ${plCls}" style="font-weight:700;">${r.actual_r>=0?'+':''}${fmt(r.actual_r,2)}R</td>
      <td class="num muted">${r.hold_days||0}d</td>
      <td class="muted">${r.exit_reason||'—'}</td>
    </tr>`;
  }).join('') : '';

  el.innerHTML = `
    <div class="section-label">Realized · ${daysLabel} · ${p.total} closed trade${p.total !== 1 ? 's' : ''}</div>
    <div class="kpi-grid" style="padding-top:0;">
      <div class="kpi highlight">
        <div class="kpi-label">Total P&amp;L</div>
        <div class="kpi-value ${pnlCls}">${pnlStr}</div>
        <div class="kpi-sub">&nbsp;</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Win Rate</div>
        <div class="kpi-value ${p.win_rate >= 50 ? 'green' : 'red'}">${wr}</div>
        <div class="kpi-sub">${p.wins}W / ${p.losses}L</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Avg Hold</div>
        <div class="kpi-value">${hold}</div>
        <div class="kpi-sub">days per trade</div>
      </div>
    </div>
    ${barRows ? `<hr class="perf-hr"><div class="section-label">Exit Breakdown</div>${barRows}` : ''}
    ${recentRows ? `
    <hr class="perf-hr">
    <div class="section-label">Recent Closed Trades</div>
    <div class="table-scroll" style="padding:0 0 8px;">
      <table style="font-size:12px;">
        <thead><tr>
          <th>Symbol</th><th>Date</th>
          <th class="num">Fill $</th><th class="num">Exit $</th>
          <th class="num">P&amp;L $</th><th class="num">R</th>
          <th class="num">Hold</th><th>Reason</th>
        </tr></thead>
        <tbody>${recentRows}</tbody>
      </table>
    </div>` : ''}`;
}

// ── Close position ─────────────────────────────────────────────────────────
async function closePosition(symbol, buyOrderId, btn) {
  if (!confirm(`Close ${symbol}?\n\nThis will cancel all open orders for ${symbol} and place a market sell for the full position.`)) return;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const res  = await fetch('/api/positions/close', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol, buy_order_id: buyOrderId}),
    });
    const data = await res.json();
    if (data.ok) {
      showToast(`${symbol} closed — ${data.qty} sh sold, ${data.cancelled_orders} order(s) cancelled`, 'success');
      setTimeout(loadLiveTrades, 1500);
    } else {
      showToast(`Close failed: ${data.error}`, 'error');
      btn.disabled = false;
      btn.textContent = 'Close';
    }
  } catch(e) {
    showToast('Request failed: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Close';
  }
}

// ── Controls ───────────────────────────────────────────────────────────────
function loadToday() { loadDashboard(todayStr()); }
function refresh() {
  loadDashboard(currentDate);
  if (document.getElementById('tab-history').classList.contains('active')) loadHistory();
  if (document.getElementById('tab-trades').classList.contains('active'))  loadLiveTrades();
  resetCountdown();
}
document.getElementById('date-picker').addEventListener('change', e => loadDashboard(e.target.value));

// ── Auto-refresh every 15 min with countdown ───────────────────────────────
function resetCountdown() {
  countdown = 15 * 60;
}
function tickCountdown() {
  countdown--;
  if (countdown <= 0) {
    refresh();
  } else {
    const m = Math.floor(countdown / 60);
    const s = String(countdown % 60).padStart(2, '0');
    // refresh-countdown removed from header
  }
}
setInterval(tickCountdown, 1000);
resetCountdown();

// ── Next scan countdown ────────────────────────────────────────────────────
let _nextScanTs     = null;
let _runnerActive   = false;
let _scanInProgress = false;
let _marketOpen     = null;   // null = unknown, true/false from API

async function fetchNextScan() {
  try {
    const d = await fetch('/api/scan/next').then(r => r.json());
    _nextScanTs     = d.next_scan_ts ? new Date(d.next_scan_ts) : null;
    _runnerActive   = !!d.runner_running;
    _scanInProgress = !!d.scan_running;
    if (d.market_open !== undefined) _marketOpen = d.market_open;

    // ── Market status pill
    const mktPill = document.getElementById('h-mkt-pill');
    const mktTime = document.getElementById('h-mkt-time');
    if (d.market_open) {
      mktPill.className = 'h-pill h-pill-open';
      mktPill.textContent = 'MKT OPEN';
      mktTime.textContent = 'Closes 16:00 ET';
    } else {
      mktPill.className = 'h-pill h-pill-closed';
      mktPill.textContent = 'MKT CLOSED';
      if (d.next_open) {
        const t = new Date(d.next_open).toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', timeZone:'America/New_York'});
        mktTime.textContent = `Opens ${t} ET`;
      } else {
        mktTime.textContent = '';
      }
    }

    // ── Scanning pill
    const scanPill = document.getElementById('h-scan-pill');
    scanPill.style.display = d.scan_running ? '' : 'none';

    // ── Last scan text
    if (d.last_scan_ts) {
      const ls = new Date(d.last_scan_ts);
      const lsStr = ls.toLocaleString('en-US', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', timeZone:'America/New_York'}).replace(',','');
      document.getElementById('h-last-scan').textContent = `Last scan: ${lsStr} ET`;
    }

    // ── Scan Now / View Scan button state
    const scanBtn = document.getElementById('h-scan-now-btn');
    if (scanBtn) {
      const scanning = d.scan_running || _scanPollTimer !== null;
      scanBtn.textContent = scanning ? '● View Scan' : '▶ Scan Now';
      scanBtn.disabled    = false;  // always clickable — reopens panel if scanning
    }
  } catch(e) {}
}

function tickScanCountdown() {
  const el = document.getElementById('scan-countdown');
  el.style.display = 'none';   // hidden — status shown via h-scan-pill / h-last-scan

  if (_scanInProgress) {
    el.textContent = 'Scanning…';
    el.className = 'scan-next-info imminent';
    el.style.color = '';
    return;
  }

  // Market status unknown (initial load) — show placeholder
  if (_marketOpen === null) {
    el.textContent = '—';
    el.className = 'scan-next-info';
    el.style.color = 'var(--muted)';
    return;
  }

  if (_marketOpen === false) {
    el.textContent = 'Market closed';
    el.className = 'scan-next-info';
    el.style.color = 'var(--muted)';
    return;
  }

  // Market is open
  if (!_runnerActive) {
    el.textContent = 'Market open';
    el.className = 'scan-next-info imminent';
    el.style.color = '';
    return;
  }

  // Runner active — show countdown
  if (!_nextScanTs) {
    el.textContent = 'Next scan …';
    el.className = 'scan-next-info';
    el.style.color = '';
    return;
  }

  const secsLeft = Math.round((_nextScanTs - Date.now()) / 1000);
  if (secsLeft <= 0) {
    el.textContent = 'Scan due…';
    el.className = 'scan-next-info imminent';
    el.style.color = '';
    fetchNextScan();
    return;
  }
  const m = Math.floor(secsLeft / 60);
  const s = String(secsLeft % 60).padStart(2, '0');
  el.textContent = `Next scan ${m}:${s}`;
  el.className = secsLeft < 60 ? 'scan-next-info imminent' : 'scan-next-info';
  el.style.color = '';
}

setInterval(tickScanCountdown, 1000);
setInterval(fetchNextScan, 30_000);  // re-sync every 30s
fetchNextScan();

// ── Runner daemon status ────────────────────────────────────────────────────

async function updateRunnerStatus() {
  try {
    const r = await fetch('/api/runner/status');
    const d = await r.json();
    setRunnerUI(d.running);
    if (typeof d.is_paper !== 'undefined') {
      _isLiveAccount = d.is_paper === false || d.is_paper === 'false';
      applyTradingModeBanner();
    }
  } catch {}
}

function setRunnerUI(running) {
  // Today tab: dot shows runner state; label always stays "Scan Now"
  const dot = document.getElementById('runner-dot');
  if (running) {
    dot.classList.add('on');
  } else {
    dot.classList.remove('on');
  }
  // Header pills & action buttons
  const hRunnerPill = document.getElementById('h-runner-pill');
  const hStopBtn    = document.getElementById('h-stop-runner-btn');
  if (running) {
    hRunnerPill.className   = 'h-pill h-pill-on';
    hRunnerPill.textContent = 'RUNNER ON';
    if (hStopBtn) hStopBtn.style.display = '';
  } else {
    hRunnerPill.className   = 'h-pill h-pill-off';
    hRunnerPill.textContent = 'RUNNER OFF';
    if (hStopBtn) hStopBtn.style.display = 'none';
  }
  // Legacy hidden elements (kept for compat)
  const hDot   = document.getElementById('header-runner-dot');
  const hLabel = document.getElementById('header-runner-label');
  if (hDot)   running ? hDot.classList.add('on') : hDot.classList.remove('on');
  if (hLabel) hLabel.textContent = running ? 'Runner ON' : 'Runner OFF';
}

// Open scan panel; start a new scan only if one isn't already running
async function doScan() {
  openScanPanel();
  if (_scanPollTimer !== null) {
    // Scan in progress — just show the panel, don't restart
    return;
  }
  startScan();
}

// Stops the runner daemon
async function doStopRunner() {
  const btn = document.getElementById('h-stop-runner-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/runner/stop', { method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':_getCsrfToken()}, body:'{}' });
    const d = await r.json();
    setRunnerUI(d.running);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Legacy alias kept so any stray references don't crash
async function onScanNowClick() { await doScan(); }

updateRunnerStatus();
setInterval(updateRunnerStatus, 10000);

// ── Scan progress panel ─────────────────────────────────────────────────────

let _scanPollTimer  = null;
let _scanOffset     = 0;
let _scanExecute    = false;
let _isLiveAccount  = false;  // true when IS_PAPER=false in .env

async function fetchTradingMode() {
  try {
    const r = await fetch('/api/trading-mode');
    const d = await r.json();
    _isLiveAccount = d.is_paper === false || d.is_paper === 'false';
    applyTradingModeBanner();
  } catch {}
}

function applyTradingModeBanner() {
  const banner = document.getElementById('live-trading-banner');
  if (banner) banner.style.display = _isLiveAccount ? 'block' : 'none';
}

function toggleExecuteMode() {
  _scanExecute = !_scanExecute;
  const el = document.getElementById('execute-toggle');
  if (_scanExecute) {
    const msg = _isLiveAccount
      ? 'Switch to EXECUTE mode?\n\nWARNING: IS_PAPER is OFF — Scan Now will place REAL MONEY orders on your live Alpaca account.'
      : 'Switch to EXECUTE mode? Scan Now will place orders on your Alpaca paper account.';
    if (!confirm(msg)) {
      _scanExecute = false;
      return;
    }
    el.textContent = _isLiveAccount ? '⚠ REAL MONEY' : 'EXECUTE';
    el.classList.add('live');
  } else {
    el.textContent = 'DRY RUN';
    el.classList.remove('live');
  }
}

function openScanPanel() {
  document.getElementById('scan-overlay').classList.add('open');
  document.getElementById('scan-panel').classList.add('open');
}

function closeScanPanel() {
  document.getElementById('scan-overlay').classList.remove('open');
  document.getElementById('scan-panel').classList.remove('open');
}

function clearScanLog() {
  document.getElementById('scan-log').innerHTML = '';
  document.getElementById('scan-summary').textContent = '';
}

function _setScanBtnState(scanning) {
  const lbl = document.getElementById('runner-btn-label');
  if (lbl) lbl.textContent = scanning ? '● Scanning…' : '▶ Scan Now';
}

async function startScan() {
  clearScanLog();
  _scanOffset = 0;
  _setScanBtnState(true);
  document.getElementById('scan-status-label').textContent = 'Running…';
  document.getElementById('scan-status-label').style.color = 'var(--cyan)';
  document.getElementById('scan-again-btn').style.display  = 'none';
  const modeLabel = _scanExecute
    ? (_isLiveAccount ? '⚠ REAL MONEY — orders placed on live Alpaca account' : '⚡ EXECUTE — paper orders will be placed')
    : 'Dry run — no orders';
  const modeCls   = _scanExecute ? 'error' : 'mute';
  appendScanLine(modeLabel, modeCls);
  appendScanLine('Starting scanner…', 'mute');
  try {
    await fetch('/api/scan/start', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({execute: _scanExecute}) });
  } catch(e) {
    appendScanLine('Failed to start scan: ' + e.message, 'error');
    return;
  }
  if (_scanPollTimer) clearInterval(_scanPollTimer);
  _scanPollTimer = setInterval(pollScanOutput, 800);
}

async function pollScanOutput() {
  try {
    const r = await fetch('/api/scan/output?offset=' + _scanOffset);
    const d = await r.json();
    d.lines.forEach(line => appendScanLine(line, classifyLine(line)));
    _scanOffset = d.offset;
    if (!d.running) {
      clearInterval(_scanPollTimer);
      _scanPollTimer = null;
      finaliseScan();
    }
  } catch {}
}

function classifyLine(line) {
  const l = line.toLowerCase();
  if (/error|exception|traceback|failed/.test(l))  return 'error';
  if (/warn|warning|skip|no data/.test(l))          return 'warn';
  if (/candidate|score|buy|trade|placed/.test(l))   return 'good';
  if (/^\s*$/.test(line))                            return 'mute';
  return 'info';
}

function appendScanLine(text, cls) {
  const log  = document.getElementById('scan-log');
  const line = document.createElement('div');
  line.className = 'scan-log-line ' + (cls || 'info');
  // Strip ANSI colour codes
  line.textContent = text.replace(/\x1b\[[0-9;]*m/g, '');
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function finaliseScan() {
  const log      = document.getElementById('scan-log');
  const lines    = log.querySelectorAll('.scan-log-line');
  const errors   = log.querySelectorAll('.scan-log-line.error').length;
  const warnings = log.querySelectorAll('.scan-log-line.warn').length;
  const trades   = [...lines].filter(l => l.classList.contains('good')).length;
  document.getElementById('scan-status-label').textContent = 'Done';
  document.getElementById('scan-status-label').style.color = 'var(--green)';
  document.getElementById('scan-again-btn').style.display  = '';
  const parts = [];
  if (trades)   parts.push(trades   + ' signal' + (trades   > 1 ? 's' : ''));
  if (warnings) parts.push(warnings + ' warning' + (warnings > 1 ? 's' : ''));
  if (errors)   parts.push(errors   + ' error'   + (errors   > 1 ? 's' : ''));
  document.getElementById('scan-summary').textContent = parts.join(' · ') || 'Completed';
  _setScanBtnState(false);
  // Refresh dashboard to show new candidates
  loadDashboard(todayStr());
}

// ── Config ─────────────────────────────────────────────────────────────────

async function loadConfig() {
  try {
    const [cfgRes, symRes] = await Promise.all([
      fetch('/api/config'),
      fetch('/api/symbols/fallback'),
    ]);
    const data = await cfgRes.json();
    const syms = await symRes.json();
    fallbackSymbols = syms.symbols || [];
    renderSymbolChips();
    updateSymbolBadge();
    populateConfig(data);
    document.getElementById('save-status').textContent = 'Loaded from .env — edit and save to apply changes.';
  } catch(e) {
    showToast('Failed to load config', 'error');
  }
}

function populateConfig(data) {
  // Simple text/number inputs
  const fields = [
    'BREAKOUT_MIN_PRICE','BREAKOUT_MIN_AVG_VOLUME','BREAKOUT_VOLUME_SURGE_MULT',
    'BREAKOUT_MIN_VOLUME_RATIO','BREAKOUT_RSI_LOW','BREAKOUT_RSI_HIGH','BREAKOUT_RSI_MAX',
    'EARNINGS_MIN_EPS_GROWTH','GAP_UP_THRESHOLD',
    'BREAKOUT_CONSOLIDATION_LOOKBACK','BREAKOUT_CONSOLIDATION_DAILY_VOL',
    'BREAKOUT_HIGHER_LOWS_LOOKBACK','BREAKOUT_MIN_SCORE',
    'BREAKOUT_ATR_STOP_MULT','BREAKOUT_MAX_STOP_PCT','BREAKOUT_SUPPORT_LOOKBACK','BREAKOUT_RR_RATIO',
    'PARTIAL_EXIT_R','PARTIAL_EXIT_PCT','TRAIL_ATR_MULT',
    'ACCUM_LOOKBACK_DAYS','BULL_TRAP_SCORE_THRESHOLD',
    'SCORE_VCP','SCORE_CONSOLIDATION','SCORE_HIGHER_LOWS',
    'SCORE_52W_HIGH_PROXIMITY','SCORE_EARNINGS_PROXIMITY',
    'SCORE_VOLUME_SURGE','SCORE_BREAKOUT_20D','SCORE_RSI_ZONE','SCORE_RELATIVE_STRENGTH',
    'SCORE_MARKET_BREADTH','SCORE_ACCUM_MAX_BONUS','SCORE_TRAP_MAX_PENALTY',
    'MAX_POSITION_SIZE','MAX_PORTFOLIO_RISK','MAX_CONCURRENT_TRADES','MAX_DAILY_LOSS_PCT',
    'BACKTEST_INITIAL_CAPITAL','BACKTEST_MAX_HOLD_DAYS','BACKTEST_SLIPPAGE_PCT',
    'ALPACA_BASE_URL',
    'WHATSAPP_PHONE','WHATSAPP_APIKEY',
    'PME_ADD_SCORE_THRESHOLD','PME_HOLD_SCORE_MIN','PME_TRIM_LIGHT_SCORE_MIN','PME_TRIM_HEAVY_SCORE_MIN',
    'PME_ADD_SIZE_PCT','PME_ADD_MAX_MULTIPLIER','PME_TRIM_LIGHT_PCT','PME_TRIM_HEAVY_PCT',
    'PME_RS_ADD_MIN_PCT','PME_RS_DOWNGRADE_BELOW_PCT','PME_R_TRIM_FLOOR','PME_R_TRIM_ENFORCE',
    'PME_FOLLOWTHROUGH_DAYS','PME_VOLUME_SELLOFF_MULT',
  ];
  fields.forEach(k => {
    const el = document.getElementById('cfg-' + k);
    if (el && data[k] !== undefined) el.value = data[k];
  });

  // Toggles (boolean)
  const boolFields = ['IS_PAPER', 'REGIME_AWARE_SCANNING', 'EARNINGS_FILTER_ENABLED'];
  boolFields.forEach(k => {
    const el = document.getElementById('cfg-' + k);
    if (el) el.checked = (data[k] === 'true' || data[k] === true);
  });
  document.getElementById('live-warning').style.display =
    (data.IS_PAPER === 'false' || data.IS_PAPER === false) ? 'block' : 'none';

  // Selects
  const selectFields = ['SCANNER_INTERVAL_MINUTES', 'REGIME_OVERRIDE'];
  selectFields.forEach(k => {
    const el = document.getElementById('cfg-' + k);
    if (el && data[k] !== undefined) el.value = data[k];
  });

  updateWeightsTotal();  // also calls updateThresholdBadges()
}

const _WEIGHT_FIELDS = [
  // Predictive
  'SCORE_VCP','SCORE_CONSOLIDATION','SCORE_HIGHER_LOWS',
  'SCORE_52W_HIGH_PROXIMITY','SCORE_EARNINGS_PROXIMITY',
  // Confirmation
  'SCORE_VOLUME_SURGE','SCORE_BREAKOUT_20D','SCORE_RSI_ZONE','SCORE_RELATIVE_STRENGTH',
  // Market health
  'SCORE_MARKET_BREADTH',
];

// Sync wt-badge labels in Scanner Thresholds to match live Signal Weights inputs
const _THRESHOLD_BADGE_MAP = {
  'SCORE_VOLUME_SURGE':  'wt-SCORE_VOLUME_SURGE',
  'SCORE_RSI_ZONE':      'wt-SCORE_RSI_ZONE',
  'SCORE_CONSOLIDATION': 'wt-SCORE_CONSOLIDATION',
  'SCORE_HIGHER_LOWS':   'wt-SCORE_HIGHER_LOWS',
};
function updateThresholdBadges() {
  for (const [scoreKey, badgeId] of Object.entries(_THRESHOLD_BADGE_MAP)) {
    const input = document.getElementById('cfg-' + scoreKey);
    const badge = document.getElementById(badgeId);
    if (!input || !badge) continue;
    const v = parseFloat(input.value);
    badge.textContent = isNaN(v) ? '— pts' : v + ' pts';
  }
}

function updateWeightsTotal() {
  // Sum with fixed-precision to avoid floating-point drift (e.g. 0.1+0.2≠0.3)
  updateThresholdBadges();
  const totalRaw = _WEIGHT_FIELDS.reduce((s, k) => {
    return s + (parseFloat(document.getElementById('cfg-' + k)?.value) || 0);
  }, 0);
  const total = Math.round(totalRaw * 100) / 100;  // round to 2 dp
  const badge = document.getElementById('weights-total-badge');
  if (!badge) return;
  const diff  = Math.round((total - 100) * 100) / 100;  // 2 dp diff
  if (diff === 0) {
    badge.style.color = 'var(--green)';
    badge.textContent = `Total: 100 ✓`;
  } else {
    badge.style.color = diff > 0 ? 'var(--red)' : 'var(--yellow)';
    badge.textContent = `Total: ${total} — ${diff > 0 ? '+' : ''}${diff} off (must = 100)`;
  }
}

function collectConfig() {
  const cfg = {};

  // Text/number inputs
  const fields = [
    'BREAKOUT_MIN_PRICE','BREAKOUT_MIN_AVG_VOLUME','BREAKOUT_VOLUME_SURGE_MULT',
    'BREAKOUT_MIN_VOLUME_RATIO','BREAKOUT_RSI_LOW','BREAKOUT_RSI_HIGH','BREAKOUT_RSI_MAX',
    'EARNINGS_MIN_EPS_GROWTH','GAP_UP_THRESHOLD',
    'BREAKOUT_CONSOLIDATION_LOOKBACK','BREAKOUT_CONSOLIDATION_DAILY_VOL',
    'BREAKOUT_HIGHER_LOWS_LOOKBACK','BREAKOUT_MIN_SCORE',
    'BREAKOUT_ATR_STOP_MULT','BREAKOUT_MAX_STOP_PCT','BREAKOUT_SUPPORT_LOOKBACK','BREAKOUT_RR_RATIO',
    'PARTIAL_EXIT_R','PARTIAL_EXIT_PCT','TRAIL_ATR_MULT',
    'ACCUM_LOOKBACK_DAYS','BULL_TRAP_SCORE_THRESHOLD',
    'SCORE_VCP','SCORE_CONSOLIDATION','SCORE_HIGHER_LOWS',
    'SCORE_52W_HIGH_PROXIMITY','SCORE_EARNINGS_PROXIMITY',
    'SCORE_VOLUME_SURGE','SCORE_BREAKOUT_20D','SCORE_RSI_ZONE','SCORE_RELATIVE_STRENGTH',
    'SCORE_MARKET_BREADTH','SCORE_ACCUM_MAX_BONUS','SCORE_TRAP_MAX_PENALTY',
    'MAX_POSITION_SIZE','MAX_PORTFOLIO_RISK','MAX_CONCURRENT_TRADES','MAX_DAILY_LOSS_PCT',
    'BACKTEST_INITIAL_CAPITAL','BACKTEST_MAX_HOLD_DAYS','BACKTEST_SLIPPAGE_PCT',
    'ALPACA_BASE_URL',
    'WHATSAPP_PHONE','WHATSAPP_APIKEY',
    'PME_ADD_SCORE_THRESHOLD','PME_HOLD_SCORE_MIN','PME_TRIM_LIGHT_SCORE_MIN','PME_TRIM_HEAVY_SCORE_MIN',
    'PME_ADD_SIZE_PCT','PME_ADD_MAX_MULTIPLIER','PME_TRIM_LIGHT_PCT','PME_TRIM_HEAVY_PCT',
    'PME_RS_ADD_MIN_PCT','PME_RS_DOWNGRADE_BELOW_PCT','PME_R_TRIM_FLOOR','PME_R_TRIM_ENFORCE',
    'PME_FOLLOWTHROUGH_DAYS','PME_VOLUME_SELLOFF_MULT',
  ];
  fields.forEach(k => {
    const el = document.getElementById('cfg-' + k);
    if (el) cfg[k] = el.value.trim();
  });

  // Booleans
  cfg.IS_PAPER               = document.getElementById('cfg-IS_PAPER').checked               ? 'true' : 'false';
  cfg.REGIME_AWARE_SCANNING  = document.getElementById('cfg-REGIME_AWARE_SCANNING').checked  ? 'true' : 'false';
  cfg.EARNINGS_FILTER_ENABLED = document.getElementById('cfg-EARNINGS_FILTER_ENABLED').checked ? 'true' : 'false';

  // Selects
  ['SCANNER_INTERVAL_MINUTES','REGIME_OVERRIDE'].forEach(k => {
    const el = document.getElementById('cfg-' + k);
    if (el) cfg[k] = el.value;
  });

  return cfg;
}

async function saveConfig() {
  const btn = document.getElementById('save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const [r1, r2] = await Promise.all([
      fetch('/api/config', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(collectConfig()),
      }),
      fetch('/api/symbols/fallback', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({symbols: fallbackSymbols}),
      }),
    ]);
    const [d1, d2] = await Promise.all([r1.json(), r2.json()]);
    if (d1.ok && d2.ok) {
      showToast('Configuration saved', 'success');
      document.getElementById('save-status').textContent = 'Saved at ' + new Date().toLocaleTimeString();
    } else {
      showToast('Save failed: ' + (d1.error || d2.error || 'unknown error'), 'error');
    }
  } catch(e) {
    showToast('Save failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '&#10003; Save Config';
  }
}

// ── Symbol chips ───────────────────────────────────────────────────────────
function renderChips(containerId, list, removeFn) {
  const el = document.getElementById(containerId);
  if (list.length === 0) {
    el.innerHTML = '<span class="muted" style="font-size:11px;padding:4px 0;">None added yet</span>';
    return;
  }
  el.innerHTML = list.map(sym =>
    `<span class="chip">${esc(sym)}<span class="chip-x" data-sym="${esc(sym)}">&times;</span></span>`
  ).join('');
  el.onclick = function(e) {
    const x = e.target.closest('.chip-x');
    if (x) removeFn(x.dataset.sym);
  };
}

function updateSymbolBadge() {
  const badge = document.getElementById('symbol-count-badge');
  if (badge) badge.textContent = fallbackSymbols.length + ' symbols';
}

function renderSymbolChips() {
  const el     = document.getElementById('symbol-chips');
  const query  = symbolFilter.toLowerCase();
  const list   = query ? fallbackSymbols.filter(s => s.toLowerCase().includes(query)) : fallbackSymbols;
  if (list.length === 0) {
    el.innerHTML = '<span class="muted" style="font-size:11px;padding:4px 0;">'
      + (query ? 'No symbols match "' + symbolFilter + '"' : 'No symbols in list') + '</span>';
    return;
  }
  el.innerHTML = list.map(sym =>
    `<span class="chip">${esc(sym)}<span class="chip-x" data-sym="${esc(sym)}">&times;</span></span>`
  ).join('');
  el.onclick = function(e) {
    const x = e.target.closest('.chip-x');
    if (x) removeSymbol(x.dataset.sym);
  };
}

function filterSymbols(query) {
  symbolFilter = query;
  renderSymbolChips();
}

function addSymbol() {
  const input = document.getElementById('symbol-input');
  const sym   = input.value.trim().toUpperCase();
  if (!sym) return;
  if (fallbackSymbols.includes(sym)) { showToast(sym + ' already in list', 'error'); return; }
  fallbackSymbols.push(sym);
  renderSymbolChips();
  updateSymbolBadge();
  input.value = '';
  input.focus();
}

function removeSymbol(sym) {
  fallbackSymbols = fallbackSymbols.filter(s => s !== sym);
  renderSymbolChips();
  updateSymbolBadge();
}

async function resetFallbackSymbols() {
  if (!confirm('Reset to the default S&P 500 fallback list? All custom changes will be lost.')) return;
  try {
    const res  = await fetch('/api/symbols/fallback/reset', {method: 'DELETE'});
    const data = await res.json();
    if (data.ok) {
      const symRes = await fetch('/api/symbols/fallback');
      const syms   = await symRes.json();
      fallbackSymbols = syms.symbols || [];
      symbolFilter    = '';
      document.getElementById('symbol-filter').value = '';
      renderSymbolChips();
      updateSymbolBadge();
      showToast('Reset to ' + data.count + ' default symbols', 'success');
    }
  } catch(e) {
    showToast('Reset failed', 'error');
  }
}


// ── Paper/Live toggle ──────────────────────────────────────────────────────
function onPaperToggle(cb) {
  const warning = document.getElementById('live-warning');
  if (!cb.checked) {
    if (!confirm('Switch to LIVE trading mode?\n\nThis will use REAL MONEY from your Alpaca live account. Make sure you have tested your strategy and your live credentials are configured.')) {
      cb.checked = true;
      return;
    }
    _isLiveAccount = true;
    applyTradingModeBanner();
    warning.style.display = 'block';
    document.getElementById('cfg-ALPACA_BASE_URL').value = 'https://api.alpaca.markets';
  } else {
    warning.style.display = 'none';
    document.getElementById('cfg-ALPACA_BASE_URL').value = 'https://paper-api.alpaca.markets';
    _isLiveAccount = false;
    applyTradingModeBanner();
  }
}

// ── WhatsApp test ──────────────────────────────────────────────────────────
async function testWhatsapp() {
  const phone  = document.getElementById('cfg-WHATSAPP_PHONE').value.trim();
  const apikey = document.getElementById('cfg-WHATSAPP_APIKEY').value.trim();
  if (!phone || !apikey) {
    showToast('Enter phone number and API key first', 'error');
    return;
  }
  try {
    const res  = await fetch('/api/notifications/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({phone, apikey}),
    });
    const data = await res.json();
    if (data.ok) showToast('Test message sent — check WhatsApp', 'success');
    else         showToast('Failed: ' + (data.error || 'unknown'), 'error');
  } catch(e) {
    showToast('Request failed: ' + e.message, 'error');
  }
}

// ── Secret key visibility (unused — Alpaca creds removed from UI) ─────────
function toggleSecretVisibility() {
}

// ── Toast notification ─────────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg, type='success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className   = 'toast ' + type + ' show';
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}

// ── Boot ───────────────────────────────────────────────────────────────────
function bootLoadLatest() {
  loadDashboard(todayStr());
}
bootLoadLatest();
fetchTradingMode();
loadLiveTrades();

// ── Session security ───────────────────────────────────────────────────────

// Intercept all fetch calls: inject CSRF token on mutations, redirect to /login on 401
const _origFetch = window.fetch.bind(window);
window.fetch = async function(input, init = {}) {
  const method = (init.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
    init = { ...init, headers: { ...(init.headers || {}), 'X-CSRF-Token': _getCsrfToken() } };
  }
  let res;
  try { res = await _origFetch(input, init); }
  catch(e) { throw e; }
  if (res.status === 401) {
    window.location.href = '/login?reason=session';
  }
  return res;
};

// Auto-logout after 10 minutes of inactivity
const IDLE_TIMEOUT_MS = 10 * 60 * 1000;
let _idleTimer = null;

async function autoLogout() {
  try { await _origFetch('/logout', { method: 'POST' }); } catch(e) {}
  window.location.href = '/login?reason=idle';
}

function resetIdleTimer() {
  clearTimeout(_idleTimer);
  _idleTimer = setTimeout(autoLogout, IDLE_TIMEOUT_MS);
}

['mousedown', 'mousemove', 'keydown', 'scroll', 'touchstart', 'click'].forEach(evt => {
  document.addEventListener(evt, resetIdleTimer, { passive: true });
});
resetIdleTimer();

// Manual logout
async function logout() {
  if (!confirm('Sign out of A1TRADES?')) return;
  try { await _origFetch('/logout', { method: 'POST' }); } catch(e) {}
  window.location.href = '/login';
}

// ── Event delegation: close-position buttons (rendered dynamically) ────────
document.getElementById('live-trades-body').addEventListener('click', function(e) {
  const btn = e.target.closest('.close-pos-btn');
  if (!btn) return;
  closePosition(btn.dataset.symbol, btn.dataset.orderId, btn);
});

// ── Event delegation: history rows (rendered dynamically) ──────────────────
document.getElementById('history-body').addEventListener('click', function(e) {
  const row = e.target.closest('tr[data-date]');
  if (!row) return;
  toggleHistoryDetail(row.dataset.date, row);
});

// ── Static HTML element event wiring ───────────────────────────────────────
(function wireStaticHandlers() {
  const $ = id => document.getElementById(id);

  // Scan panel
  $('scan-overlay').addEventListener('click', closeScanPanel);
  $('scan-close-btn').addEventListener('click', closeScanPanel);
  $('scan-clear-btn').addEventListener('click', clearScanLog);
  $('scan-again-btn').addEventListener('click', startScan);

  // Header controls
  $('header-runner-pill').addEventListener('click', doScan);  // legacy (hidden)
  $('h-scan-now-btn').addEventListener('click', doScan);
  $('h-stop-runner-btn').addEventListener('click', doStopRunner);
  $('refresh-btn').addEventListener('click', refresh);
  $('logout-btn').addEventListener('click', logout);

  // Tab bar (delegation on container)
  document.querySelector('.tabs').addEventListener('click', function(e) {
    const tab = e.target.closest('[data-tab]');
    if (tab) switchTab(tab.dataset.tab, tab);
  });

  // Today tab scanner controls — always scan, never stop-runner
  $('runner-btn').addEventListener('click', doScan);
  $('execute-toggle').addEventListener('click', toggleExecuteMode);

  // Overview tab
  $('positions-refresh-btn').addEventListener('click', loadLiveTrades);

  // Config tab
  $('reset-universe-btn').addEventListener('click', resetFallbackSymbols);
  $('symbol-filter').addEventListener('input', function() { filterSymbols(this.value); });
  $('symbol-input').addEventListener('keydown', function(e) { if (e.key === 'Enter') addSymbol(); });
  $('add-symbol-btn').addEventListener('click', addSymbol);
  $('test-whatsapp-btn').addEventListener('click', testWhatsapp);
  $('reload-config-btn').addEventListener('click', loadConfig);
  $('save-btn').addEventListener('click', saveConfig);

  // Score weight inputs (event delegation on config grid)
  document.querySelectorAll('.score-weight-input').forEach(function(inp) {
    inp.addEventListener('input', updateWeightsTotal);
  });

  // IS_PAPER toggle
  const paperToggle = $('cfg-IS_PAPER');
  if (paperToggle) paperToggle.addEventListener('change', function() { onPaperToggle(this); });
})();
