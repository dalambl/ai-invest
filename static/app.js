const API = '';
let state = {
    positions: [],
    trades: [],
    watchlist: [],
    horizonPnl: {},
    currentHorizon: '1d',
    perfHorizon: 'ytd',
    chartHorizon: '1y',
    sortCol: 'market_value',
    sortAsc: false,
    theme: localStorage.getItem('theme') || 'dark',
    fredOverlays: JSON.parse(localStorage.getItem('fredOverlays') || '[]'),
    fredCache: {},
    perfData: null,
    modalSymbol: null,
    modalHorizon: '1y',
};

// --- Init ---
document.documentElement.setAttribute('data-theme', state.theme);

document.addEventListener('DOMContentLoaded', () => {
    setupNav();
    setupHorizonBars();
    setupThemeToggle();
    setupCommandBar();
    setupTableSort();
    checkStatus();
    loadPositions();
    setInterval(checkStatus, 30000);
});

// --- Navigation ---
function setupNav() {
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = btn.dataset.page;
            navigateTo(page);
        });
    });
}

function navigateTo(page) {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`.nav-btn[data-page="${page}"]`)?.classList.add('active');
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`page-${page}`)?.classList.add('active');

    if (page === 'performance') loadPerformance();
    else if (page === 'risk') loadRisk();
    else if (page === 'exposure') loadExposure();
    else if (page === 'trades') loadTrades();
    else if (page === 'watchlist') loadWatchlist();
    else if (page === 'market') loadMarket();
}

// --- Status ---
async function checkStatus() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();
        const dot = document.getElementById('status-dot');
        const text = document.getElementById('status-text');
        if (data.tws_connected) {
            dot.className = 'status-dot connected';
            text.textContent = 'TWS Connected';
        } else {
            dot.className = 'status-dot disconnected';
            text.textContent = 'TWS Disconnected';
        }
        document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
        loadAccountSummary();
    } catch { }
}

async function loadAccountSummary() {
    try {
        const res = await fetch(`${API}/api/account`);
        const data = await res.json();
        if (data.NetLiquidation) document.getElementById('nav-nlv').textContent = fmt(data.NetLiquidation.value);
        if (data.TotalCashValue) document.getElementById('nav-cash').textContent = fmt(data.TotalCashValue.value);
    } catch { }
}

// --- Holdings ---
async function loadPositions() {
    try {
        const res = await fetch(`${API}/api/positions`);
        state.positions = await res.json();
        loadHorizonPnl(state.currentHorizon);
    } catch { renderHoldings(); }
}

async function loadHorizonPnl(horizon) {
    state.currentHorizon = horizon;
    try {
        const res = await fetch(`${API}/api/pnl?horizon=${horizon}`);
        state.horizonPnl = await res.json();
        document.getElementById('nav-pnl').textContent = fmtPnl(state.horizonPnl.total_pnl);
        document.getElementById('nav-pnl').className = 'value ' + (state.horizonPnl.total_pnl >= 0 ? 'pos' : 'neg');
    } catch {
        state.horizonPnl = { positions: [], total_pnl: 0, total_pnl_pct: 0 };
    }
    renderHoldings();
}

