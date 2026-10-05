"""收盤價來源：證交所、櫃買中心 OpenAPI，美股使用 Yahoo Finance。

所有來源都是公開行情，不需要登入任何券商帳戶。
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date

TWSE_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_ALL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_MONTH_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={ymd}&stockNo={symbol}&response=json"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"

USER_AGENT = "Mozilla/5.0 (asset-tracker)"


class PriceError(RuntimeError):
    pass


def _get_json(url: str, timeout: float = 20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def parse_number(value) -> float | None:
    """'1,005.00' -> 1005.0；'--'、'' 或 None -> None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_roc_date(value) -> date | None:
    """民國日期 '1150903'、'115/09/03' -> date(2026, 9, 3)。"""
    if not value:
        return None
    m = re.fullmatch(r"(\d{2,3})/?(\d{2})/?(\d{2})", str(value).strip())
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y + 1911, mo, d)
    except ValueError:
        return None


def _first(row: dict, *keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def parse_market_table(rows: list[dict]) -> tuple[dict[str, float], date | None]:
    """解析證交所 / 櫃買 OpenAPI 的全市場收盤表。"""
    prices: dict[str, float] = {}
    trade_date = None
    for row in rows:
        code = _first(row, "Code", "SecuritiesCompanyCode", "證券代號")
        close = parse_number(_first(row, "ClosingPrice", "Close", "收盤價"))
        if code and close and close > 0:
            prices[str(code).strip().upper()] = close
        if trade_date is None:
            trade_date = parse_roc_date(_first(row, "Date", "日期"))
    return prices, trade_date


@dataclass
class Quotes:
    prices: dict[str, float] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    trade_date: date | None = None
    errors: list[str] = field(default_factory=list)


def fetch_quotes(symbols_by_market: dict[str, set[str]], need_usd: bool = False) -> Quotes:
    """抓取所需標的的收盤價。

    symbols_by_market: {"AUTO": {...}, "TWSE": {...}, "TPEX": {...}, "US": {...}}
    """
    q = Quotes()
    tw_needed = symbols_by_market.get("AUTO", set()) | symbols_by_market.get("TWSE", set())
    otc_needed = symbols_by_market.get("AUTO", set()) | symbols_by_market.get("TPEX", set())

    twse: dict[str, float] = {}
    tpex: dict[str, float] = {}
    if tw_needed:
        try:
            twse, d = parse_market_table(_get_json(TWSE_ALL_URL))
            q.trade_date = q.trade_date or d
        except Exception as e:  # noqa: BLE001 - 網路錯誤種類多，統一記錄
            q.errors.append(f"證交所行情抓取失敗: {e}")
    if otc_needed - set(twse):
        try:
            tpex, d = parse_market_table(_get_json(TPEX_ALL_URL))
            q.trade_date = q.trade_date or d
        except Exception as e:  # noqa: BLE001
            q.errors.append(f"櫃買中心行情抓取失敗: {e}")

    for s in symbols_by_market.get("TWSE", set()):
        if s in twse:
            q.prices[s], q.sources[s] = twse[s], "TWSE"
    for s in symbols_by_market.get("TPEX", set()):
        if s in tpex:
            q.prices[s], q.sources[s] = tpex[s], "TPEX"
    for s in symbols_by_market.get("AUTO", set()):
        if s in twse:
            q.prices[s], q.sources[s] = twse[s], "TWSE"
        elif s in tpex:
            q.prices[s], q.sources[s] = tpex[s], "TPEX"

    us = set(symbols_by_market.get("US", set()))
    if us and need_usd:
        us.add("TWD=X")
    for s in sorted(us):
        try:
            data = _get_json(YAHOO_URL.format(symbol=s))
            price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
            q.prices[s], q.sources[s] = float(price), "YAHOO"
        except Exception as e:  # noqa: BLE001
            q.errors.append(f"Yahoo 抓取 {s} 失敗: {e}")
    return q


def fetch_twse_month(symbol: str, year: int, month: int) -> dict[date, float]:
    """證交所個股單月日成交資訊（僅上市股票/ETF），用於回補歷史價格。"""
    data = _get_json(TWSE_MONTH_URL.format(ymd=f"{year}{month:02d}01", symbol=symbol))
    if data.get("stat") != "OK":
        raise PriceError(f"{symbol} {year}-{month:02d}: {data.get('stat')}")
    fields = data.get("fields", [])
    close_idx = fields.index("收盤價") if "收盤價" in fields else 6
    out: dict[date, float] = {}
    for row in data.get("data", []):
        d = parse_roc_date(row[0])
        close = parse_number(row[close_idx])
        if d and close:
            out[d] = close
    return out
