"""規則式投資建議：依維持率、槓桿、債券比重、集中度與大盤趨勢給出加碼/避險建議。

這是透明、可調整的規則引擎，不是預測模型。所有門檻都在 portfolio.toml 的 [strategy]。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Strategy
from .portfolio import Summary

# 等級越高越優先
LEVELS = {"緊急": 5, "避險": 4, "減碼": 3, "分散": 2, "加碼": 1, "維持": 0}


@dataclass
class Advice:
    level: str
    title: str
    reason: str
    amount: float | None = None  # 建議金額（台幣），None 表示不適用


@dataclass
class Trend:
    symbol: str
    price: float | None
    ma: float | None
    days: int

    @property
    def known(self) -> bool:
        return self.price is not None and self.ma is not None

    @property
    def up(self) -> bool:
        return self.known and self.price >= self.ma


def is_etf(symbol: str) -> bool:
    """台灣 ETF 代號以 00 開頭（例如 0050、006208、00679B）。"""
    return symbol.startswith("00")


def compute_trend(symbol: str, history: list[float], ma_days: int) -> Trend:
    if len(history) < ma_days:
        return Trend(symbol, history[-1] if history else None, None, len(history))
    window = history[-ma_days:]
    return Trend(symbol, window[-1], sum(window) / ma_days, len(history))


def advise(summary: Summary, strategy: Strategy, trend: Trend) -> tuple[str, list[Advice]]:
    s = strategy
    out: list[Advice] = []
    net = summary.net_equity

    if net <= 0:
        out.append(Advice("緊急", "淨值為負", "負債已超過總資產，請立即聯絡券商處理還款或補擔保品。"))
        return "緊急", out

    # 1. 維持率
    for l in summary.loans:
        m = l.maintenance
        if m is None:
            continue
        coll, principal = l.collateral_value, l.loan.principal
        repay_to_safe = max(0.0, principal - coll / s.maintenance_safe)
        collateral_to_safe = max(0.0, s.maintenance_safe * principal - coll)
        name = l.loan.name or l.loan.id
        if m < s.maintenance_call:
            out.append(
                Advice(
                    "緊急",
                    f"{name} 維持率 {m:.0%} 已低於追繳線 {s.maintenance_call:.0%}",
                    f"需還款約 {repay_to_safe:,.0f} 元或補擔保品市值約 {collateral_to_safe:,.0f} 元"
                    f"才能回到安全線 {s.maintenance_safe:.0%}。請先向券商確認實際追繳金額與期限。",
                    repay_to_safe,
                )
            )
        elif m < s.maintenance_warn:
            out.append(
                Advice(
                    "避險",
                    f"{name} 維持率 {m:.0%} 低於警戒線 {s.maintenance_warn:.0%}",
                    f"擔保品再跌 {l.drop_to(s.maintenance_call):.1%} 就會被追繳。"
                    f"建議還款約 {repay_to_safe:,.0f} 元（或補擔保品約 {collateral_to_safe:,.0f} 元）回到 {s.maintenance_safe:.0%}。",
                    repay_to_safe,
                )
            )

    # 2. 槓桿過高
    lev = summary.equity_leverage or 0.0
    if lev > s.max_equity_leverage:
        reduce = (lev - s.target_equity_leverage) * net
        out.append(
            Advice(
                "減碼",
                f"股票等效槓桿 {lev:.2f}x 超過上限 {s.max_equity_leverage:.2f}x",
                f"建議降低股票曝險約 {reduce:,.0f} 元（賣出後優先償還質押借款），回到目標 {s.target_equity_leverage:.2f}x。",
                reduce,
            )
        )
    elif trend.known and not trend.up and lev > s.target_equity_leverage:
        reduce = (lev - s.target_equity_leverage) * net
        out.append(
            Advice(
                "減碼",
                f"{trend.symbol} 跌破 {s.trend_ma_days} 日均線，且槓桿 {lev:.2f}x 高於目標",
                f"趨勢轉弱時建議把槓桿降回 {s.target_equity_leverage:.2f}x，約減少曝險 {reduce:,.0f} 元。",
                reduce,
            )
        )

    # 3. 債券比重
    gross = summary.gross_assets
    if summary.bond_ratio < s.bond_ratio_min:
        need = s.bond_ratio_min * gross - summary.bond_value
        out.append(
            Advice(
                "避險",
                f"債券 ETF 比重 {summary.bond_ratio:.1%} 低於下限 {s.bond_ratio_min:.0%}",
                f"可用現金或部分股票轉入美債 ETF 約 {need:,.0f} 元。"
                "注意：股債並非永遠負相關（例如 2022 年同跌），且美債 ETF 有匯率風險。",
                need,
            )
        )

    # 4. 集中度
    for symbol, ratio in summary.concentration():
        if is_etf(symbol):
            continue  # ETF 本身已分散持股，不套用單一持股上限
        if ratio > s.max_single_position:
            excess = (ratio - s.max_single_position) * net
            out.append(
                Advice(
                    "分散",
                    f"{symbol} 曝險佔淨值 {ratio:.0%}，超過單一上限 {s.max_single_position:.0%}",
                    f"超出約 {excess:,.0f} 元。若同時是質押擔保品，下跌時損益與維持率會一起惡化。",
                    excess,
                )
            )

    # 5. 加碼條件：沒有任何風險警示、趨勢向上、槓桿低於目標、維持率在安全線以上
    risky = any(LEVELS[a.level] >= LEVELS["分散"] for a in out)
    maint_ok = summary.min_maintenance is None or summary.min_maintenance >= s.maintenance_safe
    if not risky and lev < s.target_equity_leverage:
        gap = (s.target_equity_leverage - lev) * net
        if not trend.known:
            out.append(
                Advice(
                    "維持",
                    "槓桿低於目標，但趨勢資料不足",
                    f"{trend.symbol} 只有 {trend.days} 天價格，需要 {s.trend_ma_days} 天才能判斷趨勢。"
                    "可執行 backfill 回補歷史價格。",
                )
            )
        elif not trend.up:
            out.append(
                Advice(
                    "維持",
                    f"槓桿低於目標，但 {trend.symbol} 在 {s.trend_ma_days} 日均線之下",
                    "趨勢未轉強前不建議增加借款加碼，可用現金分批買進。",
                )
            )
        elif maint_ok:
            borrow_room = sum(
                max(0.0, l.collateral_value / s.maintenance_safe - l.loan.principal) for l in summary.loans
            )
            amount = min(gap, borrow_room) if summary.loans else gap
            out.append(
                Advice(
                    "加碼",
                    f"{trend.symbol} 站上 {s.trend_ma_days} 日均線，槓桿 {lev:.2f}x 低於目標 {s.target_equity_leverage:.2f}x",
                    f"可加碼約 {amount:,.0f} 元"
                    + (f"（維持率安全線限制下最多可再借 {borrow_room:,.0f} 元）" if summary.loans else "")
                    + "。建議分批進場，並注意新增借款會增加利息成本。",
                    amount,
                )
            )

    if not out:
        out.append(Advice("維持", "各項指標都在目標區間內", "不需要調整，持續每日追蹤即可。"))

    out.sort(key=lambda a: -LEVELS[a.level])
    return out[0].level, out
