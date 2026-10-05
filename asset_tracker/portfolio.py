"""資產估值、質押損益、維持率與等效槓桿計算。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import Holding, Loan, Portfolio

STRESS_DROPS = (0.10, 0.20, 0.30, 0.40)


@dataclass
class Position:
    holding: Holding
    price: float  # 換算後的台幣價格
    stale: bool = False  # True 表示用的是舊價格

    @property
    def market_value(self) -> float:
        return self.holding.shares * self.price

    @property
    def unrealized(self) -> float:
        """未實現損益（含已領股利，信託另含公司獎勵金不計入成本）。"""
        return self.market_value - self.holding.cost + self.holding.dividends

    @property
    def return_pct(self) -> float | None:
        return self.unrealized / self.holding.cost if self.holding.cost else None

    @property
    def equity_exposure(self) -> float:
        if self.holding.category == "bond_etf":
            return 0.0
        return self.market_value * self.holding.exposure


@dataclass
class LoanStatus:
    loan: Loan
    days: int
    interest_accrued: float
    interest_unpaid: float
    collateral_value: float
    funded_value: float
    funded_cost: float
    funded_dividends: float
    missing_collateral: list[str] = field(default_factory=list)

    @property
    def liability(self) -> float:
        return self.loan.principal + self.interest_unpaid

    @property
    def maintenance(self) -> float | None:
        """維持率 = 擔保品市值 / 借款本金。"""
        return self.collateral_value / self.loan.principal if self.loan.principal > 0 else None

    def drop_to(self, ratio: float) -> float | None:
        """擔保品下跌多少比例時維持率會跌到 ratio（負數表示已經低於）。"""
        if self.loan.principal <= 0 or self.collateral_value <= 0:
            return None
        return 1 - ratio * self.loan.principal / self.collateral_value

    @property
    def funded_pnl(self) -> float:
        return self.funded_value - self.funded_cost + self.funded_dividends

    @property
    def pledge_pnl(self) -> float:
        """質押損益 = 借款買進部位損益 + 已實現損益 - 累積利息。"""
        return self.funded_pnl + self.loan.realized_pnl - self.interest_accrued


@dataclass
class Summary:
    as_of: date
    positions: list[Position]
    loans: list[LoanStatus]
    cash: float
    warnings: list[str]

    @property
    def holdings_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def gross_assets(self) -> float:
        return self.holdings_value + self.cash

    @property
    def liabilities(self) -> float:
        return sum(l.liability for l in self.loans)

    @property
    def net_equity(self) -> float:
        return self.gross_assets - self.liabilities

    @property
    def equity_exposure(self) -> float:
        return sum(p.equity_exposure for p in self.positions)

    @property
    def bond_value(self) -> float:
        return sum(p.market_value for p in self.positions if p.holding.category == "bond_etf")

    def _over_net(self, value: float) -> float | None:
        return value / self.net_equity if self.net_equity > 0 else None

    @property
    def asset_leverage(self) -> float | None:
        """資產槓桿 = 總資產 / 淨值。"""
        return self._over_net(self.gross_assets)

    @property
    def equity_leverage(self) -> float | None:
        """股票等效槓桿 = 股票曝險（含槓桿 ETF 倍數）/ 淨值。"""
        return self._over_net(self.equity_exposure)

    @property
    def bond_ratio(self) -> float:
        return self.bond_value / self.gross_assets if self.gross_assets > 0 else 0.0

    @property
    def min_maintenance(self) -> float | None:
        values = [l.maintenance for l in self.loans if l.maintenance is not None]
        return min(values) if values else None

    @property
    def pledge_pnl(self) -> float:
        return sum(l.pledge_pnl for l in self.loans)

    @property
    def total_unrealized(self) -> float:
        return sum(p.unrealized for p in self.positions)

    def by_category(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.positions:
            out[p.holding.category] = out.get(p.holding.category, 0.0) + p.market_value
        return out

    def concentration(self) -> list[tuple[str, float]]:
        """各股票（合併正股與信託）佔淨值的曝險比例，由大到小。"""
        exposure: dict[str, float] = {}
        for p in self.positions:
            if p.equity_exposure:
                exposure[p.holding.symbol] = exposure.get(p.holding.symbol, 0.0) + p.equity_exposure
        if self.net_equity <= 0:
            return []
        return sorted(((s, v / self.net_equity) for s, v in exposure.items()), key=lambda x: -x[1])

    def stress(self) -> list[dict]:
        """股票全面下跌情境（債券價格假設不變，屬簡化估計）。"""
        rows = []
        for drop in STRESS_DROPS:
            net_after = self.net_equity - self.equity_exposure * drop
            maint = [
                (l.collateral_value * (1 - drop)) / l.loan.principal
                for l in self.loans
                if l.loan.principal > 0
            ]
            rows.append(
                {
                    "drop": drop,
                    "net_equity": net_after,
                    "net_change_pct": (net_after / self.net_equity - 1) if self.net_equity > 0 else None,
                    "min_maintenance": min(maint) if maint else None,
                }
            )
        return rows

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "gross_assets": self.gross_assets,
            "liabilities": self.liabilities,
            "net_equity": self.net_equity,
            "cash": self.cash,
            "equity_exposure": self.equity_exposure,
            "asset_leverage": self.asset_leverage,
            "equity_leverage": self.equity_leverage,
            "bond_ratio": self.bond_ratio,
            "min_maintenance": self.min_maintenance,
            "pledge_pnl": self.pledge_pnl,
            "by_category": self.by_category(),
            "positions": [
                {
                    "symbol": p.holding.symbol,
                    "category": p.holding.category,
                    "shares": p.holding.shares,
                    "price": p.price,
                    "market_value": p.market_value,
                    "unrealized": p.unrealized,
                    "stale": p.stale,
                }
                for p in self.positions
            ],
            "loans": [
                {
                    "id": l.loan.id,
                    "principal": l.loan.principal,
                    "collateral_value": l.collateral_value,
                    "maintenance": l.maintenance,
                    "interest_accrued": l.interest_accrued,
                    "pledge_pnl": l.pledge_pnl,
                }
                for l in self.loans
            ],
        }


def evaluate(
    portfolio: Portfolio,
    prices: dict[str, float],
    as_of: date,
    stale: set[str] | None = None,
    usd_twd: float | None = None,
) -> Summary:
    """prices 為原幣價格（美股為美元），US 市場標的會用 usd_twd 換算。"""
    stale = stale or set()
    warnings: list[str] = []
    positions: list[Position] = []
    price_twd: dict[str, float] = {}

    for h in portfolio.holdings:
        if h.market == "MANUAL":
            raw = h.price
        else:
            raw = prices.get(h.symbol)
        if raw is None:
            warnings.append(f"{h.symbol} 沒有價格，暫以成本估值")
            raw_twd = h.cost / h.shares if h.shares else 0.0
        elif h.market == "US":
            if not usd_twd:
                warnings.append(f"{h.symbol} 為美股但沒有 USD/TWD 匯率，暫以成本估值")
                raw_twd = h.cost / h.shares if h.shares else 0.0
            else:
                raw_twd = raw * usd_twd
        else:
            raw_twd = raw
        price_twd[h.symbol] = raw_twd
        positions.append(Position(holding=h, price=raw_twd, stale=h.symbol in stale))

    stock_shares: dict[str, float] = {}
    for h in portfolio.holdings:
        if h.category == "stock":
            stock_shares[h.symbol] = stock_shares.get(h.symbol, 0.0) + h.shares

    pledged: dict[str, float] = {}
    loans: list[LoanStatus] = []
    for loan in portfolio.loans:
        days = max(0, (as_of - loan.start_date).days)
        accrued = loan.principal * loan.annual_rate * days / 365
        coll_value = 0.0
        missing = []
        for c in loan.collateral:
            pledged[c.symbol] = pledged.get(c.symbol, 0.0) + c.shares
            p = price_twd.get(c.symbol, prices.get(c.symbol))
            if p is None:
                missing.append(c.symbol)
                warnings.append(f"質押 {loan.id} 的擔保品 {c.symbol} 沒有價格，維持率不含此檔")
                continue
            coll_value += c.shares * p
        funded_value = 0.0
        for pu in loan.purchases:
            p = price_twd.get(pu.symbol, prices.get(pu.symbol))
            if p is None:
                warnings.append(f"質押 {loan.id} 買進的 {pu.symbol} 沒有價格，暫以成本估值")
                funded_value += pu.cost
            else:
                funded_value += pu.shares * p
        loans.append(
            LoanStatus(
                loan=loan,
                days=days,
                interest_accrued=accrued,
                interest_unpaid=max(0.0, accrued - loan.interest_paid),
                collateral_value=coll_value,
                funded_value=funded_value,
                funded_cost=sum(pu.cost for pu in loan.purchases),
                funded_dividends=sum(pu.dividends for pu in loan.purchases),
                missing_collateral=missing,
            )
        )

    for symbol, shares in pledged.items():
        held = stock_shares.get(symbol, 0.0)
        if shares > held:
            warnings.append(f"{symbol} 質押 {shares:,.0f} 股，但正股只登記 {held:,.0f} 股，請確認")

    for p in positions:
        if p.stale:
            warnings.append(f"{p.holding.symbol} 今日價格抓取失敗，使用資料庫中的舊價格")

    return Summary(as_of=as_of, positions=positions, loans=loans, cash=portfolio.cash, warnings=warnings)