function renderHoldings() {
    const tbody = document.getElementById('holdings-body');
    const positions = state.positions;
    const pnlMap = {};
    (state.horizonPnl.positions || []).forEach(p => { pnlMap[p.symbol] = p; });

    const totalMV = positions.reduce((s, p) => s + Math.abs(p.market_value || 0), 0);

    let rows = positions.map(p => {
        const hp = pnlMap[p.symbol] || {};
        const weight = totalMV ? (Math.abs(p.market_value || 0) / totalMV * 100) : 0;
        // USD-denominated unrealized so the column reconciles with Stock + FX.
        // Falls back to local unrealized_pnl when USD fields are absent.
        const unrl = p.unrealized_pnl_usd != null ? p.unrealized_pnl_usd : (p.unrealized_pnl || 0);
        const cb_usd = p.cost_basis_usd != null ? p.cost_basis_usd : (p.market_value - (p.unrealized_pnl || 0));
        const pnl_pct = cb_usd ? (unrl / Math.abs(cb_usd) * 100) : 0;
        const divs = p.dividends_cumulative || 0;
        const total_return = (p.total_return_usd != null) ? p.total_return_usd : (unrl + divs);
        return { ...p, weight, pnl_pct, dividends_cumulative: divs, total_return,
                 unrealized_pnl_display: unrl,
                 horizon_pnl: hp.pnl || 0, horizon_pnl_pct: hp.pnl_pct || 0 };
    });

    if (state.sortCol) {
        rows.sort((a, b) => {
            let va = a[state.sortCol], vb = b[state.sortCol];
            if (typeof va === 'string') return state.sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
            return state.sortAsc ? (va - vb) : (vb - va);
        });
    }

    let totalUnrl = 0, totalHPnl = 0, totalMV2 = 0, totalDivs = 0, totalTR = 0;
    let totalMVUsd = 0, totalStockPnl = 0, totalFxPnl = 0;
    let html = '';
    for (const r of rows) {
        totalUnrl += r.unrealized_pnl_display || 0;
        totalHPnl += r.horizon_pnl || 0;
        totalMV2 += r.market_value || 0;
        totalMVUsd += (r.market_value_usd != null ? r.market_value_usd : r.market_value) || 0;
        totalStockPnl += r.stock_pnl_usd || 0;
        totalFxPnl += r.fx_pnl_usd || 0;
        totalDivs += r.dividends_cumulative || 0;
        totalTR += r.total_return || 0;
        const mvUsd = r.market_value_usd != null ? r.market_value_usd : r.market_value;
        const stockPnl = r.stock_pnl_usd != null ? r.stock_pnl_usd : null;
        const fxPnl = r.fx_pnl_usd != null ? r.fx_pnl_usd : null;
        html += `<tr>
            <td><strong onclick="openSymbolModal('${r.symbol}', ${r.avg_cost || 0})">${r.symbol}</strong></td>
            <td>${r.sec_type || ''}</td>
            <td>${r.purchase_date || ''}</td>
            <td class="num">${fmtN(r.quantity)}</td>
            <td class="num">${fmtP(r.avg_cost)}</td>
            <td class="num">${fmtP(r.market_price)}</td>
            <td class="num">${fmt(r.market_value)}</td>
            <td class="num">${fmt(mvUsd)}</td>
            <td class="num">${r.weight.toFixed(1)}%</td>
            <td class="num ${clsPnl(r.unrealized_pnl_display)}">${fmtPnl(r.unrealized_pnl_display)}</td>
            <td class="num ${clsPnl(stockPnl)}">${stockPnl == null ? '—' : fmtPnl(stockPnl)}</td>
            <td class="num ${clsPnl(fxPnl)}">${fxPnl == null ? '—' : fmtPnl(fxPnl)}</td>
            <td class="num ${clsPnl(r.pnl_pct)}">${r.pnl_pct.toFixed(2)}%</td>
            <td class="num pos">${fmt(r.dividends_cumulative)}</td>
            <td class="num ${clsPnl(r.total_return)}">${fmtPnl(r.total_return)}</td>
            <td class="num ${clsPnl(r.horizon_pnl)}">${fmtPnl(r.horizon_pnl)}</td>
            <td class="num ${clsPnl(r.horizon_pnl_pct)}">${r.horizon_pnl_pct.toFixed(2)}%</td>
            <td>${r.currency || ''}</td>
        </tr>`;
    }
    const totalPct = totalMV2 ? (totalHPnl / (totalMV2 - totalHPnl) * 100) : 0;
    html += `<tr class="summary-row">
        <td>TOTAL</td><td></td><td></td><td></td><td></td><td></td>
        <td class="num">${fmt(totalMV2)}</td>
        <td class="num">${fmt(totalMVUsd)}</td>
        <td class="num">100%</td>
        <td class="num ${clsPnl(totalUnrl)}">${fmtPnl(totalUnrl)}</td>
        <td class="num ${clsPnl(totalStockPnl)}">${fmtPnl(totalStockPnl)}</td>
        <td class="num ${clsPnl(totalFxPnl)}">${fmtPnl(totalFxPnl)}</td>
        <td></td>
        <td class="num pos">${fmt(totalDivs)}</td>
        <td class="num ${clsPnl(totalTR)}">${fmtPnl(totalTR)}</td>
        <td class="num ${clsPnl(totalHPnl)}">${fmtPnl(totalHPnl)}</td>
        <td class="num ${clsPnl(totalPct)}">${totalPct.toFixed(2)}%</td><td></td>
    </tr>`;
    tbody.innerHTML = html;
}

async function refreshPositions() {
    try {
        await fetch(`${API}/api/snapshot`, { method: 'POST' });
    } catch { }
    loadPositions();
}

// --- Performance ---
async function loadPerformance() {
    const horizon = state.perfHorizon;
    const days = { '1m': 30, '3m': 90, '6m': 180, 'ytd': 365, '1y': 365, '3y': 1095, '5y': 1825, 'all': 3650 };
    const d = days[horizon] || 365;
    const from = new Date(Date.now() - d * 86400000).toISOString().slice(0, 10);

    let fromDate;
    if (horizon === 'ytd') fromDate = new Date().getFullYear() + '-01-01';
    else if (horizon === 'all') fromDate = '2000-01-01';
    else fromDate = from;
    const toDate = new Date().toISOString().slice(0, 10);

    try {
        const res = await fetch(`${API}/api/pnl/timeseries?from_date=${fromDate}&to_date=${toDate}`);
        const data = await res.json();
        state.perfData = data;
        renderCumulativePnlChart(data);
        renderPerfDrawdownChart(data);
        renderPerfChart({ portfolio: data });
        renderValueChart({ portfolio: data });
        reloadFredOverlays();
    } catch { }

    try {
        const res = await fetch(`${API}/api/returns/monthly?from_date=2000-01-01&to_date=${new Date().toISOString().slice(0, 10)}`);
        const data = await res.json();
        renderMonthlyGrid(data);
    } catch { }
}

