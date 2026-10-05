import io
import json
import subprocess
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from asset_tracker import cli, prices
from asset_tracker.advisor import Trend, advise, compute_trend
from asset_tracker.brokers import cathay
from asset_tracker.dashboard import funding_splits, render_dashboard
from asset_tracker.config import Collateral, Draw, Holding, Loan, Portfolio, Purchase, Strategy, load
from asset_tracker.portfolio import evaluate

ROOT = Path(__file__).resolve().parent.parent


def make_portfolio(principal=1_000_000, collateral_shares=2000, bond_shares=20000, cash=0.0):
    holdings = [
        Holding(symbol="2330", category="stock", shares=3000, cost=2_400_000),
        Holding(symbol="0050", category="stock", shares=10000, cost=1_500_000),
        Holding(symbol="00679B", category="bond_etf", shares=bond_shares, cost=600_000),
    ]
    loans = [
        Loan(
            id="L1",
            principal=principal,
            annual_rate=0.0365,
            start_date=date(2026, 1, 1),
            interest_paid=0,
            collateral=[Collateral("2330", collateral_shares)],
            purchases=[Purchase("0050", 5000, 800_000, dividends=10_000)],
        )
    ]
    return Portfolio(holdings=holdings, loans=loans, strategy=Strategy(), cash=cash)


PRICES = {"2330": 1000.0, "0050": 180.0, "00679B": 30.0}


class EvaluateTest(unittest.TestCase):
    def test_core_numbers(self):
        s = evaluate(make_portfolio(), PRICES, as_of=date(2026, 4, 11))  # 100 天
        loan = s.loans[0]
        self.assertEqual(loan.days, 100)
        self.assertAlmostEqual(loan.interest_accrued, 1_000_000 * 0.0365 * 100 / 365)  # 10,000
        self.assertAlmostEqual(loan.collateral_value, 2_000_000)
        self.assertAlmostEqual(loan.maintenance, 2.0)
        self.assertAlmostEqual(loan.drop_to(1.30), 1 - 1.3 / 2.0)
        # 借款部位：5000*180 - 800000 + 10000 = 110000，扣利息 10000
        self.assertAlmostEqual(loan.pledge_pnl, 100_000)

        holdings = 3_000_000 + 1_800_000 + 600_000
        self.assertAlmostEqual(s.gross_assets, holdings)
        self.assertAlmostEqual(s.liabilities, 1_010_000)  # 本金 + 未繳利息
        net = holdings - 1_010_000
        self.assertAlmostEqual(s.net_equity, net)
        self.assertAlmostEqual(s.equity_leverage, 4_800_000 / net)
        self.assertAlmostEqual(s.asset_leverage, holdings / net)
        self.assertAlmostEqual(s.bond_ratio, 600_000 / holdings)

    def test_multiple_draws(self):
        # 元大證金式：同一帳戶分三次撥款，利息各自起算，維持率以合計本金計算
        p = make_portfolio()
        p.loans[0] = Loan(
            id="SF", principal=0, annual_rate=0.0392, start_date=date(2026, 8, 5),
            draws=[Draw(date(2026, 8, 5), 600_000), Draw(date(2026, 8, 13), 400_000), Draw(date(2026, 9, 4), 192_000)],
            collateral=[Collateral("0050", 10000)],
        )
        p.loans[0].principal = sum(d.amount for d in p.loans[0].draws)
        s = evaluate(p, PRICES, as_of=date(2026, 10, 5))
        loan = s.loans[0]
        expected = (600_000 * 61 + 400_000 * 53 + 192_000 * 31) * 0.0392 / 365
        self.assertAlmostEqual(loan.interest_accrued, expected)
        self.assertAlmostEqual(loan.maintenance, 1_800_000 / 1_192_000)

    def test_draws_from_config(self):
        from asset_tracker.config import _loan

        loan = _loan({"id": "SF", "annual_rate": 0.0392, "draws": [
            {"date": "2026-08-13", "amount": 400000}, {"date": "2026-08-05", "amount": 600000}]})
        self.assertEqual(loan.principal, 1_000_000)
        self.assertEqual(loan.start_date, date(2026, 8, 5))
        legacy = _loan({"id": "L", "principal": 5, "annual_rate": 0.01, "start_date": "2026-01-01"})
        self.assertEqual([(d.date, d.amount) for d in legacy.draws], [(date(2026, 1, 1), 5.0)])

    def test_leveraged_etf_exposure(self):
        p = make_portfolio()
        p.holdings.append(Holding(symbol="00631L", shares=1000, cost=200_000, exposure=2.0))
        s = evaluate(p, {**PRICES, "00631L": 200.0}, as_of=date(2026, 1, 1))
        self.assertAlmostEqual(s.equity_exposure, 4_800_000 + 400_000)

    def test_missing_price_and_over_pledged_warn(self):
        p = make_portfolio(collateral_shares=5000)
        s = evaluate(p, {"2330": 1000.0, "0050": 180.0}, as_of=date(2026, 1, 1))
        text = "\n".join(s.warnings)
        self.assertIn("00679B 沒有價格", text)
        self.assertIn("2330 質押 5,000 股", text)

    def test_us_conversion(self):
        p = make_portfolio()
        p.holdings.append(Holding(symbol="TLT", category="bond_etf", market="US", shares=100, cost=280_000))
        s = evaluate(p, {**PRICES, "TLT": 90.0}, as_of=date(2026, 1, 1), usd_twd=32.0)
        tlt = next(x for x in s.positions if x.holding.symbol == "TLT")
        self.assertAlmostEqual(tlt.market_value, 100 * 90 * 32)


