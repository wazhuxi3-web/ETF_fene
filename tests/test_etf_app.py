import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from openpyxl import Workbook

from etf_database import ETFDatabase
from etf_fetcher import (
    ETFNetworkError,
    check_sse_connection,
    check_szse_connection,
    fetch_szse_rows_with_daily_fallback,
    fetch_szse_rows_for_range,
    probe_szse_connection,
    normalize_worker_count,
    parse_sse_payload,
    parse_sse_table_html,
    parse_szse_payload,
    split_szse_date_ranges,
    split_date_ranges,
)
from szse_download_fetcher import (
    check_szse_download_connection,
    collect_szse_rows_from_downloads,
    parse_szse_download_file,
    split_szse_download_batch_ranges,
    split_szse_download_ranges,
    validate_szse_download_batch_range,
)
from etf_web_app import ETFWebServer, HTML, parse_web_endpoint


class ParseSSEPayloadTests(unittest.TestCase):
    def test_parses_jsonp_pagehelp_rows(self):
        payload = {
            "pageHelp": {
                "data": [
                    {
                        "STAT_DATE": "2026-07-06",
                        "SEC_CODE": "510300",
                        "SEC_NAME": "沪深300ETF华泰柏瑞",
                        "TOT_VOL": "1682148.77",
                    }
                ]
            }
        }
        rows = parse_sse_payload("callback(" + json.dumps(payload, ensure_ascii=False) + ")")

        self.assertEqual(
            rows,
            [
                {
                    "trade_date": "2026-07-06",
                    "fund_code": "510300",
                    "fund_name": "沪深300ETF华泰柏瑞",
                    "total_share": 1682148.77,
                }
            ],
        )

    def test_parses_table_html_rows(self):
        html = """
        <table><thead><tr><th>日期</th><th>基金代码</th><th>基金扩位简称</th><th>总份额（万份）</th></tr></thead>
        <tbody><tr><td>2026-07-06</td><td>510300</td><td>沪深300ETF华泰柏瑞</td><td>1,682,148.77</td></tr></tbody></table>
        """

        rows = parse_sse_table_html(html)

        self.assertEqual(
            rows,
            [
                {
                    "trade_date": "2026-07-06",
                    "fund_code": "510300",
                    "fund_name": "沪深300ETF华泰柏瑞",
                    "total_share": 1682148.77,
                }
            ],
        )

    def test_check_connection_reports_success_or_failure(self):
        ok, message = check_sse_connection("2026-07-06", fetch_func=lambda date: [])
        self.assertTrue(ok)
        self.assertIn("连通", message)

        def fail(date):
            raise ETFNetworkError("boom")

        ok, message = check_sse_connection("2026-07-06", fetch_func=fail)
        self.assertFalse(ok)
        self.assertIn("boom", message)

    def test_normalize_worker_count_bounds_threads(self):
        self.assertEqual(normalize_worker_count("bad"), 16)
        self.assertEqual(normalize_worker_count("0"), 1)
        self.assertEqual(normalize_worker_count("99"), 64)
        self.assertEqual(normalize_worker_count("8"), 8)


