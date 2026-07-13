import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
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
from sse_pcf_fetcher import (
    normalize_pcf_info,
    normalize_pcf_item,
    parse_sse_pcf_html,
    parse_sse_pcf_xml,
    parse_sse_pcf_download,
    build_sse_pcf_download_url,
    fetch_sse_pcf_for_fund,
)
from szse_pcf_fetcher import (
    SZSEPCFReference,
    choose_szse_substitute_amount,
    extract_szse_pcf_references,
    parse_szse_pcf_download,
)
from etf_web_app import ETFWebServer, HTML, parse_web_endpoint


class PCFNormalizationTests(unittest.TestCase):
    def test_normalizes_info_with_chinese_columns_and_null_missing_values(self):
        info = normalize_pcf_info(
            {
                "最新公告日期": "2026-07-13",
                "内容日期": "2026-07-13",
                "基金代码": "510010",
                "基金名称": "治理ETF",
                "现金差额": "21,388.57",
                "现金替代比例上限": "30%",
            }
        )

        self.assertEqual(info["基金代码"], "510010")
        self.assertEqual(info["现金差额"], 21388.57)
        self.assertEqual(info["现金替代比例上限"], 30.0)
        self.assertIsNone(info["申购赎回模式"])
        self.assertEqual(info["交易所"], "SSE")

    def test_normalizes_item_fields(self):
        item = normalize_pcf_item(
            {
                "证券代码": "600009",
                "证券简称": "上海机场",
                "股票数量": "300",
                "现金替代标志": "允许",
                "申购现金替代溢价比例": "34%",
                "替代金额": "-",
                "挂牌市场": "上海证券交易所",
            }
        )

        self.assertEqual(item["股票数量"], 300.0)
        self.assertEqual(item["申购现金替代溢价比例"], 34.0)
        self.assertIsNone(item["替代金额"])


