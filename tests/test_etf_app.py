import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, call, patch
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
from eastmoney_holding_fetcher import (
    EastmoneyHoldingParseError,
    EastmoneyHoldingNoDataError,
    check_eastmoney_holding_connection,
    fetch_eastmoney_holdings,
    parse_eastmoney_report_dates,
    parse_eastmoney_holding_response,
)
from szse_pcf_fetcher import (
    SZSEPCFBrowserSession,
    SZSEPCFReference,
    SZSEPCFPageError,
    check_szse_pcf_connection,
    choose_szse_substitute_amount,
    collect_szse_pcf_via_browser,
    extract_szse_pcf_references,
    parse_szse_pcf_download,
)
from etf_gui import (
    ETFApp,
    collection_panel_state,
    format_coverage_cell,
    load_database_path,
    save_database_path,
)
from etf_web_app import ETFWebServer, HTML, parse_web_endpoint


class EastmoneyHoldingParserTests(unittest.TestCase):
    @staticmethod
    def _response():
        content = (
            '<h4 class="t">2024年1季度股票投资明细</h4>'
            '<table><tr><th>序号</th><th>股票代码</th><th>股票名称</th>'
            '<th>占净值比例</th><th>持股数（万股）</th><th>持仓市值（万元）</th></tr>'
            '<tr><td>1</td><td>600000</td><td>浦发银行</td><td>5.5%</td>'
            '<td>12.3</td><td>456.7</td></tr></table>'
            '<h4 class="t">2024年2季度股票投资明细</h4>'
            '<table><tr><th>序号</th><th>股票代码</th><th>股票名称</th>'
            '<th>占净值比例</th><th>持股数（万股）</th><th>持仓市值（万元）</th></tr>'
            '<tr><td>1</td><td>600001</td><td>邯郸钢铁</td><td>2%</td>'
            '<td>3</td><td>4</td></tr></table>'
        )
        return 'var apidata={content:' + json.dumps(content, ensure_ascii=False) + '};'

    def test_parses_all_quarter_tables_and_converts_units(self):
        rows = parse_eastmoney_holding_response(
            self._response(), "510010", exchange="SSE", fund_name="治理ETF"
        )
        self.assertEqual([row["报告期"] for row in rows], ["2024-03-31", "2024-06-30"])
        self.assertEqual(rows[0]["基金名称"], "治理ETF")
        self.assertEqual(rows[0]["持股数"], 123000.0)
        self.assertEqual(rows[0]["持仓市值"], 4567000.0)
        self.assertEqual(rows[0]["数据完整性"], "部分披露")
        self.assertEqual(rows[1]["数据完整性"], "完整披露")

    def test_marks_explicit_empty_response_as_no_data(self):
        with self.assertRaises(EastmoneyHoldingNoDataError):
            parse_eastmoney_holding_response('var apidata={content:"<p>暂无数据</p>"};', "510010")

    def test_connection_probe_treats_no_holdings_as_reachable(self):
        opener = Mock()
        response = Mock()
        response.read.return_value = 'var apidata={content:"<p>暂无数据</p>"};'.encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener.return_value = response
        ok, message = check_eastmoney_holding_connection("159001", 2016, opener=opener)
        self.assertTrue(ok)
        self.assertIn("没有股票季度持仓", message)

    def test_parses_report_announcement_dates_without_using_report_period_as_available_date(self):
        payload = json.dumps(
            {
                "Data": [
                    ["510010", "治理ETF：2024年第1季度报告", "治理ETF", "", "", "2024-04-22", "", "AN1"],
                    ["510010", "治理ETF：2024年半年度报告", "治理ETF", "", "", "2024-08-30", "", "AN2"],
                    ["510010", "治理ETF：2024年年度报告", "治理ETF", "", "", "2025-03-31", "", "AN3"],
                ]
            }
        )
        self.assertEqual(
            parse_eastmoney_report_dates(payload),
            {"2024-03-31": "2024-04-22", "2024-06-30": "2024-08-30", "2024-12-31": "2025-03-31"},
        )

    def test_fetch_builds_annual_request_and_parses_response(self):
        opener = Mock()
        response = Mock()
        response.read.return_value = self._response().encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener.return_value = response
        rows = fetch_eastmoney_holdings(
            "510010", 2024, opener=opener, request_interval=0
        )
        request = opener.call_args.args[0]
        self.assertIn("type=jjcc", request.full_url)
        self.assertIn("year=2024", request.full_url)
        self.assertEqual(len(rows), 2)

    def test_retries_transient_no_data_response(self):
        def response(payload):
            item = Mock()
            item.read.return_value = payload.encode("utf-8")
            item.__enter__ = Mock(return_value=item)
            item.__exit__ = Mock(return_value=False)
            return item

        opener = Mock(side_effect=[
            response('var apidata={content:"<p>\\u6682\\u65e0\\u6570\\u636e</p>"};'),
            response(self._response()),
        ])
        rows = fetch_eastmoney_holdings(
            "510010",
            2024,
            opener=opener,
            request_interval=0,
            retry_attempts=2,
            retry_backoff=0,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(opener.call_count, 2)

    def test_preserves_columns_when_related_links_cell_is_malformed(self):
        response = (
            '<h4>2024\u5e741\u5b63\u5ea6\u80a1\u7968\u6295\u8d44\u660e\u7ec6</h4>'
            '<table><tr><th>\u5e8f\u53f7</th><th>\u80a1\u7968\u4ee3\u7801</th>'
            '<th>\u80a1\u7968\u540d\u79f0</th><th>\u76f8\u5173\u8d44\u8baf</th>'
            '<th>\u5360\u51c0\u503c\u6bd4\u4f8b</th><th>\u6301\u80a1\u6570</th>'
            '<th>\u6301\u4ed3\u5e02\u503c</th></tr>'
            '<tr><td>1</td><td><span>400174</span></td><td><span>\u4e2d\u8bc13</span></td>'
            '<td class="xglj"><span>\u80a1\u5427</span><span>\u884c\u60c5</span><span>\u6863\u6848'
            '<td class="tor">0.13%</td><td class="tor">4,054.35</td>'
            '<td class="tor">567.61</td></tr></table>'
        )
        rows = parse_eastmoney_holding_response(
            'var apidata={content:' + json.dumps(response, ensure_ascii=False) + '};',
            "512200",
        )
        self.assertEqual(rows[0]["股票代码"], "400174")
        self.assertEqual(rows[0]["占净值比例"], 0.13)
        self.assertEqual(rows[0]["持股数"], 40543500.0)
        self.assertEqual(rows[0]["持仓市值"], 5676100.0)


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
300001|特锐德|100|1|0.1|12.5|12.5|XSHE
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

    def test_parses_eight_field_legacy_amounts_and_exchange_codes(self):
        legacy = """Version=2.0
SecurityID=159915
TradingDay=20260714
TAGTAG
300001|特锐德|100|1|0.1|10|12|XSHE
600000|浦发银行|200|1|0.2|20|20|XSHG
ENDENDEND
"""

        info, items, mismatches = parse_szse_pcf_download(legacy)

        self.assertEqual(info["基金代码"], "159915")
        self.assertEqual(items[0]["替代金额"], 10.0)
        self.assertEqual(items[0]["挂牌市场"], "SZSE")
        self.assertEqual(items[1]["替代金额"], 20.0)
        self.assertEqual(items[1]["挂牌市场"], "SSE")
        self.assertEqual(mismatches, 1)

    def test_legacy_header_keys_and_detection_are_case_insensitive(self):
        legacy = """vErSiOn=2.0
sEcUrItYiD=159915
FuNdNaMe=创业板ETF
SyMbOl=兼容名称
tRaDiNgDaY=20260714
TaGtAg
300001|特锐德|100|1|0.1|12.5|12.5|xShE
EnDeNdEnD
"""

        info, items, mismatches = parse_szse_pcf_download(legacy)

        self.assertEqual(info["基金代码"], "159915")
        self.assertEqual(info["基金名称"], "创业板ETF")
        self.assertEqual(info["内容日期"], "2026-07-14")
        self.assertEqual(items[0]["挂牌市场"], "SZSE")
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

    def test_preserves_virtual_security_and_unknown_market_source(self):
        xml = b"""<PCFFile>
          <SecurityID>159915</SecurityID><TradingDay>20260714</TradingDay>
          <Component><UnderlyingSecurityID>159900</UnderlyingSecurityID>
          <ComponentShare>1</ComponentShare><UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource></Component>
          <Component><UnderlyingSecurityID>000001</UnderlyingSecurityID>
          <ComponentShare>1</ComponentShare><UnderlyingSecurityIDSource>999</UnderlyingSecurityIDSource></Component>
        </PCFFile>"""

        _, items, _ = parse_szse_pcf_download(xml)

        self.assertEqual([item["证券代码"] for item in items], ["159900", "000001"])
        self.assertEqual([item["挂牌市场"] for item in items], ["SZSE", "999"])

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

    def test_extracts_unquoted_download_href_from_live_report_format(self):
        payload = [{"data": [{"jjdm": (
            "<a style='cursor:pointer'"
            "href=/modules/report/views/eft_download_new.html?"
            "path=%2Ffiles%2Ftext%2FETFDown%2F&"
            "filename=pcf_159915_20260710%3B159915ETF20260710&"
            "opencode=ETF15991520260710.txt target='_blank'>下载</a>"
        )}]}]

        refs = extract_szse_pcf_references(payload)

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].fund_code, "159915")
        self.assertEqual(refs[0].content_date, "2026-07-10")
        self.assertIn("eft_download_new.html", refs[0].download_url)