class AdvisorTest(unittest.TestCase):
    up = Trend("0050", 200.0, 180.0, 60)
    down = Trend("0050", 170.0, 180.0, 60)

    def test_margin_call_is_emergency(self):
        s = evaluate(make_portfolio(principal=1_800_000), PRICES, as_of=date(2026, 1, 1))
        verdict, advice = advise(s, Strategy(), self.up)
        self.assertEqual(verdict, "緊急")
        # 回到 200% 需還款 1,800,000 - 2,000,000/2 = 800,000
        self.assertAlmostEqual(advice[0].amount, 800_000)

    def test_warning_zone_is_hedge(self):
        s = evaluate(make_portfolio(principal=1_400_000), PRICES, as_of=date(2026, 1, 1))
        verdict, _ = advise(s, Strategy(), self.up)
        self.assertEqual(verdict, "避險")

    def test_add_when_under_target_and_trend_up(self):
        strat = Strategy(target_equity_leverage=1.5, max_equity_leverage=2.0, max_single_position=0.9)
        s = evaluate(make_portfolio(principal=500_000, bond_shares=40000), PRICES, as_of=date(2026, 1, 1))
        verdict, advice = advise(s, strat, self.up)
        self.assertEqual(verdict, "加碼")
        # 最多再借到維持率 200%：2,000,000/2 - 500,000 = 500,000
        self.assertLessEqual(advice[0].amount, 500_000 + 1e-6)

    def test_no_add_when_trend_down(self):
        strat = Strategy(target_equity_leverage=1.5, max_equity_leverage=2.0, max_single_position=0.9)
        s = evaluate(make_portfolio(principal=500_000, bond_shares=40000), PRICES, as_of=date(2026, 1, 1))
        verdict, _ = advise(s, strat, self.down)
        self.assertEqual(verdict, "維持")

    def test_etf_exempt_from_single_position_limit(self):
        p = make_portfolio(principal=500_000, bond_shares=40000)
        p.holdings[0].shares = 0  # 只剩 0050 是大部位
        p.holdings[1].shares = 30000
        s = evaluate(p, PRICES, as_of=date(2026, 1, 1))
        self.assertGreater(dict(s.concentration())["0050"], Strategy().max_single_position)
        _, advice = advise(s, Strategy(), self.up)
        self.assertFalse(any(a.level == "分散" for a in advice))

    def test_trend_requires_enough_history(self):
        self.assertFalse(compute_trend("0050", [1.0] * 10, 60).known)
        t = compute_trend("0050", list(range(1, 61)), 60)
        self.assertAlmostEqual(t.ma, 30.5)
        self.assertTrue(t.up)


