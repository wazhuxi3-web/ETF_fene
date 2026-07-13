from __future__ import annotations

import re
import json
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlencode
from urllib.request import Request, urlopen


INFO_COLUMNS = (
    "交易所",
    "基金代码",
    "基金名称",
    "基金管理公司名称",
    "最新公告日期",
    "内容日期",
    "现金差额",
    "最小申购、赎回单位净值",
    "基金份额净值",
    "最小申购、赎回单位的预估现金部分",
    "现金替代比例上限",
    "当日累计可申购的基金份额上限",
    "当日累计可赎回的基金份额上限",
    "当日净申购的基金份额上限",
    "当日净赎回的基金份额上限",
    "单个证券账户当日净申购的基金份额上限",
    "单个证券账户当日净赎回的基金份额上限",
    "单个证券账户当日累计可申购的基金份额上限",
    "单个证券账户当日累计可赎回的基金份额上限",
    "是否需要公布IOPV",
    "最小申购、赎回单位",
    "申购赎回的允许情况",
    "申购赎回模式",
)

ITEM_COLUMNS = (
    "交易所",
    "基金代码",
    "内容日期",
    "证券代码",
    "证券简称",
    "股票数量",
    "现金替代标志",
    "申购现金替代溢价比例",
    "赎回现金替代折价比例",
    "替代金额",
    "挂牌市场",
)

_NULL_VALUES = {"", "-", "--", "—", "－", "None", "null", "N/A"}


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return None if text in _NULL_VALUES else text


def _number(value) -> float | None:
    text = _clean_text(value)
    if text is None:
        return None
    text = text.replace(",", "").replace("￥", "").replace("¥", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _copy_columns(raw: dict, columns: tuple[str, ...], exchange: str) -> dict:
    normalized = {column: None for column in columns}
    normalized["交易所"] = str(exchange or "SSE").strip().upper()
    for column in columns:
        if column == "交易所":
            continue
        value = raw.get(column)
        if column in {
            "现金差额",
            "最小申购、赎回单位净值",
            "基金份额净值",
            "最小申购、赎回单位的预估现金部分",
            "现金替代比例上限",
            "当日累计可申购的基金份额上限",
            "当日累计可赎回的基金份额上限",
            "当日净申购的基金份额上限",
            "当日净赎回的基金份额上限",
            "单个证券账户当日净申购的基金份额上限",
            "单个证券账户当日净赎回的基金份额上限",
            "单个证券账户当日累计可申购的基金份额上限",
            "单个证券账户当日累计可赎回的基金份额上限",
            "最小申购、赎回单位",
        }:
            normalized[column] = _number(value)
        else:
            normalized[column] = _clean_text(value)
    return normalized


def normalize_pcf_info(raw: dict, exchange: str = "SSE") -> dict:
    return _copy_columns(raw, INFO_COLUMNS, exchange)


def normalize_pcf_item(raw: dict, exchange: str = "SSE") -> dict:
    normalized = _copy_columns(raw, ITEM_COLUMNS, exchange)
    for column in (
        "股票数量",
        "申购现金替代溢价比例",
        "赎回现金替代折价比例",
        "替代金额",
    ):
        normalized[column] = _number(raw.get(column))
    return normalized


def _header_name(value: str) -> str:
    name = _clean_text(value) or ""
    name = re.sub(r"[（(].*?[）)]", "", name)
    return re.sub(r"\s+", "", name)


class _PCFTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = ""
        self._heading_parts: list[str] | None = None
        self._table_rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self.tables: list[tuple[str, list[list[str]]]] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"h1", "h2", "h3", "h4"}:
            self._heading_parts = []
        elif tag == "table":
            self._table_rows = []
        elif tag == "tr" and self._table_rows is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"h1", "h2", "h3", "h4"} and self._heading_parts is not None:
            self.heading = _clean_text("".join(self._heading_parts)) or ""
            self._heading_parts = None
        elif tag in {"td", "th"} and self._cell_parts is not None and self._row is not None:
            self._row.append("".join(self._cell_parts).strip())
            self._cell_parts = None
        elif tag == "tr" and self._row is not None and self._table_rows is not None:
            if self._row:
                self._table_rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table_rows is not None:
            self.tables.append((self.heading, self._table_rows))
            self._table_rows = None

    def handle_data(self, data):
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        elif self._cell_parts is not None:
            self._cell_parts.append(data)