function renderCumulativePnlChart(data) {
    if (!data.dates || !data.dates.length) {
        Plotly.newPlot('pnl-chart', [], chartLayout('P&L ($)', true), { responsive: true, displayModeBar: false });
        return;
    }
    const traces = [];
    if (data.pnl) {
        const lastPnl = data.pnl[data.pnl.length - 1];
        const color = lastPnl >= 0 ? getCSS('--green') : getCSS('--red');
        const fillColor = lastPnl >= 0 ? 'rgba(63, 185, 80, 0.15)' : 'rgba(248, 81, 73, 0.15)';
        traces.push({
            x: data.dates, y: data.pnl, name: 'P&L',
            fill: 'tozeroy', line: { color: color, width: 2 }, fillcolor: fillColor
        });
    }
    if (data.total_return) {
        traces.push({
            x: data.dates, y: data.total_return, name: 'Total Return (P&L + Dividends)',
            line: { color: getCSS('--accent'), width: 2, dash: 'dot' }
        });
    }
    const layout = chartLayout('P&L ($)', true);
    state.fredOverlays.forEach((sid, i) => {
        const series = state.fredCache[sid];
        if (!series || !series.dates.length) return;
        const axisName = i === 0 ? 'y2' : 'y' + (2 + i);
        traces.push({
            x: series.dates, y: series.values, name: sid,
            yaxis: axisName,
            line: { color: getCSS('--accent2') || '#bc8cff', width: 1.2, dash: 'dot' },
            opacity: 0.85,
        });
        layout[i === 0 ? 'yaxis2' : 'yaxis' + (2 + i)] = {
            color: getCSS('--text2'), overlaying: 'y',
            side: i % 2 === 0 ? 'right' : 'left',
            position: i < 2 ? undefined : (i % 2 === 0 ? 1 - 0.04 * Math.floor(i/2) : 0.04 * Math.floor(i/2)),
            title: sid, showgrid: false,
        };
    });
    if (state.fredOverlays.length) {
        layout.xaxis = { ...(layout.xaxis || {}), domain: [0, 1] };
    }
    Plotly.newPlot('pnl-chart', traces, layout, { responsive: true, displayModeBar: false });
    renderFredChips();
}

function renderFredChips() {
    const el = document.getElementById('fred-overlay-chips');
    if (!el) return;
    el.innerHTML = state.fredOverlays.map(sid =>
        `<span class="fred-chip">${sid}<button onclick="removeFredOverlay('${sid}')" title="Remove">x</button></span>`
    ).join('');
}

async function addFredOverlay() {
    const input = document.getElementById('fred-overlay-input');
    const sid = (input.value || '').trim().toUpperCase();
    if (!sid) return;
    if (state.fredOverlays.includes(sid)) { input.value = ''; return; }
    const from = state.perfData?.dates?.[0];
    const to = state.perfData?.dates?.[state.perfData.dates.length - 1];
    let url = `${API}/api/fred/${encodeURIComponent(sid)}`;
    if (from && to) url += `?from_date=${from}&to_date=${to}`;
    try {
        const res = await fetch(url);
        const data = await res.json();
        if (data.error || !data.dates?.length) {
            alert(`FRED series ${sid}: ${data.error || 'no data in range'}`);
            return;
        }
        state.fredCache[sid] = data;
        state.fredOverlays.push(sid);
        localStorage.setItem('fredOverlays', JSON.stringify(state.fredOverlays));
        input.value = '';
        if (state.perfData) renderCumulativePnlChart(state.perfData);
    } catch (e) {
        alert('FRED fetch failed: ' + e);
    }
}

function removeFredOverlay(sid) {
    state.fredOverlays = state.fredOverlays.filter(s => s !== sid);
    delete state.fredCache[sid];
    localStorage.setItem('fredOverlays', JSON.stringify(state.fredOverlays));
    if (state.perfData) renderCumulativePnlChart(state.perfData);
}

async function reloadFredOverlays() {
    if (!state.perfData?.dates?.length) return;
    const from = state.perfData.dates[0];
    const to = state.perfData.dates[state.perfData.dates.length - 1];
    for (const sid of [...state.fredOverlays]) {
        if (state.fredCache[sid]) continue;
        try {
            const res = await fetch(`${API}/api/fred/${encodeURIComponent(sid)}?from_date=${from}&to_date=${to}`);
            const data = await res.json();
            if (data.dates?.length) state.fredCache[sid] = data;
        } catch { }
    }
    renderCumulativePnlChart(state.perfData);
}