class DashboardTest(unittest.TestCase):
    def test_funding_splits(self):
        p = make_portfolio(cash=300_000)
        p.holdings.append(Holding(symbol="2882", category="trust", shares=1000, cost=50_000))
        p.loans[0].collateral.append(Collateral("2882", 1000))  # 信託不可質押，應被忽略
        s = evaluate(p, {**PRICES, "2882": 60.0}, as_of=date(2026, 1, 1))
        rows = {x.label: x for x in funding_splits(s)}
        tsmc, etf, trust, cash = rows["2330"], rows["0050"], rows["2882"], rows["現金"]
        self.assertAlmostEqual(tsmc.own_pledged, 2_000_000)
        self.assertAlmostEqual(tsmc.own_free, 1_000_000)
        self.assertAlmostEqual(etf.borrowed, 900_000)
        self.assertAlmostEqual(etf.own_free, 900_000)
        self.assertAlmostEqual(trust.own_free, 60_000)
        self.assertEqual(trust.borrowed + trust.own_pledged, 0)
        # 借 1,000,000、登記買進 800,000 → 200,000 視為還在現金中
        self.assertAlmostEqual(cash.borrowed, 200_000)
        self.assertAlmostEqual(cash.own_free, 100_000)
        # 每個部位拆分後總和等於市值
        for pos in s.positions:
            self.assertAlmostEqual(rows[pos.holding.symbol].total, pos.market_value)

    def test_render_escapes_names(self):
        p = make_portfolio()
        p.holdings[0].name = "<script>x</script>"
        s = evaluate(p, PRICES, as_of=date(2026, 1, 1))
        verdict, advice = advise(s, Strategy(), Trend("0050", None, None, 0))
        page = render_dashboard(s, Strategy(), Trend("0050", None, None, 0), verdict, advice)
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("我的錢", page)


class PricesTest(unittest.TestCase):
    def test_parse_tables(self):
        twse = [{"Code": "2330", "ClosingPrice": "1,005.00", "Date": "1150903"}, {"Code": "9999", "ClosingPrice": "--"}]
        p, d = prices.parse_market_table(twse)
        self.assertEqual(p, {"2330": 1005.0})
        self.assertEqual(d, date(2026, 9, 3))
        tpex = [{"SecuritiesCompanyCode": "00679B", "Close": "28.10", "Date": "115/09/03"}]
        p, d = prices.parse_market_table(tpex)
        self.assertEqual(p, {"00679B": 28.1})
        self.assertEqual(d, date(2026, 9, 3))


class BuildPricesTest(unittest.TestCase):
    def test_build_merges_sources(self):
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("build_prices", ROOT / "scripts" / "build_prices.py")
        bp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bp)

        def fake_get(url, timeout=20):
            if url == prices.TWSE_ALL_URL:
                return [{"Code": "2330", "ClosingPrice": "1,005.00", "Date": "1151002"}]
            if url == prices.TPEX_ALL_URL:
                return [{"SecuritiesCompanyCode": "00679B", "Close": "28.10", "Date": "1151002"}]
            raise OSError("offline")

        def fake_month(sym, y, m):
            return {date(y, m, 1): 150.0 + m}

        quotes = prices.Quotes(prices={"TLT": 90.0, "TWD=X": 32.1})
        with mock.patch.object(bp.px, "_get_json", fake_get), \
             mock.patch.object(bp.px, "fetch_twse_month", fake_month), \
             mock.patch.object(bp.px, "fetch_quotes", return_value=quotes):
            data, errors = bp.build(date(2026, 10, 5))
        self.assertEqual(data["date"], "2026-10-02")
        self.assertEqual(data["twse"], {"2330": 1005.0})
        self.assertEqual(data["tpex"], {"00679B": 28.1})
        self.assertEqual(data["us"], {"TLT": 90.0})
        self.assertEqual(data["usd_twd"], 32.1)
        self.assertEqual(len(data["history"]["0050"]), bp.HISTORY_MONTHS)
        self.assertEqual(data["history"]["0050"][0][0], "2026-07-01")
        self.assertEqual(errors, [])
        self.assertEqual(data["dates"], {"twse": "2026-10-02", "tpex": "2026-10-02"})

        # 櫃買抓取失敗時沿用上次的資料，並保留它原本的日期
        def tpex_down(url, timeout=20):
            if url == prices.TPEX_ALL_URL:
                raise prices.PriceError("IncompleteRead")
            return fake_get(url, timeout)

        previous = {"tpex": {"00679B": 27.9}, "dates": {"tpex": "2026-10-01"}}
        with mock.patch.object(bp.px, "_get_json", tpex_down), \
             mock.patch.object(bp.px, "fetch_twse_month", fake_month), \
             mock.patch.object(bp.px, "fetch_quotes", return_value=quotes):
            data, errors = bp.build(date(2026, 10, 5), previous)
        self.assertEqual(data["tpex"], {"00679B": 27.9})
        self.assertEqual(data["dates"], {"twse": "2026-10-02", "tpex": "2026-10-01"})
        self.assertEqual(data["date"], "2026-10-02")
        self.assertTrue(any("沿用" in e for e in errors))