class ParseSZSEPayloadTests(unittest.TestCase):
    def test_parses_szse_wan_share_and_normalizes_to_real_share(self):
        text = (
            '[{"data":[{"size_date":"2026-07-08",'
            '"fund_code":"159001","security_short_name":"ETF A",'
            '"current_size":"1,670.34"}]}]'
        )

        rows = parse_szse_payload(text)

        self.assertEqual(rows[0]["total_share"], 16703400.0)
        self.assertEqual(rows[0]["exchange"], "SZSE")
        self.assertEqual(rows[0]["share_unit"], "share")

    def test_splits_szse_ranges_into_at_most_six_months(self):
        ranges = list(split_date_ranges("2025-01-01", "2026-07-08"))

        self.assertEqual(ranges[0], ("2025-01-01", "2025-06-30"))
        self.assertEqual(ranges[-1][1], "2026-07-08")

    def test_splits_szse_collection_ranges_by_month(self):
        ranges = list(split_szse_date_ranges("2016-01-01", "2016-04-15"))

        self.assertEqual(
            ranges,
            [
                ("2016-01-01", "2016-01-31"),
                ("2016-02-01", "2016-02-29"),
                ("2016-03-01", "2016-03-31"),
                ("2016-04-01", "2016-04-15"),
            ],
        )

    def test_szse_month_chunk_falls_back_to_trade_dates(self):
        seen = []

        def range_fetcher(start, end, **kwargs):
            seen.append((start, end))
            if (start, end) == ("2016-01-01", "2016-01-31"):
                raise ETFNetworkError("month too large")
            return [
                {
                    "trade_date": start,
                    "fund_code": "159001",
                    "fund_name": "ETF A",
                    "total_share": 1,
                }
            ]

        rows = fetch_szse_rows_with_daily_fallback(
            "2016-01-01",
            "2016-01-31",
            workers=1,
            sleep_func=lambda seconds: None,
            trade_dates=["2016-01-04", "2016-01-05"],
            range_fetcher=range_fetcher,
        )

        self.assertEqual(
            seen,
            [
                ("2016-01-01", "2016-01-31"),
                ("2016-01-04", "2016-01-04"),
                ("2016-01-05", "2016-01-05"),
            ],
        )
        self.assertEqual([row["trade_date"] for row in rows], ["2016-01-04", "2016-01-05"])

    def test_szse_fetch_uses_date_range_and_paginates(self):
        seen = []
        seen_referers = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'[{"metadata":{"pagecount":1},"data":[]}]'

        def opener(request, timeout):
            seen.append(request.full_url)
            seen_referers.append(request.headers.get("Referer"))
            return FakeResponse()

        self.assertEqual(
            fetch_szse_rows_for_range(
                "2026-07-08", "2026-07-08", opener=opener
            ),
            [],
        )
        self.assertIn("txtStart=2026-07-08", seen[0])
        self.assertIn("txtEnd=2026-07-08", seen[0])
        self.assertEqual(
            seen_referers[0], "https://www.szse.cn/market/fund/volume/etf/index.html"
        )

    def test_check_szse_connection_uses_szse_fetcher(self):
        seen = []

        def fetcher(start, end):
            seen.append((start, end))
            return []

        ok, message = check_szse_connection("2026-07-08", fetch_func=fetcher)

        self.assertTrue(ok)
        self.assertEqual(seen, [("2026-07-08", "2026-07-08")])
        self.assertIn("接口", message)

    def test_szse_connection_probe_only_requests_first_page(self):
        seen = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'[{"metadata":{"pagecount":35},"data":[]}]'

        def opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse()

        self.assertTrue(
            probe_szse_connection(
                "2026-07-08", opener=opener, sleep_func=lambda seconds: None
            )
        )
        self.assertEqual(len(seen), 1)
        self.assertIn("PAGENO=1", seen[0])

    def test_szse_range_moves_weekend_boundaries_to_weekdays(self):
        seen = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'[{"metadata":{"pagecount":1},"data":[]}]'

        def opener(request, timeout):
            seen.append(request.full_url)
            return FakeResponse()

        fetch_szse_rows_for_range(
            "2026-07-04", "2026-07-12", opener=opener, workers=1
        )

        self.assertIn("txtStart=2026-07-06", seen[0])
        self.assertIn("txtEnd=2026-07-10", seen[0])

    def test_szse_page_retries_transient_disconnect(self):
        attempts = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'[{"metadata":{"pagecount":1},"data":[]}]'

        def opener(request, timeout):
            attempts.append(request.full_url)
            if len(attempts) == 1:
                raise OSError("Remote end closed connection without response")
            return FakeResponse()

        self.assertEqual(
            fetch_szse_rows_for_range(
                "2026-07-08",
                "2026-07-08",
                opener=opener,
                workers=1,
                retries=2,
                sleep_func=lambda seconds: None,
            ),
            [],
        )
        self.assertEqual(len(attempts), 2)