function renderPerfDrawdownChart(data) {
    const pnl = data.pnl;
    const vals = data.values;
    if (!pnl || !vals || pnl.length < 2) {
        Plotly.newPlot('perf-drawdown-chart', [], chartLayout('Drawdown %', true), { responsive: true, displayModeBar: false });
        return;
    }
    // Build a return index from P&L relative to portfolio value (adjusts for cash flows)
    // index[0] = 100, index[i] = index[i-1] * (1 + daily_return[i])
    // daily_return[i] = (pnl[i] - pnl[i-1]) / vals[i-1]
    const idx = [100];
    for (let i = 1; i < pnl.length; i++) {
        const prevVal = vals[i - 1];
        const dailyPnlChange = pnl[i] - pnl[i - 1];
        const dailyRet = prevVal > 0 ? dailyPnlChange / prevVal : 0;
        idx.push(idx[i - 1] * (1 + dailyRet));
    }
    const dd = [];
    let peak = idx[0];
    for (const v of idx) {
        if (v > peak) peak = v;
        dd.push(peak > 0 ? ((v - peak) / peak) * 100 : 0);
    }
    const trace = {
        x: data.dates, y: dd, fill: 'tozeroy',
        line: { color: getCSS('--red'), width: 1.5 },
        fillcolor: 'rgba(248, 81, 73, 0.15)', name: 'Drawdown'
    };
    Plotly.newPlot('perf-drawdown-chart', [trace], chartLayout('Drawdown %', true), { responsive: true, displayModeBar: false });
}

function renderPerfChart(data) {
    const traces = [];
    const d = data.portfolio || data;
    if (d && d.dates && d.dates.length) {
        traces.push({ x: d.dates, y: d.returns, name: 'Portfolio',
            line: { color: getCSS('--accent'), width: 2 } });
    }
    if (data.benchmark && data.benchmark.dates && data.benchmark.dates.length) {
        traces.push({ x: data.benchmark.dates, y: data.benchmark.returns,
            name: data.benchmark.symbol, line: { color: getCSS('--text2'), width: 1, dash: 'dot' } });
    }
    Plotly.newPlot('perf-chart', traces, chartLayout('Return %', true), { responsive: true, displayModeBar: false });
}

function renderValueChart(data) {
    const d = data.portfolio || data;
    if (!d || !d.dates || !d.dates.length) return;
    const trace = { x: d.dates, y: d.values, name: 'Portfolio Value',
        fill: 'tozeroy', line: { color: getCSS('--accent'), width: 2 },
        fillcolor: 'rgba(88, 166, 255, 0.1)' };
    Plotly.newPlot('value-chart', [trace], chartLayout('Value ($)', false), { responsive: true, displayModeBar: false });
}

function renderMonthlyGrid(data) {
    // `data` is { monthly: { "YYYY-MM": pct }, yearly: { "YYYY": pct } }
    const monthly = (data && data.monthly) || {};
    const yearly = (data && data.yearly) || {};
    const years = new Set(Object.keys(monthly).map(k => k.slice(0, 4)));
    if (!years.size) {
        document.getElementById('monthly-grid').innerHTML = '<div style="color:var(--text2);padding:20px">No snapshot data</div>';
        return;
    }
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    let html = '<div class="monthly-grid"><div class="monthly-header"></div>';
    months.forEach(m => { html += `<div class="monthly-header">${m}</div>`; });
    html += '<div class="monthly-header">Year</div>';

    for (const year of [...years].sort()) {
        html += `<div class="monthly-header">${year}</div>`;
        for (let m = 1; m <= 12; m++) {
            const key = `${year}-${String(m).padStart(2, '0')}`;
            const ret = monthly[key];
            if (ret == null) {
                html += '<div class="monthly-cell">—</div>';
            } else {
                const bg = ret >= 0 ? `rgba(63,185,80,${Math.min(Math.abs(ret) / 10, 0.6)})` :
                    `rgba(248,81,73,${Math.min(Math.abs(ret) / 10, 0.6)})`;
                html += `<div class="monthly-cell" style="background:${bg}" title="${ret.toFixed(2)}%">${ret.toFixed(1)}</div>`;
            }
        }
        const yr = yearly[year];
        const ybg = (yr ?? 0) >= 0 ? 'var(--green)' : 'var(--red)';
        const yrText = yr == null ? '—' : yr.toFixed(1);
        html += `<div class="monthly-cell" style="color:${ybg};font-weight:700">${yrText}</div>`;
    }
    html += '</div>';
    document.getElementById('monthly-grid').innerHTML = html;
}

// --- Risk ---
async function loadRisk() {
    try {
        const res = await fetch(`${API}/api/risk`);
        const data = await res.json();
        const metrics = [
            { label: 'Ann. Return', val: data.annualized_return + '%', cls: clsPnl(data.annualized_return) },
            { label: 'Volatility', val: data.volatility + '%', cls: '' },
            { label: 'Sharpe Ratio', val: data.sharpe, cls: clsPnl(data.sharpe) },
            { label: 'Max Drawdown', val: data.max_drawdown_pct + '%', cls: 'neg' },
        ];
        document.getElementById('risk-metrics').innerHTML = metrics.map(m =>
            `<div class="risk-card"><div class="label">${m.label}</div><div class="val ${m.cls}">${m.val}</div></div>`
        ).join('');

        const sf = data.sharpe_by_frequency || {};
        const rfPct = data.risk_free_rate != null ? (data.risk_free_rate * 100).toFixed(2) + '%' : '—';
        const rows = [
            { freq: 'Daily', val: sf.daily, periods: 252 },
            { freq: 'Weekly', val: sf.weekly, periods: 52 },
            { freq: 'Monthly', val: sf.monthly, periods: 12 },
        ];
        document.getElementById('sharpe-body').innerHTML = rows.map(r =>
            `<tr>
                <td>${r.freq}</td>
                <td class="num ${clsPnl(r.val)}">${r.val == null ? '—' : Number(r.val).toFixed(2)}</td>
                <td class="num">${r.periods}</td>
                <td>${rfPct}</td>
            </tr>`
        ).join('');
    } catch { }

    try {
        const res = await fetch(`${API}/api/pnl/timeseries`);
        const data = await res.json();
        renderDrawdownChart(data);
    } catch { }
}

