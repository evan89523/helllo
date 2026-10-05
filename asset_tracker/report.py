"""產生每日報告（Markdown，終端機可直接閱讀）。"""

from __future__ import annotations

from .advisor import Advice, Trend
from .config import Strategy
from .portfolio import Summary

CATEGORY_NAMES = {"stock": "正股", "trust": "持股信託", "bond_etf": "美債 ETF"}


def _money(v: float) -> str:
    return f"{v:,.0f}"


def _pct(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}%}"


def _x(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}x"


def render(summary: Summary, strategy: Strategy, trend: Trend, verdict: str, advice: list[Advice]) -> str:
    s = summary
    lines: list[str] = []
    add = lines.append

    add(f"# 資產日報 {s.as_of.isoformat()}")
    add("")
    add(f"**今日建議：{verdict}**")
    add("")

    add("## 總覽")
    add("")
    add("| 項目 | 數值 |")
    add("|---|---:|")
    add(f"| 總資產 | {_money(s.gross_assets)} |")
    add(f"| 負債（質押本金＋未繳利息） | {_money(s.liabilities)} |")
    add(f"| 淨值 | {_money(s.net_equity)} |")
    add(f"| 資產槓桿（總資產 / 淨值） | {_x(s.asset_leverage)} |")
    add(f"| 股票等效槓桿（股票曝險 / 淨值） | {_x(s.equity_leverage)}（目標 {strategy.target_equity_leverage:.2f}x，上限 {strategy.max_equity_leverage:.2f}x） |")
    add(f"| 債券比重 | {_pct(s.bond_ratio)}（區間 {strategy.bond_ratio_min:.0%}–{strategy.bond_ratio_max:.0%}） |")
    add(f"| 最低維持率 | {_pct(s.min_maintenance, 0)}（追繳線 {strategy.maintenance_call:.0%}） |")
    add(f"| 質押損益（扣利息） | {_money(s.pledge_pnl)} |")
    add(f"| 全部未實現損益 | {_money(s.total_unrealized)} |")
    if trend.known:
        state = "之上" if trend.up else "之下"
        add(f"| 趨勢 {trend.symbol} | {trend.price:,.2f}，{strategy.trend_ma_days} 日均線 {trend.ma:,.2f}（{state}） |")
    else:
        add(f"| 趨勢 {trend.symbol} | 資料不足（{trend.days}/{strategy.trend_ma_days} 天） |")
    add("")

    add("## 資產配置")
    add("")
    add("| 類別 | 市值 | 佔總資產 |")
    add("|---|---:|---:|")
    gross = s.gross_assets or 1
    for cat, value in s.by_category().items():
        add(f"| {CATEGORY_NAMES.get(cat, cat)} | {_money(value)} | {_pct(value / gross)} |")
    if s.cash:
        add(f"| 現金 | {_money(s.cash)} | {_pct(s.cash / gross)} |")
    add("")

    add("## 持股明細")
    add("")
    add("| 類別 | 代號 | 名稱 | 股數 | 價格 | 市值 | 成本 | 損益 | 報酬率 |")
    add("|---|---|---|---:|---:|---:|---:|---:|---:|")
    for p in sorted(s.positions, key=lambda p: -p.market_value):
        h = p.holding
        price = f"{p.price:,.2f}" + ("*" if p.stale else "")
        add(
            f"| {CATEGORY_NAMES.get(h.category, h.category)} | {h.symbol} | {h.name} | {h.shares:,.0f} | {price} "
            f"| {_money(p.market_value)} | {_money(h.cost)} | {_money(p.unrealized)} | {_pct(p.return_pct)} |"
        )
    trusts = [p for p in s.positions if p.holding.category == "trust" and p.holding.company_match]
    for p in trusts:
        h = p.holding
        add("")
        add(f"持股信託 {h.symbol}：自提成本 {_money(h.cost)}，公司獎勵金 {_money(h.company_match)}，"
            f"含獎勵金總報酬 {_money(p.unrealized)}（獎勵金不計入成本）。")
    add("")

    if s.loans:
        add("## 質押借款")
        add("")
        add("| 借款 | 本金 | 年利率 | 天數 | 累積利息 | 擔保品市值 | 維持率 | 跌多少追繳 | 借款部位損益 | 質押損益 |")
        add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for l in s.loans:
            drop = l.drop_to(strategy.maintenance_call)
            add(
                f"| {l.loan.name or l.loan.id} | {_money(l.loan.principal)} | {l.loan.annual_rate:.2%} | {l.days} "
                f"| {_money(l.interest_accrued)} | {_money(l.collateral_value)} | {_pct(l.maintenance, 0)} "
                f"| {_pct(drop)} | {_money(l.funded_pnl)} | {_money(l.pledge_pnl)} |"
            )
        add("")
        add("質押損益 = 用借款買進部位的損益（含股利）＋已實現損益 − 累積利息。"
            "要賺錢，借款部位的年化報酬必須高於借款利率。")
        add("")

    add("## 壓力測試（股票同步下跌，債券不變）")
    add("")
    add("| 股票下跌 | 淨值 | 淨值變化 | 最低維持率 |")
    add("|---:|---:|---:|---:|")
    for row in s.stress():
        mm = row["min_maintenance"]
        flag = " ⚠追繳" if mm is not None and mm < strategy.maintenance_call else ""
        add(f"| {row['drop']:.0%} | {_money(row['net_equity'])} | {_pct(row['net_change_pct'])} | {_pct(mm, 0)}{flag} |")
    add("")

    add("## 建議")
    add("")
    for a in advice:
        amount = f"（約 {_money(a.amount)} 元）" if a.amount else ""
        add(f"- **[{a.level}] {a.title}**{amount}")
        add(f"  {a.reason}")
    add("")

    if s.warnings:
        add("## 注意")
        add("")
        for w in s.warnings:
            add(f"- {w}")
        add("")

    add("---")
    add("本報告由規則引擎依你設定的門檻自動產生，不是投資顧問意見，也不保證獲利。"
        "實際維持率與追繳金額以券商通知為準。")
    return "\n".join(lines) + "\n"