class PCFParserTests(unittest.TestCase):
    def test_parses_announcement_content_and_component_rows(self):
        html = """
        <h2>申购赎回清单</h2>
        <table><tr><td>最新公告日期</td><td>2026-07-13</td></tr>
        <tr><td>基金名称</td><td>上证180公司治理ETF</td></tr>
        <tr><td>基金管理公司名称</td><td>交银施罗德基金管理有限公司</td></tr>
        <tr><td>基金代码</td><td>510010</td></tr></table>
        <h2>2026-07-10日内容信息</h2>
        <table><tr><td>现金差额(单位：元)</td><td>21388.57</td></tr>
        <tr><td>基金份额净值(单位：元)</td><td>1.6840</td></tr></table>
        <h2>2026-07-13日内容信息</h2>
        <table><tr><td>现金替代比例上限</td><td>30%</td></tr>
        <tr><td>最小申购、赎回单位(单位:份)</td><td>1000000</td></tr>
        <tr><td>申购赎回模式</td><td>沪市成分证券实物对价</td></tr></table>
        <h2>成份股信息内容</h2>
        <table><thead><tr><th>证券代码</th><th>证券简称</th>
        <th>股票数量(股)</th><th>现金替代标志</th>
        <th>申购现金替代溢价比例</th><th>赎回现金替代折价比例</th>
        <th>替代金额(单位：人民币元)</th><th>挂牌市场</th></tr></thead>
        <tbody><tr><td>600009</td><td>上海机场</td><td>300</td><td>允许</td>
        <td>34%</td><td>0%</td><td>-</td><td>上海证券交易所</td></tr></tbody></table>
        """

        info, items = parse_sse_pcf_html(html, "510010")

        self.assertEqual(info["基金代码"], "510010")
        self.assertEqual(info["内容日期"], "2026-07-13")
        self.assertEqual(info["现金差额"], 21388.57)
        self.assertEqual(info["现金替代比例上限"], 30.0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["证券代码"], "600009")
        self.assertEqual(items[0]["股票数量"], 300.0)
        self.assertNotIn("(单位：元)", "".join(info.keys()))

    def test_parses_official_xml_and_maps_current_content_date(self):
        xml = """
        <SSEPortfolioCompositionFile>
          <FundInstrumentID>510010</FundInstrumentID>
          <CreationRedemptionUnit>1000000</CreationRedemptionUnit>
          <TradingDay>20260713</TradingDay>
          <PreTradingDay>20260710</PreTradingDay>
          <NAVperCU>1683754.39</NAVperCU>
          <NAV>1.684</NAV>
          <PreCashComponent>21388.57</PreCashComponent>
          <EstimatedCashComponent>21247.09</EstimatedCashComponent>
          <MaxCashRatio>0.3</MaxCashRatio>
          <PublishIOPVFlag>1</PublishIOPVFlag>
          <CreationRedemptionMechanism>1</CreationRedemptionMechanism>
          <ComponentList>
            <Component>
              <InstrumentID>600009</InstrumentID>
              <InstrumentName>上海机场</InstrumentName>
              <Quantity>300</Quantity>
              <SubstitutionFlag>1</SubstitutionFlag>
              <CreationPremiumRate>0.34</CreationPremiumRate>
              <RedemptionDiscountRate>0</RedemptionDiscountRate>
              <UnderlyingSecurityID>101</UnderlyingSecurityID>
            </Component>
          </ComponentList>
        </SSEPortfolioCompositionFile>
        """
        info, items = parse_sse_pcf_xml(
            xml,
            "510010",
            api_info={
                "FUND_NAME": "治理ETF",
                "FUND_COMP_NAME": "交银施罗德基金管理有限公司",
                "TRADING_DAY": "20260713",
            },
        )
        self.assertEqual(info["基金代码"], "510010")
        self.assertEqual(info["内容日期"], "2026-07-13")
        self.assertEqual(info["基金名称"], "治理ETF")
        self.assertEqual(info["现金差额"], 21388.57)
        self.assertEqual(info["现金替代比例上限"], 30.0)
        self.assertEqual(info["是否需要公布IOPV"], "是")
        self.assertEqual(items[0]["证券代码"], "600009")
        self.assertEqual(items[0]["股票数量"], 300.0)
        self.assertEqual(items[0]["申购现金替代溢价比例"], 34.0)
        self.assertEqual(items[0]["挂牌市场"], "上交所")

    def test_download_url_contains_only_supported_current_parameters(self):
        url = build_sse_pcf_download_url("510010", "5")
        self.assertIn("fundCode=510010", url)
        self.assertIn("etfType=5", url)
        self.assertNotIn("startDate", url)

    def test_parses_legacy_etfmq_download(self):
        legacy = """[ETFMQ]\r
Fundid1=510071\r
CreationRedemptionUnit=500000\r
MaxCashRatio=0.30000\r
Publish=1\r
CreationRedemption=1\r
Recordnum=2\r
EstimateCashComponent=-5368.00\r
TradingDay=20200624\r
PreTradingDay=20200623\r
CashComponent=-3450.00\r
NAVperCU=1080646.00\r
NAV=2.161\r
TAGTAG\r
600031|三一重工|3200|1|0.10000||\r
603444|吉比特|100|2||47666.000|\r
ENDENDEND\r
"""
        info, items = parse_sse_pcf_download(
            legacy.encode("gb18030"),
            "510070",
            api_info={"FUND_NAME": "上证综指ETF", "FUND_COMP_NAME": "测试基金公司"},
        )
        self.assertEqual(info["基金代码"], "510070")
        self.assertEqual(info["内容日期"], "2020-06-24")
        self.assertEqual(info["现金差额"], -3450.0)
        self.assertEqual(info["现金替代比例上限"], 30.0)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["证券代码"], "603444")
        self.assertEqual(items[1]["替代金额"], 47666.0)

    def test_retries_transient_download_failure(self):
        calls = []
        info_payload = 'cb({"result":[{"FUND_NAME":"治理ETF","TRADING_DAY":"20260713","ETF_TYPE":"5"}]})'
        xml = "<SSEPortfolioCompositionFile><FundInstrumentID>510010</FundInstrumentID><TradingDay>20260713</TradingDay><ComponentList><Component><InstrumentID>600009</InstrumentID><Quantity>300</Quantity></Component></ComponentList></SSEPortfolioCompositionFile>"

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 2:
                raise OSError("temporary EOF")
            return type("Response", (), {
                "read": lambda self: (info_payload if "commonQuery.do" in request.full_url else xml).encode("utf-8"),
                "__enter__": lambda self: self,
                "__exit__": lambda self, *args: None,
            })()

        info, items = fetch_sse_pcf_for_fund("510010", opener=opener, retry_delay=0)
        self.assertEqual(info["内容日期"], "2026-07-13")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 4)


    def test_applies_global_request_interval_to_each_sse_request(self):
        info_payload = 'cb({"result":[{"FUND_NAME":"治理ETF","TRADING_DAY":"20260713","ETF_TYPE":"5"}]})'
        xml = "<SSEPortfolioCompositionFile><FundInstrumentID>510010</FundInstrumentID><TradingDay>20260713</TradingDay><ComponentList><Component><InstrumentID>600009</InstrumentID><Quantity>300</Quantity></Component></ComponentList></SSEPortfolioCompositionFile>"

        def opener(request, timeout):
            return type("Response", (), {
                "read": lambda self: (info_payload if "commonQuery.do" in request.full_url else xml).encode("utf-8"),
                "__enter__": lambda self: self,
                "__exit__": lambda self, *args: None,
            })()

        with patch("sse_pcf_fetcher._wait_for_sse_request_slot") as wait:
            fetch_sse_pcf_for_fund("510010", opener=opener, request_interval=0.35)
        self.assertEqual(wait.call_count, 2)
        wait.assert_called_with(0.35)

    def test_fetches_api_metadata_then_official_xml_download(self):
        calls = []
        info_payload = (
            'cb({"result":[{"FUND_NAME":"治理ETF",'
            '"FUND_COMP_NAME":"交银施罗德基金管理有限公司",'
            '"TRADING_DAY":"20260713","ETF_TYPE":"5",'
            '"CREATION_REDEMPTION":"申购和赎回皆允许"}]})'
        )
        xml = """
        <SSEPortfolioCompositionFile>
          <FundInstrumentID>510010</FundInstrumentID>
          <TradingDay>20260713</TradingDay>
          <ComponentList><Component><InstrumentID>600009</InstrumentID>
          <InstrumentName>上海机场</InstrumentName><Quantity>300</Quantity>
          <UnderlyingSecurityID>101</UnderlyingSecurityID></Component></ComponentList>
        </SSEPortfolioCompositionFile>
        """

        def opener(request, timeout):
            calls.append(request.full_url)
            return type("Response", (), {
                "read": lambda self: (info_payload if "commonQuery.do" in request.full_url else xml).encode("utf-8"),
                "__enter__": lambda self: self,
                "__exit__": lambda self, *args: None,
            })()

        info, items = fetch_sse_pcf_for_fund("510010", opener=opener)
        self.assertEqual(info["内容日期"], "2026-07-13")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("FUNDID2=510010", calls[0])
        self.assertIn("fundCode=510010", calls[1])