function renderDrawdownChart(data) {
    if (!data.pnl || !data.values || data.values.length < 2) return;
    // Build return index from P&L changes / portfolio value (adjusts for cash flows)
    const idx = [100];
    for (let i = 1; i < data.pnl.length; i++) {
        const prevVal = data.values[i - 1];
        const dailyPnlChange = data.pnl[i] - data.pnl[i - 1];
        const dailyRet = prevVal > 0 ? dailyPnlChange / prevVal : 0;
        idx.push(idx[i - 1] * (1 + dailyRet));
    }
    const dd = [];
    let peak = idx[0];
    for (const v of idx) {
        if (v > peak) peak = v;
        dd.push(peak > 0 ? ((v - peak) / peak) * 100 : 0);
    }
    const trace = { x: data.dates, y: dd, fill: 'tozeroy', line: { color: getCSS('--red'), width: 1 },
        fillcolor: 'rgba(248, 81, 73, 0.2)' };
    Plotly.newPlot('drawdown-chart', [trace], chartLayout('Drawdown %', true), { responsive: true, displayModeBar: false });
}

// --- Exposure ---
async function loadExposure() {
    try {
        const res = await fetch(`${API}/api/exposure/sector`);
        const data = await res.json();

        if (data.length) {
            Plotly.newPlot('exposure-pie', [{
                labels: data.map(d => d.category), values: data.map(d => d.value),
                type: 'pie', hole: 0.4, textinfo: 'label+percent',
                marker: { colors: ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#79c0ff', '#56d364'] },
                textfont: { color: getCSS('--text'), size: 11 }
            }], { ...baseLayout(), showlegend: false }, { responsive: true, displayModeBar: false });
        }

        const positions = state.positions.filter(p => p.market_value).sort((a, b) => Math.abs(b.market_value) - Math.abs(a.market_value));
        if (positions.length) {
            const colors = positions.map(p => (p.unrealized_pnl || 0) >= 0 ? getCSS('--green') : getCSS('--red'));
            Plotly.newPlot('exposure-bar', [{
                x: positions.map(p => p.symbol), y: positions.map(p => Math.abs(p.market_value)),
                type: 'bar', marker: { color: colors }
            }], { ...baseLayout(), xaxis: { color: getCSS('--text2') }, yaxis: { color: getCSS('--text2'), title: 'Market Value' } },
                { responsive: true, displayModeBar: false });
        }

        renderHeatmap(positions);
    } catch { }
}

function renderHeatmap(positions) {
    if (!positions.length) return;
    const vals = positions.filter(p => p.market_value > 0);
    if (!vals.length) return;
    Plotly.newPlot('heatmap', [{
        type: 'treemap', labels: vals.map(p => p.symbol),
        parents: vals.map(() => ''), values: vals.map(p => Math.abs(p.market_value)),
        text: vals.map(p => {
            const pnl = p.unrealized_pnl || 0;
            const pct = p.avg_cost ? ((p.market_price - p.avg_cost) / p.avg_cost * 100).toFixed(1) : '0';
            return `${pct}%`;
        }),
        textinfo: 'label+text',
        marker: {
            colors: vals.map(p => {
                const pnl = p.avg_cost ? ((p.market_price - p.avg_cost) / p.avg_cost * 100) : 0;
                if (pnl > 5) return '#238636';
                if (pnl > 0) return '#2ea043';
                if (pnl > -5) return '#da3633';
                return '#b62324';
            })
        },
        textfont: { color: '#fff', size: 13 },
    }], { ...baseLayout(), margin: { t: 10, b: 10, l: 10, r: 10 } }, { responsive: true, displayModeBar: false });
}

// --- Trades ---
async function loadTrades() {
    const from = document.getElementById('trades-from').value;
    const to = document.getElementById('trades-to').value;
    let url = `${API}/api/trades?limit=500`;
    if (from) url += `&from_date=${from}`;
    if (to) url += `&to_date=${to}`;
    try {
        const res = await fetch(url);
        state.trades = await res.json();
        renderTrades();
    } catch { }
}

function renderTrades() {
    const tbody = document.getElementById('trades-body');
    tbody.innerHTML = state.trades.map(t => `<tr>
        <td>${t.trade_date?.slice(0, 10) || ''}</td>
        <td><strong>${t.symbol}</strong></td>
        <td class="${t.action === 'BUY' ? 'pos' : 'neg'}">${t.action}</td>
        <td class="num">${fmtN(t.quantity)}</td>
        <td class="num">${fmtP(t.price)}</td>
        <td class="num">${fmtP(t.commission)}</td>
        <td class="num ${clsPnl(t.net_amount)}">${fmt(t.net_amount)}</td>
        <td>${t.exchange || ''}</td>
        <td>${t.asset_class || ''}</td>
        <td>${t.account || ''}</td>
    </tr>`).join('');
}

// --- Watchlist ---
async function loadWatchlist() {
    try {
        const res = await fetch(`${API}/api/watchlist`);
        state.watchlist = await res.json();
        renderWatchlist();
    } catch { }
}

function renderWatchlist() {
    const tbody = document.getElementById('watchlist-body');
    tbody.innerHTML = state.watchlist.map(sym => `<tr id="wl-${sym}">
        <td><strong>${sym}</strong></td>
        <td class="num">—</td><td class="num">—</td><td class="num">—</td>
        <td class="num">—</td><td class="num">—</td><td class="num">—</td>
        <td><button class="btn btn-secondary" onclick="removeFromWatchlist('${sym}')" style="padding:2px 6px;font-size:11px">✕</button></td>
    </tr>`).join('');
    state.watchlist.forEach(fetchWatchlistQuote);
}

async function fetchWatchlistQuote(symbol) {
    try {
        const res = await fetch(`${API}/api/market/quote/${symbol}`);
        const q = await res.json();
        if (q.error) return;
        const row = document.getElementById(`wl-${symbol}`);
        if (!row) return;
        const cells = row.querySelectorAll('td');
        const change = q.last - q.close;
        cells[1].textContent = fmtP(q.last);
        cells[1].className = 'num ' + clsPnl(change);
        cells[2].textContent = fmtP(q.bid);
        cells[3].textContent = fmtP(q.ask);
        cells[4].textContent = q.volume ? fmtN(q.volume) : '—';
        cells[5].textContent = fmtP(q.high);
        cells[6].textContent = fmtP(q.low);
    } catch { }
}

async function addToWatchlist() {
    const input = document.getElementById('watchlist-add');
    const sym = input.value.trim().toUpperCase();
    if (!sym) return;
    if (!state.watchlist.includes(sym)) state.watchlist.push(sym);
    await fetch(`${API}/api/watchlist`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(state.watchlist) });
    input.value = '';
    renderWatchlist();
}

