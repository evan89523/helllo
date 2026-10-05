// 由 Python 測試呼叫：檢查 web/core.js 的訂閱計算，失敗時以非 0 結束。
const assert = require("assert");
const C = require("../web/core.js");
const subs = C.normalizeSubs([
  { id: "a", name: "月卡", amount: 150, unit: "month", every: 1, start_date: "2026-01-31" },
  { id: "b", name: "年費", amount: 1200, unit: "year", every: 1, start_date: "2025-12-01" },
  { id: "c", name: "US", amount: 10, currency: "USD", unit: "month", every: 1, start_date: "2026-09-10" },
  { id: "d", name: "雙週", amount: 70, unit: "week", every: 2, start_date: "2026-09-28" },
  { id: "e", name: "暫停", amount: 999, unit: "month", every: 1, start_date: "2026-01-01", active: false },
  { id: "f", name: "", amount: 1, start_date: "2026-01-01" }, // 沒名稱會被濾掉
]);
assert.strictEqual(subs.length, 5);
assert.deepStrictEqual([0, 1, 2, 3].map((k) => C.nthBilling(subs[0], k)), ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]);
assert.strictEqual(C.nextBilling(subs[0], "2026-10-05"), "2026-10-31");
assert.strictEqual(C.nextBilling(subs[1], "2026-10-05"), "2026-12-01");
assert.strictEqual(C.nextBilling(subs[3], "2026-10-05"), "2026-10-12");
assert.strictEqual(C.nextBilling(subs[0], "2026-10-31"), "2026-10-31"); // 當天就是扣款日
const s = C.subsSummary(subs, "2026-10-05", 32);
assert.ok(Math.abs(s.monthly - (150 + 100 + 320 + (70 * 52) / 12 / 2)) < 1e-9);
assert.strictEqual(s.activeCount, 4);
assert.deepStrictEqual(s.upcoming.map((r) => r.sub.id), ["c", "d", "a"]);
assert.strictEqual(C.subsSummary(subs, "2026-10-05", null).missingRate, true);
const ics = C.buildIcs(subs, "2026-10-05", 32);
assert.strictEqual((ics.match(/BEGIN:VEVENT/g) || []).length, 4);
assert.ok(ics.includes("RRULE:FREQ=MONTHLY;INTERVAL=1;BYMONTHDAY=28,29,30,31;BYSETPOS=-1"));
assert.ok(ics.includes("RRULE:FREQ=WEEKLY;INTERVAL=2"));
assert.ok(ics.includes("TRIGGER:-P1D"));
console.log("ok");