class SZSEPCFParserTests(unittest.TestCase):
    def test_chooses_substitute_amount_and_counts_mismatches(self):
        self.assertEqual(choose_szse_substitute_amount("10", "10"), (10.0, False))
        self.assertEqual(choose_szse_substitute_amount("0", "12"), (12.0, False))
        self.assertEqual(choose_szse_substitute_amount("10", "12"), (10.0, True))

    def test_parses_legacy_szse_text_with_gb18030_encoding(self):
        legacy = """Version=2.0
SecurityID=159915
Symbol=创业板ETF
FundManagementCompany=易方达基金
TradingDay=20260714
CashComponent=10.5
NAVperCU=1000000
NAV=1.25
EstimateCashComponent=11.5
MaxCashRatio=0.3
Creation=1
Redemption=1
Publish=1
CreationRedemptionUnit=1000000
TAGTAG
300001|特锐德|100|1|0.1|0.2|12.5|12.5|102
ENDENDEND
"""

        info, items, mismatches = parse_szse_pcf_download(legacy.encode("gb18030"))

        self.assertEqual(info["交易所"], "SZSE")
        self.assertEqual(info["基金代码"], "159915")
        self.assertEqual(info["内容日期"], "2026-07-14")
        self.assertEqual(items[0]["证券代码"], "300001")
        self.assertEqual(items[0]["挂牌市场"], "SZSE")
        self.assertEqual(items[0]["替代金额"], 12.5)
        self.assertEqual(mismatches, 0)

    def test_parses_namespaced_xml_and_normalizes_exchange_fields(self):
        xml = """
        <PCFFile xmlns="urn:szse:pcf">
          <SecurityID>159915</SecurityID>
          <Symbol>创业板ETF</Symbol>
          <FundManagementCompany>易方达基金</FundManagementCompany>
          <TradingDay>20260714</TradingDay>
          <CashComponent>10.5</CashComponent>
          <NAVperCU>1000000</NAVperCU>
          <NAV>1.25</NAV>
          <EstimateCashComponent>11.5</EstimateCashComponent>
          <MaxCashRatio>0.3</MaxCashRatio>
          <Creation>1</Creation>
          <Redemption>1</Redemption>
          <Publish>1</Publish>
          <CreationRedemptionUnit>1000000</CreationRedemptionUnit>
          <ComponentList>
            <Component>
              <UnderlyingSecurityID>300001</UnderlyingSecurityID>
              <UnderlyingSymbol>特锐德</UnderlyingSymbol>
              <ComponentShare>100</ComponentShare>
              <SubstituteFlag>1</SubstituteFlag>
              <PremiumRatio>0.1</PremiumRatio>
              <DiscountRatio>0.2</DiscountRatio>
              <CreationCashSubstitute>12.5</CreationCashSubstitute>
              <RedemptionCashSubstitute>12.5</RedemptionCashSubstitute>
              <UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
            </Component>
          </ComponentList>
        </PCFFile>
        """

        info, items, mismatches = parse_szse_pcf_download(xml)

        self.assertEqual(info["交易所"], "SZSE")
        self.assertEqual(info["基金代码"], "159915")
        self.assertEqual(info["内容日期"], "2026-07-14")
        self.assertEqual(items[0]["证券代码"], "300001")
        self.assertEqual(items[0]["挂牌市场"], "SZSE")
        self.assertEqual(items[0]["替代金额"], 12.5)
        self.assertEqual(mismatches, 0)

    def test_extracts_download_references_from_report_payload(self):
        payload = [{"data": [{"jjdm": (
            "<a href='/modules/report/views/eft_download_new.html?"
            "path=%2Ffiles%2Ftext%2FETFDown%2F&"
            "filename=pcf_159915_20260714%3B159915ETF20260714&"
            "opencode=ETF15991520260714.txt'>下载</a>"
        )}]}]

        refs = extract_szse_pcf_references(payload)

        self.assertEqual(
            refs,
            [SZSEPCFReference("159915", "2026-07-14", refs[0].download_url)],
        )
        self.assertIn("eft_download_new.html", refs[0].download_url)


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


