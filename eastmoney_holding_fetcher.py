from __future__ import annotations

import html
import json
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EASTMONEY_HOLDING_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
EASTMONEY_ANNOUNCEMENT_URL = "http://api.fund.eastmoney.com/f10/JJGG"
HOLDING_COLUMNS = (
    "交易所",
    "基金代码",
    "基金名称",
    "报告年度",
    "报告季度",
    "报告期",
    "可用日期",
    "数据完整性",
    "序号",
    "股票代码",
    "股票名称",
    "占净值比例",
    "持股数",
    "持仓市值",
    "挂牌市场",
)

_NULL_VALUES = {"", "-", "--", "—", "－", "None", "null", "N/A", "暂无"}
_REQUEST_SLOT_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


class EastmoneyHoldingError(RuntimeError):
    """东方财富季度持仓请求或解析失败。"""


class EastmoneyHoldingParseError(EastmoneyHoldingError):
    """东方财富返回内容不是可识别的季度持仓数据。"""


class EastmoneyHoldingNoDataError(EastmoneyHoldingError):
    """基金在该年度没有可披露的股票季度持仓。"""


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", html.unescape(str(value))).strip()
    return None if text in _NULL_VALUES else text


def _number(value, *, multiplier: float = 1.0) -> float | None:
    text = _clean_text(value)
    if text is None:
        return None
    text = text.replace(",", "").replace("%", "").replace("￥", "").replace("¥", "")
    try:
        return float(text) * multiplier
    except (TypeError, ValueError):
        return None


def _header_name(value: str) -> str:
    name = _clean_text(value) or ""
    name = re.sub(r"[（(].*?[）)]", "", name)
    return re.sub(r"\s+", "", name)


class _HoldingTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._heading_parts: list[str] | None = None
        self._table_rows: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self.tables: list[tuple[str, list[list[str]]]] = []
        self.heading = ""

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


