// 介面層：資料只存在這支手機的瀏覽器儲存空間（localStorage），不會上傳。
(function () {
  "use strict";
  const C = window.Core;
  const APP_VERSION = 4;

  // 手機上的快取若混到舊版 core.js，清掉快取並重新載入一次（避免按鈕沒反應）
  if (!C || C.VERSION !== APP_VERSION) {
    const KEY_RELOAD = "asset-tracker-reloaded";
    let tried = false;
    try {
      tried = sessionStorage.getItem(KEY_RELOAD) === String(APP_VERSION);
      sessionStorage.setItem(KEY_RELOAD, String(APP_VERSION));
    } catch (e) {
      /* 私密模式等情況 */
    }
    if (!tried) {
      const clear = window.caches ? caches.keys().then((ks) => Promise.all(ks.map((k) => caches.delete(k)))) : Promise.resolve();
      clear.finally(() => location.reload());
      return;
    }
    document.body.insertAdjacentHTML(
      "afterbegin",
      '<div style="padding:12px 16px;background:#fab219;color:#0b0b0b;font-size:14px">App 版本不一致，請把 App 完全關掉再重新打開。</div>'
    );
  }
  const KEY = "asset-tracker-v1";
  const TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL";
  const TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes";
  const HISTORY_KEEP = 400;
  const CAT = { stock: "正股", trust: "持股信託", bond_etf: "美債 ETF", cash: "交割戶" };
  const MARKETS = { AUTO: "自動（上市／上櫃）", TWSE: "上市", TPEX: "上櫃", US: "美股（手動輸入美元價格）", MANUAL: "手動" };

  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fmt = (v) => (v === null || v === undefined || !Number.isFinite(v) ? "—" : Math.round(v).toLocaleString("en-US"));
  const wan = (v) => (Math.abs(v) >= 10000 ? `${(v / 10000).toLocaleString("en-US", { maximumFractionDigits: 1, minimumFractionDigits: 1 })} 萬` : fmt(v));
  const pct = (v, d = 1) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
  const x2 = (v) => (v === null || v === undefined ? "—" : `${v.toFixed(2)}x`);
  const signed = (v) => `${v >= 0 ? "+" : ""}${fmt(v)}`;

  function today() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  // ---------- 儲存 ----------
  let state = load();
  let tab = "overview";

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) return C.normalize(JSON.parse(raw));
    } catch (e) {
      console.warn(e);
    }
    return C.emptyState();
  }
  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(state));
      return true;
    } catch (e) {
      toast("無法儲存到手機（可能是私密瀏覽模式），請記得匯出備份");
      return false;
    }
  }
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 3200);
  }

  function setPrice(symbol, price, date, source) {
    symbol = symbol.toUpperCase();
    state.prices[symbol] = { price, date, source };
    const hist = (state.price_history[symbol] = state.price_history[symbol] || []);
    const i = hist.findIndex((r) => r[0] === date);
    if (i >= 0) hist[i] = [date, price];
    else {
      hist.push([date, price]);
      hist.sort((a, b) => (a[0] < b[0] ? -1 : 1));
    }
    if (hist.length > HISTORY_KEEP) hist.splice(0, hist.length - HISTORY_KEEP);
  }

  // ---------- 計算 ----------
  function compute() {
    const asOf = today();
    const s = C.evaluate(state, asOf);
    const st = state.strategy;
    const trend = C.computeTrend(st.benchmark, state.price_history[st.benchmark] || [], st.trend_ma_days);
    const { verdict, advice } = C.advise(s, st, trend);
    if (state.holdings.length) {
      const snap = C.snapshot(s, verdict);
      const i = state.history.findIndex((h) => h.date === snap.date);
      if (i >= 0) state.history[i] = snap;
      else state.history.push(snap);
      if (state.history.length > HISTORY_KEEP) state.history.splice(0, state.history.length - HISTORY_KEEP);
      save();
    }
    return { s, st, trend, verdict, advice };
  }

  function statusOf(m, st) {
    if (m === null) return ["st-none", "–", "無借款"];
    if (m < st.maintenance_call) return ["st-critical", "✕", "已低於追繳線"];
    if (m < st.maintenance_warn) return ["st-serious", "!", "警戒"];
    if (m < st.maintenance_safe) return ["st-warning", "△", "注意"];
    return ["st-good", "✓", "安全"];
  }

  function seg(cls, value, total, label) {
    if (value <= 0 || total <= 0) return "";
    const w = (value / total) * 100;
    return `<div class="seg ${cls}" style="flex-basis:${w.toFixed(3)}%">${label ? `<span class="seg-label">${esc(label)}</span>` : ""}</div>`;
  }

  function fitLabels() {
    document.querySelectorAll(".seg-label").forEach((l) => {
      l.style.visibility = "";
      if (l.offsetWidth > l.parentElement.clientWidth - 16) l.style.visibility = "hidden";
    });
  }

  // ---------- 總覽 ----------
  function renderOverview() {
    if (!state.holdings.length) return renderWelcome();
    const { s, st, trend, verdict, advice } = compute();
    const own = Math.max(s.net, 0);
    const debt = s.liabilities;
    const debtRatio = s.gross > 0 ? debt / s.gross : 0;
    const splits = C.fundingSplits(s);
    const maxTotal = Math.max(1, ...splits.map((r) => r.total));
    const lev = s.equityLeverage;

    const rows = splits
      .map((r) => {
        const tags = [];
        if (r.borrowed > 0) tags.push(`<span class="tag tag-borrowed">借款 ${pct(r.borrowed / r.total, 0)}</span>`);
        if (r.ownPledged > 0) tags.push(`<span class="tag tag-own">質押 ${pct(r.ownPledged / r.total, 0)}</span>`);
        return `<div class="row"><div class="row-head"><div><div class="row-name">${esc(r.label)}</div>
          <div class="s">${CAT[r.category] || ""}</div></div>
          <div class="row-val">${fmt(r.total)}<br>${tags.join("") || '<span class="tag tag-own">全部自有</span>'}</div></div>
          <div class="bar thin" role="img" aria-label="${esc(r.label)}：自有 ${fmt(r.ownFree)}，質押中 ${fmt(r.ownPledged)}，借款買進 ${fmt(r.borrowed)}">
          ${seg("own", r.ownFree, maxTotal)}${seg("own pledged", r.ownPledged, maxTotal)}${seg("borrowed", r.borrowed, maxTotal)}</div></div>`;
      })
      .join("");

    const loanCards = s.loans
      .map((l) => {
        const [cls, ico, txt] = statusOf(l.maintenance, st);
        return `<div class="card"><div class="verdict"><h3>${esc(l.l.name || l.l.id)}</h3>
          <span class="status ${cls}"><span class="ico">${ico}</span>${pct(l.maintenance, 0)} ${txt}</span></div>
          <dl><dt>借了</dt><dd class="borrowed-ink">${fmt(l.l.principal)}</dd>
          <dt>擔保品市值</dt><dd>${fmt(l.collateralValue)}</dd>
          <dt>再跌多少會追繳</dt><dd>${pct(l.dropTo(st.maintenance_call))}</dd>
          <dt>累積利息（${l.days} 天）</dt><dd>${fmt(l.interestAccrued)}</dd>
          <dt>質押損益（扣利息）</dt><dd class="${l.pledgePnl >= 0 ? "pos" : "neg"}">${signed(l.pledgePnl)}</dd></dl></div>`;
      })
      .join("");

    const adviceHtml = advice
      .map((a) => `<li><span class="adv adv-${esc(a.level)}">${esc(a.level)}</span><div><strong>${esc(a.title)}</strong><p>${esc(a.reason)}</p></div></li>`)
      .join("");

    const stressRows = s.stress
      .map((r) => {
        const flag = r.minMaintenance !== null && r.minMaintenance < st.maintenance_call ? " ⚠ 追繳" : "";
        return `<tr><td>股票跌 ${pct(r.drop, 0)}</td><td>${fmt(r.net)}</td><td>${pct(r.netChangePct)}</td><td>${pct(r.minMaintenance, 0)}${flag}</td></tr>`;
      })
      .join("");

    const warn = s.warnings.length ? `<h2>注意</h2><div class="card small"><ul style="margin:0;padding-left:18px">${s.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";

    $("#view").innerHTML = `
      <div class="card verdict"><div><div class="k">今日建議</div><div class="v">${esc(verdict)}</div></div>
        <span class="adv adv-${esc(verdict)}">${esc(advice[0].title)}</span></div>

      <div class="card">
        <div class="k">總資產</div><div class="v big">${fmt(s.gross)}</div>
        <div class="bar" role="img" aria-label="我的錢 ${fmt(own)} 元，借來的錢 ${fmt(debt)} 元">
          ${seg("own", own, s.gross, `我的 ${wan(own)}`)}${seg("borrowed", debt, s.gross, `借的 ${wan(debt)}`)}</div>
        <div class="grid2" style="margin-top:12px">
          <div><div class="k"><i class="sw own"></i>我的錢（淨值）</div><div class="v">${fmt(s.net)}</div></div>
          <div><div class="k"><i class="sw borrowed"></i>借來的錢</div><div class="v borrowed-ink">${fmt(debt)}</div>
            <div class="s">佔總資產 ${pct(debtRatio)}</div></div>
        </div>
      </div>

      <div class="grid2">
        <div class="card"><div class="k">股票等效槓桿</div><div class="v">${x2(lev)}</div><div class="s">目標 ${st.target_equity_leverage.toFixed(2)}x・上限 ${st.max_equity_leverage.toFixed(2)}x</div></div>
        <div class="card"><div class="k">最低維持率</div><div class="v">${pct(s.minMaintenance, 0)}</div><div class="s">追繳線 ${pct(st.maintenance_call, 0)}</div></div>
        <div class="card"><div class="k">質押損益（扣利息）</div><div class="v ${s.pledgePnl >= 0 ? "pos" : "neg"}">${signed(s.pledgePnl)}</div><div class="s">借錢投資到目前賺／賠</div></div>
        <div class="card"><div class="k">債券比重</div><div class="v">${pct(s.bondRatio)}</div>
          <div class="s">${trend.known ? `${esc(trend.symbol)} ${trend.up ? "站上" : "跌破"} ${st.trend_ma_days} 日線` : `趨勢資料 ${trend.days}/${st.trend_ma_days} 天`}</div></div>
      </div>

      <h2>每一筆資產是用誰的錢買的</h2>
      <div class="card">
        <div class="legend" style="margin:0 0 10px"><span><i class="sw own"></i>自有資金</span><span><i class="sw pledged"></i>自有・質押中</span><span><i class="sw borrowed"></i>借款買的（現值）</span></div>
        ${rows}
      </div>

      ${loanCards ? `<h2>借款</h2>${loanCards}` : ""}

      <h2>建議</h2>
      <div class="card"><ul class="advice">${adviceHtml}</ul></div>

      <h2>壓力測試</h2>
      <div class="card table-wrap"><table><thead><tr><th>情境</th><th>淨值</th><th>變化</th><th>最低維持率</th></tr></thead><tbody>${stressRows}</tbody></table>
        <p class="s" style="margin:8px 0 0">假設所有股票同步下跌、債券不變；實際上股債可能同跌。</p></div>
      ${warn}
      <footer class="note">本頁依你設定的門檻自動計算，不是投資顧問意見；實際維持率與追繳金額以券商通知為準。資料只存在這支手機。</footer>`;
    requestAnimationFrame(fitLabels);
  }

  function renderWelcome() {
    $("#view").innerHTML = `
      <div class="card">
        <h3>開始使用</h3>
        <p class="muted small">資料只會存在這支手機，不會上傳到任何地方。</p>
        <div class="btn-row">
          <button class="btn primary" data-act="add-holding">新增第一筆持股</button>
          <button class="btn" data-act="import">匯入備份 JSON</button>
          <button class="btn" data-act="demo">載入範例資料看看</button>
        </div>
      </div>
      ${installCard()}`;
  }

  function installCard() {
    const standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;
    if (standalone) return "";
    return `<div class="card install small"><strong>加到 iPhone 主畫面</strong>
      <ol style="margin:6px 0 0;padding-left:20px"><li>用 Safari 開啟這個網頁</li><li>點下方的「分享」按鈕</li><li>選「加入主畫面」</li></ol>
      <p class="muted" style="margin:6px 0 0">之後從主畫面圖示打開，就會像 App 一樣全螢幕使用。</p></div>`;
  }

  // ---------- 持股 ----------
  function renderHoldings() {
    const groups = { stock: [], trust: [], bond_etf: [] };
    state.holdings.forEach((h, i) => groups[h.category].push([h, i]));
    const section = (cat) => {
      const items = groups[cat];
      if (!items.length) return "";
      return `<h2>${CAT[cat]}</h2><div class="card">${items
        .map(([h, i]) => {
          const p = state.prices[h.symbol];
          const priceTxt = p ? `${p.price.toLocaleString("en-US")}${h.market === "US" ? " USD" : ""}・${p.date || ""}` : "尚無價格";
          return `<div class="list-item" data-act="edit-holding" data-i="${i}"><div><div class="row-name">${esc(h.symbol)} ${esc(h.name)}</div>
            <div class="s">${fmt(h.shares)} 股・成本 ${fmt(h.cost)}・${esc(priceTxt)}</div></div><span class="chev">›</span></div>`;
        })
        .join("")}</div>`;
    };
    $("#view").innerHTML = `
      <div class="btn-row" style="margin-top:0"><button class="btn primary" data-act="add-holding">＋ 新增持股</button>
        <button class="btn" data-act="prices">手動輸入價格</button></div>
      ${state.holdings.length ? section("stock") + section("trust") + section("bond_etf") : '<div class="empty">還沒有持股</div>'}
      <div class="card small" style="margin-top:16px"><div class="k">交割戶現金</div><div class="v">${fmt(state.cash)}</div>
        <div class="btn-row"><button class="btn" data-act="edit-cash">修改現金</button></div></div>`;
  }

  // ---------- 借款 ----------
  function renderLoans() {
    const items = state.loans
      .map((l, i) => {
        const coll = l.collateral.map((c) => `${c.symbol} ${fmt(c.shares)} 股`).join("、") || "未登記";
        const buy = l.purchases.map((p) => `${p.symbol} ${fmt(p.shares)} 股`).join("、") || "未登記";
        return `<div class="list-item" data-act="edit-loan" data-i="${i}"><div><div class="row-name">${esc(l.name || l.id)}</div>
          <div class="s">本金 ${fmt(l.principal)}・年利率 ${pct(l.annual_rate, 2)}・${l.draws.length > 1 ? `${l.draws.length} 次撥款，` : ""}${esc(l.start_date)} 起</div>
          <div class="s">質押：${esc(coll)}</div><div class="s">買了：${esc(buy)}</div></div><span class="chev">›</span></div>`;
      })
      .join("");
    $("#view").innerHTML = `
      <div class="btn-row" style="margin-top:0"><button class="btn primary" data-act="add-loan">＋ 新增質押借款</button></div>
      ${items ? `<div class="card" style="margin-top:12px">${items}</div>` : '<div class="empty">沒有質押借款</div>'}
      <p class="s">維持率 = 擔保品市值 ÷ 借款本金。「買了」用來計算質押損益，以及總覽中哪些資產是用借來的錢買的。</p>`;
  }

  // ---------- 設定 ----------
  const STRAT_FIELDS = [
    ["target_equity_leverage", "股票等效槓桿目標（倍）"],
    ["max_equity_leverage", "股票等效槓桿上限（倍）"],
    ["maintenance_call", "追繳線（1.30 = 130%）"],
    ["maintenance_warn", "警戒線"],
    ["maintenance_safe", "安全線（加碼借款不會低於此）"],
    ["bond_ratio_min", "債券比重下限（0.15 = 15%）"],
    ["bond_ratio_max", "債券比重上限"],
    ["max_single_position", "單一股票佔淨值上限"],
    ["benchmark", "大盤趨勢標的"],
    ["trend_ma_days", "均線天數"],
  ];

  function renderSettings() {
    const hist = [...state.history].slice(-30).reverse();
    const histRows = hist
      .map((h) => `<tr><td>${esc(h.date)}</td><td>${fmt(h.net)}</td><td>${fmt(h.liabilities)}</td><td>${x2(h.equityLeverage)}</td><td>${pct(h.minMaintenance, 0)}</td><td>${fmt(h.pledgePnl)}</td><td>${esc(h.verdict)}</td></tr>`)
      .join("");
    $("#view").innerHTML = `
      <h2 style="margin-top:4px">策略門檻</h2>
      <form class="card" id="stratForm">
        ${STRAT_FIELDS.map(([k, label]) => `<div class="field"><label for="s-${k}">${label}</label>
          <input id="s-${k}" name="${k}" ${k === "benchmark" ? 'autocapitalize="characters"' : 'inputmode="decimal"'} value="${esc(state.strategy[k])}"></div>`).join("")}
        <div class="field"><label for="s-usd">USD/TWD 匯率（持有美股 ETF 時需要）</label><input id="s-usd" name="usd_twd" inputmode="decimal" value="${esc(state.usd_twd ?? "")}"></div>
        <button class="btn primary block" type="submit">儲存</button>
      </form>

      <h2>每日紀錄</h2>
      <div class="card table-wrap">${histRows ? `<table><thead><tr><th>日期</th><th>淨值</th><th>借款</th><th>槓桿</th><th>維持率</th><th>質押損益</th><th>建議</th></tr></thead><tbody>${histRows}</tbody></table>` : '<div class="empty">打開總覽後會自動記錄當天數字</div>'}</div>

      <h2>備份與同步</h2>
      <div class="card">
        <p class="small muted" style="margin-top:0">資料只存在這支手機的瀏覽器裡。刪除 App、清除 Safari 網站資料時會一起消失，請定期匯出備份。
        電腦版可用 <code>python -m asset_tracker export-json</code> 產生 JSON 再匯入這裡。</p>
        <div class="btn-row"><button class="btn" data-act="export">匯出備份</button><button class="btn" data-act="import">匯入 JSON</button>
          <button class="btn" data-act="paste">貼上 JSON</button></div>
      </div>
      ${installCard()}
      <div class="card"><button class="btn danger block" data-act="reset">清除這支手機上的所有資料</button></div>
      <input type="file" id="fileIn" accept="application/json,.json" hidden>`;
    $("#stratForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const next = { ...state.strategy };
      for (const [k] of STRAT_FIELDS) {
        const v = f.get(k);
        next[k] = k === "benchmark" ? String(v).trim().toUpperCase() : parseFloat(v);
        if (k !== "benchmark" && !Number.isFinite(next[k])) return toast(`「${STRAT_FIELDS.find((x) => x[0] === k)[1]}」不是數字`);
      }
      if (!(next.maintenance_call < next.maintenance_warn && next.maintenance_warn <= next.maintenance_safe))
        return toast("需要：追繳線 < 警戒線 ≤ 安全線");
      next.trend_ma_days = Math.max(1, Math.round(next.trend_ma_days));
      state.strategy = next;
      const usd = parseFloat(f.get("usd_twd"));
      state.usd_twd = Number.isFinite(usd) && usd > 0 ? usd : null;
      save();
      toast("已儲存");
    });
  }

  // ---------- 編輯面板 ----------
  function openSheet(title, bodyHtml, onSave, onDelete) {
    const sheet = $("#sheet");
    sheet.innerHTML = `<div class="sheet-head"><button class="btn" type="button" data-sheet="cancel">取消</button>
      <h3 id="sheetTitle">${esc(title)}</h3><button class="btn primary" type="button" data-sheet="save">儲存</button></div>
      <form id="sheetForm" novalidate>${bodyHtml}</form>
      ${onDelete ? '<button class="btn danger block" type="button" data-sheet="delete" style="margin-top:12px">刪除</button>' : ""}`;
    $("#sheetBack").style.display = "block";
    requestAnimationFrame(() => sheet.classList.add("open"));
    sheet.onclick = (e) => {
      const sub = e.target.closest("[data-sub]");
      if (sub) {
        if (sub.dataset.sub === "remove") sub.closest(".sub-row").remove();
        if (sub.dataset.sub === "add") {
          const kind = sub.dataset.kind;
          const wrap = document.createElement("div");
          wrap.innerHTML = subRowsHtml(kind, [kind === "draws" ? { date: today(), amount: "" } : { symbol: "", shares: "", cost: "" }]);
          $(`#rows-${kind}`).appendChild(wrap.querySelector(".sub-row"));
        }
        return;
      }
      const b = e.target.closest("[data-sheet]");
      if (!b) return;
      const act = b.dataset.sheet;
      if (act === "cancel") closeSheet();
      if (act === "save") {
        const err = onSave($("#sheetForm"));
        if (err) toast(err);
        else {
          save();
          closeSheet();
          render();
        }
      }
      if (act === "delete" && confirm("確定刪除？")) {
        onDelete();
        save();
        closeSheet();
        render();
      }
    };
    $("#sheetForm").addEventListener("submit", (e) => e.preventDefault());
  }
  function closeSheet() {
    $("#sheet").classList.remove("open");
    $("#sheetBack").style.display = "none";
  }
  $("#sheetBack").addEventListener("click", closeSheet);

  const field = (name, label, value, opts = {}) => {
    const { type = "text", hint = "", mode = "decimal", options } = opts;
    const id = `f-${name}`;
    if (options) {
      return `<div class="field"><label for="${id}">${label}</label><select id="${id}" name="${name}">${Object.entries(options)
        .map(([v, t]) => `<option value="${v}" ${v === value ? "selected" : ""}>${esc(t)}</option>`)
        .join("")}</select>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
    }
    const im = type === "text" && mode ? `inputmode="${mode}"` : "";
    return `<div class="field"><label for="${id}">${label}</label><input id="${id}" name="${name}" type="${type}" ${im}
      value="${esc(value ?? "")}" autocomplete="off">${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  };
  const numOf = (form, name, req) => {
    const raw = String(form.elements[name]?.value ?? "").replace(/,/g, "").trim();
    if (raw === "") return req ? NaN : 0;
    return parseFloat(raw);
  };

  function editHolding(i) {
    const isNew = i === undefined;
    const h = isNew ? { symbol: "", name: "", category: "stock", market: "AUTO", shares: 0, cost: 0, exposure: 1, dividends: 0, company_match: 0 } : state.holdings[i];
    const p = state.prices[h.symbol];
    openSheet(
      isNew ? "新增持股" : `${h.symbol} ${h.name}`,
      field("symbol", "代號", h.symbol, { mode: "text", hint: "例如 2330、00679B、TLT" }) +
        field("name", "名稱", h.name, { mode: "text" }) +
        field("category", "類別", h.category, { options: { stock: "正股", trust: "持股信託", bond_etf: "美債 ETF" } }) +
        field("market", "價格來源", h.market, { options: MARKETS }) +
        field("shares", "股數", h.shares || "") +
        field("cost", "總成本（台幣，含手續費）", h.cost || "", { hint: "持股信託填自己提撥的金額" }) +
        field("price", "目前價格（留空＝用自動更新的價格）", "", { hint: p ? `現在記錄的價格：${p.price}（${p.date || "未知日期"}）` : "尚無價格" }) +
        field("dividends", "累積已領股利", h.dividends || "") +
        field("company_match", "公司獎勵金（持股信託）", h.company_match || "", { hint: "不算成本，會反映在報酬中" }) +
        field("exposure", "曝險倍數", h.exposure, { hint: "一般股票 1；槓桿 ETF（如 00631L）填 2" }),
      (form) => {
        const symbol = form.elements.symbol.value.trim().toUpperCase();
        if (!symbol) return "請輸入代號";
        const next = {
          symbol,
          name: form.elements.name.value.trim(),
          category: form.elements.category.value,
          market: form.elements.market.value,
          shares: numOf(form, "shares", true),
          cost: numOf(form, "cost"),
          exposure: numOf(form, "exposure") || 1,
          dividends: numOf(form, "dividends"),
          company_match: numOf(form, "company_match"),
        };
        if (!(next.shares >= 0)) return "股數必須是數字";
        if ([next.cost, next.dividends, next.company_match].some((v) => !Number.isFinite(v))) return "金額欄位必須是數字";
        const dup = state.holdings.findIndex((x, j) => j !== i && x.symbol === symbol && x.category === next.category);
        if (dup >= 0) return "同類別已經有這個代號";
        const price = numOf(form, "price");
        if (!Number.isFinite(price)) return "價格必須是數字";
        if (price > 0) setPrice(symbol, price, today(), "manual");
        if (isNew) state.holdings.push(next);
        else state.holdings[i] = next;
      },
      isNew ? null : () => state.holdings.splice(i, 1)
    );
  }

  function editPrices() {
    const symbols = [...new Set([...state.holdings.map((h) => h.symbol), state.strategy.benchmark])];
    openSheet(
      "手動輸入今日價格",
      `<p class="small muted" style="margin-top:0">留空的不會變更。美股請輸入美元價格。</p>` +
        symbols.map((s) => field(`p_${s}`, s, "", { hint: state.prices[s] ? `目前 ${state.prices[s].price}（${state.prices[s].date || ""}）` : "尚無價格" })).join(""),
      (form) => {
        const d = today();
        for (const s of symbols) {
          const v = numOf(form, `p_${s}`);
          if (!Number.isFinite(v)) return `${s} 的價格不是數字`;
          if (v > 0) setPrice(s, v, d, "manual");
        }
      }
    );
  }

  function subRowsHtml(kind, rows) {
    if (kind === "draws") {
      const inner = rows
        .map(
          (r) => `<div class="sub-row two" data-kind="draws">
          <input type="date" value="${esc(r.date)}" data-k="date" aria-label="撥款日">
          <input placeholder="金額" inputmode="decimal" value="${esc(r.amount || "")}" data-k="amount" aria-label="金額">
          <button type="button" class="x" data-sub="remove" aria-label="移除">×</button></div>`
        )
        .join("");
      return `<div class="sub-rows" id="rows-draws">${inner}</div><button type="button" class="btn" data-sub="add" data-kind="draws">＋ 新增一次撥款</button>`;
    }
    const two = kind === "collateral";
    const inner = rows
      .map(
        (r) => `<div class="sub-row ${two ? "two" : ""}" data-kind="${kind}">
        <input placeholder="代號" value="${esc(r.symbol)}" data-k="symbol" autocapitalize="characters" aria-label="代號">
        <input placeholder="股數" inputmode="decimal" value="${esc(r.shares || "")}" data-k="shares" aria-label="股數">
        ${two ? "" : `<input placeholder="成本" inputmode="decimal" value="${esc(r.cost || "")}" data-k="cost" aria-label="成本">`}
        <button type="button" class="x" data-sub="remove" aria-label="移除">×</button></div>`
      )
      .join("");
    return `<div class="sub-rows" id="rows-${kind}">${inner}</div><button type="button" class="btn" data-sub="add" data-kind="${kind}">＋ 新增一列</button>`;
  }

  function editLoan(i) {
    const isNew = i === undefined;
    const l = isNew
      ? { id: `loan-${Date.now().toString(36)}`, name: "", principal: 0, annual_rate: 0.025, start_date: today(), interest_paid: 0, realized_pnl: 0, draws: [{ date: today(), amount: "" }], collateral: [{ symbol: "", shares: 0 }], purchases: [] }
      : state.loans[i];
    openSheet(
      isNew ? "新增質押借款" : l.name || l.id,
      field("name", "名稱", l.name, { mode: "text", hint: "例如：國泰不限用途借貸" }) +
        field("annual_rate", "年利率（%）", l.annual_rate ? +(l.annual_rate * 100).toFixed(4) : "", { hint: "例如 3.92" }) +
        `<fieldset><legend>撥款紀錄（日期、金額）</legend>${subRowsHtml("draws", l.draws)}
          <p class="s">分好幾次借的，每次撥款各記一列；利息從各自的撥款日起算，本金＝合計。</p></fieldset>` +
        field("interest_paid", "已繳利息", l.interest_paid || "", { hint: "未繳的利息會計入負債" }) +
        field("realized_pnl", "已實現損益", l.realized_pnl || "", { hint: "用這筆借款買賣已經實現的賺賠" }) +
        `<fieldset><legend>質押了哪些股票（擔保品）</legend>${subRowsHtml("collateral", l.collateral)}</fieldset>` +
        `<fieldset><legend>用這筆錢買了什麼</legend>${subRowsHtml("purchases", l.purchases)}
          <p class="s">買進的股票也要另外在「持股」中登記。</p></fieldset>`,
      (form) => {
        const rate = numOf(form, "annual_rate", true);
        if (!(rate >= 0)) return "請輸入年利率";
        const draws = [...document.querySelectorAll("#rows-draws .sub-row")]
          .map((row) => ({
            date: row.querySelector('[data-k="date"]').value,
            amount: parseFloat(String(row.querySelector('[data-k="amount"]').value || "").replace(/,/g, "")),
          }))
          .filter((d) => d.date || Number.isFinite(d.amount));
        if (!draws.length) return "請至少輸入一次撥款";
        if (draws.some((d) => !/^\d{4}-\d{2}-\d{2}$/.test(d.date) || !(d.amount > 0))) return "每次撥款都要有日期和大於 0 的金額";
        const read = (kind) =>
          [...document.querySelectorAll(`#rows-${kind} .sub-row`)]
            .map((row) => {
              const g = (k) => row.querySelector(`[data-k="${k}"]`);
              const n = (k) => parseFloat(String(g(k)?.value || "").replace(/,/g, ""));
              return { symbol: g("symbol").value.trim().toUpperCase(), shares: n("shares"), cost: n("cost"), dividends: 0 };
            })
            .filter((r) => r.symbol);
        const coll = read("collateral");
        const buys = read("purchases");
        if (coll.some((r) => !(r.shares > 0))) return "擔保品的股數要大於 0";
        if (buys.some((r) => !(r.shares > 0) || !(r.cost >= 0))) return "買進的股數與成本要填數字";
        const prevBuys = isNew ? [] : l.purchases;
        const next = C.withDraws({
          id: l.id,
          name: form.elements.name.value.trim(),
          principal: 0,
          annual_rate: rate / 100,
          start_date: "",
          draws,
          interest_paid: numOf(form, "interest_paid") || 0,
          realized_pnl: numOf(form, "realized_pnl") || 0,
          collateral: coll.map(({ symbol, shares }) => ({ symbol, shares })),
          purchases: buys.map((b) => ({ ...b, dividends: prevBuys.find((p) => p.symbol === b.symbol)?.dividends || 0 })),
        });
        if (isNew) state.loans.push(next);
        else state.loans[i] = next;
      },
      isNew ? null : () => state.loans.splice(i, 1)
    );
  }

  function editCash() {
    openSheet("交割戶現金", field("cash", "金額", state.cash || ""), (form) => {
      const v = numOf(form, "cash");
      if (!Number.isFinite(v)) return "請輸入數字";
      state.cash = v;
    });
  }

  // ---------- 股價更新 ----------
  async function fetchJson(url) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 15000);
    try {
      const r = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } finally {
      clearTimeout(t);
    }
  }

  async function refreshPrices() {
    const btn = $("#refresh");
    btn.disabled = true;
    btn.textContent = "更新中…";
    try {
      const msg = (await refreshFromFeed()) || (await refreshDirect());
      save();
      toast(msg);
    } catch (e) {
      console.error(e);
      toast(`更新失敗：${e && e.message ? e.message : e}。請把 App 完全關掉再重新打開`);
    } finally {
      btn.disabled = false;
      btn.textContent = "更新股價";
      render();
    }
  }

  // 1. 先讀同一個網站上的 prices.json（GitHub Actions 每個交易日自動更新）
  async function refreshFromFeed() {
    let feed;
    try {
      feed = await fetchJson(`prices.json?t=${Date.now()}`);
    } catch (e) {
      return null;
    }
    const { hits, missing, date, usdTwd, usdDate } = C.quotesFromFeed(feed, state);
    const d = date || today();
    for (const [s, q] of Object.entries(hits)) setPrice(s, q.price, q.date || d, "feed");
    if (usdTwd) state.prices.USDTWD = { price: usdTwd, date: usdDate || d, source: "feed" };
    // 預先抓好的大盤歷史價格：補進手機上的價格歷史，讓均線判斷馬上可用
    for (const [s, rows] of Object.entries(feed.history || {})) for (const [hd, hp] of rows) if (hp > 0) mergeHistory(s, hd, hp);
    const n = Object.keys(hits).length;
    const when = d === today() ? "今日" : `${d} `;
    const old = Object.entries(hits).filter(([, q]) => q.date && q.date < d).map(([s]) => s);
    if (n && old.length) return `已更新 ${n} 檔；${old.join("、")} 的價格來源今天抓取失敗，暫用較舊的收盤價`;
    if (!n) return `價格檔（${d}）裡找不到你的持股代號，請到「持股」手動輸入`;
    return missing.length ? `已更新 ${when}收盤價 ${n} 檔；找不到：${missing.join("、")}，請手動輸入` : `已更新 ${when}收盤價 ${n} 檔`;
  }

  function mergeHistory(symbol, date, price) {
    const hist = (state.price_history[symbol] = state.price_history[symbol] || []);
    if (hist.some((r) => r[0] === date)) return;
    hist.push([date, price]);
    hist.sort((a, b) => (a[0] < b[0] ? -1 : 1));
    if (hist.length > HISTORY_KEEP) hist.splice(0, hist.length - HISTORY_KEEP);
  }

  // 2. 沒有價格檔時（例如在電腦本機測試），才直接連證交所／櫃買中心（可能被瀏覽器跨網域限制擋下）
  async function refreshDirect() {
    const tables = {};
    for (const [key, url] of [["twse", TWSE_URL], ["tpex", TPEX_URL]]) {
      try {
        tables[key] = C.parseMarketTable(await fetchJson(url));
      } catch (e) {
        /* 被擋或連不上 */
      }
    }
    if (!tables.twse && !tables.tpex) return "連不上價格來源，請到「持股」手動輸入價格";
    const feed = { twse: tables.twse?.prices, tpex: tables.tpex?.prices, dates: { twse: tables.twse?.date, tpex: tables.tpex?.date } };
    const { hits, missing } = C.quotesFromFeed(feed, state);
    for (const [s, q] of Object.entries(hits)) setPrice(s, q.price, q.date || today(), "auto");
    const n = Object.keys(hits).length;
    return missing.length ? `更新 ${n} 檔；找不到：${missing.join("、")}，請手動輸入` : `已更新 ${n} 檔收盤價`;
  }

  // ---------- 匯入匯出 ----------
  async function exportData() {
    const text = JSON.stringify(state, null, 2);
    const name = `asset-tracker-${today()}.json`;
    try {
      const file = new File([text], name, { type: "application/json" });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: "資產追蹤備份" });
        return;
      }
    } catch (e) {
      if (e && e.name === "AbortError") return;
    }
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: "application/json" }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  function importText(text) {
    let raw;
    try {
      raw = JSON.parse(text);
    } catch (e) {
      return toast("不是有效的 JSON");
    }
    const next = C.normalize(raw);
    if (!next.holdings.length && !next.loans.length) return toast("檔案裡沒有持股或借款資料");
    if (state.holdings.length && !confirm(`匯入會取代目前資料（${next.holdings.length} 筆持股、${next.loans.length} 筆借款），確定？`)) return;
    // 保留手機上已累積的價格歷史與每日紀錄
    for (const [k, v] of Object.entries(state.price_history)) if (!next.price_history[k]) next.price_history[k] = v;
    if (!next.history.length) next.history = state.history;
    state = next;
    save();
    toast("匯入完成");
    tab = "overview";
    render();
  }

  function pickFile() {
    let input = $("#fileIn");
    if (!input) {
      input = document.createElement("input");
      input.type = "file";
      input.accept = "application/json,.json";
      input.hidden = true;
      input.id = "fileIn";
      document.body.appendChild(input);
    }
    input.value = "";
    input.onchange = async () => {
      const f = input.files[0];
      if (f) importText(await f.text());
    };
    input.click();
  }

  function pasteJson() {
    openSheet("貼上 JSON", '<div class="field"><textarea name="json" rows="10" placeholder="{ ... }" style="min-height:200px"></textarea></div>', (form) => {
      const text = form.elements.json.value.trim();
      if (!text) return "請貼上內容";
      closeSheet();
      importText(text);
      return null;
    });
  }

  function loadDemo() {
    state = C.normalize({
      cash: 200000,
      holdings: [
        { symbol: "2330", name: "台積電", category: "stock", shares: 3000, cost: 2400000 },
        { symbol: "0050", name: "元大台灣50", category: "stock", shares: 10000, cost: 1500000, dividends: 30000 },
        { symbol: "2882", name: "國泰金（持股信託）", category: "trust", shares: 8000, cost: 360000, company_match: 120000 },
        { symbol: "00679B", name: "元大美債20年", category: "bond_etf", shares: 20000, cost: 600000, dividends: 15000 },
      ],
      loans: [
        {
          id: "pledge-1",
          name: "範例：不限用途借貸",
          principal: 1000000,
          annual_rate: 0.025,
          start_date: "2026-03-02",
          interest_paid: 8000,
          collateral: [{ symbol: "2330", shares: 2000 }],
          purchases: [
            { symbol: "0050", shares: 5000, cost: 800000, dividends: 15000 },
            { symbol: "00679B", shares: 7000, cost: 210000 },
          ],
        },
      ],
    });
    const d = today();
    for (const [s, p] of Object.entries({ "2330": 1000, "0050": 180, "2882": 60, "00679B": 30 })) setPrice(s, p, d, "demo");
    save();
    toast("已載入範例資料（數字都是假的）");
    render();
  }

  // ---------- 路由 ----------
  function render() {
    document.querySelectorAll("nav.tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    const titles = { overview: "我的錢與借來的錢", holdings: "持股", loans: "質押借款", settings: "設定" };
    $("#title").textContent = titles[tab];
    const dates = Object.values(state.prices).map((p) => p.date).filter(Boolean).sort();
    $("#asof").textContent = dates.length ? `價格日期 ${dates.at(-1)}` : "尚未更新價格";
    ({ overview: renderOverview, holdings: renderHoldings, loans: renderLoans, settings: renderSettings })[tab]();
  }

  document.querySelector("nav.tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    tab = b.dataset.tab;
    window.scrollTo(0, 0);
    render();
  });
  $("#view").addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const i = el.dataset.i === undefined ? undefined : +el.dataset.i;
    ({
      "add-holding": () => editHolding(),
      "edit-holding": () => editHolding(i),
      "add-loan": () => editLoan(),
      "edit-loan": () => editLoan(i),
      "edit-cash": editCash,
      prices: editPrices,
      export: exportData,
      import: pickFile,
      paste: pasteJson,
      demo: loadDemo,
      reset: () => {
        if (confirm("確定清除這支手機上的所有資料？建議先匯出備份。")) {
          state = C.emptyState();
          save();
          render();
        }
      },
    })[el.dataset.act]?.();
  });
  $("#refresh").addEventListener("click", refreshPrices);
  addEventListener("resize", fitLabels);

  if ("serviceWorker" in navigator && location.protocol === "https:") {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
  render();
})();