def _rows_to_dict(rows: list[list[str]]) -> dict:
    result = {}
    for row in rows:
        if len(row) >= 2:
            result[_header_name(row[0])] = row[1]
    return result


def parse_sse_pcf_html(
    html: str,
    fund_code: str,
    content_date: str | None = None,
) -> tuple[dict, list[dict]]:
    parser = _PCFTableParser()
    parser.feed(html)

    announcement = {}
    component_rows: list[dict] = []
    dated_contents: list[tuple[str, dict]] = []
    for heading, rows in parser.tables:
        normalized_rows = {_header_name(row[0]) for row in rows if row}
        if "最新公告日期" in normalized_rows and "基金代码" in normalized_rows:
            announcement.update(_rows_to_dict(rows))
            continue

        heading_date = re.search(r"\d{4}-\d{2}-\d{2}", heading or "")
        if heading_date:
            dated_contents.append((heading_date.group(0), _rows_to_dict(rows)))
            continue

        if rows:
            headers = [_header_name(value) for value in rows[0]]
            if "证券代码" in headers and "证券简称" in headers:
                for row in rows[1:]:
                    if len(row) < len(headers):
                        row = row + [""] * (len(headers) - len(row))
                    component_rows.append(dict(zip(headers, row)))

    if dated_contents:
        dated_contents.sort(key=lambda item: item[0])
        for date, values in dated_contents:
            announcement.update(values)
        parsed_content_date = dated_contents[-1][0]
    else:
        parsed_content_date = content_date

    announcement["基金代码"] = _clean_text(announcement.get("基金代码")) or str(fund_code).strip()
    announcement["内容日期"] = parsed_content_date or content_date
    info = normalize_pcf_info(announcement)

    items = []
    for raw_item in component_rows:
        raw_item["交易所"] = "SSE"
        raw_item["基金代码"] = info["基金代码"]
        raw_item["内容日期"] = info["内容日期"]
        item = normalize_pcf_item(raw_item)
        if item["证券代码"]:
            items.append(item)
    return info, items


def _format_sse_date(value) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def _xml_value(root: ET.Element, name: str):
    node = root.find(f".//{name}")
    return None if node is None else node.text


def _xml_percent(value):
    number = _number(value)
    if number is None:
        return None
    if abs(number) <= 1:
        number *= 100
    return f"{number:g}%"


def _creation_mode(value):
    return {
        "0": "现金申赎",
        "1": "沪市成分证券实物对价",
        "2": "沪市、深市成分证券实物对价",
        "3": "银行间市场债券实物对价",
    }.get(str(value or "").strip(), _clean_text(value))


def _market_name(value):
    return {
        "101": "上交所",
        "102": "深交所",
        "103": "港交所",
        "105": "外汇交易中心",
        "106": "北交所",
        "9999": "其他",
    }.get(str(value or "").strip(), _clean_text(value))