async function removeFromWatchlist(sym) {
    state.watchlist = state.watchlist.filter(s => s !== sym);
    await fetch(`${API}/api/watchlist`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(state.watchlist) });
    renderWatchlist();
}

// --- Market ---
async function loadMarket() {
    try {
        const res = await fetch(`${API}/api/market/movers`);
        const movers = await res.json();
        document.getElementById('movers-body').innerHTML = movers.map(m => `<tr>
            <td><strong>${m.symbol}</strong></td>
            <td class="num">${fmtN(m.quantity)}</td>
            <td class="num">${fmtP(m.market_price)}</td>
            <td class="num">${fmt(m.market_value)}</td>
            <td class="num ${clsPnl(m.unrealized_pnl)}">${fmtPnl(m.unrealized_pnl)}</td>
        </tr>`).join('');
    } catch { }
    loadSymbolChart();
}

async function loadSymbolChart() {
    const sym = document.getElementById('chart-symbol').value.trim().toUpperCase();
    if (!sym) return;
    try {
        const res = await fetch(`${API}/api/market/history/${sym}?period=${state.chartHorizon}`);
        const data = await res.json();
        if (!data.length) return;
        Plotly.newPlot('symbol-chart', [{
            x: data.map(d => d.date), open: data.map(d => d.open),
            high: data.map(d => d.high), low: data.map(d => d.low), close: data.map(d => d.close),
            type: 'candlestick',
            increasing: { line: { color: getCSS('--green') } },
            decreasing: { line: { color: getCSS('--red') } }
        }], { ...baseLayout(), xaxis: { color: getCSS('--text2'), rangeslider: { visible: false } },
            yaxis: { color: getCSS('--text2'), title: sym } },
            { responsive: true, displayModeBar: false });
    } catch { }
}

// --- Symbol Modal ---
function openSymbolModal(symbol, avgCost) {
    state.modalSymbol = symbol;
    state.modalAvgCost = avgCost || 0;
    document.getElementById('modal-symbol').textContent = symbol;
    document.getElementById('symbol-modal').classList.add('active');
    loadModalChart();
}

function closeSymbolModal() {
    document.getElementById('symbol-modal').classList.remove('active');
    state.modalSymbol = null;
}