class PCFDatabaseTests(unittest.TestCase):
    def _info(self, date="2026-07-13"):
        return {
            "交易所": "SSE",
            "基金代码": "510010",
            "基金名称": "治理ETF",
            "基金管理公司名称": "交银施罗德基金管理有限公司",
            "最新公告日期": date,
            "内容日期": date,
            "现金差额": 100.0,
            "基金份额净值": 1.68,
            "现金替代比例上限": 30.0,
            "最小申购、赎回单位": 1000000.0,
            "申购赎回模式": "沪市成分证券实物对价",
        }

    def _item(self, code="600009", date="2026-07-13"):
        return {
            "交易所": "SSE",
            "基金代码": "510010",
            "内容日期": date,
            "证券代码": code,
            "证券简称": "上海机场",
            "股票数量": 300.0,
            "现金替代标志": "允许",
            "申购现金替代溢价比例": 34.0,
            "赎回现金替代折价比例": 0.0,
            "替代金额": None,
            "挂牌市场": "上海证券交易所",
        }

    def test_creates_pcf_tables_with_chinese_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()

            with closing(db.connect()) as conn:
                info_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(ETF_INFO)")
                }
                item_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(ETF_ITEM)")
                }

            self.assertIn("基金代码", info_columns)
            self.assertIn("内容日期", info_columns)
            self.assertIn("证券代码", item_columns)
            self.assertIn("挂牌市场", item_columns)

    def test_upsert_is_idempotent_and_complete_check_is_per_fund_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            info = self._info()
            item = self._item()

            self.assertEqual(db.upsert_pcf([info], [item]), (1, 1))
            self.assertTrue(db.pcf_is_complete("SSE", "510010", "2026-07-13"))
            self.assertEqual(
                db.existing_pcf_keys("SSE", "2026-07-01", "2026-07-31"),
                {("510010", "2026-07-13")},
            )

            db.upsert_pcf([info], [item])
            with closing(db.connect()) as conn:
                info_count = conn.execute("SELECT COUNT(*) FROM ETF_INFO").fetchone()[0]
                item_count = conn.execute("SELECT COUNT(*) FROM ETF_ITEM").fetchone()[0]
            self.assertEqual(info_count, 1)
            self.assertEqual(item_count, 1)

    def test_incomplete_pcf_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            db.upsert_pcf([self._info()], [])

            self.assertFalse(db.pcf_is_complete("SSE", "510010", "2026-07-13"))

    def test_lists_distinct_fund_codes_for_pcf_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            db.upsert_rows(
                [
                    {"trade_date": "2026-07-13", "exchange": "SSE", "fund_code": "510010", "fund_name": "治理ETF", "total_share": 1},
                    {"trade_date": "2026-07-12", "exchange": "SSE", "fund_code": "510010", "fund_name": "治理ETF", "total_share": 1},
                    {"trade_date": "2026-07-13", "exchange": "SZSE", "fund_code": "159001", "fund_name": "易方达", "total_share": 1},
                ],
                recalculate=False,
            )
            self.assertEqual(db.list_fund_codes("SSE"), [{"fund_code": "510010", "fund_name": "治理ETF"}])


class PCFGuiTests(unittest.TestCase):
    def test_gui_has_pcf_actions_and_independent_log_scrollbar(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8-sig")
        self.assertIn("fetch_sse_pcf_for_fund", source)
        self.assertIn("采集当前 PCF", source)
        self.assertIn("批量采集上交所当前 PCF", source)
        self.assertIn("log_frame", source)
        self.assertIn("log_scrollbar", source)
        self.assertIn("yscrollcommand=log_scrollbar.set", source)


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