class SZSEDownloadFetcherTests(unittest.TestCase):
    def _write_download_file(self, path: Path, size_header: str = "基金规模(份)") -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["日期", "基金代码", "基金简称", size_header])
        sheet.append(["2026-07-10", "159001", "货币ETF易方达", "22,029,288.00"])
        sheet.append(["2026-07-10", "159003", "招商快线ETF", 1879250])
        workbook.save(path)

    def test_split_download_ranges_uses_fifteen_day_chunks(self):
        ranges = list(split_szse_download_ranges("2026-01-01", "2026-02-05"))

        self.assertEqual(
            ranges,
            [
                ("2026-01-01", "2026-01-15"),
                ("2026-01-16", "2026-01-30"),
                ("2026-01-31", "2026-02-05"),
            ],
        )

    def test_splits_large_download_ranges_into_five_month_batches(self):
        ranges = list(split_szse_download_batch_ranges("2026-01-01", "2026-07-12"))

        self.assertEqual(
            ranges,
            [
                ("2026-01-01", "2026-05-31"),
                ("2026-06-01", "2026-07-12"),
            ],
        )

    def test_parse_download_file_keeps_share_unit_when_file_is_in_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "基金规模.xlsx"
            self._write_download_file(path, "基金规模(份)")

            rows = parse_szse_download_file(path)

        self.assertEqual(rows[0]["trade_date"], "2026-07-10")
        self.assertEqual(rows[0]["fund_code"], "159001")
        self.assertEqual(rows[0]["fund_name"], "货币ETF易方达")
        self.assertEqual(rows[0]["total_share"], 22029288.0)
        self.assertEqual(rows[0]["exchange"], "SZSE")
        self.assertEqual(rows[0]["source"], "szse_download")

    def test_parse_download_file_multiplies_when_file_is_in_wan_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "基金规模.xlsx"
            self._write_download_file(path, "基金规模(万份)")

            rows = parse_szse_download_file(path)

        self.assertEqual(rows[0]["total_share"], 220292880000.0)

    def test_collect_downloads_merges_all_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "基金规模.xlsx"
            self._write_download_file(path)
            seen = []

            def downloader(start, end):
                seen.append((start, end))
                return path

            rows = collect_szse_rows_from_downloads(
                "2026-01-01", "2026-01-20", downloader
            )

        self.assertEqual(seen, [("2026-01-01", "2026-01-15"), ("2026-01-16", "2026-01-20")])
        self.assertEqual(len(rows), 4)

    def test_collect_downloads_accepts_ranges_over_five_months(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "鍩洪噾瑙勬ā.xlsx"
            self._write_download_file(path)
            seen = []

            def downloader(start, end):
                seen.append((start, end))
                return path

            rows = collect_szse_rows_from_downloads(
                "2026-01-01", "2026-06-02", downloader
            )

        self.assertIn(("2026-05-31", "2026-05-31"), seen)
        self.assertIn(("2026-06-01", "2026-06-02"), seen)
        self.assertGreater(len(rows), 0)


    def test_collect_downloads_skips_completed_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "szse.xlsx"
            self._write_download_file(path)
            seen = []

            def downloader(start, end):
                seen.append((start, end))
                return path

            rows = collect_szse_rows_from_downloads(
                "2026-01-01",
                "2026-01-20",
                downloader,
                should_skip_range=lambda start, end: start == "2026-01-01",
            )

        self.assertEqual(seen, [("2026-01-16", "2026-01-20")])
        self.assertEqual(len(rows), 2)

    def test_browser_download_runs_headless_by_default(self):
        source = Path("szse_download_fetcher.py").read_text(encoding="utf-8")

        self.assertIn("visible: bool = False", source)
        self.assertIn("headless=not visible", source)

    def test_download_connection_uses_browser_download_path(self):
        seen = []

        def fetcher(start, end, **kwargs):
            seen.append((start, end, kwargs.get("visible")))
            return []

        ok, message = check_szse_download_connection("2026-07-10", fetch_func=fetcher)

        self.assertTrue(ok)
        self.assertEqual(seen, [("2026-07-10", "2026-07-10", False)])
        self.assertIn("浏览器下载", message)


class ETFDatabaseTests(unittest.TestCase):
    def test_existing_trade_dates_are_scoped_by_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            db.upsert_rows(
                [
                    {
                        "trade_date": "2026-07-01",
                        "fund_code": "510300",
                        "fund_name": "ETF A",
                        "total_share": 100.0,
                        "exchange": "SSE",
                    },
                    {
                        "trade_date": "2026-07-02",
                        "fund_code": "159001",
                        "fund_name": "ETF B",
                        "total_share": 200.0,
                        "exchange": "SZSE",
                    },
                ]
            )

            self.assertEqual(
                db.existing_trade_dates("SSE", "2026-07-01", "2026-07-03"),
                {"2026-07-01"},
            )
            self.assertEqual(
                db.existing_trade_dates("SZSE", "2026-07-01", "2026-07-03"),
                {"2026-07-02"},
            )

    def test_migrates_legacy_etf_table_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stock_data.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE ETF (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_date TEXT NOT NULL,
                        fund_code TEXT NOT NULL,
                        fund_name TEXT NOT NULL,
                        total_share REAL NOT NULL,
                        share_delta REAL,
                        source TEXT NOT NULL DEFAULT 'sse_commonQuery',
                        updated_at TEXT NOT NULL,
                        UNIQUE(trade_date, fund_code)
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO ETF (trade_date, fund_code, fund_name, total_share, updated_at) "
                    "VALUES ('2026-07-08', '159001', 'ETF A', 100, 'now')"
                )
                conn.commit()

            db = ETFDatabase(path)
            db.initialize()
            db.upsert_rows(
                [
                    {
                        "trade_date": "2026-07-08",
                        "fund_code": "159001",
                        "fund_name": "ETF A",
                        "total_share": 200,
                        "exchange": "SZSE",
                    }
                ]
            )

            self.assertEqual(db.get_stats()["rows_count"], 2)

    def test_share_delta_is_scoped_by_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            rows = []
            for exchange, first, second in (("SSE", 100, 110), ("SZSE", 200, 250)):
                rows.extend(
                    [
                        {
                            "trade_date": "2026-07-07",
                            "fund_code": "159001",
                            "fund_name": "ETF A",
                            "total_share": first,
                            "exchange": exchange,
                        },
                        {
                            "trade_date": "2026-07-08",
                            "fund_code": "159001",
                            "fund_name": "ETF A",
                            "total_share": second,
                            "exchange": exchange,
                        },
                    ]
                )
            db.upsert_rows(rows)

            deltas = {
                (row["exchange"], row["trade_date"]): row["share_delta"]
                for row in db.get_history(["159001"])
            }
            self.assertEqual(deltas[("SSE", "2026-07-08")], 10)
            self.assertEqual(deltas[("SZSE", "2026-07-08")], 50)

    def test_same_code_from_two_exchanges_coexists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            base = {
                "trade_date": "2026-07-08",
                "fund_code": "159001",
                "fund_name": "ETF A",
            }

            db.upsert_rows(
                [
                    {**base, "total_share": 100.0, "exchange": "SSE"},
                    {**base, "total_share": 200.0, "exchange": "SZSE"},
                ]
            )

            self.assertEqual(db.get_stats()["rows_count"], 2)
            self.assertEqual(
                sorted(row["exchange"] for row in db.get_history(["159001"])),
                ["SSE", "SZSE"],
            )

    def test_upserts_rows_and_calculates_share_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()

            db.upsert_rows(
                [
                    {
                        "trade_date": "2026-07-05",
                        "fund_code": "510300",
                        "fund_name": "沪深300ETF华泰柏瑞",
                        "total_share": 100.0,
                    },
                    {
                        "trade_date": "2026-07-06",
                        "fund_code": "510300",
                        "fund_name": "沪深300ETF华泰柏瑞",
                        "total_share": 125.5,
                    },
                ]
            )

            history = db.get_history(["510300"])
            self.assertEqual(len(history), 2)
            self.assertIsNone(history[0]["share_delta"])
            self.assertEqual(history[1]["share_delta"], 25.5)

            db.upsert_rows(
                [
                    {
                        "trade_date": "2026-07-06",
                        "fund_code": "510300",
                        "fund_name": "沪深300ETF华泰柏瑞",
                        "total_share": 126.0,
                    }
                ]
            )

            latest = db.list_latest_etfs()
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]["total_share"], 126.0)

    def test_can_defer_delta_recalculation_for_batch_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            db.upsert_rows(
                [
                    {
                        "trade_date": "2026-07-05",
                        "fund_code": "510300",
                        "fund_name": "ETF A",
                        "total_share": 100.0,
                    },
                    {
                        "trade_date": "2026-07-06",
                        "fund_code": "510300",
                        "fund_name": "ETF A",
                        "total_share": 130.0,
                    },
                ],
                recalculate=False,
            )

            self.assertIsNone(db.get_history(["510300"])[1]["share_delta"])

            db.recalculate_deltas(["510300"])

            self.assertEqual(db.get_history(["510300"])[1]["share_delta"], 30.0)

    def test_searches_by_name_or_code_and_sorts_by_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            db.upsert_rows(
                [
                    {
                        "trade_date": "2026-07-06",
                        "fund_code": "510300",
                        "fund_name": "沪深300ETF华泰柏瑞",
                        "total_share": 100.0,
                    },
                    {
                        "trade_date": "2026-07-06",
                        "fund_code": "510050",
                        "fund_name": "上证50ETF华夏",
                        "total_share": 200.0,
                    },
                    {
                        "trade_date": "2026-07-06",
                        "fund_code": "510500",
                        "fund_name": "ETF测试",
                        "total_share": 50.0,
                    },
                ]
            )

            by_name = db.list_latest_etfs("华夏", "share_desc")
            by_code = db.list_latest_etfs("5103", "share_desc")
            by_keyword = db.list_latest_etfs("ETF", "share_desc")

            self.assertEqual([r["fund_code"] for r in by_name], ["510050"])
            self.assertEqual([r["fund_code"] for r in by_code], ["510300"])
            self.assertEqual([r["fund_code"] for r in by_keyword], ["510050", "510300", "510500"])

    def test_history_includes_stock_daily_price_and_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            db.upsert_rows(
                [
                    {
                        "trade_date": "2026-07-06",
                        "fund_code": "510300",
                        "fund_name": "ETF A",
                        "total_share": 100.0,
                    }
                ]
            )

            with closing(db.connect()) as conn:
                conn.execute(
                    """
                    CREATE TABLE stock_daily (
                        股票代码 TEXT,
                        日期 INTEGER,
                        开盘价 REAL,
                        最高价 REAL,
                        最低价 REAL,
                        收盘价 REAL,
                        成交量 REAL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO stock_daily
                    (股票代码, 日期, 开盘价, 最高价, 最低价, 收盘价, 成交量)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("510300", 20260706, 4.12, 4.78, 4.01, 4.56, 123456.0),
                )
                conn.commit()

            history = db.get_history(["510300"])

            self.assertEqual(history[0]["open_price"], 4.12)
            self.assertEqual(history[0]["high_price"], 4.78)
            self.assertEqual(history[0]["low_price"], 4.01)
            self.assertEqual(history[0]["close_price"], 4.56)
            self.assertEqual(history[0]["volume"], 123456.0)


class ETFWebServerTests(unittest.TestCase):
    def test_chart_toolbar_has_full_range_controls(self):
        self.assertIn('id="fullRange"', HTML)
        self.assertIn('id="latestRange"', HTML)

    def test_chart_draws_stock_daily_close_price(self):
        self.assertIn("close_price", HTML)
        self.assertIn("收盘价", HTML)

    def test_chart_has_price_and_share_visibility_toggles(self):
        self.assertIn('id="togglePrice"', HTML)
        self.assertIn('id="toggleShare"', HTML)
        self.assertIn('id="toggleKline"', HTML)
        self.assertIn('id="toggleVolume"', HTML)
        self.assertIn('id="price"', HTML)
        self.assertIn('id="volume"', HTML)
        self.assertIn('id="shareLine"', HTML)
        self.assertIn('id="hoverInfo"', HTML)
        self.assertIn("hover-kline", HTML)
        self.assertIn("hover-share", HTML)
        self.assertIn("隐藏股价", HTML)
        self.assertIn("隐藏份额", HTML)
        self.assertIn("K线", HTML)
        self.assertIn("成交量", HTML)
        self.assertIn("drawCandle", HTML)
        self.assertNotIn("虚线=收盘价", HTML)

    def test_etf_list_shows_exchange_source(self):
        self.assertIn("e.exchange", HTML)
        self.assertIn("深交所", HTML)

    def test_parse_web_endpoint_validates_host_and_port(self):
        self.assertEqual(parse_web_endpoint("127.0.0.1", "8877"), ("127.0.0.1", 8877))

        with self.assertRaises(ValueError):
            parse_web_endpoint("", "8877")
        with self.assertRaises(ValueError):
            parse_web_endpoint("127.0.0.1", "70000")

    def test_configure_stops_running_server_when_endpoint_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = ETFWebServer(Path(tmp) / "stock_data.db", port=0)
            try:
                server.start()
                self.assertIsNotNone(server.server)

                server.configure("127.0.0.1", 0)

                self.assertIsNone(server.server)
                self.assertEqual(server.host, "127.0.0.1")
                self.assertEqual(server.port, 0)
            finally:
                server.stop()


class ETFGuiTests(unittest.TestCase):
    def test_gui_contains_exchange_selector_and_szse_dispatch(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("深交所", source)
        self.assertIn("沪深两市", source)
        self.assertIn("fetch_szse_rows_via_browser_downloads", source)

    def test_gui_removed_old_szse_api_chunk_path(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertNotIn("failed_start", source)
        self.assertNotIn("_fetch_szse_chunk_with_daily_fallback", source)

    def test_gui_uses_szse_browser_download_mode(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("fetch_szse_rows_via_browser_downloads", source)
        self.assertNotIn("validate_szse_download_batch_range(start, end)", source)

    def test_gui_connection_test_uses_selected_exchange_when_not_paused(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("exchanges = (self.paused_task[1],) if self.paused_task", source)
        self.assertIn("else self._selected_exchanges()", source)

    def test_gui_connection_test_uses_szse_browser_download_probe(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("check_szse_download_connection", source)
        self.assertNotIn("check_szse_connection(test_date)", source)

    def test_gui_entrypoint_writes_crash_log(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("ETF_CRASH_LOG", source)
        self.assertIn("ETF_RUNTIME_LOG", source)
        self.assertIn("faulthandler.enable", source)
        self.assertIn("sys.excepthook", source)
        self.assertIn("threading.excepthook", source)
        self.assertIn("report_callback_exception", source)
        self.assertIn("traceback.format_exc()", source)
        self.assertIn("etf_crash.log", source)
        self.assertIn("etf_runtime.log", source)

    def test_network_diagnosis_includes_szse_host(self):
        source = Path("etf_fetcher.py").read_text(encoding="utf-8")

        self.assertIn('"www.szse.cn"', source)

    def test_szse_range_preflights_real_szse_connection_before_threads(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("fetch_szse_rows_via_browser_downloads", source)

    def test_gui_skips_existing_dates_before_fetching(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn('existing_trade_dates("SSE"', source)
        self.assertIn('existing_trade_dates("SZSE"', source)
        self.assertIn("existing_trade_dates(exchange, date, date)", source)
        self.assertIn("should_skip_range=", source)

    def test_gui_filters_existing_szse_dates_before_writing(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("rows_before_filter = len(rows)", source)
        self.assertIn('row.get("trade_date") not in existing_dates', source)
        self.assertIn("深交所过滤数据库已有日期", source)


if __name__ == "__main__":
    unittest.main()