def _extract_object_string(text: str, key: str) -> str | None:
    match = re.search(rf"(?:^|[{{,])\s*{re.escape(key)}\s*:\s*\"", text)
    if not match:
        match = re.search(rf"\"{re.escape(key)}\"\s*:\s*\"", text)
    if not match:
        return None
    start = match.end() - 1
    escaped = False
    end = None
    for index in range(start + 1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            end = index
            break
    if end is None:
        raise EastmoneyHoldingParseError(f"返回内容中的 {key} 字段未闭合")
    raw = text[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EastmoneyHoldingParseError(f"返回内容中的 {key} 字段无法解码") from exc


def _extract_content(text: str) -> str:
    content = _extract_object_string(text, "content")
    if content is None:
        raise EastmoneyHoldingParseError("返回内容没有 content 字段")
    return content


def _report_period(year: int, quarter: int) -> str:
    return {
        1: f"{year:04d}-03-31",
        2: f"{year:04d}-06-30",
        3: f"{year:04d}-09-30",
        4: f"{year:04d}-12-31",
    }[quarter]


def _announcement_quarter(title: str) -> tuple[int, int] | None:
    text = _clean_text(title) or ""
    year_match = re.search(r"(\d{4})年", text)
    if not year_match:
        return None
    year = int(year_match.group(1))
    if "半年度" in text or "中期报告" in text:
        return year, 2
    if "年报" in text or "年度报告" in text:
        return year, 4
    match = re.search(r"第?([1-4一二三四])季度", text)
    if not match:
        return None
    quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(match.group(1))
    return year, quarter or int(match.group(1))


def _announcement_date(value) -> str | None:
    text = _clean_text(value) or ""
    match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if not match:
        return None
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def parse_eastmoney_report_dates(payload: str | bytes) -> dict[str, str]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(payload)
        items = data.get("Data") or data.get("data") or []
    except (TypeError, ValueError) as exc:
        raise EastmoneyHoldingParseError("基金公告响应不是有效 JSON") from exc
    result: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict):
            title = item.get("Title") or item.get("title") or item.get("公告标题")
            date = item.get("NOTICE_DATE") or item.get("notice_date") or item.get("公告日期")
        elif isinstance(item, (list, tuple)) and len(item) >= 6:
            title, date = item[1], item[5]
        else:
            continue
        quarter_info = _announcement_quarter(str(title or ""))
        announcement_date = _announcement_date(date)
        if quarter_info is None or announcement_date is None:
            continue
        period = _report_period(*quarter_info)
        previous = result.get(period)
        if previous is None or announcement_date < previous:
            result[period] = announcement_date
    return result


def _quarter_from_heading(heading: str, fallback_year: int) -> tuple[int, int] | None:
    text = _clean_text(heading) or ""
    match = re.search(r"(\d{4})\s*年\s*([1-4一二三四])\s*季", text)
    if not match:
        return None
    quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(match.group(2))
    return int(match.group(1)), quarter or int(match.group(2))


def _normalize_row(
    values: dict[str, str],
    *,
    exchange: str,
    fund_code: str,
    fund_name: str | None,
    report_year: int,
    report_quarter: int,
    available_date: str | None,
) -> dict:
    return {
        "交易所": str(exchange or "SSE").strip().upper(),
        "基金代码": str(fund_code).strip(),
        "基金名称": _clean_text(fund_name),
        "报告年度": report_year,
        "报告季度": report_quarter,
        "报告期": _report_period(report_year, report_quarter),
        "可用日期": _clean_text(available_date),
        "数据完整性": "部分披露" if report_quarter in (1, 3) else "完整披露",
        "序号": int(_number(values.get("序号")) or 0) or None,
        "股票代码": _clean_text(values.get("股票代码")),
        "股票名称": _clean_text(values.get("股票名称")),
        "占净值比例": _number(values.get("占净值比例")),
        # 东方财富表格的持股数、持仓市值单位分别是万股、万元，入库统一成股、元。
        "持股数": _number(values.get("持股数"), multiplier=10000),
        "持仓市值": _number(values.get("持仓市值"), multiplier=10000),
        "挂牌市场": _clean_text(values.get("挂牌市场")),
    }


def parse_eastmoney_holding_response(
    text: str,
    fund_code: str,
    exchange: str = "SSE",
    fund_name: str | None = None,
    available_date: str | None = None,
    year: int | None = None,
) -> list[dict]:
    """解析 FundArchivesDatas.aspx type=jjcc 的年度响应。"""
    if not isinstance(text, str) or not text.strip():
        raise EastmoneyHoldingParseError("返回内容为空")
    content = _extract_content(text)
    parser = _HoldingTableParser()
    parser.feed(content)
    result: list[dict] = []
    fallback_year = int(year or datetime_year())
    for heading, rows in parser.tables:
        quarter_info = _quarter_from_heading(heading, fallback_year)
        if quarter_info is None or not rows:
            continue
        report_year, report_quarter = quarter_info
        headers = [_header_name(value) for value in rows[0]]
        if "股票代码" not in headers or "股票名称" not in headers:
            continue
        aliases = {
            "持股数": "持股数",
            "持股数万股": "持股数",
            "持仓市值": "持仓市值",
            "持仓市值万元": "持仓市值",
        }
        for raw_row in rows[1:]:
            padded = raw_row + [""] * max(0, len(headers) - len(raw_row))
            values = {}
            for header, value in zip(headers, padded):
                normalized_header = aliases.get(header, header)
                if normalized_header != "相关资讯":
                    values[normalized_header] = value
            item = _normalize_row(
                values,
                exchange=exchange,
                fund_code=fund_code,
                fund_name=fund_name,
                report_year=report_year,
                report_quarter=report_quarter,
                available_date=available_date,
            )
            if item["股票代码"]:
                result.append(item)
    if not result:
        no_data_markers = ("暂无数据", "暂无持仓", "没有数据", "无相关数据", "未披露")
        if not parser.tables or any(marker in content for marker in no_data_markers):
            raise EastmoneyHoldingNoDataError("该基金该年度没有可识别的季度股票持仓")
        raise EastmoneyHoldingParseError("返回内容中没有可识别的季度持仓表")
    return result


def datetime_year() -> int:
    return int(time.strftime("%Y"))


def _wait_request_slot(interval: float) -> None:
    global _NEXT_REQUEST_AT
    if interval <= 0:
        return
    with _REQUEST_SLOT_LOCK:
        now = time.monotonic()
        delay = max(0.0, _NEXT_REQUEST_AT - now)
        _NEXT_REQUEST_AT = max(now, _NEXT_REQUEST_AT) + interval
    if delay:
        time.sleep(delay)


def fetch_eastmoney_holdings(
    fund_code: str,
    year: int,
    *,
    exchange: str = "SSE",
    fund_name: str | None = None,
    available_date: str | None = None,
    opener=urlopen,
    timeout: float = 30,
    request_interval: float = 0.5,
    include_report_dates: bool = False,
) -> list[dict]:
    code = str(fund_code).strip()
    if not (len(code) == 6 and code.isascii() and code.isdigit()):
        raise ValueError("基金代码必须是 6 位数字")
    year_value = int(year)
    params = {
        "type": "jjcc",
        "code": code,
        "topline": "10000",
        "year": str(year_value),
        "month": "",
        "rt": str(time.time()),
    }
    request = Request(
        f"{EASTMONEY_HOLDING_URL}?{urlencode(params)}",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Referer": f"https://fundf10.eastmoney.com/jjcc_{code}.html",
            "Accept": "*/*",
        },
    )
    try:
        _wait_request_slot(request_interval)
        with opener(request, timeout=timeout) as response:
            payload = response.read()
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8-sig", errors="replace")
        rows = parse_eastmoney_holding_response(
            payload,
            code,
            exchange=exchange,
            fund_name=fund_name,
            available_date=available_date,
            year=year_value,
        )
        if include_report_dates:
            try:
                report_dates = fetch_eastmoney_report_dates(
                    code,
                    opener=opener,
                    timeout=timeout,
                    request_interval=request_interval,
                )
                for row in rows:
                    row["可用日期"] = report_dates.get(row["报告期"])
            except EastmoneyHoldingError:
                # 持仓接口成功时公告日期允许为空，避免因公告接口单独波动丢失持仓。
                pass
        return rows
    except EastmoneyHoldingError:
        raise
    except Exception as exc:
        raise EastmoneyHoldingError(f"{code} {year_value} 年度持仓请求失败: {exc}") from exc


