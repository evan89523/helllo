"""產生手機 App 用的 prices.json（全市場收盤價，公開資料，不含任何個人持股）。

由 GitHub Actions 定時執行，輸出檔和 App 放在同一個網站，
手機就不會被瀏覽器的跨網域限制（CORS）擋下。

用法：python scripts/build_prices.py web/prices.json [上次發布的 prices.json 網址]
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_tracker import prices as px  # noqa: E402

# 預先抓歷史價格的大盤標的（讓 App 的均線趨勢判斷可以馬上使用）
HISTORY_SYMBOLS = ("0050", "006208")
HISTORY_MONTHS = 4

# 常見的美國掛牌美債 ETF（以美元報價），App 中 market 設為「美股」的持股會使用
US_SYMBOLS = ("TLT", "TLH", "IEF", "IEI", "SHY", "SGOV", "BIL", "EDV", "ZROZ", "VGLT", "GOVT", "TMF")


def months_back(today: date, n: int) -> list[tuple[int, int]]:
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def build(today: date, previous: dict | None = None) -> tuple[dict, list[str]]:
    errors: list[str] = []
    data: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": None,
        "dates": {},
        "twse": {},
        "tpex": {},
        "us": {},
        "usd_twd": None,
        "history": {},
    }

    for key, url in (("twse", px.TWSE_ALL_URL), ("tpex", px.TPEX_ALL_URL)):
        try:
            table, d = px.parse_market_table(px._get_json(url, timeout=60))
            data[key] = table
            data["dates"][key] = d.isoformat() if d else today.isoformat()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{key}: {e}")
            if previous and previous.get(key):
                # 沿用上次成功抓到的資料，並保留它原本的日期
                data[key] = previous[key]
                data["dates"][key] = (previous.get("dates") or {}).get(key) or previous.get("date")
                errors.append(f"{key}: 沿用上次的資料（{data['dates'][key]}）")
    if data["dates"]:
        data["date"] = max(d for d in data["dates"].values() if d)

    for sym in HISTORY_SYMBOLS:
        rows: dict[date, float] = {}
        for y, m in months_back(today, HISTORY_MONTHS):
            try:
                rows.update(px.fetch_twse_month(sym, y, m))
            except Exception as e:  # noqa: BLE001
                errors.append(f"history {sym} {y}-{m:02d}: {e}")
        if rows:
            data["history"][sym] = [[d.isoformat(), c] for d, c in sorted(rows.items())]

    quotes = px.fetch_quotes({"US": set(US_SYMBOLS)}, need_usd=True)
    for sym, price in quotes.prices.items():
        if sym == "TWD=X":
            data["usd_twd"] = price
        else:
            data["us"][sym] = price
    errors.extend(quotes.errors)
    return data, errors


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "web/prices.json")
    previous = None
    if len(argv) > 2:  # 上次發布的 prices.json 網址，某個來源失敗時沿用
        try:
            previous = px._get_json(argv[2], timeout=30, attempts=2)
        except Exception as e:  # noqa: BLE001
            print(f"warning: 讀不到上次的 prices.json: {e}", file=sys.stderr)
    data, errors = build(date.today(), previous)
    for e in errors:
        print(f"warning: {e}", file=sys.stderr)
    print(
        f"dates={data['dates']} twse={len(data['twse'])} tpex={len(data['tpex'])} "
        f"us={len(data['us'])} usd_twd={data['usd_twd']} history={ {k: len(v) for k, v in data['history'].items()} }"
    )
    if not data["twse"] and not data["tpex"]:
        print("error: 證交所與櫃買中心都抓不到資料", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
