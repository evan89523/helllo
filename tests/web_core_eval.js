// 由 Python 測試呼叫：讀入 export-json 的資料，輸出 JS 計算結果供比對。
const fs = require("fs");
const Core = require("../web/core.js");
const [file, asOf] = process.argv.slice(2);
const state = Core.normalize(JSON.parse(fs.readFileSync(file, "utf8")));
const s = Core.evaluate(state, asOf);
const st = state.strategy;
const trend = Core.computeTrend(st.benchmark, state.price_history[st.benchmark] || [], st.trend_ma_days);
const { verdict, advice } = Core.advise(s, st, trend);
const splits = Core.fundingSplits(s);
console.log(JSON.stringify({
  gross: s.gross, liabilities: s.liabilities, net: s.net,
  assetLeverage: s.assetLeverage, equityLeverage: s.equityLeverage, bondRatio: s.bondRatio,
  minMaintenance: s.minMaintenance, pledgePnl: s.pledgePnl,
  verdict, levels: advice.map((a) => a.level), amounts: advice.map((a) => a.amount ?? null),
  splits: splits.map((r) => [r.symbol, r.ownFree, r.ownPledged, r.borrowed]),
}));
