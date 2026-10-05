"""讀取 portfolio.toml 設定檔。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

CATEGORIES = ("stock", "trust", "bond_etf")
MARKETS = ("AUTO", "TWSE", "TPEX", "US", "MANUAL")


class ConfigError(ValueError):
    pass


@dataclass
class Holding:
    symbol: str
    category: str = "stock"
    shares: float = 0.0
    cost: float = 0.0
    name: str = ""
    market: str = "AUTO"
    exposure: float = 1.0
    dividends: float = 0.0
    company_match: float = 0.0
    price: float | None = None  # market = MANUAL 時使用

    @property
    def key(self) -> str:
        return f"{self.category}:{self.symbol}"


@dataclass
class Collateral:
    symbol: str
    shares: float


@dataclass
class Purchase:
    """用質押借款買進的部位（用來計算質押損益）。"""

    symbol: str
    shares: float
    cost: float
    dividends: float = 0.0


@dataclass
class Loan:
    id: str
    principal: float
    annual_rate: float
    start_date: date
    name: str = ""
    interest_paid: float = 0.0
    realized_pnl: float = 0.0
    collateral: list[Collateral] = field(default_factory=list)
    purchases: list[Purchase] = field(default_factory=list)


@dataclass
class Strategy:
    target_equity_leverage: float = 1.3
    max_equity_leverage: float = 1.6
    maintenance_call: float = 1.30
    maintenance_warn: float = 1.66
    maintenance_safe: float = 2.00
    bond_ratio_min: float = 0.15
    bond_ratio_max: float = 0.40
    max_single_position: float = 0.40
    benchmark: str = "0050"
    trend_ma_days: int = 60


@dataclass
class Portfolio:
    holdings: list[Holding]
    loans: list[Loan]
    strategy: Strategy
    cash: float = 0.0
    db_path: Path = Path("data/tracker.db")
    report_dir: Path = Path("reports")
    manual_prices: dict[str, float] = field(default_factory=dict)
    usd_twd: float | None = None


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as e:
        raise ConfigError(f"日期格式錯誤（應為 YYYY-MM-DD）: {value!r}") from e


def _holding(raw: dict) -> Holding:
    if "symbol" not in raw:
        raise ConfigError(f"holdings 缺少 symbol: {raw}")
    h = Holding(
        symbol=str(raw["symbol"]).strip().upper(),
        category=raw.get("category", "stock"),
        shares=float(raw.get("shares", 0)),
        cost=float(raw.get("cost", 0)),
        name=raw.get("name", ""),
        market=str(raw.get("market", "AUTO")).upper(),
        exposure=float(raw.get("exposure", 1.0)),
        dividends=float(raw.get("dividends", 0)),
        company_match=float(raw.get("company_match", 0)),
        price=float(raw["price"]) if "price" in raw else None,
    )
    if h.category not in CATEGORIES:
        raise ConfigError(f"{h.symbol}: category 必須是 {CATEGORIES} 之一，收到 {h.category!r}")
    if h.market not in MARKETS:
        raise ConfigError(f"{h.symbol}: market 必須是 {MARKETS} 之一，收到 {h.market!r}")
    if h.market == "MANUAL" and h.price is None:
        raise ConfigError(f"{h.symbol}: market = MANUAL 時必須填 price")
    if h.shares < 0:
        raise ConfigError(f"{h.symbol}: shares 不可為負")
    return h


def _loan(raw: dict) -> Loan:
    for k in ("id", "principal", "annual_rate", "start_date"):
        if k not in raw:
            raise ConfigError(f"loans 缺少 {k}: {raw}")
    return Loan(
        id=str(raw["id"]),
        name=raw.get("name", ""),
        principal=float(raw["principal"]),
        annual_rate=float(raw["annual_rate"]),
        start_date=_parse_date(raw["start_date"]),
        interest_paid=float(raw.get("interest_paid", 0)),
        realized_pnl=float(raw.get("realized_pnl", 0)),
        collateral=[
            Collateral(symbol=str(c["symbol"]).strip().upper(), shares=float(c["shares"]))
            for c in raw.get("collateral", [])
        ],
        purchases=[
            Purchase(
                symbol=str(p["symbol"]).strip().upper(),
                shares=float(p["shares"]),
                cost=float(p["cost"]),
                dividends=float(p.get("dividends", 0)),
            )
            for p in raw.get("purchases", [])
        ],
    )


def load(path: str | Path) -> Portfolio:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"找不到設定檔 {path}，請先複製 portfolio.example.toml 為 portfolio.toml")
    with path.open("rb") as f:
        raw = tomllib.load(f)

    base = path.parent
    settings = raw.get("settings", {})

    holdings_raw = list(raw.get("holdings", []))
    for extra in settings.get("holdings_files", []):
        extra_path = base / extra
        if extra_path.exists():
            with extra_path.open("rb") as f:
                holdings_raw.extend(tomllib.load(f).get("holdings", []))

    holdings = [_holding(h) for h in holdings_raw]
    loans = [_loan(l) for l in raw.get("loans", [])]

    seen = set()
    for h in holdings:
        if h.key in seen:
            raise ConfigError(f"重複的持股 {h.key}（可能同時出現在 portfolio.toml 與匯入檔）")
        seen.add(h.key)

    if len({l.id for l in loans}) != len(loans):
        raise ConfigError("loans 的 id 不可重複")

    strategy_fields = Strategy.__dataclass_fields__
    strat_raw = raw.get("strategy", {})
    unknown = set(strat_raw) - set(strategy_fields)
    if unknown:
        raise ConfigError(f"strategy 有未知欄位: {sorted(unknown)}")
    strategy = Strategy(**strat_raw)
    if not strategy.maintenance_call < strategy.maintenance_warn <= strategy.maintenance_safe:
        raise ConfigError("維持率門檻必須滿足 maintenance_call < maintenance_warn <= maintenance_safe")

    return Portfolio(
        holdings=holdings,
        loans=loans,
        strategy=strategy,
        cash=float(settings.get("cash", raw.get("cash", 0))),
        db_path=base / settings.get("db_path", "data/tracker.db"),
        report_dir=base / settings.get("report_dir", "reports"),
        manual_prices={str(k).upper(): float(v) for k, v in raw.get("prices", {}).items()},
        usd_twd=float(settings["usd_twd"]) if "usd_twd" in settings else None,
    )
