"""匯入國泰證券匯出的庫存 CSV。

國泰證券沒有提供給個人程式直接登入讀取帳戶的公開介面（本工具也不會保存你的帳號密碼），
所以採用「在國泰 App／網頁下單系統匯出庫存 → 用本模組轉成 holdings 檔」的方式。

匯出檔的欄位名稱可能因版本不同而異，下方 COLUMN_ALIASES 列出常見名稱；
對不上時可用 --map 指定，例如：--map 商品代碼=symbol --map 庫存數量=shares
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

COLUMN_ALIASES = {
    "symbol": ("代號", "股票代號", "證券代號", "商品代號", "商品代碼", "股票代碼"),
    "name": ("名稱", "股票名稱", "證券名稱", "商品名稱"),
    "shares": ("股數", "庫存股數", "庫存", "現股庫存", "持有股數", "庫存數量", "今日庫存"),
    "cost": ("成本", "付出成本", "持有成本", "總成本", "買進成本", "成本金額"),
}


class ImportError_(ValueError):
    pass


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportError_(f"無法辨識 {path} 的編碼（試過 UTF-8、CP950、Big5）")


def _num(text: str) -> float:
    cleaned = re.sub(r"[,\s元股$]", "", text or "")
    if cleaned in ("", "-", "--"):
        return 0.0
    return float(cleaned)


def guess_category(symbol: str) -> str:
    # 台灣債券 ETF 代號慣例：00 開頭、B 結尾（例如 00679B、00687B）
    return "bond_etf" if symbol.startswith("00") and symbol.endswith("B") else "stock"


def parse(path: str | Path, mapping: dict[str, str] | None = None, lots: bool = False) -> list[dict]:
    text = _read_text(Path(path))
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise ImportError_("CSV 是空的")

    # 有些匯出檔前面有標題列，找第一列含代號欄位的當表頭
    header_idx = None
    for i, row in enumerate(rows[:10]):
        cells = {c.strip() for c in row}
        if mapping and set(mapping) & cells:
            header_idx = i
            break
        if cells & set(COLUMN_ALIASES["symbol"]):
            header_idx = i
            break
    if header_idx is None:
        raise ImportError_("找不到含「代號」的表頭列，請用 --map 指定欄位")

    header = [c.strip() for c in rows[header_idx]]
    col: dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in header:
                col[field] = header.index(a)
                break
    for src, field in (mapping or {}).items():
        if src not in header:
            raise ImportError_(f"--map 指定的欄位 {src!r} 不在表頭 {header} 中")
        col[field] = header.index(src)
    for required in ("symbol", "shares"):
        if required not in col:
            raise ImportError_(f"找不到 {required} 欄位，表頭為 {header}，請用 --map 指定")

    merged: dict[str, dict] = {}
    for row in rows[header_idx + 1 :]:
        if len(row) <= col["symbol"]:
            continue
        symbol = row[col["symbol"]].strip().strip("=\"'").upper()
        if not re.fullmatch(r"[0-9A-Z]{4,6}", symbol):
            continue  # 合計列或備註列
        shares = _num(row[col["shares"]]) * (1000 if lots else 1)
        cost = _num(row[col["cost"]]) if "cost" in col and len(row) > col["cost"] else 0.0
        name = row[col["name"]].strip() if "name" in col and len(row) > col["name"] else ""
        item = merged.setdefault(
            symbol, {"symbol": symbol, "name": name, "category": guess_category(symbol), "shares": 0.0, "cost": 0.0}
        )
        item["shares"] += shares
        item["cost"] += cost
    if not merged:
        raise ImportError_("沒有讀到任何持股資料列")
    return list(merged.values())


def to_toml(holdings: list[dict]) -> str:
    out = ["# 由國泰證券庫存 CSV 自動產生，重新匯入會覆蓋此檔。", ""]
    for h in holdings:
        out.append("[[holdings]]")
        out.append(f"symbol = {json.dumps(h['symbol'], ensure_ascii=False)}")
        if h.get("name"):
            out.append(f"name = {json.dumps(h['name'], ensure_ascii=False)}")
        out.append(f"category = {json.dumps(h['category'])}")
        out.append(f"shares = {h['shares']:g}")
        out.append(f"cost = {h['cost']:g}")
        out.append("")
    return "\n".join(out)