async function loadModalChart() {
    const sym = state.modalSymbol;
    if (!sym) return;
    const period = state.modalHorizon;
    const meta = document.getElementById('modal-meta');
    meta.textContent = `period: ${period.toUpperCase()}  ·  loading…`;
    let bars = [];
    try {
        const res = await fetch(`${API}/api/market/history/${encodeURIComponent(sym)}?period=${period}`);
        bars = await res.json();
    } catch { }
    if (!bars?.length) {
        Plotly.newPlot('modal-chart', [], { ...baseLayout(), annotations: [{
            text: 'No price data', xref: 'paper', yref: 'paper', x: 0.5, y: 0.5, showarrow: false,
            font: { color: getCSS('--text2'), size: 14 }
        }] }, { responsive: true, displayModeBar: false });
        meta.textContent = `period: ${period.toUpperCase()}  ·  no data`;
        return;
    }
    let dividends = [];
    try {
        const dres = await fetch(`${API}/api/dividends?symbol=${encodeURIComponent(sym)}&from_date=${bars[0].date}&to_date=${bars[bars.length - 1].date}`);
        dividends = await dres.json();
    } catch { }

    const dates = bars.map(b => b.date);
    const closes = bars.map(b => b.close);
    const traces = [{
        x: dates, open: bars.map(b => b.open), high: bars.map(b => b.high),
        low: bars.map(b => b.low), close: closes, type: 'candlestick', name: sym,
        increasing: { line: { color: getCSS('--green') } },
        decreasing: { line: { color: getCSS('--red') } },
    }];
    const shapes = [];
    if (state.modalAvgCost && state.modalAvgCost > 0) {
        shapes.push({
            type: 'line', xref: 'paper', x0: 0, x1: 1,
            y0: state.modalAvgCost, y1: state.modalAvgCost,
            line: { color: getCSS('--accent'), width: 1, dash: 'dash' },
        });
    }
    if (dividends?.length) {
        const inRange = dividends.filter(d => d.date >= dates[0] && d.date <= dates[dates.length - 1]);
        const lo = Math.min(...bars.map(b => b.low));
        traces.push({
            x: inRange.map(d => d.date),
            y: inRange.map(() => lo),
            mode: 'markers',
            type: 'scatter',
            marker: { symbol: 'triangle-up', size: 10, color: getCSS('--green') },
            name: 'Dividend',
            text: inRange.map(d => `Div ${fmt(d.amount)}`),
            hoverinfo: 'text+x',
        });
    }
    const lastClose = closes[closes.length - 1];
    const firstClose = closes[0];
    const pct = firstClose ? ((lastClose / firstClose - 1) * 100).toFixed(2) : '0';
    const avgCostNote = state.modalAvgCost > 0 ? `  ·  avg cost ${fmtP(state.modalAvgCost)}` : '';
    meta.textContent = `period: ${period.toUpperCase()}  ·  ${dates[0]} → ${dates[dates.length - 1]}  ·  ${pct}%${avgCostNote}`;

    const layout = {
        ...baseLayout(),
        xaxis: { color: getCSS('--text2'), rangeslider: { visible: false }, gridcolor: getCSS('--border'), showgrid: false },
        yaxis: { color: getCSS('--text2'), gridcolor: getCSS('--border'), title: 'Price' },
        shapes,
        showlegend: dividends?.length > 0,
        legend: { x: 0, y: 1.1, orientation: 'h', font: { size: 11 } },
    };
    Plotly.newPlot('modal-chart', traces, layout, { responsive: true, displayModeBar: false });
}

// --- Horizon bars ---
function setupHorizonBars() {
    document.getElementById('holdings-horizon').addEventListener('click', e => {
        if (!e.target.classList.contains('horizon-pill')) return;
        document.querySelectorAll('#holdings-horizon .horizon-pill').forEach(p => p.classList.remove('active'));
        e.target.classList.add('active');
        loadHorizonPnl(e.target.dataset.h);
    });
    document.getElementById('perf-horizon').addEventListener('click', e => {
        if (!e.target.classList.contains('horizon-pill')) return;
        document.querySelectorAll('#perf-horizon .horizon-pill').forEach(p => p.classList.remove('active'));
        e.target.classList.add('active');
        state.perfHorizon = e.target.dataset.h;
        loadPerformance();
    });
    document.getElementById('chart-horizon').addEventListener('click', e => {
        if (!e.target.classList.contains('horizon-pill')) return;
        document.querySelectorAll('#chart-horizon .horizon-pill').forEach(p => p.classList.remove('active'));
        e.target.classList.add('active');
        state.chartHorizon = e.target.dataset.h;
        loadSymbolChart();
    });
    const mh = document.getElementById('modal-horizon');
    if (mh) mh.addEventListener('click', e => {
        if (!e.target.classList.contains('horizon-pill')) return;
        document.querySelectorAll('#modal-horizon .horizon-pill').forEach(p => p.classList.remove('active'));
        e.target.classList.add('active');
        state.modalHorizon = e.target.dataset.h;
        loadModalChart();
    });
    const sm = document.getElementById('symbol-modal');
    if (sm) sm.addEventListener('click', e => { if (e.target === sm) closeSymbolModal(); });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') closeSymbolModal();
    });
}

// --- Theme ---
function setupThemeToggle() {
    document.getElementById('theme-toggle').addEventListener('click', () => {
        state.theme = state.theme === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', state.theme);
        localStorage.setItem('theme', state.theme);
    });
}

// --- Table sort ---
function setupTableSort() {
    document.querySelectorAll('th[data-sort]').forEach(th => {
        th.addEventListener('click', () => {
            const col = th.dataset.sort;
            if (state.sortCol === col) state.sortAsc = !state.sortAsc;
            else { state.sortCol = col; state.sortAsc = true; }
            renderHoldings();
        });
    });
}

