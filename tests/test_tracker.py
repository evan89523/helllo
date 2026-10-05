import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from asset_tracker import cli, prices
from asset_tracker.advisor import Trend, advise, compute_trend
from asset_tracker.brokers import cathay
from asset_tracker.config import Collateral, Holding, Loan, Portfolio, Purchase, Strategy, load
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

    def test_trend_requires_enough_history(self):
        self.assertFalse(compute_trend("0050", [1.0] * 10, 60).known)
        t = compute_trend("0050", list(range(1, 61)), 60)
        self.assertAlmostEqual(t.ma, 30.5)
        self.assertTrue(t.up)


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
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(["-c", str(cfg), "history"])
            self.assertIn("2026-10-05", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
