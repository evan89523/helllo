"""產生單一 HTML 儀表板：一眼看出哪些錢是自己的、哪些是借來的。

輸出為完全離線的 HTML 檔（不載入任何外部資源），只存在本機，不會上傳。
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

from .advisor import Advice, Trend
from .config import Strategy
from .portfolio import Summary
from .report import CATEGORY_NAMES


@dataclass
class Split:
    """一個部位的市值拆成：自有未質押、自有質押中、用借款買進。"""

    label: str
    sublabel: str
    own_free: float
    own_pledged: float
    borrowed: float

    @property
    def total(self) -> float:
        return self.own_free + self.own_pledged + self.borrowed


def funding_splits(summary: Summary) -> list[Split]:
    """依 loans 的 purchases（借款買了什麼）與 collateral（質押了什麼）拆分每個部位。

    股數分配順序：先算借款買進的股數，再從剩下的股數算質押中，其餘為自有未質押。
    持股信託不能質押、也不會用借款買，全部算自有。
    """
    purchased: dict[str, float] = {}
    pledged: dict[str, float] = {}
    for l in summary.loans:
        for p in l.loan.purchases:
            purchased[p.symbol] = purchased.get(p.symbol, 0.0) + p.shares
        for c in l.loan.collateral:
            pledged[c.symbol] = pledged.get(c.symbol, 0.0) + c.shares

    splits: list[Split] = []
    for pos in sorted(summary.positions, key=lambda p: -p.market_value):
        h = pos.holding
        shares = h.shares
        if h.category == "trust":
            b = q = 0.0
        else:
            b = min(purchased.pop(h.symbol, 0.0), shares)
            q = min(pledged.pop(h.symbol, 0.0), shares - b)
        free = shares - b - q
        name = h.name or h.symbol
        splits.append(
            Split(
                label=name if name == h.symbol else f"{h.symbol} {name}",
                sublabel=CATEGORY_NAMES.get(h.category, h.category),
                own_free=free * pos.price,
                own_pledged=q * pos.price,
                borrowed=b * pos.price,
            )
        )

    if summary.cash:
        unspent = sum(max(0.0, l.loan.principal - sum(p.cost for p in l.loan.purchases)) for l in summary.loans)
        borrowed_cash = min(summary.cash, unspent)
        splits.append(Split("現金", "交割戶", summary.cash - borrowed_cash, 0.0, borrowed_cash))
    return splits


def _m(v: float) -> str:
    return f"{v:,.0f}"


def _wan(v: float) -> str:
    """以「萬」為單位的短標籤，給長條上的直接標示用。"""
    return f"{v / 10_000:,.1f} 萬" if abs(v) >= 10_000 else f"{v:,.0f}"


def _pct(v: float | None, d: int = 1) -> str:
    return "—" if v is None else f"{v:.{d}%}"


def _status(m: float | None, s: Strategy) -> tuple[str, str, str]:
    """回傳 (css class, 圖示, 文字)；狀態色一定搭配圖示與文字。"""
    if m is None:
        return "st-none", "–", "無借款"
    if m < s.maintenance_call:
        return "st-critical", "✕", "已低於追繳線"
    if m < s.maintenance_warn:
        return "st-serious", "!", "警戒"
    if m < s.maintenance_safe:
        return "st-warning", "△", "注意"
    return "st-good", "✓", "安全"


def _seg(cls: str, value: float, total: float, tip: str, label: str = "") -> str:
    if value <= 0 or total <= 0:
        return ""
    width = value / total * 100
    text = f'<span class="seg-label">{html.escape(label)}</span>' if label and width >= 12 else ""
    return (
        f'<div class="seg {cls}" style="flex-basis:{width:.4f}%" tabindex="0" '
        f'data-tip="{html.escape(tip)}">{text}</div>'
    )


def render_dashboard(summary: Summary, strategy: Strategy, trend: Trend, verdict: str, advice: list[Advice]) -> str:
    s = summary
    e = html.escape
    own = max(s.net_equity, 0.0)
    debt = s.liabilities
    gross = s.gross_assets
    debt_ratio = debt / gross if gross > 0 else 0.0

    # --- 主長條：總資產 = 我的錢 + 借來的錢 ---
    main_bar = (
        _seg("own", own, gross, f"我的錢（淨值）：{_m(own)} 元，佔總資產 {_pct(own / gross if gross else None)}",
             f"我的錢 {_wan(own)}")
        + _seg("borrowed", debt, gross, f"借來的錢（借款本金＋未繳利息）：{_m(debt)} 元，佔總資產 {_pct(debt_ratio)}",
               f"借來的 {_wan(debt)}")
    )

    # --- 各部位拆分 ---
    splits = funding_splits(s)
    max_total = max((x.total for x in splits), default=0.0) or 1.0
    rows = []
    for x in splits:
        segs = (
            _seg("own", x.own_free, max_total, f"{x.label}｜自有資金買的（未質押）：{_m(x.own_free)} 元")
            + _seg("own pledged", x.own_pledged, max_total, f"{x.label}｜自有、但已質押給券商：{_m(x.own_pledged)} 元")
            + _seg("borrowed", x.borrowed, max_total, f"{x.label}｜用借來的錢買的（現值）：{_m(x.borrowed)} 元")
        )
        borrowed_share = x.borrowed / x.total if x.total else 0
        tags = []
        if x.borrowed > 0:
            tags.append(f'<span class="tag tag-borrowed">借款 {borrowed_share:.0%}</span>')
        if x.own_pledged > 0:
            tags.append(f'<span class="tag tag-own">質押 {x.own_pledged / x.total:.0%}</span>')
        tag = "".join(tags) or '<span class="tag tag-own">全部自有</span>'
        rows.append(
            f'<div class="row"><div class="row-head"><div><div class="row-name">{e(x.label)}</div>'
            f'<div class="row-sub">{e(x.sublabel)}</div></div><div class="row-val">{_m(x.total)} {tag}</div></div>'
            f'<div class="bar small">{segs}</div></div>'
        )

    # --- 借款卡片 ---
    loan_cards = []
    for l in s.loans:
        cls, icon, text = _status(l.maintenance, strategy)
        drop = l.drop_to(strategy.maintenance_call)
        bought = "、".join(f"{p.symbol} {p.shares:,.0f} 股" for p in l.loan.purchases) or "未登記"
        coll = "、".join(f"{c.symbol} {c.shares:,.0f} 股" for c in l.loan.collateral) or "未登記"
        pnl_cls = "pos" if l.pledge_pnl >= 0 else "neg"
        loan_cards.append(f"""
