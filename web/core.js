// 計算核心：與 Python 版 asset_tracker 相同的估值、質押、槓桿與建議規則。
// 純函式、無 DOM 相依，瀏覽器與 Node（測試）都可載入。
(function (root) {
  "use strict";

  const DEFAULT_STRATEGY = {
    target_equity_leverage: 1.3,
    max_equity_leverage: 1.6,
    maintenance_call: 1.3,
    maintenance_warn: 1.66,
    maintenance_safe: 2.0,
    bond_ratio_min: 0.15,
    bond_ratio_max: 0.4,
    max_single_position: 0.4,
    benchmark: "0050",
    trend_ma_days: 60,
  };

  const LEVELS = { 緊急: 5, 避險: 4, 減碼: 3, 分散: 2, 加碼: 1, 維持: 0 };
  const STRESS_DROPS = [0.1, 0.2, 0.3, 0.4];
  const DAY_MS = 86400000;

  const num = (v, d = 0) => {
    const n = typeof v === "number" ? v : parseFloat(String(v ?? "").replace(/,/g, ""));
    return Number.isFinite(n) ? n : d;
  };
  const sym = (s) => String(s || "").trim().toUpperCase();

  function emptyState() {
    return {
      version: 1,
      cash: 0,
      usd_twd: null,
      strategy: { ...DEFAULT_STRATEGY },
      holdings: [],
      loans: [],
      prices: {},
      price_history: {},
      history: [],
    };
  }

  // 正規化匯入的資料（來自手機備份或 Python 版 export-json）
  function normalize(raw) {
    const s = emptyState();
    if (!raw || typeof raw !== "object") return s;
    s.cash = num(raw.cash);
    s.usd_twd = raw.usd_twd ? num(raw.usd_twd) : null;
    s.strategy = { ...DEFAULT_STRATEGY };
    for (const k of Object.keys(DEFAULT_STRATEGY)) {
      if (raw.strategy && raw.strategy[k] !== undefined) {
        s.strategy[k] = k === "benchmark" ? sym(raw.strategy[k]) : num(raw.strategy[k], DEFAULT_STRATEGY[k]);
      }
    }
    s.holdings = (raw.holdings || []).map((h) => ({
      symbol: sym(h.symbol),
      name: h.name || "",
      category: ["stock", "trust", "bond_etf"].includes(h.category) ? h.category : "stock",
      market: ["AUTO", "TWSE", "TPEX", "US", "MANUAL"].includes(sym(h.market)) ? sym(h.market) : "AUTO",
      shares: num(h.shares),
      cost: num(h.cost),
      exposure: num(h.exposure, 1),
      dividends: num(h.dividends),
      company_match: num(h.company_match),
    }));
    s.loans = (raw.loans || []).map((l, i) => withDraws({
      id: String(l.id || `loan-${i + 1}`),
      name: l.name || "",
      principal: num(l.principal),
      annual_rate: num(l.annual_rate),
      start_date: String(l.start_date || "").slice(0, 10),
      draws: (l.draws || [])
        .map((d) => ({ date: String(d.date || "").slice(0, 10), amount: num(d.amount) }))
        .filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d.date) && d.amount > 0),
      interest_paid: num(l.interest_paid),
      realized_pnl: num(l.realized_pnl),
      collateral: (l.collateral || []).map((c) => ({ symbol: sym(c.symbol), shares: num(c.shares) })),
      purchases: (l.purchases || []).map((p) => ({
        symbol: sym(p.symbol),
        shares: num(p.shares),
        cost: num(p.cost),
        dividends: num(p.dividends),
      })),
    }));
    // prices 可為 {SYM: 數字} 或 {SYM: {price, date, source}}
    for (const [k, v] of Object.entries(raw.prices || {})) {
      const p = typeof v === "object" && v ? v : { price: v };
      if (num(p.price) > 0) s.prices[sym(k)] = { price: num(p.price), date: p.date || null, source: p.source || "manual" };
    }
    for (const [k, v] of Object.entries(raw.price_history || {})) {
      if (Array.isArray(v)) s.price_history[sym(k)] = v.filter((r) => Array.isArray(r) && r.length === 2);
    }
    s.history = Array.isArray(raw.history) ? raw.history : [];
    return s;
  }

  // 同一個借款帳戶可分多次撥款：有撥款紀錄時，本金＝撥款合計、起借日＝最早撥款日；
  // 沒有撥款紀錄的舊資料，視為在起借日一次撥款
  function withDraws(l) {
    if (l.draws.length) {
      l.draws.sort((a, b) => (a.date < b.date ? -1 : 1));
      l.principal = l.draws.reduce((a, d) => a + d.amount, 0);
      l.start_date = l.draws[0].date;
    } else if (l.principal > 0 && l.start_date) {
      l.draws = [{ date: l.start_date, amount: l.principal }];
    }
    return l;
  }

  function daysBetween(start, asOf) {
    const a = Date.parse(start + "T00:00:00Z");
    const b = Date.parse(asOf + "T00:00:00Z");
    if (!Number.isFinite(a) || !Number.isFinite(b)) return 0;
    return Math.max(0, Math.round((b - a) / DAY_MS));
  }

  // asOf: "YYYY-MM-DD"
  function evaluate(state, asOf) {
    const warnings = [];
    // 手動設定的匯率優先，否則用每日價格檔附帶的匯率
    const usdTwd = state.usd_twd || state.prices.USDTWD?.price || null;
    const priceOf = {};
    const positions = state.holdings.map((h) => {
      const p = state.prices[h.symbol];
      let price;
      let stale = false;
      if (!p) {
        warnings.push(`${h.symbol} 沒有價格，暫以成本估值`);
        price = h.shares ? h.cost / h.shares : 0;
      } else if (h.market === "US") {
        if (!usdTwd) {
          warnings.push(`${h.symbol} 為美股但沒有 USD/TWD 匯率，暫以成本估值`);
          price = h.shares ? h.cost / h.shares : 0;
        } else {
          price = p.price * usdTwd;
        }
        stale = !!(p.date && p.date < asOf);
      } else {
        price = p.price;
        stale = !!(p.date && p.date < asOf);
      }
      priceOf[h.symbol] = price;
      const mv = h.shares * price;
      return {
        h,
        price,
        stale,
        mv,
        unrealized: mv - h.cost + h.dividends,
        returnPct: h.cost ? (mv - h.cost + h.dividends) / h.cost : null,
        equityExposure: h.category === "bond_etf" ? 0 : mv * h.exposure,
      };
    });

    const stockShares = {};
    for (const h of state.holdings) if (h.category === "stock") stockShares[h.symbol] = (stockShares[h.symbol] || 0) + h.shares;
    const pledged = {};

    const loans = state.loans.map((l) => {
      const days = daysBetween(l.start_date, asOf);
      const accrued = l.draws.reduce((a, d) => a + (d.amount * l.annual_rate * daysBetween(d.date, asOf)) / 365, 0);
      let coll = 0;
      for (const c of l.collateral) {
        pledged[c.symbol] = (pledged[c.symbol] || 0) + c.shares;
        const p = priceOf[c.symbol] ?? state.prices[c.symbol]?.price;
        if (p === undefined) warnings.push(`質押 ${l.id} 的擔保品 ${c.symbol} 沒有價格，維持率不含此檔`);
        else coll += c.shares * p;
      }
      let fundedValue = 0;
      for (const pu of l.purchases) {
        const p = priceOf[pu.symbol] ?? state.prices[pu.symbol]?.price;
        if (p === undefined) {
          warnings.push(`質押 ${l.id} 買進的 ${pu.symbol} 沒有價格，暫以成本估值`);
          fundedValue += pu.cost;
        } else fundedValue += pu.shares * p;
      }
      const fundedCost = l.purchases.reduce((a, p) => a + p.cost, 0);
      const fundedDiv = l.purchases.reduce((a, p) => a + p.dividends, 0);
      const unpaid = Math.max(0, accrued - l.interest_paid);
      const maintenance = l.principal > 0 ? coll / l.principal : null;
      const fundedPnl = fundedValue - fundedCost + fundedDiv;
      return {
        l,
        days,
        interestAccrued: accrued,
        interestUnpaid: unpaid,
        liability: l.principal + unpaid,
        collateralValue: coll,
        maintenance,
        fundedValue,
        fundedCost,
        fundedPnl,
        pledgePnl: fundedPnl + l.realized_pnl - accrued,
        dropTo(ratio) {
          if (l.principal <= 0 || coll <= 0) return null;
          return 1 - (ratio * l.principal) / coll;
        },
      };
    });

    for (const [s, shares] of Object.entries(pledged)) {
      const held = stockShares[s] || 0;
      if (shares > held) warnings.push(`${s} 質押 ${fmt(shares)} 股，但正股只登記 ${fmt(held)} 股，請確認`);
    }
    for (const p of positions) if (p.stale) warnings.push(`${p.h.symbol} 的價格不是今天的（${state.prices[p.h.symbol].date}）`);

    const holdingsValue = positions.reduce((a, p) => a + p.mv, 0);
    const gross = holdingsValue + state.cash;
    const liabilities = loans.reduce((a, l) => a + l.liability, 0);
    const net = gross - liabilities;
    const equityExposure = positions.reduce((a, p) => a + p.equityExposure, 0);
    const bondValue = positions.filter((p) => p.h.category === "bond_etf").reduce((a, p) => a + p.mv, 0);
    const maints = loans.map((l) => l.maintenance).filter((m) => m !== null);

    const byCategory = {};
    for (const p of positions) byCategory[p.h.category] = (byCategory[p.h.category] || 0) + p.mv;

    const expBySym = {};
    for (const p of positions) if (p.equityExposure) expBySym[p.h.symbol] = (expBySym[p.h.symbol] || 0) + p.equityExposure;
    const concentration =
      net > 0 ? Object.entries(expBySym).map(([s, v]) => [s, v / net]).sort((a, b) => b[1] - a[1]) : [];

    const stress = STRESS_DROPS.map((drop) => {
      const netAfter = net - equityExposure * drop;
      const mm = loans.filter((l) => l.l.principal > 0).map((l) => (l.collateralValue * (1 - drop)) / l.l.principal);
      return {
        drop,
        net: netAfter,
        netChangePct: net > 0 ? netAfter / net - 1 : null,
        minMaintenance: mm.length ? Math.min(...mm) : null,
      };
    });

    return {
      asOf,
      positions,
      loans,
      cash: state.cash,
      warnings,
      gross,
      liabilities,
      net,
      equityExposure,
      bondValue,
      assetLeverage: net > 0 ? gross / net : null,
      equityLeverage: net > 0 ? equityExposure / net : null,
      bondRatio: gross > 0 ? bondValue / gross : 0,
      minMaintenance: maints.length ? Math.min(...maints) : null,
      pledgePnl: loans.reduce((a, l) => a + l.pledgePnl, 0),
      totalUnrealized: positions.reduce((a, p) => a + p.unrealized, 0),
      byCategory,
      concentration,
      stress,
    };
  }

  function computeTrend(symbol, history, maDays) {
    const closes = history.map((r) => r[1]);
    if (closes.length < maDays) return { symbol, price: closes.at(-1) ?? null, ma: null, days: closes.length, known: false, up: false };
    const w = closes.slice(-maDays);
    const ma = w.reduce((a, b) => a + b, 0) / maDays;
    const price = w.at(-1);
    return { symbol, price, ma, days: closes.length, known: true, up: price >= ma };
  }

  const pct = (v, d = 1) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
  const fmt = (v) => Math.round(v).toLocaleString("en-US");

  function advise(s, st, trend) {
    const out = [];
    const net = s.net;
    if (net <= 0) {
      out.push({ level: "緊急", title: "淨值為負", reason: "負債已超過總資產，請立即聯絡券商處理還款或補擔保品。" });
      return { verdict: "緊急", advice: out };
    }

    for (const l of s.loans) {
      const m = l.maintenance;
      if (m === null) continue;
      const coll = l.collateralValue;
      const principal = l.l.principal;
      const repay = Math.max(0, principal - coll / st.maintenance_safe);
      const addColl = Math.max(0, st.maintenance_safe * principal - coll);
      const name = l.l.name || l.l.id;
      if (m < st.maintenance_call) {
        out.push({
          level: "緊急",
          title: `${name} 維持率 ${pct(m, 0)} 已低於追繳線 ${pct(st.maintenance_call, 0)}`,
          reason: `需還款約 ${fmt(repay)} 元或補擔保品市值約 ${fmt(addColl)} 元才能回到安全線 ${pct(st.maintenance_safe, 0)}。請先向券商確認實際追繳金額與期限。`,
          amount: repay,
        });
      } else if (m < st.maintenance_warn) {
        out.push({
          level: "避險",
          title: `${name} 維持率 ${pct(m, 0)} 低於警戒線 ${pct(st.maintenance_warn, 0)}`,
          reason: `擔保品再跌 ${pct(l.dropTo(st.maintenance_call))} 就會被追繳。建議還款約 ${fmt(repay)} 元（或補擔保品約 ${fmt(addColl)} 元）回到 ${pct(st.maintenance_safe, 0)}。`,
          amount: repay,
        });
      }
    }

    const lev = s.equityLeverage || 0;
    if (lev > st.max_equity_leverage) {
      const reduce = (lev - st.target_equity_leverage) * net;
      out.push({
        level: "減碼",
        title: `股票等效槓桿 ${lev.toFixed(2)}x 超過上限 ${st.max_equity_leverage.toFixed(2)}x`,
        reason: `建議降低股票曝險約 ${fmt(reduce)} 元（賣出後優先償還質押借款），回到目標 ${st.target_equity_leverage.toFixed(2)}x。`,
        amount: reduce,
      });
    } else if (trend.known && !trend.up && lev > st.target_equity_leverage) {
      const reduce = (lev - st.target_equity_leverage) * net;
      out.push({
        level: "減碼",
        title: `${trend.symbol} 跌破 ${st.trend_ma_days} 日均線，且槓桿 ${lev.toFixed(2)}x 高於目標`,
        reason: `趨勢轉弱時建議把槓桿降回 ${st.target_equity_leverage.toFixed(2)}x，約減少曝險 ${fmt(reduce)} 元。`,
        amount: reduce,
      });
    }

    if (s.bondRatio < st.bond_ratio_min) {
      const need = st.bond_ratio_min * s.gross - s.bondValue;
      out.push({
        level: "避險",
        title: `債券 ETF 比重 ${pct(s.bondRatio)} 低於下限 ${pct(st.bond_ratio_min, 0)}`,
        reason: `可用現金或部分股票轉入美債 ETF 約 ${fmt(need)} 元。注意：股債並非永遠負相關（例如 2022 年同跌），且美債 ETF 有匯率風險。`,
        amount: need,
      });
    }

    for (const [symbol, ratio] of s.concentration) {
      if (ratio > st.max_single_position) {
        const excess = (ratio - st.max_single_position) * net;
        out.push({
          level: "分散",
          title: `${symbol} 曝險佔淨值 ${pct(ratio, 0)}，超過單一上限 ${pct(st.max_single_position, 0)}`,
          reason: `超出約 ${fmt(excess)} 元。若同時是質押擔保品，下跌時損益與維持率會一起惡化。`,
          amount: excess,
        });
      }
    }

    const risky = out.some((a) => LEVELS[a.level] >= LEVELS["分散"]);
    const maintOk = s.minMaintenance === null || s.minMaintenance >= st.maintenance_safe;
    if (!risky && lev < st.target_equity_leverage) {
      const gap = (st.target_equity_leverage - lev) * net;
      if (!trend.known) {
        out.push({
          level: "維持",
          title: "槓桿低於目標，但趨勢資料不足",
          reason: `${trend.symbol} 只有 ${trend.days} 天價格，需要 ${st.trend_ma_days} 天才能判斷趨勢。每天更新一次股價就會累積。`,
        });
      } else if (!trend.up) {
        out.push({
          level: "維持",
          title: `槓桿低於目標，但 ${trend.symbol} 在 ${st.trend_ma_days} 日均線之下`,
          reason: "趨勢未轉強前不建議增加借款加碼，可用現金分批買進。",
        });
      } else if (maintOk) {
        const room = s.loans.reduce((a, l) => a + Math.max(0, l.collateralValue / st.maintenance_safe - l.l.principal), 0);
        const amount = s.loans.length ? Math.min(gap, room) : gap;
        out.push({
          level: "加碼",
          title: `${trend.symbol} 站上 ${st.trend_ma_days} 日均線，槓桿 ${lev.toFixed(2)}x 低於目標 ${st.target_equity_leverage.toFixed(2)}x`,
          reason:
            `可加碼約 ${fmt(amount)} 元` +
            (s.loans.length ? `（維持率安全線限制下最多可再借 ${fmt(room)} 元）` : "") +
            "。建議分批進場，並注意新增借款會增加利息成本。",
          amount,
        });
      }
    }

    if (!out.length) out.push({ level: "維持", title: "各項指標都在目標區間內", reason: "不需要調整，持續每日追蹤即可。" });
    out.sort((a, b) => LEVELS[b.level] - LEVELS[a.level]);
    return { verdict: out[0].level, advice: out };
  }

  // 每個部位拆成：自有未質押、自有質押中、用借款買進（與 Python dashboard.funding_splits 相同）
  function fundingSplits(s) {
    const purchased = {};
    const pledged = {};
    for (const l of s.loans) {
      for (const p of l.l.purchases) purchased[p.symbol] = (purchased[p.symbol] || 0) + p.shares;
      for (const c of l.l.collateral) pledged[c.symbol] = (pledged[c.symbol] || 0) + c.shares;
    }
    const rows = [...s.positions]
      .sort((a, b) => b.mv - a.mv)
      .map((p) => {
        const h = p.h;
        let b = 0;
        let q = 0;
        if (h.category !== "trust") {
          b = Math.min(purchased[h.symbol] || 0, h.shares);
          delete purchased[h.symbol];
          q = Math.min(pledged[h.symbol] || 0, h.shares - b);
          delete pledged[h.symbol];
        }
        return {
          symbol: h.symbol,
          label: h.name && h.name !== h.symbol ? `${h.symbol} ${h.name}` : h.symbol,
          category: h.category,
          ownFree: (h.shares - b - q) * p.price,
          ownPledged: q * p.price,
          borrowed: b * p.price,
        };
      });
    if (s.cash) {
      const unspent = s.loans.reduce((a, l) => a + Math.max(0, l.l.principal - l.l.purchases.reduce((x, p) => x + p.cost, 0)), 0);
      const bc = Math.min(s.cash, unspent);
      rows.push({ symbol: "CASH", label: "現金", category: "cash", ownFree: s.cash - bc, ownPledged: 0, borrowed: bc });
    }
    for (const r of rows) r.total = r.ownFree + r.ownPledged + r.borrowed;
    return rows;
  }

  // 證交所 / 櫃買 OpenAPI 全市場收盤表
  function parseRocDate(v) {
    const m = String(v || "").trim().match(/^(\d{2,3})\/?(\d{2})\/?(\d{2})$/);
    if (!m) return null;
    const y = +m[1] + 1911;
    return `${y}-${m[2]}-${m[3]}`;
  }

  function parseMarketTable(rows) {
    const prices = {};
    let date = null;
    for (const r of rows || []) {
      const code = r.Code ?? r.SecuritiesCompanyCode;
      const close = num(r.ClosingPrice ?? r.Close, NaN);
      if (code && close > 0) prices[sym(code)] = close;
      if (!date) date = parseRocDate(r.Date);
    }
    return { prices, date };
  }

  // 從 prices.json（GitHub Actions 每日產生）取出需要的價格，依各持股的 market 選擇上市／上櫃／美股表
  function quotesFromFeed(feed, state) {
    const twse = feed.twse || {};
    const tpex = feed.tpex || {};
    const us = feed.us || {};
    const pick = (symbol, market) => {
      if (market === "TWSE") return twse[symbol];
      if (market === "TPEX") return tpex[symbol];
      if (market === "US") return us[symbol];
      return twse[symbol] ?? tpex[symbol];
    };
    const wanted = new Map();
    for (const h of state.holdings) if (h.market !== "MANUAL") wanted.set(h.symbol, h.market);
    for (const l of state.loans) for (const r of [...l.collateral, ...l.purchases]) if (!wanted.has(r.symbol)) wanted.set(r.symbol, "AUTO");
    if (!wanted.has(state.strategy.benchmark)) wanted.set(state.strategy.benchmark, "AUTO");
    const dates = feed.dates || {};
    const dateOf = (src) => dates[src] || feed.date || null;
    const hits = {};
    const missing = [];
    for (const [symbol, market] of wanted) {
      let src;
      if (market === "TWSE" || market === "TPEX" || market === "US") src = market.toLowerCase();
      else src = twse[symbol] !== undefined ? "twse" : "tpex";
      const v = num(pick(symbol, market), NaN);
      if (v > 0) hits[symbol] = { price: v, date: dateOf(src) };
      else missing.push(symbol);
    }
    return { hits, missing, date: feed.date || null, usdTwd: num(feed.usd_twd, NaN) > 0 ? num(feed.usd_twd) : null, usdDate: dateOf("us") };
  }

  function snapshot(s, verdict) {
    return {
      date: s.asOf,
      gross: s.gross,
      liabilities: s.liabilities,
      net: s.net,
      assetLeverage: s.assetLeverage,
      equityLeverage: s.equityLeverage,
      bondRatio: s.bondRatio,
      minMaintenance: s.minMaintenance,
      pledgePnl: s.pledgePnl,
      verdict,
    };
  }

  const api = {
    DEFAULT_STRATEGY,
    LEVELS,
    emptyState,
    normalize,
    evaluate,
    computeTrend,
    advise,
    fundingSplits,
    parseMarketTable,
    parseRocDate,
    quotesFromFeed,
    snapshot,
    daysBetween,
    withDraws,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Core = api;
})(typeof self !== "undefined" ? self : this);