// --- Command bar ---
function setupCommandBar() {
    const overlay = document.getElementById('command-overlay');
    const input = document.getElementById('command-input');
    const results = document.getElementById('command-results');

    const commands = [
        { name: 'Holdings', key: 'HLD', action: () => navigateTo('holdings') },
        { name: 'Performance', key: 'PRF', action: () => navigateTo('performance') },
        { name: 'Risk', key: 'RSK', action: () => navigateTo('risk') },
        { name: 'Exposure', key: 'EXP', action: () => navigateTo('exposure') },
        { name: 'Trades', key: 'TRD', action: () => navigateTo('trades') },
        { name: 'Watchlist', key: 'WCH', action: () => navigateTo('watchlist') },
        { name: 'Market', key: 'MKT', action: () => navigateTo('market') },
        { name: 'Toggle Theme', key: 'D', action: () => document.getElementById('theme-toggle').click() },
        { name: 'Refresh', key: 'R', action: () => refreshPositions() },
        { name: 'Export Trades CSV', key: 'ET', action: () => exportCSV('trades') },
        { name: 'Export Positions CSV', key: 'EP', action: () => exportCSV('positions') },
    ];

    document.addEventListener('keydown', e => {
        if (e.key === '/' && !e.ctrlKey && document.activeElement.tagName !== 'INPUT') {
            e.preventDefault();
            overlay.classList.add('active');
            input.value = '';
            input.focus();
            renderCommands('');
        }
        if (e.key === 'Escape') overlay.classList.remove('active');
        if (e.key === 'd' && document.activeElement.tagName !== 'INPUT') {
            document.getElementById('theme-toggle').click();
        }
    });

    overlay.addEventListener('click', e => {
        if (e.target === overlay) overlay.classList.remove('active');
    });

    input.addEventListener('input', () => renderCommands(input.value));
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            const sel = results.querySelector('.command-item.selected') || results.querySelector('.command-item');
            if (sel) sel.click();
        }
    });

    function renderCommands(q) {
        const filtered = q ? commands.filter(c => c.name.toLowerCase().includes(q.toLowerCase()) || c.key.toLowerCase().includes(q.toLowerCase())) : commands;
        results.innerHTML = filtered.map((c, i) =>
            `<div class="command-item ${i === 0 ? 'selected' : ''}" data-idx="${i}"><span>${c.name}</span><span class="key">${c.key}</span></div>`
        ).join('');
        results.querySelectorAll('.command-item').forEach((el, i) => {
            el.addEventListener('click', () => { overlay.classList.remove('active'); filtered[i].action(); });
        });
    }
}

// --- Import/Export ---
function exportCSV(type) {
    window.open(`${API}/api/export/${type}`, '_blank');
}

async function importCSV(type, input) {
    const file = input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    try {
        const res = await fetch(`${API}/api/import/${type}`, { method: 'POST', body: form });
        const data = await res.json();
        alert(`Imported ${data.imported} records`);
        if (type === 'positions') loadPositions();
        else loadTrades();
    } catch (e) { alert('Import failed: ' + e); }
    input.value = '';
}

// --- Formatting ---
function fmt(n) {
    if (n == null || isNaN(n)) return '—';
    return Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtP(n) {
    if (n == null || isNaN(n) || n === 0) return '—';
    return Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}
function fmtN(n) {
    if (n == null || isNaN(n)) return '—';
    return Number(n).toLocaleString('en-US', { maximumFractionDigits: 2 });
}
function fmtPnl(n) {
    if (n == null || isNaN(n)) return '—';
    const prefix = n >= 0 ? '+' : '';
    return prefix + fmt(n);
}
function clsPnl(n) {
    if (n == null || n === 0) return '';
    return n > 0 ? 'pos' : 'neg';
}
function getCSS(v) {
    return getComputedStyle(document.documentElement).getPropertyValue(v).trim();
}

// --- Plotly layouts ---
function baseLayout() {
    return {
        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
        font: { color: getCSS('--text'), size: 11, family: getCSS('--font') },
        margin: { t: 20, b: 40, l: 60, r: 20 },
    };
}
function chartLayout(yTitle, zeroline) {
    return {
        ...baseLayout(),
        xaxis: { color: getCSS('--text2'), gridcolor: getCSS('--border'), showgrid: false },
        yaxis: { color: getCSS('--text2'), gridcolor: getCSS('--border'), title: yTitle,
            zeroline: zeroline, zerolinecolor: getCSS('--border') },
        showlegend: true, legend: { x: 0, y: 1.1, orientation: 'h', font: { size: 11 } },
    };
}

// --- WebSocket ---
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    try {
        const ws = new WebSocket(`${proto}://${location.host}/ws/prices`);
        ws.onmessage = e => {
            const msg = JSON.parse(e.data);
            if (msg.type === 'positions' && msg.data) {
                state.positions = msg.data;
                renderHoldings();
            }
        };
        ws.onclose = () => setTimeout(connectWS, 5000);
    } catch { setTimeout(connectWS, 5000); }
}
connectWS();