class CathayImportTest(unittest.TestCase):
    def test_parse_cp950_with_title_row(self):
        content = (
            "國泰證券 庫存查詢\n"
            "股票代號,股票名稱,庫存股數,付出成本\n"
            '2330,台積電,"2,000","1,600,000"\n'
            "2330,台積電,500,450000\n"
            "00679B,元大美債20年,10000,300000\n"
            "合計,,,2350000\n"
        )
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "inv.csv"
            f.write_bytes(content.encode("cp950"))
            rows = {r["symbol"]: r for r in cathay.parse(f)}
        self.assertEqual(rows["2330"]["shares"], 2500)
        self.assertEqual(rows["2330"]["cost"], 2_050_000)
        self.assertEqual(rows["00679B"]["category"], "bond_etf")
        self.assertEqual(len(rows), 2)

    def test_custom_mapping_and_lots(self):
        content = "商品,張數\n2330,3\n"
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "inv.csv"
            f.write_text(content, encoding="utf-8")
            rows = cathay.parse(f, mapping={"商品": "symbol", "張數": "shares"}, lots=True)
        self.assertEqual(rows[0]["shares"], 3000)


class CliTest(unittest.TestCase):
    def test_example_config_daily_offline(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "portfolio.toml"
            shutil.copy(ROOT / "portfolio.example.toml", cfg)
            loaded = load(cfg)
            self.assertEqual(len(loaded.holdings), 4)
            self.assertEqual(loaded.cash, 200000)
            args = ["-c", str(cfg), "daily", "--offline", "--date", "2026-10-05",
                    "--price", "2330=1000", "--price", "0050=180", "--price", "2882=60", "--price", "00679B=30"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(args)
            out = buf.getvalue()
            self.assertEqual(code, 0, out)
            self.assertIn("股票等效槓桿", out)
            self.assertIn("質押損益", out)
            self.assertTrue((Path(d) / "reports" / "2026-10-05.md").exists())
            self.assertTrue((Path(d) / "reports" / "dashboard.html").exists())
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["-c", str(cfg), "history"])
            self.assertIn("2026-10-05", buf.getvalue())


@unittest.skipUnless(shutil.which("node"), "需要 Node.js")
class WebCoreParityTest(unittest.TestCase):
    """手機版（web/core.js）與 Python 版的計算結果必須一致。"""

    def test_same_numbers(self):
        from asset_tracker.advisor import compute_trend as py_trend
        from asset_tracker.storage import Store

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "portfolio.toml"
            shutil.copy(ROOT / "portfolio.example.toml", cfg)
            portfolio = load(cfg)
            store = Store(portfolio.db_path)
            prices = {"2330": 1000.0, "0050": 180.0, "2882": 60.0, "00679B": 30.0}
            store.save_prices(date(2026, 10, 5), prices, "test")
            for i in range(60):  # 讓趨勢判斷有足夠資料
                store.save_prices(date(2026, 6, 1) + __import__("datetime").timedelta(days=i), {"0050": 150.0 + i * 0.1}, "test")
            data = cli.portfolio_to_json(portfolio, store)
            history = store.price_history("0050", date(2026, 10, 5), 60)
            store.close()
            f = Path(d) / "export.json"
            f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            out = subprocess.run(
                ["node", str(ROOT / "tests" / "web_core_eval.js"), str(f), "2026-10-05"],
                capture_output=True, text=True, check=True,
            )
        js = json.loads(out.stdout)

        s = evaluate(portfolio, prices, as_of=date(2026, 10, 5))
        trend = py_trend("0050", history, 60)
        verdict, advice = advise(s, portfolio.strategy, trend)
        for key, py in (
            ("gross", s.gross_assets), ("liabilities", s.liabilities), ("net", s.net_equity),
            ("assetLeverage", s.asset_leverage), ("equityLeverage", s.equity_leverage),
            ("bondRatio", s.bond_ratio), ("minMaintenance", s.min_maintenance), ("pledgePnl", s.pledge_pnl),
        ):
            self.assertAlmostEqual(js[key], py, places=6, msg=key)
        self.assertEqual(js["verdict"], verdict)
        self.assertEqual(js["levels"], [a.level for a in advice])
        for a, b in zip(js["amounts"], [a.amount for a in advice]):
            if b is None:
                self.assertIsNone(a)
            else:
                self.assertAlmostEqual(a, b, places=4)
        py_splits = [(("CASH" if x.label == "現金" else x.label.split()[0]), x.own_free, x.own_pledged, x.borrowed)
                     for x in funding_splits(s)]
        self.assertEqual(len(js["splits"]), len(py_splits))
        for (js_sym, *js_v), (py_sym, *py_v) in zip(js["splits"], py_splits):
            self.assertEqual(js_sym, py_sym)
            for a, b in zip(js_v, py_v):
                self.assertAlmostEqual(a, b, places=6)


if __name__ == "__main__":
    unittest.main()