class SZSEPCFCollectorTests(unittest.TestCase):
    xml = b"""<PCFFile>
      <SecurityID>159915</SecurityID><TradingDay>20260714</TradingDay>
      <Component><UnderlyingSecurityID>300001</UnderlyingSecurityID>
      <UnderlyingSymbol>\xe7\x89\xb9\xe9\x94\x90\xe5\xbe\xb7</UnderlyingSymbol><ComponentShare>100</ComponentShare>
      <SubstituteFlag>1</SubstituteFlag><CreationCashSubstitute>12.5</CreationCashSubstitute>
      <RedemptionCashSubstitute>12.5</RedemptionCashSubstitute>
      <UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource></Component>
    </PCFFile>"""

    class FakeSession:
        def __init__(self, references, files):
            self.references = references
            self.files = files
            self.queries = []
            self.reads = []
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.closed = True

        def query(self, trade_date, fund_code=""):
            self.queries.append((trade_date, fund_code))
            return self.references

        def read_file(self, reference):
            self.reads.append(reference)
            value = self.files[reference.fund_code]
            if isinstance(value, Exception):
                raise value
            return value

    class FakeResponse:
        def __init__(self, url, *, payload=None, body=b""):
            self.url = url
            self.payload = payload
            self.body_bytes = body

        def json(self):
            return self.payload

        def body(self):
            return self.body_bytes

    class FakeExpectation:
        def __init__(self, response, page=None):
            self.value = response
            self.page = page

        def __enter__(self):
            if self.page is not None:
                self.page.expectation_active = True
            return self

        def __exit__(self, exc_type, exc, traceback):
            if self.page is not None:
                self.page.expectation_active = False

    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector

        def fill(self, value):
            self.page.fills.append((self.selector, value))

        def click(self):
            self.page.clicks.append(self.selector)

    class FakeQueryPage:
        def __init__(self, first_response, later_payload):
            self.first_response = first_response
            self.later_payload = later_payload
            self.fills = []
            self.clicks = []
            self.goto_calls = []
            self.evaluate_calls = []

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))

        def locator(self, selector):
            return SZSEPCFCollectorTests.FakeLocator(self, selector)

        def expect_response(self, predicate, timeout):
            if not predicate(self.first_response):
                raise AssertionError("report response filter rejected the report")
            return SZSEPCFCollectorTests.FakeExpectation(self.first_response)

        def evaluate(self, script, url):
            self.evaluate_calls.append((script, url))
            return self.later_payload

    class FakeContext:
        def __init__(self, pages):
            self.pages = list(pages)
            self.new_page_calls = 0
            self.closed = False

        def new_page(self):
            self.new_page_calls += 1
            value = self.pages.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        def close(self):
            self.closed = True

    class FakeBrowser:
        def __init__(self, context):
            self.context = context
            self.closed = False

        def new_context(self):
            return self.context

        def close(self):
            self.closed = True

    class FakeChromium:
        def __init__(self, browser):
            self.browser = browser
            self.launch_calls = []

        def launch(self, **kwargs):
            self.launch_calls.append(kwargs)
            return self.browser

    class FakePlaywrightManager:
        def __init__(self, chromium):
            self.playwright = type("FakePlaywright", (), {"chromium": chromium})()
            self.exited = False

        def __enter__(self):
            return self.playwright

        def __exit__(self, exc_type, exc, traceback):
            self.exited = True

    def test_collects_unfinished_references_through_one_session(self):
        references = [
            SZSEPCFReference("159001", "2026-07-14", "https://example.test/159001"),
            SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915"),
        ]
        fake_session = self.FakeSession(
            references, {"159001": self.xml, "159915": self.xml}
        )
        saved = []
        sleeps = []

        def save_snapshot(info, items):
            saved.append((info, items))

        summary = collect_szse_pcf_via_browser(
            ["2026-07-14"],
            is_complete=lambda code, date: code == "159001",
            save_snapshot=save_snapshot,
            session_factory=lambda visible=False: fake_session,
            sleep_func=lambda seconds: sleeps.append(seconds),
            delay_func=lambda: 0.8,
        )

        self.assertEqual(summary["dates"], 1)
        self.assertEqual(summary["discovered"], 2)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["items"], 1)
        self.assertEqual(summary["mismatched_amounts"], 0)
        self.assertEqual(summary["failures"], [])
        self.assertEqual(len(saved), 1)
        self.assertEqual(fake_session.queries, [("2026-07-14", "")])
        self.assertEqual([ref.fund_code for ref in fake_session.reads], ["159915"])
        self.assertEqual(sleeps, [0.8])
        self.assertTrue(fake_session.closed)

    def test_replace_existing_ignores_completed_snapshot(self):
        reference = SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915")
        fake_session = self.FakeSession([reference], {"159915": self.xml})
        saved = []

        summary = collect_szse_pcf_via_browser(
            ["2026-07-14"],
            replace_existing=True,
            is_complete=lambda code, date: True,
            save_snapshot=lambda info, items: saved.append((info, items)),
            session_factory=lambda visible=False: fake_session,
            sleep_func=lambda seconds: None,
            delay_func=lambda: 0.8,
        )

        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(len(saved), 1)

    def test_retries_file_failures_and_continues_to_later_references(self):
        references = [
            SZSEPCFReference("159001", "2026-07-14", "https://example.test/159001"),
            SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915"),
        ]
        fake_session = self.FakeSession(
            references,
            {"159001": RuntimeError("download failed"), "159915": self.xml},
        )
        saved = []
        sleeps = []

        summary = collect_szse_pcf_via_browser(
            ["2026-07-14"],
            is_complete=lambda code, date: False,
            save_snapshot=lambda info, items: saved.append((info, items)),
            session_factory=lambda visible=False: fake_session,
            sleep_func=lambda seconds: sleeps.append(seconds),
            delay_func=lambda: 0.8,
        )

        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(summary["failures"]), 1)
        self.assertEqual([ref.fund_code for ref in fake_session.reads], ["159001"] * 4 + ["159915"])
        self.assertEqual(sleeps, [2, 5, 10, 0.8, 0.8])
        self.assertEqual(len(saved), 1)

    def test_paces_after_final_file_failure(self):
        reference = SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915")
        fake_session = self.FakeSession([reference], {"159915": RuntimeError("download failed")})
        sleeps = []

        summary = collect_szse_pcf_via_browser(
            ["2026-07-14"],
            is_complete=lambda code, date: False,
            save_snapshot=lambda info, items: None,
            session_factory=lambda visible=False: fake_session,
            sleep_func=sleeps.append,
            delay_func=lambda: 0.8,
        )

        self.assertEqual(summary["failed"], 1)
        self.assertEqual([ref.fund_code for ref in fake_session.reads], ["159915"] * 4)
        self.assertEqual(sleeps, [2, 5, 10, 0.8])

    def test_retries_invalid_snapshot_identity_without_writing(self):
        reference = SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915")
        cases = {
            "non-ascii code": ({"基金代码": "１５９９１５", "内容日期": "2026-07-14"}, [{"证券代码": "300001"}]),
            "invalid date": ({"基金代码": "159915", "内容日期": "2026-02-30"}, [{"证券代码": "300001"}]),
            "wrong code": ({"基金代码": "159900", "内容日期": "2026-07-14"}, [{"证券代码": "300001"}]),
            "wrong date": ({"基金代码": "159915", "内容日期": "2026-07-13"}, [{"证券代码": "300001"}]),
            "no items": ({"基金代码": "159915", "内容日期": "2026-07-14"}, []),
        }

        for label, parsed in cases.items():
            with self.subTest(label=label):
                fake_session = self.FakeSession([reference], {"159915": self.xml})
                saved = []
                sleeps = []
                with patch("szse_pcf_fetcher.parse_szse_pcf_download", return_value=(*parsed, 0)):
                    summary = collect_szse_pcf_via_browser(
                        ["2026-07-14"],
                        is_complete=lambda code, date: False,
                        save_snapshot=lambda info, items: saved.append((info, items)),
                        session_factory=lambda visible=False: fake_session,
                        sleep_func=sleeps.append,
                        delay_func=lambda: 0.8,
                    )

                self.assertEqual(summary["succeeded"], 0)
                self.assertEqual(summary["failed"], 1)
                self.assertEqual(saved, [])
                self.assertEqual([ref.fund_code for ref in fake_session.reads], ["159915"] * 4)
                self.assertEqual(sleeps, [2, 5, 10, 0.8])

    def test_date_logs_include_discovery_and_completion_totals(self):
        references = [
            SZSEPCFReference("159001", "2026-07-14", "https://example.test/159001"),
            SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915"),
        ]
        fake_session = self.FakeSession(
            references, {"159001": self.xml, "159915": self.xml}
        )
        messages = []

        collect_szse_pcf_via_browser(
            ["2026-07-14"],
            is_complete=lambda code, date: code == "159001",
            save_snapshot=lambda info, items: None,
            session_factory=lambda visible=False: fake_session,
            sleep_func=lambda seconds: None,
            delay_func=lambda: 0.8,
            on_progress=messages.append,
        )

        self.assertEqual(len(messages), 2)
        for expected in ("发现 2", "跳过 1", "待采 1"):
            self.assertIn(expected, messages[0])
        for expected in ("发现 2", "跳过 1", "待采 0", "成功 1", "失败 0", "成分 1"):
            self.assertIn(expected, messages[1])

    def test_wraps_session_startup_failures_with_first_pending_trade_date(self):
        class FailingEnter:
            def __enter__(self):
                raise RuntimeError("initial page load failed")

            def __exit__(self, exc_type, exc, traceback):
                return False

        def failing_factory(visible=False):
            raise RuntimeError("session factory failed")

        for label, session_factory, message in (
            ("factory", failing_factory, "session factory failed"),
            ("enter", lambda visible=False: FailingEnter(), "initial page load failed"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(SZSEPCFPageError, message) as caught:
                    collect_szse_pcf_via_browser(
                        ["2026-07-14", "2026-07-15"],
                        is_complete=lambda code, date: False,
                        save_snapshot=lambda info, items: None,
                        session_factory=session_factory,
                    )

                self.assertEqual(caught.exception.trade_date, "2026-07-14")

    def test_second_date_save_failure_propagates_and_cleanup_cannot_mask_it(self):
        reference = SZSEPCFReference(
            "159915", "2026-07-14", "https://example.test/159915"
        )

        class FailingExitSession(self.FakeSession):
            def __exit__(self, exc_type, exc, traceback):
                self.closed = True
                raise RuntimeError("session cleanup failed")

        session = FailingExitSession([reference], {"159915": self.xml})
        database_error = sqlite3.DatabaseError("snapshot insert failed")
        save_calls = []
        sleeps = []

        def save_snapshot(info, items):
            save_calls.append((info, items))
            if len(save_calls) >= 2:
                raise database_error

        with self.assertRaises(sqlite3.DatabaseError) as caught:
            collect_szse_pcf_via_browser(
                ["2026-07-14", "2026-07-15"],
                is_complete=lambda code, date: False,
                save_snapshot=save_snapshot,
                session_factory=lambda visible=False: session,
                sleep_func=sleeps.append,
                delay_func=lambda: 0.8,
            )

        self.assertIs(caught.exception, database_error)
        self.assertNotIsInstance(caught.exception, SZSEPCFPageError)
        self.assertEqual(
            session.queries,
            [("2026-07-14", ""), ("2026-07-15", "")],
        )
        self.assertEqual(len(save_calls), 2)
        self.assertEqual(sleeps, [0.8, 0.8])
        self.assertTrue(session.closed)

    def test_converts_global_browser_network_failures_to_page_error(self):
        reference = SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915")
        for marker in (
            "ERR_INTERNET_DISCONNECTED",
            "ERR_NETWORK_CHANGED",
            "ERR_NAME_NOT_RESOLVED",
            "ERR_CONNECTION_CLOSED",
            "ERR_CONNECTION_RESET",
        ):
            with self.subTest(marker=marker):
                fake_session = self.FakeSession(
                    [reference], {"159915": RuntimeError(f"net::{marker}")}
                )
                sleeps = []

                with self.assertRaisesRegex(SZSEPCFPageError, marker) as caught:
                    collect_szse_pcf_via_browser(
                        ["2026-07-14", "2026-07-15"],
                        is_complete=lambda code, date: False,
                        save_snapshot=lambda info, items: None,
                        session_factory=lambda visible=False: fake_session,
                        sleep_func=sleeps.append,
                        delay_func=lambda: 0.8,
                    )

                self.assertEqual(caught.exception.trade_date, "2026-07-14")
                self.assertEqual(fake_session.queries, [("2026-07-14", "")])
                self.assertEqual([ref.fund_code for ref in fake_session.reads], ["159915"])
                self.assertEqual(sleeps, [0.8])

    def test_wraps_query_failure_with_the_current_trade_date(self):
        fake_session = self.FakeSession([], {})

        def fail_query(trade_date, fund_code=""):
            raise RuntimeError("report unavailable")

        fake_session.query = fail_query

        with self.assertRaisesRegex(SZSEPCFPageError, "report unavailable") as caught:
            collect_szse_pcf_via_browser(
                ["2026-07-14", "2026-07-15"],
                is_complete=lambda code, date: False,
                save_snapshot=lambda info, items: None,
                session_factory=lambda visible=False: fake_session,
            )

        self.assertEqual(caught.exception.trade_date, "2026-07-14")
        self.assertTrue(fake_session.closed)

    def test_rethrows_session_page_error_without_file_retries(self):
        references = [
            SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915"),
            SZSEPCFReference("159001", "2026-07-14", "https://example.test/159001"),
        ]
        fake_session = self.FakeSession(
            references,
            {
                "159915": SZSEPCFPageError("2026-07-14", "browser closed"),
                "159001": self.xml,
            },
        )
        sleeps = []

        with self.assertRaisesRegex(SZSEPCFPageError, "browser closed") as caught:
            collect_szse_pcf_via_browser(
                ["2026-07-14", "2026-07-15"],
                is_complete=lambda code, date: False,
                save_snapshot=lambda info, items: None,
                session_factory=lambda visible=False: fake_session,
                sleep_func=lambda seconds: sleeps.append(seconds),
            )

        self.assertEqual(caught.exception.trade_date, "2026-07-14")
        self.assertEqual(fake_session.queries, [("2026-07-14", "")])
        self.assertEqual([ref.fund_code for ref in fake_session.reads], ["159915"])
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 0.8)
        self.assertLessEqual(sleeps[0], 1.8)
        self.assertTrue(fake_session.closed)

    def test_browser_session_launches_and_queries_through_one_context(self):
        first_payload = [{
            "metadata": {"pagecount": 2},
            "data": [{"jjdm": (
                "<a href='/modules/report/views/eft_download_new.html?"
                "filename=pcf_159915_20260714'>下载</a>"
            )}],
        }]
        second_payload = [{
            "metadata": {"pagecount": 2},
            "data": [{"jjdm": (
                "<a href='/modules/report/views/eft_download_new.html?"
                "filename=pcf_159001_20260714'>下载</a>"
            )}],
        }]
        report_response = self.FakeResponse(
            "https://www.szse.cn/api/report/ShowReport/data?"
            "CATALOGID=sgshqd&PAGENO=1&tab1PAGENO=1",
            payload=first_payload,
        )
        page = self.FakeQueryPage(report_response, second_payload)
        context = self.FakeContext([page])
        browser = self.FakeBrowser(context)
        chromium = self.FakeChromium(browser)
        manager = self.FakePlaywrightManager(chromium)

        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            with SZSEPCFBrowserSession(visible=False) as session:
                references = session.query("2026-07-14", "159915")

        self.assertEqual(chromium.launch_calls, [{"headless": True, "slow_mo": 120}])
        self.assertEqual(
            page.goto_calls[0][0],
            "https://www.szse.cn/disclosure/fund/currency/index.html",
        )
        self.assertEqual(
            page.fills,
            [
                ("input.query-txtJCorDH", "159915"),
                ("input.query-txtStart", "2026-07-14"),
                ("input.query-txtEnd", "2026-07-14"),
            ],
        )
        self.assertEqual(page.clicks, ["button.confirm-query"])
        self.assertEqual([ref.fund_code for ref in references], ["159915", "159001"])
        self.assertEqual(context.new_page_calls, 1)
        self.assertEqual(len(page.evaluate_calls), 1)
        self.assertIn("PAGENO=2", page.evaluate_calls[0][1])
        self.assertIn("tab1PAGENO=2", page.evaluate_calls[0][1])
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(manager.exited)

    def test_browser_session_honors_visible_launch_option(self):
        page = self.FakeQueryPage(self.FakeResponse("unused"), [])
        context = self.FakeContext([page])
        browser = self.FakeBrowser(context)
        chromium = self.FakeChromium(browser)
        manager = self.FakePlaywrightManager(chromium)

        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            with SZSEPCFBrowserSession(visible=True):
                pass

        self.assertEqual(chromium.launch_calls, [{"headless": False, "slow_mo": 120}])

    def test_read_file_returns_final_response_without_wrapper_navigation(self):
        wrapper = self.FakeResponse(
            "https://www.szse.cn/modules/report/views/eft_download_new.html",
            body=b"wrapper",
        )
        final = self.FakeResponse(
            "https://www.szse.cn/files/text/ETFDown/ETF15991520260714.txt",
            body=self.xml,
        )

        class DownloadPage:
            def __init__(self):
                self.expectation_active = False
                self.closed = False
                self.filter_results = []

            def expect_response(page_self, predicate, timeout):
                page_self.filter_results = [predicate(wrapper), predicate(final)]
                return SZSEPCFCollectorTests.FakeExpectation(final, page_self)

            def goto(page_self, url, **kwargs):
                if not page_self.expectation_active:
                    raise AssertionError("final-response wait must start before navigation")
                return wrapper

            def wait_for_url(page_self, url, timeout):
                raise AssertionError("download wrapper stays on its own URL")

            def close(page_self):
                page_self.closed = True

        download_page = DownloadPage()
        session = SZSEPCFBrowserSession()
        session.context = self.FakeContext([download_page])
        reference = SZSEPCFReference(
            "159915", "2026-07-14", "https://example.test/download"
        )

        raw = session.read_file(reference)

        self.assertEqual(raw, self.xml)
        self.assertEqual(download_page.filter_results, [False, True])
        self.assertTrue(download_page.closed)

    def test_read_file_converts_closed_context_to_page_error(self):
        class TargetClosedError(RuntimeError):
            pass

        session = SZSEPCFBrowserSession()
        session.context = self.FakeContext([TargetClosedError("context closed")])
        reference = SZSEPCFReference(
            "159915", "2026-07-14", "https://example.test/download"
        )

        with self.assertRaisesRegex(SZSEPCFPageError, "context closed") as caught:
            session.read_file(reference)

        self.assertEqual(caught.exception.trade_date, "2026-07-14")

    def test_read_file_converts_fatal_page_close_failure_to_page_error(self):
        final = self.FakeResponse(
            "https://www.szse.cn/files/text/ETFDown/ETF15991520260714.txt",
            body=self.xml,
        )

        class TargetClosedError(RuntimeError):
            pass

        class DownloadPage:
            def expect_response(self, predicate, timeout):
                return SZSEPCFCollectorTests.FakeExpectation(final)

            def goto(self, url, **kwargs):
                return None

            def wait_for_url(self, url, timeout):
                return None

            def close(self):
                raise TargetClosedError("browser closed during page cleanup")

        session = SZSEPCFBrowserSession()
        session.context = self.FakeContext([DownloadPage()])
        reference = SZSEPCFReference(
            "159915", "2026-07-14", "https://example.test/download"
        )

        with self.assertRaisesRegex(SZSEPCFPageError, "browser closed") as caught:
            session.read_file(reference)

        self.assertEqual(caught.exception.trade_date, "2026-07-14")

    def test_collector_preserves_fatal_body_error_when_page_close_also_fails(self):
        class TargetClosedError(RuntimeError):
            pass

        class FatalResponse(self.FakeResponse):
            def body(self):
                raise TargetClosedError("original browser body failure")

        final = FatalResponse(
            "https://www.szse.cn/files/text/ETFDown/ETF15991520260714.txt"
        )

        class DownloadPage:
            def expect_response(self, predicate, timeout):
                return SZSEPCFCollectorTests.FakeExpectation(final)

            def goto(self, url, **kwargs):
                return None

            def wait_for_url(self, url, timeout):
                return None

            def close(self):
                raise RuntimeError("download page close failure")

        class ReusableContext:
            def __init__(self):
                self.new_page_calls = 0

            def new_page(self):
                self.new_page_calls += 1
                return DownloadPage()

        reference = SZSEPCFReference(
            "159915", "2026-07-14", "https://example.test/download"
        )

        class BrowserBackedSession:
            def __init__(self):
                self.browser_session = SZSEPCFBrowserSession()
                self.context = ReusableContext()
                self.browser_session.context = self.context
                self.queries = []
                self.reads = []
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                self.closed = True

            def query(self, trade_date, fund_code=""):
                self.queries.append((trade_date, fund_code))
                return [reference]

            def read_file(self, requested_reference):
                self.reads.append(requested_reference)
                return self.browser_session.read_file(requested_reference)

        session = BrowserBackedSession()
        sleeps = []

        with self.assertRaisesRegex(
            SZSEPCFPageError, "original browser body failure"
        ) as caught:
            collect_szse_pcf_via_browser(
                ["2026-07-14", "2026-07-15"],
                is_complete=lambda code, date: False,
                save_snapshot=lambda info, items: None,
                session_factory=lambda visible=False: session,
                sleep_func=lambda seconds: sleeps.append(seconds),
            )

        self.assertNotIn("close failure", str(caught.exception))
        self.assertEqual(caught.exception.trade_date, "2026-07-14")
        self.assertEqual(session.queries, [("2026-07-14", "")])
        self.assertEqual(len(session.reads), 1)
        self.assertEqual(session.context.new_page_calls, 1)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 0.8)
        self.assertLessEqual(sleeps[0], 1.8)
        self.assertTrue(session.closed)

    def test_collector_preserves_fatal_navigation_error_when_page_close_also_fails(self):
        class TargetClosedError(RuntimeError):
            pass

        final = self.FakeResponse(
            "https://www.szse.cn/files/text/ETFDown/ETF15991520260714.txt",
            body=self.xml,
        )
        reference = SZSEPCFReference(
            "159915", "2026-07-14", "https://example.test/download"
        )

        for failure_stage in ("goto",):
            with self.subTest(failure_stage=failure_stage):
                class DownloadPage:
                    def expect_response(self, predicate, timeout):
                        return SZSEPCFCollectorTests.FakeExpectation(final)

                    def goto(self, url, **kwargs):
                        if failure_stage == "goto":
                            raise TargetClosedError("original goto failure")

                    def wait_for_url(self, url, timeout):
                        if failure_stage == "wait_for_url":
                            raise TargetClosedError("original wait_for_url failure")

                    def close(self):
                        raise RuntimeError("download page close failure")

                class ReusableContext:
                    def __init__(self):
                        self.new_page_calls = 0

                    def new_page(self):
                        self.new_page_calls += 1
                        return DownloadPage()

                class BrowserBackedSession:
                    def __init__(self):
                        self.browser_session = SZSEPCFBrowserSession()
                        self.context = ReusableContext()
                        self.browser_session.context = self.context
                        self.queries = []
                        self.reads = []
                        self.closed = False

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, traceback):
                        self.closed = True

                    def query(self, trade_date, fund_code=""):
                        self.queries.append((trade_date, fund_code))
                        return [reference]

                    def read_file(self, requested_reference):
                        self.reads.append(requested_reference)
                        return self.browser_session.read_file(requested_reference)

                session = BrowserBackedSession()
                sleeps = []

                with self.assertRaisesRegex(
                    SZSEPCFPageError, f"original {failure_stage} failure"
                ) as caught:
                    collect_szse_pcf_via_browser(
                        ["2026-07-14", "2026-07-15"],
                        is_complete=lambda code, date: False,
                        save_snapshot=lambda info, items: None,
                        session_factory=lambda visible=False: session,
                        sleep_func=lambda seconds: sleeps.append(seconds),
                    )

                self.assertNotIn("close failure", str(caught.exception))
                self.assertEqual(caught.exception.trade_date, "2026-07-14")
                self.assertEqual(session.queries, [("2026-07-14", "")])
                self.assertEqual(len(session.reads), 1)
                self.assertEqual(session.context.new_page_calls, 1)
                self.assertEqual(len(sleeps), 1)
                self.assertGreaterEqual(sleeps[0], 0.8)
                self.assertLessEqual(sleeps[0], 1.8)
                self.assertTrue(session.closed)

    def test_read_file_closes_download_page_when_body_read_fails(self):
        class FailingResponse(self.FakeResponse):
            def body(self):
                raise ValueError("invalid response body")

        final = FailingResponse(
            "https://www.szse.cn/files/text/ETFDown/ETF15991520260714.txt"
        )

        class DownloadPage:
            def __init__(self):
                self.closed = False

            def expect_response(self, predicate, timeout):
                return SZSEPCFCollectorTests.FakeExpectation(final)

            def goto(self, url, **kwargs):
                return None

            def wait_for_url(self, url, timeout):
                return None

            def close(self):
                self.closed = True

        download_page = DownloadPage()
        session = SZSEPCFBrowserSession()
        session.context = self.FakeContext([download_page])
        reference = SZSEPCFReference(
            "159915", "2026-07-14", "https://example.test/download"
        )

        with self.assertRaisesRegex(ValueError, "invalid response body"):
            session.read_file(reference)

        self.assertTrue(download_page.closed)

    def test_browser_session_cleans_up_when_initial_page_load_fails(self):
        class FailingPage(self.FakeQueryPage):
            def goto(self, url, **kwargs):
                raise RuntimeError("page load failed")

        page = FailingPage(self.FakeResponse("unused"), [])
        context = self.FakeContext([page])
        browser = self.FakeBrowser(context)
        chromium = self.FakeChromium(browser)
        manager = self.FakePlaywrightManager(chromium)

        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            with self.assertRaisesRegex(RuntimeError, "page load failed"):
                with SZSEPCFBrowserSession():
                    pass

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(manager.exited)

    def test_browser_session_cleanup_does_not_mask_active_page_error(self):
        class FailingCloseContext(self.FakeContext):
            def close(self):
                self.closed = True
                raise RuntimeError("context cleanup failed")

        page = self.FakeQueryPage(self.FakeResponse("unused"), [])
        context = FailingCloseContext([page])
        browser = self.FakeBrowser(context)
        chromium = self.FakeChromium(browser)
        manager = self.FakePlaywrightManager(chromium)

        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            with self.assertRaisesRegex(SZSEPCFPageError, "report failed"):
                with SZSEPCFBrowserSession():
                    raise SZSEPCFPageError("2026-07-14", "report failed")

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(manager.exited)

    def test_connection_probe_reads_only_the_first_reference(self):
        references = [
            SZSEPCFReference("159915", "2026-07-14", "https://example.test/159915"),
            SZSEPCFReference("159001", "2026-07-14", "https://example.test/159001"),
        ]
        fake_session = self.FakeSession(
            references, {"159915": self.xml, "159001": self.xml}
        )

        ok, message = check_szse_pcf_connection(
            "2026-07-14", session_factory=lambda visible=False: fake_session
        )

        self.assertTrue(ok)
        self.assertIn("浏览器采集", message)
        self.assertEqual(fake_session.queries, [("2026-07-14", "")])
        self.assertEqual([ref.fund_code for ref in fake_session.reads], ["159915"])
        self.assertTrue(fake_session.closed)


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


class CollectionCoverageDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ETFDatabase(Path(self.temp_dir.name) / "stock_data.db")
        self.db.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _info(exchange, fund_code, content_date):
        return {
            "交易所": exchange,
            "基金代码": fund_code,
            "基金名称": f"ETF {fund_code}",
            "最新公告日期": content_date,
            "内容日期": content_date,
        }

    @staticmethod
    def _item(exchange, fund_code, content_date, security_code):
        return {
            "交易所": exchange,
            "基金代码": fund_code,
            "内容日期": content_date,
            "证券代码": security_code,
            "证券简称": f"证券 {security_code}",
            "挂牌市场": exchange,
        }

    def test_returns_all_empty_coverage_cells(self):
        coverage = self.db.get_collection_coverage()

        self.assertEqual(
            coverage["share"]["SSE"],
            {
                "min_date": None,
                "max_date": None,
                "rows_count": 0,
                "fund_count": 0,
                "date_count": 0,
            },
        )
        self.assertEqual(coverage["share"]["SZSE"], coverage["share"]["SSE"])
        self.assertEqual(
            coverage["component"]["SSE"],
            {
                "min_date": None,
                "max_date": None,
                "snapshot_count": 0,
                "fund_count": 0,
                "item_count": 0,
            },
        )
        self.assertEqual(
            coverage["component"]["SZSE"], coverage["component"]["SSE"]
        )

    def test_groups_share_and_component_coverage_by_exchange(self):
        self.db.upsert_rows(
            [
                {
                    "trade_date": "2026-07-01",
                    "exchange": "SSE",
                    "fund_code": "510010",
                    "fund_name": "ETF A",
                    "total_share": 100,
                },
                {
                    "trade_date": "2026-07-03",
                    "exchange": "SSE",
                    "fund_code": "510010",
                    "fund_name": "ETF A",
                    "total_share": 110,
                },
                {
                    "trade_date": "2026-07-03",
                    "exchange": "SSE",
                    "fund_code": "510020",
                    "fund_name": "ETF B",
                    "total_share": 120,
                },
                {
                    "trade_date": "2026-07-02",
                    "exchange": "SZSE",
                    "fund_code": "159001",
                    "fund_name": "ETF C",
                    "total_share": 200,
                },
                {
                    "trade_date": "2026-07-04",
                    "exchange": "SZSE",
                    "fund_code": "159002",
                    "fund_name": "ETF D",
                    "total_share": 210,
                },
            ]
        )

        sse_infos = [
            self._info("SSE", "510010", "2026-07-13"),
            self._info("SSE", "510010", "2026-07-14"),
        ]
        sse_items = [
            self._item("SSE", "510010", "2026-07-13", "600001"),
            self._item("SSE", "510010", "2026-07-13", "600002"),
            self._item("SSE", "510010", "2026-07-14", "600001"),
            self._item("SSE", "510010", "2026-07-14", "600003"),
        ]
        szse_infos = [
            self._info("SZSE", "159001", "2026-07-12"),
            self._info("SZSE", "159002", "2026-07-14"),
        ]
        szse_items = [
            self._item("SZSE", "159001", "2026-07-12", "000001"),
            self._item("SZSE", "159001", "2026-07-12", "000002"),
            self._item("SZSE", "159002", "2026-07-14", "300001"),
        ]
        self.db.upsert_pcf(sse_infos + szse_infos, sse_items + szse_items)

        coverage = self.db.get_collection_coverage()

        self.assertEqual(
            coverage["share"]["SSE"],
            {
                "min_date": "2026-07-01",
                "max_date": "2026-07-03",
                "rows_count": 3,
                "fund_count": 2,
                "date_count": 2,
            },
        )
        self.assertEqual(coverage["share"]["SZSE"]["max_date"], "2026-07-04")
        self.assertEqual(
            coverage["component"]["SSE"],
            {
                "min_date": "2026-07-13",
                "max_date": "2026-07-14",
                "snapshot_count": 2,
                "fund_count": 1,
                "item_count": 4,
            },
        )
        self.assertEqual(coverage["component"]["SZSE"]["item_count"], 3)


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
            self.assertIn("日期", item_columns)
            self.assertIn("基金名称", item_columns)
            self.assertIn("市场", item_columns)
            self.assertIn("成分股代码", item_columns)
            self.assertIn("现金替代标志含义", item_columns)

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

    def test_writes_flattened_item_fields_for_sse_and_szse(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            sse_info = self._info("2026-07-13")
            sse_item = self._item("600009", "2026-07-13")
            szse_info = {
                **self._info("2026-07-14"),
                "交易所": "SZSE",
                "基金代码": "159915",
                "基金名称": "创业板ETF",
                "基金份额净值": 2.5,
                "最小申购、赎回单位": 500000.0,
            }
            szse_item = {
                **self._item("300001", "2026-07-14"),
                "交易所": "SZSE",
                "基金代码": "159915",
                "证券简称": "特锐德",
                "股票数量": 1200,
                "现金替代标志": "必须现金替代",
                "挂牌市场": "深圳证券交易所",
            }

            db.replace_pcf_snapshot(sse_info, [sse_item])
            db.replace_pcf_snapshot(szse_info, [szse_item])

            with closing(db.connect()) as conn:
                rows = conn.execute(
                    '''SELECT "日期", "基金代码", "基金名称", "市场", "申赎单位",
                              "单位净值", "预估现金差额", "最大现金替代比例",
                              "成分股代码", "成分股名称", "数量", "现金替代标志",
                              "现金替代标志含义", "申购现金替代溢价比例",
                              "赎回现金替代折价比例", source
                       FROM ETF_ITEM ORDER BY "基金代码"'''
                ).fetchall()

            self.assertEqual(len(rows), 2)
            rows_by_code = {row[1]: tuple(row) for row in rows}
            self.assertEqual(
                rows_by_code["510010"],
                (
                    "2026-07-13", "510010", "治理ETF", "上海证券交易所",
                    1000000.0, 1.68, 100.0, 30.0, "600009", "上海机场",
                    300.0, 1, "允许", 34.0, 0.0, "sse_pcf",
                ),
            )
            self.assertEqual(
                rows_by_code["159915"],
                (
                    "2026-07-14", "159915", "创业板ETF", "深圳证券交易所",
                    500000.0, 2.5, 100.0, 30.0, "300001", "特锐德",
                    1200.0, 2, "必须现金替代", 34.0, 0.0, "sse_pcf",
                ),
            )

    def test_incomplete_pcf_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            db.upsert_pcf([self._info()], [])

            self.assertFalse(db.pcf_is_complete("SSE", "510010", "2026-07-13"))

    def test_lists_stock_trading_dates_and_latest_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()

            self.assertEqual(db.list_stock_trading_dates("2026-07-10", "2026-07-14"), [])
            self.assertIsNone(db.latest_stock_trading_date("2026-07-14"))

            with closing(db.connect()) as conn:
                conn.execute('CREATE TABLE stock_daily ("日期" INTEGER)')
                conn.executemany(
                    'INSERT INTO stock_daily ("日期") VALUES (?)',
                    [(20260710,), (20260710,), (20260713,)],
                )
                conn.commit()

            self.assertEqual(
                db.list_stock_trading_dates("2026-07-10", "2026-07-14"),
                ["2026-07-10", "2026-07-13"],
            )
            self.assertEqual(db.latest_stock_trading_date("2026-07-14"), "2026-07-13")

    def test_replaces_one_pcf_snapshot_without_affecting_other_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            old_info = {**self._info("2026-07-13"), "交易所": "SZSE", "基金代码": "159915"}
            old_item = {
                **self._item("300002", "2026-07-13"),
                "交易所": "SZSE",
                "基金代码": "159915",
                "挂牌市场": "SZSE",
            }
            info = {**self._info("2026-07-14"), "交易所": "SZSE", "基金代码": "159915"}
            items = [
                {
                    **self._item("300001", "2026-07-14"),
                    "交易所": "SZSE",
                    "基金代码": "159915",
                    "挂牌市场": "SZSE",
                },
                {
                    **self._item("300002", "2026-07-14"),
                    "交易所": "SZSE",
                    "基金代码": "159915",
                    "挂牌市场": "SZSE",
                },
            ]

            db.replace_pcf_snapshot(old_info, [old_item])
            db.replace_pcf_snapshot(info, items)
            db.replace_pcf_snapshot(info, [items[0]], source="szse_pcf_browser")

            with closing(db.connect()) as conn:
                current_codes = [
                    row["成分股代码"]
                    for row in conn.execute(
                        'SELECT "成分股代码" FROM ETF_ITEM '
                        'WHERE "基金代码" = ? AND "日期" = ? '
                        'ORDER BY "成分股代码"',
                        ("159915", "2026-07-14"),
                    )
                ]
                old_codes = [
                    row["成分股代码"]
                    for row in conn.execute(
                        'SELECT "成分股代码" FROM ETF_ITEM '
                        'WHERE "基金代码" = ? AND "日期" = ? ',
                        ("159915", "2026-07-13"),
                    )
                ]
                info_source = conn.execute(
                    'SELECT source FROM ETF_INFO WHERE "交易所" = ? AND "基金代码" = ? AND "内容日期" = ?',
                    ("SZSE", "159915", "2026-07-14"),
                ).fetchone()["source"]
                item_source = conn.execute(
                    'SELECT source FROM ETF_ITEM WHERE "基金代码" = ? AND "日期" = ?',
                    ("159915", "2026-07-14"),
                ).fetchone()["source"]

            self.assertTrue(db.pcf_is_complete("SZSE", "159915", "2026-07-14"))
            self.assertEqual(current_codes, ["300001"])
            self.assertEqual(old_codes, ["300002"])
            self.assertEqual(info_source, "szse_pcf_browser")
            self.assertEqual(item_source, "szse_pcf_browser")

    def test_snapshot_replacement_rolls_back_after_item_insert_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            info = {**self._info("2026-07-14"), "交易所": "SZSE", "基金代码": "159915"}
            original_item = {
                **self._item("300001", "2026-07-14"),
                "交易所": "SZSE",
                "基金代码": "159915",
                "挂牌市场": "SZSE",
            }
            failed_item = {**original_item, "证券代码": "300002"}
            db.replace_pcf_snapshot(info, [original_item], source="original")

            with closing(db.connect()) as conn:
                conn.execute(
                    '''CREATE TRIGGER fail_replacement_item BEFORE INSERT ON ETF_ITEM
                    WHEN NEW."成分股代码" = '300002'
                    BEGIN SELECT RAISE(ABORT, 'forced item insert failure'); END'''
                )
                conn.commit()

            replacement_info = {**info, "基金名称": "replacement"}
            with self.assertRaisesRegex(sqlite3.IntegrityError, "forced item insert failure"):
                db.replace_pcf_snapshot(replacement_info, [failed_item], source="replacement")

            with closing(db.connect()) as conn:
                item_rows = conn.execute(
                    'SELECT "成分股代码", source FROM ETF_ITEM '
                    'WHERE "基金代码" = ? AND "日期" = ?',
                    ("159915", "2026-07-14"),
                ).fetchall()
                info_row = conn.execute(
                    'SELECT "基金名称", source FROM ETF_INFO '
                    'WHERE "交易所" = ? AND "基金代码" = ? AND "内容日期" = ?',
                    ("SZSE", "159915", "2026-07-14"),
                ).fetchone()

            self.assertEqual([(row["成分股代码"], row["source"]) for row in item_rows], [("300001", "original")])
            self.assertEqual((info_row["基金名称"], info_row["source"]), (info["基金名称"], "original"))

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


class HoldingDatabaseTests(unittest.TestCase):
    @staticmethod
    def _row(exchange="SSE", code="510010", period="2024-06-30", stock="600000"):
        return {
            "交易所": exchange,
            "基金代码": code,
            "基金名称": "治理ETF",
            "报告年度": 2024,
            "报告季度": 2,
            "报告期": period,
            "可用日期": "2024-08-30",
            "数据完整性": "完整披露",
            "序号": 1,
            "股票代码": stock,
            "股票名称": "浦发银行",
            "占净值比例": 2.5,
            "持股数": 100000.0,
            "持仓市值": 2000000.0,
            "挂牌市场": "上交所",
        }

    def test_holding_snapshot_is_replaced_idempotently_and_coverage_is_report_based(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ETFDatabase(Path(tmp) / "stock_data.db")
            db.initialize()
            with closing(db.connect()) as conn:
                columns = [row["name"] for row in conn.execute("PRAGMA table_info(ETF_HOLDING)")]
            self.assertEqual(
                set(columns),
                {
                    "编号", "交易所", "基金代码", "基金名称", "报告年度", "报告季度", "报告期",
                    "可用日期", "数据完整性", "序号", "股票代码", "股票名称", "占净值比例",
                    "持股数", "持仓市值", "挂牌市场", "来源", "更新时间",
                },
            )
            first = self._row()
            second = {**first, "股票代码": "600001", "股票名称": "邯郸钢铁"}
            db.replace_holding_snapshot([first, second])
            db.replace_holding_snapshot([{**first, "持仓市值": 3000000.0}])

            with closing(db.connect()) as conn:
                rows = conn.execute(
                    'SELECT "股票代码", "持仓市值" FROM ETF_HOLDING '
                    'WHERE "基金代码" = ? ORDER BY "股票代码"',
                    ("510010",),
                ).fetchall()
            self.assertEqual([(row["股票代码"], row["持仓市值"]) for row in rows], [("600000", 3000000.0)])
            self.assertTrue(db.holding_snapshot_exists("SSE", "510010", "2024-06-30"))
            coverage = db.get_collection_coverage()["holding"]["SSE"]
            self.assertEqual(coverage["report_count"], 1)
            self.assertEqual(coverage["item_count"], 1)
            self.assertEqual(coverage["full_report_count"], 1)


class PCFGuiTests(unittest.TestCase):
    @staticmethod
    def _app():
        app = ETFApp.__new__(ETFApp)
        app.db = Mock()
        app.log = Mock()
        app.paused_task = None
        app.paused_pcf_task = None
        app.date_var = Mock()
        app.date_var.get.return_value = "2026-07-14"
        app.workers_var = Mock()
        app.workers_var.get.return_value = "4"
        app._selected_exchanges = Mock(return_value=("SSE",))
        return app

    def test_collection_panel_state_switches_task_specific_controls(self):
        self.assertEqual(
            collection_panel_state("share", "range", "沪深两市"),
            {
                "show_single_date": False,
                "show_range_dates": True,
                "show_workers": True,
                "show_pcf_options": False,
                "button_text": "开始采集份额",
                "notice": "",
            },
        )
        self.assertEqual(
            collection_panel_state("component", "single", "上交所")["notice"],
            "上交所成分股仅更新最新快照，所选日期范围不适用。",
        )
        self.assertEqual(
            collection_panel_state("component", "range", "沪深两市")["notice"],
            "选择沪深两市时：上交所更新一次最新快照；深交所按所选日期区间采集。",
        )
        holding_state = collection_panel_state("holding", "range", "沪深两市")
        self.assertTrue(holding_state["show_holding_years"])
        self.assertFalse(holding_state["show_range_dates"])
        self.assertEqual(holding_state["button_text"], "开始采集季度持仓")

    def test_formats_share_and_component_coverage_cells(self):
        self.assertEqual(
            format_coverage_cell(
                "share",
                {
                    "min_date": "2016-01-04",
                    "max_date": "2026-07-10",
                    "rows_count": 100,
                    "fund_count": 12,
                    "date_count": 2000,
                },
            ),
            "2016-01-04 ~ 2026-07-10 | 100 行 / 12 只 / 2000 日",
        )
        self.assertEqual(
            format_coverage_cell(
                "component",
                {
                    "min_date": "2026-07-13",
                    "max_date": "2026-07-14",
                    "snapshot_count": 5,
                    "fund_count": 2,
                    "item_count": 100,
                },
            ),
            "2026-07-13 ~ 2026-07-14 | 5 快照 / 2 只 / 100 成分",
        )
        self.assertEqual(format_coverage_cell("component", {"min_date": None}), "暂无数据")
        self.assertEqual(
            format_coverage_cell(
                "holding",
                {
                    "min_date": "2024-03-31",
                    "max_date": "2024-06-30",
                    "report_count": 2,
                    "fund_count": 1,
                    "item_count": 30,
                    "full_report_count": 1,
                },
            ),
            "2024-03-31 ~ 2024-06-30 | 2 份报告 / 1 只 / 30 条 / 完整 1",
        )

    def test_gui_uses_unified_collection_form_and_independent_log_scrollbar(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8-sig")
        build_ui = source[source.index("    def _build_ui"):source.index("    def log")]
        self.assertIn("fetch_sse_pcf_for_fund", source)
        self.assertNotIn("采集当前 PCF", build_ui)
        self.assertNotIn("批量采集上交所当前 PCF", build_ui)
        self.assertNotIn("采集深交所当前 PCF", build_ui)
        self.assertNotIn("采集深交所历史 PCF", build_ui)
        self.assertIn("ETF 数据采集", build_ui)
        self.assertIn("共同采集范围", build_ui)
        self.assertIn("ETF 份额", build_ui)
        self.assertIn("ETF 成分股", build_ui)
        self.assertIn("基金季度持仓", build_ui)
        self.assertIn("报告年度", build_ui)
        self.assertIn("数据库覆盖范围", build_ui)
        self.assertIn("重新采集已有快照", source)
        self.assertIn("log_frame", source)
        self.assertIn("log_scrollbar", source)
        self.assertIn("yscrollcommand=log_scrollbar.set", source)

    def _dispatch_app(self, exchange="上交所", date_mode="range", code="", replace=False):
        app = self._app()
        app.exchange_var = Mock()
        app.exchange_var.get.return_value = exchange
        app.date_mode_var = Mock()
        app.date_mode_var.get.return_value = date_mode
        app.date_var = Mock()
        app.date_var.get.return_value = "2026-07-14"
        app.start_var = Mock()
        app.start_var.get.return_value = "2026-07-01"
        app.end_var = Mock()
        app.end_var.get.return_value = "2026-07-14"
        app.pcf_code_var = Mock()
        app.pcf_code_var.get.return_value = code
        app.pcf_replace_var = Mock()
        app.pcf_replace_var.get.return_value = replace
        app._selected_exchanges = ETFApp._selected_exchanges.__get__(app)
        app._run = Mock()
        app._fetch_sse_components = Mock()
        app._fetch_szse_pcf_current = Mock()
        app._fetch_szse_pcf_history = Mock()
        return app

    def test_component_dispatch_sse_range_fetches_latest_once(self):
        app = self._dispatch_app(exchange="上交所", date_mode="range")

        app.fetch_selected_components()
        task = app._run.call_args.args[0]
        task()

        app._fetch_sse_components.assert_called_once_with("", False)
        app._fetch_szse_pcf_current.assert_not_called()
        app._fetch_szse_pcf_history.assert_not_called()

    def test_component_dispatch_both_range_fetches_sse_then_szse(self):
        app = self._dispatch_app(exchange="沪深两市", date_mode="range")

        app.fetch_selected_components()
        task = app._run.call_args.args[0]
        task()

        app._fetch_sse_components.assert_called_once_with("", False)
        app._fetch_szse_pcf_history.assert_called_once_with(
            "2026-07-01", "2026-07-14", "", False
        )

    def test_component_dispatch_single_date_uses_selected_szse_date(self):
        app = self._dispatch_app(
            exchange="深交所", date_mode="single", code="159915"
        )

        app.fetch_selected_components()
        app._run.call_args.args[0]()

        app._fetch_szse_pcf_current.assert_called_once_with(
            "159915",
            False,
            current_date="2026-07-14",
            fallback_pending=False,
        )

    def test_component_dispatch_stops_before_szse_when_sse_pauses(self):
        app = self._dispatch_app(exchange="沪深两市", date_mode="range")

        def pause_sse(*_args):
            app.paused_pcf_task = {"mode": "sse", "codes": ["510010"]}

        app._fetch_sse_components.side_effect = pause_sse
        app.fetch_selected_components()
        app._run.call_args.args[0]()

        app._fetch_szse_pcf_history.assert_not_called()

    def test_component_code_rejects_full_width_digits(self):
        app = self._dispatch_app(code="１５９９１５")

        with patch("etf_gui.messagebox.showerror") as showerror:
            app.fetch_selected_components()

        app._run.assert_not_called()
        showerror.assert_called_once()

    def test_component_range_rejects_reversed_dates(self):
        app = self._dispatch_app(date_mode="range")
        app.start_var.get.return_value = "2026-07-15"

        with patch("etf_gui.messagebox.showerror") as showerror:
            app.fetch_selected_components()

        app._run.assert_not_called()
        showerror.assert_called_once()

    def test_component_overwrite_requires_confirmation(self):
        app = self._dispatch_app(replace=True)

        with patch("etf_gui.messagebox.askyesno", return_value=False) as askyesno:
            app.fetch_selected_components()

        askyesno.assert_called_once()
        app._run.assert_not_called()

    def test_start_selected_collection_routes_by_content_and_date_mode(self):
        app = self._dispatch_app()
        app.fetch_selected_components = Mock()
        app.fetch_single = Mock()
        app.fetch_range = Mock()

        app.data_type_var = Mock()
        app.data_type_var.get.return_value = "component"
        app.start_selected_collection()
        app.fetch_selected_components.assert_called_once()

        app.data_type_var.get.return_value = "share"
        app.date_mode_var.get.return_value = "single"
        app.start_selected_collection()
        app.fetch_single.assert_called_once()

        app.date_mode_var.get.return_value = "range"
        app.start_selected_collection()
        app.fetch_range.assert_called_once()

    def test_sse_components_skip_complete_and_replace_incomplete_snapshots(self):
        app = self._app()
        app.db.list_fund_codes.return_value = [
            {"fund_code": "510010", "fund_name": "ETF A"},
            {"fund_code": "510020", "fund_name": "ETF B"},
        ]
        app.db.pcf_is_complete.side_effect = [True, False]

        def fetched(code):
            return (
                {
                    "交易所": "SSE",
                    "基金代码": code,
                    "内容日期": "2026-07-14",
                },
                [{"证券代码": "600001"}],
            )

        with patch("etf_gui.fetch_sse_pcf_for_fund", side_effect=fetched) as fetch:
            app._fetch_sse_components("", False)

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(app.db.pcf_is_complete.call_count, 2)
        app.db.replace_pcf_snapshot.assert_called_once_with(
            {
                "交易所": "SSE",
                "基金代码": "510020",
                "内容日期": "2026-07-14",
            },
            [{"证券代码": "600001"}],
            source="sse_pcf",
        )

    def test_sse_component_overwrite_uses_only_explicit_code(self):
        app = self._app()
        info = {
            "交易所": "SSE",
            "基金代码": "510010",
            "内容日期": "2026-07-14",
        }
        items = [{"证券代码": "600001"}]
        app.db.replace_pcf_snapshot.return_value = (1, 1)

        with patch(
            "etf_gui.fetch_sse_pcf_for_fund", return_value=(info, items)
        ) as fetch:
            app._fetch_sse_components("510010", True)

        app.db.list_fund_codes.assert_not_called()
        app.db.pcf_is_complete.assert_not_called()
        fetch.assert_called_once_with("510010")
        app.db.replace_pcf_snapshot.assert_called_once_with(
            info, items, source="sse_pcf"
        )

    def test_current_queries_today_then_latest_stock_day_after_zero_discovery(self):
        app = self._app()
        app.db.latest_stock_trading_date.return_value = "2026-07-13"
        app._fetch_szse_pcf_dates = Mock(
            side_effect=[{"discovered": 0}, {"discovered": 2}]
        )

        with patch("etf_gui.datetime") as mocked_datetime:
            mocked_datetime.now.return_value.strftime.return_value = "2026-07-14"
            result = app._fetch_szse_pcf_current("159915", True)

        self.assertEqual(result, {"discovered": 2})
        self.assertEqual(
            app._fetch_szse_pcf_dates.call_args_list,
            [
                call(["2026-07-14"], "159915", True, mode="current", fallback_pending=True),
                call(["2026-07-13"], "159915", True, mode="current", fallback_pending=False),
            ],
        )
        app.db.latest_stock_trading_date.assert_called_once_with("2026-07-14")

    def test_current_does_not_look_up_latest_day_when_today_has_files(self):
        app = self._app()
        app._fetch_szse_pcf_dates = Mock(return_value={"discovered": 1})

        with patch("etf_gui.datetime") as mocked_datetime:
            mocked_datetime.now.return_value.strftime.return_value = "2026-07-14"
            app._fetch_szse_pcf_current("", False)

        app.db.latest_stock_trading_date.assert_not_called()
        app._fetch_szse_pcf_dates.assert_called_once_with(
            ["2026-07-14"], "", False, mode="current", fallback_pending=True
        )

    def test_history_uses_only_stock_daily_trading_dates(self):
        app = self._app()
        dates = ["2026-07-10", "2026-07-13"]
        app.db.list_stock_trading_dates.return_value = dates
        app._fetch_szse_pcf_dates = Mock(return_value={"discovered": 2})

        result = app._fetch_szse_pcf_history(
            "2026-07-10", "2026-07-14", "159915", False
        )

        self.assertEqual(result, {"discovered": 2})
        app.db.list_stock_trading_dates.assert_called_once_with(
            "2026-07-10", "2026-07-14"
        )
        app._fetch_szse_pcf_dates.assert_called_once_with(
            dates, "159915", False, mode="history", fallback_pending=False
        )

    def test_collector_uses_hidden_browser_and_atomic_szse_source(self):
        app = self._app()
        app.db.pcf_is_complete.return_value = True
        summary = {"discovered": 1}

        with patch(
            "etf_gui.collect_szse_pcf_via_browser", return_value=summary
        ) as collect:
            result = app._fetch_szse_pcf_dates(["2026-07-14"], "159915", True)

        self.assertEqual(result, summary)
        kwargs = collect.call_args.kwargs
        self.assertEqual(collect.call_args.args, (["2026-07-14"],))
        self.assertEqual(kwargs["fund_code"], "159915")
        self.assertTrue(kwargs["replace_existing"])
        self.assertFalse(kwargs["visible"])
        self.assertIs(kwargs["on_progress"], app.log)
        self.assertTrue(kwargs["is_complete"]("159915", "2026-07-14"))
        app.db.pcf_is_complete.assert_called_once_with(
            "SZSE", "159915", "2026-07-14"
        )
        info = {"基金代码": "159915"}
        items = [{"证券代码": "300001"}]
        kwargs["save_snapshot"](info, items)
        app.db.replace_pcf_snapshot.assert_called_once_with(
            info, items, source="szse_pcf_browser"
        )

    def test_page_error_pauses_exact_original_remaining_dates(self):
        app = self._app()
        dates = ["2026-07-10", "2026-07-11", "2026-07-14"]

        with patch(
            "etf_gui.collect_szse_pcf_via_browser",
            side_effect=SZSEPCFPageError("2026-07-11", "page failed"),
        ):
            result = app._fetch_szse_pcf_dates(dates, "159915", True)

        self.assertIsNone(result)
        self.assertEqual(
            app.paused_pcf_task,
            {
                "mode": "history",
                "dates": ["2026-07-11", "2026-07-14"],
                "code": "159915",
                "replace": True,
                "fallback_pending": False,
            },
        )

    def test_pcf_resume_retries_exact_dates_before_legacy_share_resume(self):
        app = self._app()
        paused_dates = ["2026-07-14"]
        app.paused_pcf_task = {
            "mode": "history",
            "dates": paused_dates,
            "code": "159915",
            "replace": False,
            "fallback_pending": False,
        }
        app.paused_task = ("single", "SSE", "2026-07-10", "2026-07-10")
        app._fetch_szse_pcf_dates = Mock(return_value={"discovered": 1})
        app._fetch_date = Mock()

        with (
            patch(
                "etf_gui.check_szse_pcf_connection",
                return_value=(True, "PCF connected"),
            ) as check_pcf,
            patch("etf_gui.check_sse_connection") as check_sse,
        ):
            app._continue_paused_task()

        check_pcf.assert_called_once_with("2026-07-14")
        check_sse.assert_not_called()
        app.db.list_stock_trading_dates.assert_not_called()
        app._fetch_szse_pcf_dates.assert_called_once_with(
            paused_dates, "159915", False, mode="history", fallback_pending=False
        )
        self.assertIsNone(app.paused_pcf_task)
        self.assertEqual(
            app.paused_task, ("single", "SSE", "2026-07-10", "2026-07-10")
        )
        app._fetch_date.assert_not_called()

    def test_history_resume_keeps_exact_dates_and_pause_when_dispatch_raises(self):
        app = self._app()
        paused_dates = ["2026-07-10", "2026-07-13"]
        paused_task = {
            "mode": "history",
            "dates": paused_dates,
            "code": "",
            "replace": True,
            "fallback_pending": False,
        }
        app.paused_pcf_task = paused_task
        app._fetch_szse_pcf_dates = Mock(side_effect=RuntimeError("dispatch failed"))

        with patch(
            "etf_gui.check_szse_pcf_connection", return_value=(True, "PCF connected")
        ):
            with self.assertRaisesRegex(RuntimeError, "dispatch failed"):
                app._continue_paused_task()

        app._fetch_szse_pcf_dates.assert_called_once_with(
            paused_dates, "", True, mode="history", fallback_pending=False
        )
        self.assertIs(app.paused_pcf_task, paused_task)
        app.db.list_stock_trading_dates.assert_not_called()

    def test_current_resume_retries_paused_date_then_falls_back_once_after_zero_discovery(self):
        app = self._app()
        app.paused_pcf_task = {
            "mode": "current",
            "dates": ["2026-07-14"],
            "code": "159915",
            "replace": False,
            "fallback_pending": True,
        }
        app.db.latest_stock_trading_date.return_value = "2026-07-13"
        app._fetch_szse_pcf_dates = Mock(
            side_effect=[{"discovered": 0}, {"discovered": 1}]
        )

        with patch(
            "etf_gui.check_szse_pcf_connection", return_value=(True, "PCF connected")
        ) as check_pcf:
            app._continue_paused_task()

        check_pcf.assert_called_once_with("2026-07-14")
        app.db.latest_stock_trading_date.assert_called_once_with("2026-07-14")
        self.assertEqual(
            app._fetch_szse_pcf_dates.call_args_list,
            [
                call(["2026-07-14"], "159915", False, mode="current", fallback_pending=True),
                call(["2026-07-13"], "159915", False, mode="current", fallback_pending=False),
            ],
        )
        self.assertIsNone(app.paused_pcf_task)

    def test_current_resume_after_fallback_retries_only_the_paused_date(self):
        app = self._app()
        app.paused_pcf_task = {
            "mode": "current",
            "dates": ["2026-07-13"],
            "code": "159915",
            "replace": True,
            "fallback_pending": False,
        }
        app._fetch_szse_pcf_dates = Mock(return_value={"discovered": 1})

        with patch(
            "etf_gui.check_szse_pcf_connection", return_value=(True, "PCF connected")
        ):
            app._continue_paused_task()

        app.db.latest_stock_trading_date.assert_not_called()
        app._fetch_szse_pcf_dates.assert_called_once_with(
            ["2026-07-13"], "159915", True, mode="current", fallback_pending=False
        )
        self.assertIsNone(app.paused_pcf_task)

    def test_legacy_share_resume_still_dispatches_when_no_pcf_is_paused(self):
        app = self._app()
        app.paused_task = ("single", "SSE", "2026-07-10", "2026-07-10")
        app._fetch_date = Mock()

        with patch(
            "etf_gui.check_sse_connection", return_value=(True, "SSE connected")
        ):
            app._continue_paused_task()

        app._fetch_date.assert_called_once_with(
            "2026-07-10",
            exchange="SSE",
            pause_task=("single", "SSE", "2026-07-10", "2026-07-10"),
        )
        self.assertIsNone(app.paused_task)

    def test_szse_code_accepts_empty_or_six_ascii_digits(self):
        app = self._app()
        for code in ("", "159915"):
            with self.subTest(code=code):
                app.pcf_code_var = Mock()
                app.pcf_code_var.get.return_value = code
                self.assertEqual(app._szse_pcf_code_or_none(), code)

    def test_szse_code_rejects_full_width_digits(self):
        app = self._app()
        app.pcf_code_var = Mock()
        app.pcf_code_var.get.return_value = "１５９９１５"

        with patch("etf_gui.messagebox.showerror") as showerror:
            result = app._szse_pcf_code_or_none()

        self.assertIsNone(result)
        showerror.assert_called_once()

    def test_share_connection_probe_checks_selected_exchanges_without_resuming(self):
        app = self._app()
        app.paused_task = ("single", "SSE", "2026-07-10", "2026-07-10")

        with (
            patch("etf_gui.check_sse_connection", return_value=(True, "SSE ok")) as sse,
            patch(
                "etf_gui.check_szse_download_connection", return_value=(True, "SZSE ok")
            ) as szse,
        ):
            app._test_selected_connection(
                "share", ("SSE", "SZSE"), "2026-07-14", ""
            )

        sse.assert_called_once_with("2026-07-14")
        szse.assert_called_once_with("2026-07-14")
        self.assertEqual(
            app.paused_task, ("single", "SSE", "2026-07-10", "2026-07-10")
        )

    def test_sse_component_connection_probe_reads_without_writing(self):
        app = self._app()
        app.db.list_fund_codes.return_value = [
            {"fund_code": "510010", "fund_name": "ETF A"}
        ]

        with patch(
            "etf_gui.fetch_sse_pcf_for_fund",
            return_value=(
                {"基金代码": "510010", "内容日期": "2026-07-14"},
                [{"证券代码": "600001"}],
            ),
        ) as fetch:
            app._test_selected_connection("component", ("SSE",), "2026-07-14", "")

        fetch.assert_called_once_with("510010")
        app.db.replace_pcf_snapshot.assert_not_called()
        app.db.upsert_pcf.assert_not_called()

    def test_szse_component_connection_probe_uses_shared_date(self):
        app = self._app()

        with patch(
            "etf_gui.check_szse_pcf_connection", return_value=(True, "SZSE PCF ok")
        ) as check:
            app._test_selected_connection(
                "component", ("SZSE",), "2026-07-14", "159915"
            )

        check.assert_called_once_with("2026-07-14")

    def test_continue_button_reflects_pause_and_busy_state(self):
        app = self._app()
        app.continue_button = Mock()
        app.busy = False

        app._update_continue_button()
        app.continue_button.configure.assert_called_with(state="disabled")

        app.paused_task = ("single", "SSE", "2026-07-14", "2026-07-14")
        app._update_continue_button()
        app.continue_button.configure.assert_called_with(state="normal")

        app.busy = True
        app._update_continue_button()
        app.continue_button.configure.assert_called_with(state="disabled")

    def test_continue_paused_task_does_nothing_without_saved_pause(self):
        app = self._app()
        app._run = Mock()

        app.continue_paused_task()

        app._run.assert_not_called()

    def test_refresh_stats_updates_all_four_coverage_cells_from_one_query(self):
        app = self._app()
        app.coverage_vars = {
            (kind, exchange): Mock()
            for kind in ("share", "component")
            for exchange in ("SSE", "SZSE")
        }
        app.db.get_collection_coverage.return_value = {
            "share": {
                "SSE": {
                    "min_date": "2026-07-01",
                    "max_date": "2026-07-14",
                    "rows_count": 10,
                    "fund_count": 2,
                    "date_count": 5,
                },
                "SZSE": {"min_date": None},
            },
            "component": {
                "SSE": {"min_date": None},
                "SZSE": {
                    "min_date": "2026-07-14",
                    "max_date": "2026-07-14",
                    "snapshot_count": 1,
                    "fund_count": 1,
                    "item_count": 20,
                },
            },
        }

        app._refresh_stats()

        app.db.get_collection_coverage.assert_called_once_with()
        app.coverage_vars[("share", "SSE")].set.assert_called_once_with(
            "2026-07-01 ~ 2026-07-14 | 10 行 / 2 只 / 5 日"
        )
        app.coverage_vars[("share", "SZSE")].set.assert_called_once_with("暂无数据")
        app.coverage_vars[("component", "SSE")].set.assert_called_once_with("暂无数据")
        app.coverage_vars[("component", "SZSE")].set.assert_called_once_with(
            "2026-07-14 ~ 2026-07-14 | 1 快照 / 1 只 / 20 成分"
        )

    def test_busy_state_disables_collection_controls_and_preserves_combo_mode(self):
        app = self._app()
        app.task_status_var = Mock()
        app.exchange_combo = Mock()
        regular_widget = Mock()
        app.task_input_widgets = [regular_widget, app.exchange_combo]
        app.continue_button = Mock()

        app._set_busy_ui(True)
        regular_widget.configure.assert_called_with(state="disabled")
        app.exchange_combo.configure.assert_called_with(state="disabled")
        app.task_status_var.set.assert_called_with("采集中")

        app._set_busy_ui(False, "已暂停")
        regular_widget.configure.assert_called_with(state="normal")
        app.exchange_combo.configure.assert_called_with(state="readonly")
        app.task_status_var.set.assert_called_with("已暂停")


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
    def test_database_path_config_round_trip_and_invalid_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "etf_gui_config.json"
            default_path = Path(tmp) / "default.db"
            selected_path = Path(tmp) / "selected.db"

            self.assertEqual(load_database_path(config_path, default_path), default_path)
            save_database_path(selected_path, config_path)
            self.assertEqual(load_database_path(config_path, default_path), selected_path)

            config_path.write_text("{bad json", encoding="utf-8")
            self.assertEqual(load_database_path(config_path, default_path), default_path)

    def test_gui_contains_database_path_picker_and_runtime_switch(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8-sig")
        self.assertIn("选择数据库", source)
        self.assertIn("filedialog.askopenfilename", source)
        self.assertIn("self.server.db_path = new_path", source)
        self.assertIn("save_database_path(new_path)", source)

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

    def test_gui_connection_test_uses_current_selection_without_resuming(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("def test_selected_connection", source)
        self.assertIn("exchanges = tuple(self._selected_exchanges())", source)
        self.assertIn("def continue_paused_task", source)
        self.assertNotIn("测试连通性/继续采集", source)

    def test_gui_connection_test_uses_task_specific_szse_probe(self):
        source = Path("etf_gui.py").read_text(encoding="utf-8")

        self.assertIn("check_szse_download_connection", source)
        self.assertIn("check_szse_pcf_connection", source)

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