def fetch_eastmoney_report_dates(
    fund_code: str,
    *,
    opener=urlopen,
    timeout: float = 30,
    request_interval: float = 0.5,
) -> dict[str, str]:
    code = str(fund_code).strip()
    params = {
        "fundcode": code,
        "pageIndex": "1",
        "pageSize": "1000",
        "type": "3",
        "_": str(int(time.time() * 1000)),
    }
    request = Request(
        f"{EASTMONEY_ANNOUNCEMENT_URL}?{urlencode(params)}",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Referer": f"http://fundf10.eastmoney.com/jjgg_{code}_3.html",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        _wait_request_slot(request_interval)
        with opener(request, timeout=timeout) as response:
            payload = response.read()
        return parse_eastmoney_report_dates(payload)
    except EastmoneyHoldingError:
        raise
    except Exception as exc:
        raise EastmoneyHoldingError(f"{code} 基金公告请求失败: {exc}") from exc


def check_eastmoney_holding_connection(
    fund_code: str,
    year: int,
    *,
    opener=urlopen,
    timeout: float = 15,
) -> tuple[bool, str]:
    try:
        rows = fetch_eastmoney_holdings(
            fund_code,
            year,
            opener=opener,
            timeout=timeout,
            request_interval=0,
        )
        return True, f"东方财富持仓接口正常，测试基金返回 {len(rows)} 条记录。"
    except EastmoneyHoldingNoDataError:
        return True, "东方财富持仓接口正常，但测试基金该年度没有股票季度持仓。"
    except Exception as exc:
        return False, f"东方财富持仓接口未连通: {exc}"