<div class="card loan">
  <div class="loan-head"><h3>{e(l.loan.name or l.loan.id)}</h3>
    <span class="status {cls}"><span class="ico">{icon}</span>{_pct(l.maintenance, 0)} {text}</span></div>
  <dl>
    <dt>借了</dt><dd class="borrowed-ink">{_m(l.loan.principal)}</dd>
    <dt>年利率／已借天數</dt><dd>{l.loan.annual_rate:.2%}／{l.days} 天</dd>
    <dt>累積利息</dt><dd>{_m(l.interest_accrued)}</dd>
    <dt>拿來買了</dt><dd>{e(bought)}</dd>
    <dt>質押的股票（擔保品）</dt><dd>{e(coll)}，市值 {_m(l.collateral_value)}</dd>
    <dt>擔保品再跌多少會被追繳</dt><dd>{_pct(drop)}</dd>
    <dt>質押損益（扣利息後）</dt><dd class="{pnl_cls}">{'+' if l.pledge_pnl >= 0 else ''}{_m(l.pledge_pnl)}</dd>
  </dl>
</div>""")
    if not loan_cards:
        loan_cards.append('<div class="card"><p class="muted">目前沒有質押借款，全部都是你的錢。</p></div>')

    # --- 建議 ---
    advice_html = "".join(
        f'<li><span class="adv adv-{e(a.level)}">{e(a.level)}</span><div><strong>{e(a.title)}</strong>'
        f'{f"（約 {_m(a.amount)} 元）" if a.amount else ""}<p>{e(a.reason)}</p></div></li>'
        for a in advice
    )

    # --- 表格檢視（無障礙／精確數字） ---
    table_rows = "".join(
        f"<tr><td>{e(x.label)}</td><td>{e(x.sublabel)}</td><td>{_m(x.own_free)}</td>"
        f"<td>{_m(x.own_pledged)}</td><td>{_m(x.borrowed)}</td><td>{_m(x.total)}</td></tr>"
        for x in splits
    )

    lev = s.equity_leverage
    warn_html = "".join(f"<li>{e(w)}</li>" for w in s.warnings)
    trend_txt = (
        f"{e(trend.symbol)} {'站上' if trend.up else '跌破'} {strategy.trend_ma_days} 日均線"
        if trend.known else f"趨勢資料不足（{trend.days}/{strategy.trend_ma_days} 天）"
    )
    data_json = json.dumps(s.to_dict(), ensure_ascii=False).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>我的錢與借來的錢</title>
<style>
:root {{
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --ring: rgba(11,11,11,0.10);
  --own: #2a78d6; --own-dark: #1c5cab; --borrowed: #eb6834; --borrowed-ink: #b9441a;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --pos: #006300; --neg: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --ring: rgba(255,255,255,0.10);
    --own: #3987e5; --own-dark: #184f95; --borrowed: #d95926; --borrowed-ink: #f08a5d;
    --pos: #0ca30c; --neg: #e66767;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --ring: rgba(255,255,255,0.10);
  --own: #3987e5; --own-dark: #184f95; --borrowed: #d95926; --borrowed-ink: #f08a5d;
  --pos: #0ca30c; --neg: #e66767;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif; }}
main {{ max-width: 1040px; margin: 0 auto; padding: 24px 16px 48px; }}
header {{ display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }}
h1 {{ font-size: 22px; margin: 0; }}
h2 {{ font-size: 17px; margin: 32px 0 12px; }}
h3 {{ font-size: 16px; margin: 0; }}
.muted {{ color: var(--muted); }}
.card {{ background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 20px; }}
.hero {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 16px; }}
.hero .card {{ padding: 16px 20px; }}
.hero .k {{ color: var(--ink-2); font-size: 14px; display: flex; align-items: center; gap: 8px; }}
.hero .v {{ font-size: 30px; font-weight: 650; margin-top: 4px; }}
.hero .s {{ color: var(--muted); font-size: 13px; }}
.sw {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; flex: none; }}
.sw.own {{ background: var(--own); }}
.sw.borrowed {{ background: var(--borrowed); }}
.sw.pledged {{ background: var(--own); background-image: repeating-linear-gradient(45deg, transparent 0 3px, var(--own-dark) 3px 5px); }}
.bar {{ display: flex; gap: 2px; height: 44px; margin-top: 16px; }}
.bar.small {{ height: 18px; margin-top: 6px; }}
.seg {{ min-width: 2px; position: relative; display: flex; align-items: center; padding: 0 10px;
  color: #fff; font-weight: 600; font-size: 14px; white-space: nowrap; overflow: hidden; cursor: default; outline: none; }}
.seg:first-child {{ border-radius: 4px 0 0 4px; }}
.seg:last-child {{ border-radius: 0 4px 4px 0; }}
.seg:only-child {{ border-radius: 4px; }}
.seg.own {{ background: var(--own); }}
.seg.pledged {{ background-image: repeating-linear-gradient(45deg, transparent 0 4px, var(--own-dark) 4px 7px); }}
.seg.borrowed {{ background: var(--borrowed); }}
.seg:hover, .seg:focus-visible {{ filter: brightness(1.12); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 16px; margin-top: 12px; color: var(--ink-2); font-size: 13px; }}
.legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
.equation {{ margin-top: 10px; color: var(--ink-2); font-size: 14px; }}
.row {{ padding: 10px 0; border-top: 1px solid var(--grid); }}
.row:first-of-type {{ border-top: 0; }}
.row-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }}
.row-name {{ font-weight: 600; }}
.row-sub {{ color: var(--muted); font-size: 12px; }}
.row-val {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
.tag {{ font-size: 12px; border-radius: 999px; padding: 1px 8px; margin-left: 6px; border: 1px solid; }}
.tag-borrowed {{ color: var(--borrowed-ink); border-color: var(--borrowed); }}
.tag-own {{ color: var(--ink-2); border-color: var(--grid); }}
.loans {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }}
.loan-head {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }}
dl {{ display: grid; grid-template-columns: auto 1fr; gap: 6px 16px; margin: 14px 0 0; font-size: 14px; }}
dt {{ color: var(--ink-2); }}
dd {{ margin: 0; text-align: right; font-variant-numeric: tabular-nums; }}
.borrowed-ink {{ color: var(--borrowed-ink); font-weight: 650; }}
.pos {{ color: var(--pos); font-weight: 600; }}
.neg {{ color: var(--neg); font-weight: 600; }}
.status {{ display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600;
  padding: 2px 10px 2px 4px; border-radius: 999px; border: 1px solid var(--ring); color: var(--ink); }}
.status .ico {{ width: 18px; height: 18px; border-radius: 50%; display: inline-grid; place-items: center;
  color: #fff; font-size: 11px; }}
.st-good .ico {{ background: var(--good); }}
.st-warning .ico {{ background: var(--warning); color: #0b0b0b; }}
.st-serious .ico {{ background: var(--serious); color: #0b0b0b; }}
.st-critical .ico {{ background: var(--critical); }}
.st-none .ico {{ background: var(--muted); }}
.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
.stats .card {{ padding: 14px 16px; }}
.stats .k {{ color: var(--ink-2); font-size: 13px; }}
.stats .v {{ font-size: 20px; font-weight: 650; }}
.stats .s {{ color: var(--muted); font-size: 12px; }}
ul.advice {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
ul.advice li {{ display: flex; gap: 12px; align-items: flex-start; }}
ul.advice p {{ margin: 2px 0 0; color: var(--ink-2); font-size: 14px; }}
.adv {{ flex: none; font-size: 12px; font-weight: 700; padding: 2px 10px; border-radius: 999px; border: 1px solid var(--grid); }}
.adv-緊急 {{ border-color: var(--critical); color: var(--neg); }}
.adv-避險, .adv-減碼 {{ border-color: var(--serious); }}
.adv-加碼 {{ border-color: var(--good); color: var(--pos); }}
details {{ margin-top: 12px; }}
summary {{ cursor: pointer; color: var(--ink-2); }}
.table-wrap {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 8px; }}
th, td {{ padding: 6px 8px; border-bottom: 1px solid var(--grid); text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
th {{ color: var(--ink-2); font-weight: 600; }}
.tip {{ position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); box-shadow: 0 4px 16px rgba(0,0,0,.15); border-radius: 8px;
  padding: 6px 10px; font-size: 13px; max-width: 280px; display: none; z-index: 10; }}
footer {{ margin-top: 32px; color: var(--muted); font-size: 12px; }}
@media (max-width: 720px) {{
  .hero, .stats {{ grid-template-columns: 1fr 1fr; }}
  .hero .card:first-child {{ grid-column: 1 / -1; }}
  .hero .v {{ font-size: 24px; }}
}}
@media (forced-colors: active) {{
  .seg.borrowed {{ background-image: repeating-linear-gradient(135deg, transparent 0 4px, CanvasText 4px 6px); }}
}}
</style>
</head>
<body>
<main>
<header>
  <h1>我的錢與借來的錢</h1>
  <span class="muted">{s.as_of.isoformat()}　今日建議：<strong>{e(verdict)}</strong></span>
</header>

<section class="hero" aria-label="總覽">
  <div class="card"><div class="k">總資產</div><div class="v">{_m(gross)}</div>
    <div class="s">市值＋現金</div></div>
  <div class="card"><div class="k"><span class="sw own"></span>我的錢（淨值）</div><div class="v">{_m(s.net_equity)}</div>
    <div class="s">總資產扣掉所有借款</div></div>
  <div class="card"><div class="k"><span class="sw borrowed"></span>借來的錢</div><div class="v borrowed-ink">{_m(debt)}</div>
    <div class="s">佔總資產 {_pct(debt_ratio)}</div></div>
</section>

<section class="card" style="margin-top:12px" aria-label="總資產組成">
  <h3>總資產裡，多少是我的、多少是借的</h3>
  <div class="bar" role="img" aria-label="我的錢 {_m(own)} 元，借來的錢 {_m(debt)} 元">{main_bar}</div>
  <div class="legend"><span><i class="sw own"></i>我的錢</span><span><i class="sw borrowed"></i>借來的錢</span></div>
  <div class="equation">每 100 元資產中，{(own / gross * 100 if gross else 0):.0f} 元是你的，{debt_ratio * 100:.0f} 元是借來的。</div>
</section>

<section class="stats" style="margin-top:12px" aria-label="風險指標">
  <div class="card"><div class="k">股票等效槓桿</div><div class="v">{'—' if lev is None else f'{lev:.2f}x'}</div>
    <div class="s">目標 {strategy.target_equity_leverage:.2f}x・上限 {strategy.max_equity_leverage:.2f}x</div></div>
  <div class="card"><div class="k">最低維持率</div><div class="v">{_pct(s.min_maintenance, 0)}</div>
    <div class="s">追繳線 {strategy.maintenance_call:.0%}</div></div>
  <div class="card"><div class="k">質押損益（扣利息）</div>
    <div class="v {'pos' if s.pledge_pnl >= 0 else 'neg'}">{'+' if s.pledge_pnl >= 0 else ''}{_m(s.pledge_pnl)}</div>
    <div class="s">借錢投資到目前賺／賠</div></div>
  <div class="card"><div class="k">債券比重</div><div class="v">{_pct(s.bond_ratio)}</div>
    <div class="s">{trend_txt}</div></div>
</section>

<h2>每一筆資產是用誰的錢買的</h2>
<section class="card" aria-label="各部位資金來源">
  <div class="legend" style="margin-top:0">
    <span><i class="sw own"></i>自有資金買的</span>
    <span><i class="sw pledged"></i>自有、但質押給券商（不能隨便賣）</span>
    <span><i class="sw borrowed"></i>用借來的錢買的（現值）</span>
  </div>
  {''.join(rows)}
  <details><summary>表格檢視</summary>
    <div class="table-wrap"><table>
      <thead><tr><th>標的</th><th>類別</th><th>自有未質押</th><th>自有質押中</th><th>借款買進</th><th>合計</th></tr></thead>
      <tbody>{table_rows}</tbody>
    </table></div>
  </details>
</section>

<h2>借款明細</h2>
<section class="loans">{''.join(loan_cards)}</section>

<h2>建議</h2>
<section class="card"><ul class="advice">{advice_html}</ul></section>

{f'<h2>注意</h2><section class="card"><ul>{warn_html}</ul></section>' if warn_html else ''}

<footer>
  「用借來的錢買的」是依 portfolio.toml 中 loans.purchases 登記的股數乘以現價；借款本金扣掉已登記買進成本後的餘額，視為還放在現金裡。
  本頁由規則引擎依你設定的門檻自動產生，不是投資顧問意見；實際維持率與追繳金額以券商通知為準。
</footer>
</main>
<div class="tip" id="tip" role="tooltip"></div>
<script id="data" type="application/json">{data_json}</script>
<script>
(() => {{
  const tip = document.getElementById("tip");
  const fit = () => document.querySelectorAll(".seg-label").forEach(l => {{
    l.style.visibility = "";
    if (l.offsetWidth > l.parentElement.clientWidth - 16) l.style.visibility = "hidden";
  }});
  fit();
  addEventListener("resize", fit);
  const show = (el, x, y) => {{
    tip.textContent = el.dataset.tip;
    tip.style.display = "block";
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(x + 12, innerWidth - w - 8) + "px";
    tip.style.top = Math.max(8, y - h - 12) + "px";
  }};
  document.querySelectorAll(".seg").forEach(el => {{
    el.addEventListener("mousemove", ev => show(el, ev.clientX, ev.clientY));
    el.addEventListener("mouseleave", () => tip.style.display = "none");
    el.addEventListener("focus", () => {{ const r = el.getBoundingClientRect(); show(el, r.left, r.top); }});
    el.addEventListener("blur", () => tip.style.display = "none");
  }});
}})();
</script>
</body>
</html>
"""
