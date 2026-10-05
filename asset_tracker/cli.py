"""命令列介面：python -m asset_tracker <command>"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import date
from pathlib import Path

from . import prices as px
from .advisor import advise, compute_trend
from .brokers import cathay
from .config import ConfigError, Portfolio, load
from .dashboard import render_dashboard
from .portfolio import evaluate
from .report import render
from .storage import Store


def _parse_pairs(pairs: list[str] | None, what: str) -> dict[str, str]:
    out = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"{what} 格式應為 KEY=VALUE，收到 {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def gather_prices(portfolio: Portfolio, store: Store, as_of: date, offline: bool, overrides: dict[str, float]):
    by_market: dict[str, set[str]] = {}
    for h in portfolio.holdings:
        if h.market != "MANUAL":
            by_market.setdefault(h.market, set()).add(h.symbol)
    for loan in portfolio.loans:
        for sym in [c.symbol for c in loan.collateral] + [p.symbol for p in loan.purchases]:
            if not any(sym in s for s in by_market.values()):
                by_market.setdefault("AUTO", set()).add(sym)
    bench = portfolio.strategy.benchmark.upper()
    if not any(bench in s for s in by_market.values()):
        by_market.setdefault("AUTO", set()).add(bench)

    needs_usd = bool(by_market.get("US")) and portfolio.usd_twd is None
    quotes = px.Quotes() if offline else px.fetch_quotes(by_market, need_usd=needs_usd)
    trade_date = quotes.trade_date or as_of
    if quotes.prices:
        store.save_prices(trade_date, quotes.prices, "fetch")

    prices = dict(quotes.prices)
    prices.update(portfolio.manual_prices)
    prices.update(overrides)
    if overrides or portfolio.manual_prices:
        store.save_prices(trade_date, {**portfolio.manual_prices, **overrides}, "manual")

    stale: set[str] = set()
    wanted = set().union(*by_market.values()) if by_market else set()
    if needs_usd:
        wanted.add("TWD=X")
    for sym in wanted:
        if sym not in prices:
            last = store.last_price(sym, trade_date)
            if last:
                prices[sym] = last[1]
                stale.add(sym)

    usd_twd = portfolio.usd_twd or prices.get("TWD=X")
    return prices, stale, usd_twd, trade_date, quotes.errors


def run_report(args, save: bool) -> int:
    portfolio = load(args.config)
    store = Store(portfolio.db_path)
    try:
        today = date.fromisoformat(args.date) if args.date else date.today()
        overrides = {k.upper(): float(v) for k, v in _parse_pairs(args.price, "--price").items()}
        prices, stale, usd_twd, trade_date, errors = gather_prices(
            portfolio, store, today, args.offline, overrides
        )
        as_of = trade_date if not args.date else today
        summary = evaluate(portfolio, prices, as_of, stale=stale, usd_twd=usd_twd)
        summary.warnings = errors + summary.warnings

        st = portfolio.strategy
        history = store.price_history(st.benchmark.upper(), as_of, st.trend_ma_days)
        trend = compute_trend(st.benchmark.upper(), history, st.trend_ma_days)
        verdict, advice = advise(summary, st, trend)
        text = render(summary, st, trend, verdict, advice)
        page = render_dashboard(summary, st, trend, verdict, advice)
        portfolio.report_dir.mkdir(parents=True, exist_ok=True)
        dash = portfolio.report_dir / "dashboard.html"
        dash.write_text(page, encoding="utf-8")

        if save:
            store.save_snapshot(as_of, summary.to_dict(), verdict)
            out = portfolio.report_dir / f"{as_of.isoformat()}.md"
            out.write_text(text, encoding="utf-8")
            text += f"\n（報告已存到 {out}）\n"
        if getattr(args, "gui", False):
            print(f"儀表板：{dash.resolve()}")
            if not args.no_open:
                webbrowser.open(dash.resolve().as_uri())
        else:
            print(text + f"（儀表板：{dash}）\n")
        return 2 if verdict == "緊急" else 0
    finally:
        store.close()


def cmd_history(args) -> int:
    portfolio = load(args.config)
    store = Store(portfolio.db_path)
    try:
        rows = store.snapshots(args.n)
    finally:
        store.close()
    if not rows:
        print("還沒有任何快照，請先執行 daily。")
        return 0
    print("| 日期 | 總資產 | 負債 | 淨值 | 資產槓桿 | 股票槓桿 | 債券比重 | 最低維持率 | 質押損益 | 建議 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for d, gross, liab, net, alev, elev, bond, maint, pnl, verdict in rows:
        f = lambda v, spec: "—" if v is None else format(v, spec)  # noqa: E731
        print(
            f"| {d} | {gross:,.0f} | {liab:,.0f} | {net:,.0f} | {f(alev, '.2f')}x | {f(elev, '.2f')}x "
            f"| {f(bond, '.1%')} | {f(maint, '.0%')} | {f(pnl, ',.0f')} | {verdict} |"
        )
    return 0


def cmd_backfill(args) -> int:
    portfolio = load(args.config)
    store = Store(portfolio.db_path)
    symbols = {portfolio.strategy.benchmark.upper()}
    symbols |= {h.symbol for h in portfolio.holdings if h.market in ("AUTO", "TWSE")}
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(args.months):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    try:
        for sym in sorted(symbols):
            total = 0
            for y, m in months:
                try:
                    data = px.fetch_twse_month(sym, y, m)
                except Exception as e:  # noqa: BLE001
                    print(f"{sym} {y}-{m:02d} 略過：{e}")
                    continue
                for d, close in data.items():
                    store.save_prices(d, {sym: close}, "backfill")
                total += len(data)
            print(f"{sym}: 回補 {total} 筆")
    finally:
        store.close()
    print("註：回補只支援上市（證交所）標的；上櫃債券 ETF 會從每天執行 daily 開始累積。")
    return 0


def portfolio_to_json(portfolio: Portfolio, store: Store, history_days: int = 250) -> dict:
    """轉成手機版 App（web/）可以匯入的 JSON。"""
    symbols = {h.symbol for h in portfolio.holdings} | {portfolio.strategy.benchmark.upper()}
    for loan in portfolio.loans:
        symbols |= {c.symbol for c in loan.collateral} | {p.symbol for p in loan.purchases}
    prices = {}
    history = {}
    for sym in sorted(symbols):
        rows = store.price_rows(sym, history_days)
        if rows:
            prices[sym] = {"price": rows[-1][1], "date": rows[-1][0], "source": "desktop"}
            history[sym] = [list(r) for r in rows]
    for sym, price in portfolio.manual_prices.items():
        prices.setdefault(sym, {"price": price, "date": None, "source": "manual"})
    for h in portfolio.holdings:
        if h.market == "MANUAL" and h.price is not None:
            prices[h.symbol] = {"price": h.price, "date": None, "source": "manual"}
    return {
        "version": 1,
        "cash": portfolio.cash,
        "usd_twd": portfolio.usd_twd,
        "strategy": dict(vars(portfolio.strategy)),
        "holdings": [
            {k: v for k, v in vars(h).items() if k != "price"} for h in portfolio.holdings
        ],
        "loans": [
            {
                **{k: v for k, v in vars(l).items() if k not in ("collateral", "purchases", "start_date", "draws")},
                "start_date": l.start_date.isoformat(),
                "draws": [{"date": d.date.isoformat(), "amount": d.amount} for d in l.draws],
                "collateral": [vars(c) for c in l.collateral],
                "purchases": [vars(p) for p in l.purchases],
            }
            for l in portfolio.loans
        ],
        "prices": prices,
        "price_history": history,
        "history": [],
    }


def cmd_export_json(args) -> int:
    portfolio = load(args.config)
    store = Store(portfolio.db_path)
    try:
        data = portfolio_to_json(portfolio, store)
    finally:
        store.close()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已寫入 {args.out}，用 AirDrop／iCloud 雲碟傳到手機，在 App 的「設定 → 匯入 JSON」選擇此檔。")
        print("注意：這個檔案包含你的完整持股與借款資料，傳送後請刪除不需要的副本。")
    return 0


def cmd_import_cathay(args) -> int:
    mapping = {k: v for k, v in _parse_pairs(args.map, "--map").items()}
    try:
        holdings = cathay.parse(args.csv, mapping=mapping, lots=args.lots)
    except cathay.ImportError_ as e:
        print(f"匯入失敗：{e}", file=sys.stderr)
        return 1
    text = cathay.to_toml(holdings)
    out = Path(args.out)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"已寫入 {out}（{len(holdings)} 檔）。")
    print(f"請在 portfolio.toml 的 [settings] 加上 holdings_files = [\"{out.name}\"]，")
    print("並確認 category 判斷是否正確（00xxxB 會被視為債券 ETF）。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asset_tracker", description="個人投資資產追蹤：質押、信託、正股、美債 ETF")
    p.add_argument("-c", "--config", default="portfolio.toml", help="設定檔路徑（預設 portfolio.toml）")
    sub = p.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("daily", "抓收盤價、計算、存快照與報告（每天跑一次）"),
        ("report", "只計算並顯示，不存快照"),
        ("gui", "產生並開啟儀表板（我的錢 vs 借來的錢）"),
    ):
        sp = sub.add_parser(name, help=help_text)
        if name == "gui":
            sp.add_argument("--no-open", action="store_true", help="只產生 HTML，不自動開瀏覽器")
        sp.add_argument("--offline", action="store_true", help="不連網，用資料庫裡最後的價格")
        sp.add_argument("--price", action="append", metavar="SYMBOL=PRICE", help="手動指定價格，可重複")
        sp.add_argument("--date", help="計算日期 YYYY-MM-DD（預設今天）")

    sp = sub.add_parser("history", help="列出每日快照（槓桿、維持率走勢）")
    sp.add_argument("-n", type=int, default=30)

    sp = sub.add_parser("backfill", help="從證交所回補歷史收盤價（讓均線趨勢判斷可以馬上使用）")
    sp.add_argument("--months", type=int, default=4)

    sp = sub.add_parser("export-json", help="匯出成手機 App 可匯入的 JSON")
    sp.add_argument("--out", default="portfolio-export.json", help="輸出路徑，- 表示印到螢幕")

    sp = sub.add_parser("import-cathay", help="匯入國泰證券庫存 CSV")
    sp.add_argument("csv")
    sp.add_argument("--out", default="holdings_cathay.toml")
    sp.add_argument("--map", action="append", metavar="CSV欄位=symbol|name|shares|cost")
    sp.add_argument("--lots", action="store_true", help="CSV 的數量單位是「張」時加上，會乘以 1000")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daily":
            return run_report(args, save=True)
        if args.command == "report":
            return run_report(args, save=False)
        if args.command == "gui":
            args.gui = True
            return run_report(args, save=False)
        if args.command == "history":
            return cmd_history(args)
        if args.command == "backfill":
            return cmd_backfill(args)
        if args.command == "export-json":
            return cmd_export_json(args)
        if args.command == "import-cathay":
            return cmd_import_cathay(args)
    except ConfigError as e:
        print(f"設定檔錯誤：{e}", file=sys.stderr)
        return 1
    return 1