def parse_sse_pcf_xml(
    xml: str | bytes,
    fund_code: str,
    api_info: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Parse the official SSEPortfolioCompositionFile XML download."""
    root = ET.fromstring(xml)
    api_info = api_info or {}
    code = _clean_text(_xml_value(root, "FundInstrumentID")) or str(fund_code).strip()
    content_date = _format_sse_date(
        _xml_value(root, "TradingDay") or api_info.get("TRADING_DAY")
    )
    raw_info = {
        "交易所": "SSE",
        "基金代码": code,
        "基金名称": api_info.get("FUND_NAME"),
        "基金管理公司名称": api_info.get("FUND_COMP_NAME"),
        "最新公告日期": content_date,
        "内容日期": content_date,
        "现金差额": _xml_value(root, "PreCashComponent"),
        "最小申购、赎回单位净值": _xml_value(root, "NAVperCU"),
        "基金份额净值": _xml_value(root, "NAV"),
        "最小申购、赎回单位的预估现金部分": _xml_value(root, "EstimatedCashComponent"),
        "现金替代比例上限": _xml_percent(_xml_value(root, "MaxCashRatio")),
        "当日累计可申购的基金份额上限": _xml_value(root, "CreationLimit"),
        "当日累计可赎回的基金份额上限": _xml_value(root, "RedemptionLimit"),
        "当日净申购的基金份额上限": _xml_value(root, "NetCreationLimit"),
        "当日净赎回的基金份额上限": _xml_value(root, "NetRedemptionLimit"),
        "单个证券账户当日净申购的基金份额上限": _xml_value(root, "NetCreationLimitPerAcct"),
        "单个证券账户当日净赎回的基金份额上限": _xml_value(root, "NetRedemptionLimitPerAcct"),
        "单个证券账户当日累计可申购的基金份额上限": _xml_value(root, "CreationLimitPerAcct"),
        "单个证券账户当日累计可赎回的基金份额上限": _xml_value(root, "RedemptionLimitPerAcct"),
        "是否需要公布IOPV": "是" if str(_xml_value(root, "PublishIOPVFlag")) == "1" else "否",
        "最小申购、赎回单位": _xml_value(root, "CreationRedemptionUnit"),
        "申购赎回的允许情况": api_info.get("CREATION_REDEMPTION"),
        "申购赎回模式": _creation_mode(
            _xml_value(root, "CreationRedemptionMechanism")
            or api_info.get("CREATION_REDEMPTION_MECHANISM")
        ),
    }
    info = normalize_pcf_info(raw_info)

    items = []
    for component in root.findall(".//Component"):
        raw_item = {
            "交易所": "SSE",
            "基金代码": code,
            "内容日期": content_date,
            "证券代码": _xml_value(component, "InstrumentID"),
            "证券简称": _xml_value(component, "InstrumentName"),
            "股票数量": _xml_value(component, "Quantity"),
            "现金替代标志": _xml_value(component, "SubstitutionFlag"),
            "申购现金替代溢价比例": _xml_percent(_xml_value(component, "CreationPremiumRate")),
            "赎回现金替代折价比例": _xml_percent(_xml_value(component, "RedemptionDiscountRate")),
            "替代金额": _xml_value(component, "SubstitutionCashAmount"),
            "挂牌市场": _market_name(_xml_value(component, "UnderlyingSecurityID")),
        }
        item = normalize_pcf_item(raw_item)
        if item["证券代码"]:
            items.append(item)
    return info, items


def build_sse_pcf_download_url(fund_code: str, etf_type: str | None = None) -> str:
    params = {"fundCode": str(fund_code).strip()}
    if etf_type:
        params["etfType"] = str(etf_type).strip()
    return "https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?" + urlencode(params)


def _decode_sse_download(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    if b"<SSEPortfolioCompositionFile" in raw[:500]:
        return raw.decode("utf-8-sig")
    return raw.decode("gb18030", errors="replace")


def _legacy_market(code: str) -> str | None:
    code = str(code or "").strip()
    if code.startswith(("6", "68")):
        return "上交所"
    if code.startswith(("0", "3")):
        return "深交所"
    return None


def parse_sse_pcf_legacy(
    text: str,
    fund_code: str,
    api_info: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Parse the pre-XML ETFMQ key/value and pipe-delimited format."""
    api_info = api_info or {}
    header: dict[str, str] = {}
    component_lines: list[str] = []
    in_components = False
    for line in text.replace("\r", "").split("\n"):
        stripped = line.strip()
        if stripped == "TAGTAG":
            in_components = True
            continue
        if not in_components:
            if "=" in line:
                key, value = line.split("=", 1)
                header[key.strip()] = value.strip()
        elif stripped and stripped != "ENDENDEND":
            component_lines.append(line)

    content_date = _format_sse_date(header.get("TradingDay") or api_info.get("TRADING_DAY"))
    raw_info = {
        "交易所": "SSE",
        "基金代码": str(fund_code).strip(),
        "基金名称": api_info.get("FUND_NAME"),
        "基金管理公司名称": api_info.get("FUND_COMP_NAME"),
        "最新公告日期": content_date,
        "内容日期": content_date,
        "现金差额": header.get("CashComponent"),
        "最小申购、赎回单位净值": header.get("NAVperCU"),
        "基金份额净值": header.get("NAV"),
        "最小申购、赎回单位的预估现金部分": header.get("EstimateCashComponent"),
        "现金替代比例上限": _xml_percent(header.get("MaxCashRatio")),
        "是否需要公布IOPV": "是" if header.get("Publish") == "1" else "否",
        "最小申购、赎回单位": header.get("CreationRedemptionUnit"),
        "申购赎回的允许情况": api_info.get("CREATION_REDEMPTION"),
        "申购赎回模式": _creation_mode(
            api_info.get("CREATION_REDEMPTION_MECHANISM") or header.get("CreationRedemption")
        ),
    }
    info = normalize_pcf_info(raw_info)

    items = []
    for line in component_lines:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 4 or not fields[0]:
            continue
        field_count = len(fields)
        fields += [""] * (7 - len(fields))
        cash_amount = fields[6]
        redemption_rate = fields[5]
        if fields[3] == "2" and field_count <= 7 and fields[5] and not fields[6]:
            cash_amount = fields[5]
            redemption_rate = ""
        raw_item = {
            "交易所": "SSE",
            "基金代码": str(fund_code).strip(),
            "内容日期": content_date,
            "证券代码": fields[0],
            "证券简称": fields[1],
            "股票数量": fields[2],
            "现金替代标志": fields[3],
            "申购现金替代溢价比例": _xml_percent(fields[4]),
            "赎回现金替代折价比例": _xml_percent(redemption_rate),
            "替代金额": cash_amount,
            "挂牌市场": _legacy_market(fields[0]),
        }
        item = normalize_pcf_item(raw_item)
        if item["证券代码"]:
            items.append(item)
    return info, items


def parse_sse_pcf_download(
    raw: bytes | str,
    fund_code: str,
    api_info: dict | None = None,
) -> tuple[dict, list[dict]]:
    text = _decode_sse_download(raw)
    if text.lstrip().startswith("<"):
        return parse_sse_pcf_xml(text, fund_code, api_info=api_info)
    if "TAGTAG" in text and "TradingDay=" in text:
        return parse_sse_pcf_legacy(text, fund_code, api_info=api_info)
    raise ValueError("SSE PCF download is neither XML nor legacy ETFMQ text")


def _parse_jsonp(text: str) -> dict:
    payload = text.strip()
    start = payload.find("{")
    end = payload.rfind("}")
    if start < 0 or end < start:
        raise ValueError("SSE response is not JSON/JSONP")
    return json.loads(payload[start : end + 1])


def _read_sse(request: Request, opener, timeout: float) -> bytes:
    with opener(request, timeout=timeout) as response:
        return response.read()


def fetch_sse_pcf_for_fund(
    fund_code: str,
    *,
    opener=None,
    timeout: float = 30,
    retry_attempts: int = 3,
    retry_delay: float = 0.8,
) -> tuple[dict, list[dict]]:
    """Fetch one current SSE PCF; the public endpoint has no historical date argument."""
    opener = opener or urlopen
    code = str(fund_code).strip()
    headers = {
        "Referer": "https://etf.sse.com.cn/",
        "User-Agent": "Mozilla/5.0 ETFDataCollector/1.0",
    }
    info_url = "https://query.sse.com.cn/commonQuery.do?" + urlencode(
        {
            "isPagination": "false",
            "sqlId": "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C",
            "FUNDID2": code,
            "jsonCallBack": "codexCallback",
        }
    )
    last_error = None
    for attempt in range(max(1, int(retry_attempts))):
        try:
            info_request = Request(info_url, headers=headers)
            info_payload = _parse_jsonp(
                _read_sse(info_request, opener, timeout).decode("utf-8-sig")
            )
            results = info_payload.get("result") or []
            if not results:
                raise ValueError(f"SSE returned no PCF metadata for {code}")
            api_info = dict(results[0])

            download_url = build_sse_pcf_download_url(code, api_info.get("ETF_TYPE"))
            download_request = Request(download_url, headers=headers)
            raw_download = _read_sse(download_request, opener, timeout)
            return parse_sse_pcf_download(raw_download, code, api_info=api_info)
        except (OSError, ValueError, ET.ParseError) as exc:
            last_error = exc
            if attempt + 1 >= max(1, int(retry_attempts)):
                raise
            if retry_delay > 0:
                time.sleep(float(retry_delay) * (attempt + 1))
    raise last_error or RuntimeError(f"SSE PCF fetch failed for {code}")
