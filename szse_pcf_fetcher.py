from __future__ import annotations

import html
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from sse_pcf_fetcher import normalize_pcf_info, normalize_pcf_item


@dataclass(frozen=True)
class SZSEPCFReference:
    fund_code: str
    content_date: str
    download_url: str


class SZSEPCFPageError(RuntimeError):
    def __init__(self, trade_date: str, message: str):
        super().__init__(message)
        self.trade_date = trade_date


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return None if text in {"", "-", "--", "—", "－", "None", "null", "N/A"} else text


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


def _format_date(value) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_value(node: ET.Element, name: str):
    for child in node.iter():
        if _local_name(child.tag) == name:
            return child.text
    return None


def _flag_text(value) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    return "是" if text.upper() in {"1", "Y", "YES", "TRUE"} else "否"


def _allowance_value(value) -> str:
    text = _clean_text(value)
    return "Y" if text and text.upper() in {"1", "Y", "YES", "TRUE"} else "N"


def _market_name(value):
    key = str(value or "").strip().casefold()
    return {
        "101": "SSE",
        "102": "SZSE",
        "xshg": "SSE",
        "xshe": "SZSE",
    }.get(key, _clean_text(value))


def choose_szse_substitute_amount(creation, redemption) -> tuple[float | None, bool]:
    creation_value = _number(creation)
    redemption_value = _number(redemption)
    if creation_value == redemption_value:
        return creation_value, False
    if creation_value in (None, 0.0):
        return redemption_value, False
    if redemption_value in (None, 0.0):
        return creation_value, False
    return creation_value, True


def _parse_szse_xml(xml: str | bytes) -> tuple[dict, list[dict], int]:
    root = ET.fromstring(xml)
    code = _clean_text(_xml_value(root, "SecurityID"))
    content_date = _format_date(_xml_value(root, "TradingDay"))
    raw_info = {
        "交易所": "SZSE",
        "基金代码": code,
        "基金名称": _xml_value(root, "Symbol"),
        "基金管理公司名称": _xml_value(root, "FundManagementCompany"),
        "最新公告日期": content_date,
        "内容日期": content_date,
        "现金差额": _xml_value(root, "CashComponent"),
        "最小申购、赎回单位净值": _xml_value(root, "NAVperCU"),
        "基金份额净值": _xml_value(root, "NAV"),
        "最小申购、赎回单位的预估现金部分": _xml_value(root, "EstimateCashComponent"),
        "现金替代比例上限": _xml_value(root, "MaxCashRatio"),
        "当日累计可申购的基金份额上限": _xml_value(root, "CreationLimit"),
        "当日累计可赎回的基金份额上限": _xml_value(root, "RedemptionLimit"),
        "当日净申购的基金份额上限": _xml_value(root, "NetCreationLimit"),
        "当日净赎回的基金份额上限": _xml_value(root, "NetRedemptionLimit"),
        "单个证券账户当日累计可申购的基金份额上限": _xml_value(root, "CreationLimitPerUser"),
        "单个证券账户当日累计可赎回的基金份额上限": _xml_value(root, "RedemptionLimitPerUser"),
        "单个证券账户当日净申购的基金份额上限": _xml_value(root, "NetCreationLimitPerUser"),
        "单个证券账户当日净赎回的基金份额上限": _xml_value(root, "NetRedemptionLimitPerUser"),
        "是否需要公布IOPV": _flag_text(_xml_value(root, "Publish")),
        "最小申购、赎回单位": _xml_value(root, "CreationRedemptionUnit"),
        "申购赎回的允许情况": (
            f"申购:{_allowance_value(_xml_value(root, 'Creation'))}；"
            f"赎回:{_allowance_value(_xml_value(root, 'Redemption'))}"
            if _xml_value(root, "Creation") is not None
            or _xml_value(root, "Redemption") is not None
            else None
        ),
        "申购赎回模式": _xml_value(root, "Type"),
    }
    info = normalize_pcf_info(raw_info, exchange="SZSE")

    items = []
    mismatch_count = 0
    for component in root.iter():
        if _local_name(component.tag) != "Component":
            continue
        amount, mismatch = choose_szse_substitute_amount(
            _xml_value(component, "CreationCashSubstitute"),
            _xml_value(component, "RedemptionCashSubstitute"),
        )
        mismatch_count += int(mismatch)
        raw_item = {
            "交易所": "SZSE",
            "基金代码": code,
            "内容日期": content_date,
            "证券代码": _xml_value(component, "UnderlyingSecurityID"),
            "证券简称": _xml_value(component, "UnderlyingSymbol"),
            "股票数量": _xml_value(component, "ComponentShare"),
            "现金替代标志": _xml_value(component, "SubstituteFlag"),
            "申购现金替代溢价比例": _xml_value(component, "PremiumRatio"),
            "赎回现金替代折价比例": _xml_value(component, "DiscountRatio"),
            "替代金额": amount,
            "挂牌市场": _market_name(_xml_value(component, "UnderlyingSecurityIDSource")),
        }
        item = normalize_pcf_item(raw_item, exchange="SZSE")
        if item["证券代码"]:
            items.append(item)
    return info, items, mismatch_count


def _parse_szse_legacy(text: str) -> tuple[dict, list[dict], int]:
    header: dict[str, str] = {}
    component_lines: list[str] = []
    in_components = False
    for line in text.replace("\r", "").split("\n"):
        stripped = line.strip()
        if stripped.casefold() == "tagtag":
            in_components = True
            continue
        if not in_components:
            if "=" in line:
                key, value = line.split("=", 1)
                header[key.strip().casefold()] = value.strip()
        elif stripped and stripped.casefold() not in {"endendend", "end"}:
            component_lines.append(line)

    code = _clean_text(
        header.get("securityid") or header.get("fundid1") or header.get("fundid")
    )
    content_date = _format_date(header.get("tradingday"))
    raw_info = {
        "交易所": "SZSE",
        "基金代码": code,
        "基金名称": header.get("fundname") or header.get("symbol"),
        "基金管理公司名称": header.get("fundmanagementcompany"),
        "最新公告日期": content_date,
        "内容日期": content_date,
        "现金差额": header.get("cashcomponent"),
        "最小申购、赎回单位净值": header.get("navpercu"),
        "基金份额净值": header.get("nav"),
        "最小申购、赎回单位的预估现金部分": header.get("estimatecashcomponent"),
        "现金替代比例上限": header.get("maxcashratio"),
        "当日累计可申购的基金份额上限": header.get("creationlimit"),
        "当日累计可赎回的基金份额上限": header.get("redemptionlimit"),
        "当日净申购的基金份额上限": header.get("netcreationlimit"),
        "当日净赎回的基金份额上限": header.get("netredemptionlimit"),
        "单个证券账户当日累计可申购的基金份额上限": header.get("creationlimitperuser"),
        "单个证券账户当日累计可赎回的基金份额上限": header.get("redemptionlimitperuser"),
        "单个证券账户当日净申购的基金份额上限": header.get("netcreationlimitperuser"),
        "单个证券账户当日净赎回的基金份额上限": header.get("netredemptionlimitperuser"),
        "是否需要公布IOPV": _flag_text(header.get("publish")),
        "最小申购、赎回单位": header.get("creationredemptionunit"),
        "申购赎回的允许情况": (
            f"申购:{_allowance_value(header.get('creation'))}；"
            f"赎回:{_allowance_value(header.get('redemption'))}"
            if header.get("creation") is not None or header.get("redemption") is not None
            else None
        ),
        "申购赎回模式": header.get("type"),
    }
    info = normalize_pcf_info(raw_info, exchange="SZSE")

    items = []
    mismatch_count = 0
    for line in component_lines:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 8 or not fields[0]:
            continue
        amount, mismatch = choose_szse_substitute_amount(fields[5], fields[6])
        mismatch_count += int(mismatch)
        raw_item = {
            "交易所": "SZSE",
            "基金代码": code,
            "内容日期": content_date,
            "证券代码": fields[0],
            "证券简称": fields[1],
            "股票数量": fields[2],
            "现金替代标志": fields[3],
            "申购现金替代溢价比例": fields[4],
            "赎回现金替代折价比例": None,
            "替代金额": amount,
            "挂牌市场": _market_name(fields[7]),
        }
        item = normalize_pcf_item(raw_item, exchange="SZSE")
        if item["证券代码"]:
            items.append(item)
    return info, items, mismatch_count


def _decode_szse_download(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gb18030")


def parse_szse_pcf_download(raw: bytes | str) -> tuple[dict, list[dict], int]:
    text = _decode_szse_download(raw)
    if text.lstrip().startswith("<"):
        return _parse_szse_xml(text)
    folded = text.casefold()
    if "tradingday=" in folded and "tagtag" in folded:
        return _parse_szse_legacy(text)
    raise ValueError("深交所 PCF 文件格式无法识别")


def _reference_from_href(href: str) -> SZSEPCFReference | None:
    href = html.unescape(href)
    absolute_url = urljoin("https://www.szse.cn", href)
    query = parse_qs(urlsplit(absolute_url).query)
    filename = unquote((query.get("filename") or [""])[0])
    match = re.search(r"pcf_(\d{6})[_-](\d{8})", filename, re.IGNORECASE)
    if not match:
        match = re.search(r"(\d{6})ETF(\d{8})", filename, re.IGNORECASE)
    if not match:
        return None
    return SZSEPCFReference(match.group(1), _format_date(match.group(2)), absolute_url)


def extract_szse_pcf_references(payload: list[dict]) -> list[SZSEPCFReference]:
    references: list[SZSEPCFReference] = []

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "jjdm" and isinstance(child, str):
                    for href in re.findall(
                        r"href\s*=\s*['\"]([^'\"]*eft_download_new\.html[^'\"]*)",
                        child,
                        flags=re.IGNORECASE,
                    ):
                        reference = _reference_from_href(href)
                        if reference is not None:
                            references.append(reference)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return references


def collect_szse_pcf_via_browser(
    trade_dates: list[str],
    *,
    fund_code: str = "",
    replace_existing: bool = False,
    is_complete,
    save_snapshot,
    on_progress=None,
    visible: bool = False,
    session_factory=None,
    sleep_func=time.sleep,
    delay_func=lambda: random.uniform(0.8, 1.8),
) -> dict:
    """Collect official SZSE PCF files through one reusable browser session."""
    summary = {
        "dates": len(trade_dates),
        "discovered": 0,
        "skipped": 0,
        "succeeded": 0,
        "failed": 0,
        "items": 0,
        "mismatched_amounts": 0,
        "failures": [],
    }
    session_factory = session_factory or SZSEPCFBrowserSession

    with session_factory(visible=visible) as session:
        for trade_date in trade_dates:
            try:
                references = session.query(trade_date, fund_code)
            except SZSEPCFPageError:
                raise
            except Exception as exc:
                raise SZSEPCFPageError(trade_date, str(exc)) from exc

            summary["discovered"] += len(references)
            if on_progress:
                on_progress(f"深交所 PCF {trade_date} 发现 {len(references)} 个文件。")

            for reference in references:
                if not replace_existing and is_complete(reference.fund_code, reference.content_date):
                    summary["skipped"] += 1
                    continue

                last_error = None
                for attempt, backoff in enumerate((2, 5, 10, None)):
                    try:
                        info, items, mismatches = parse_szse_pcf_download(
                            session.read_file(reference)
                        )
                        save_snapshot(info, items)
                        summary["succeeded"] += 1
                        summary["items"] += len(items)
                        summary["mismatched_amounts"] += mismatches
                        sleep_func(delay_func())
                        break
                    except SZSEPCFPageError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        if backoff is not None:
                            sleep_func(backoff)
                else:
                    summary["failed"] += 1
                    failure = {
                        "date": trade_date,
                        "fund_code": reference.fund_code,
                        "error": str(last_error),
                    }
                    summary["failures"].append(failure)
                    if on_progress:
                        on_progress(
                            f"深交所 PCF {trade_date} {reference.fund_code} 下载失败: {last_error}"
                        )

            if on_progress:
                on_progress(f"深交所 PCF {trade_date} 采集完成。")

    return summary


SZSE_PCF_PAGE_URL = "https://www.szse.cn/disclosure/fund/currency/index.html"
SZSE_PCF_REPORT_URL = "https://www.szse.cn/api/report/ShowReport/data?CATALOGID=sgshqd"


def _is_browser_session_fatal(exc: Exception) -> bool:
    if type(exc).__name__ == "TargetClosedError":
        return True
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "browser closed",
            "context has been closed",
            "context closed",
        )
    )


class SZSEPCFBrowserSession:
    """Browser-backed session for the SZSE PCF report and file redirects."""

    def __init__(self, visible: bool = False):
        self.visible = visible
        self._playwright_context = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("缺少 Playwright，无法使用深交所 PCF 浏览器采集。") from exc

        try:
            self._playwright_context = sync_playwright()
            self.playwright = self._playwright_context.__enter__()
            self.browser = self.playwright.chromium.launch(
                headless=not self.visible, slow_mo=120
            )
            self.context = self.browser.new_context()
            self.page = self.context.new_page()
            self.page.goto(SZSE_PCF_PAGE_URL, wait_until="networkidle", timeout=60000)
            return self
        except Exception as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def __exit__(self, exc_type, exc, traceback):
        cleanup_error = None
        try:
            if self.context is not None:
                try:
                    self.context.close()
                except Exception as close_exc:
                    cleanup_error = close_exc
            if self.browser is not None:
                try:
                    self.browser.close()
                except Exception as close_exc:
                    cleanup_error = cleanup_error or close_exc
            if self._playwright_context is not None:
                try:
                    self._playwright_context.__exit__(exc_type, exc, traceback)
                except Exception as close_exc:
                    cleanup_error = cleanup_error or close_exc
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None
            self._playwright_context = None
        if exc_type is None and cleanup_error is not None:
            raise cleanup_error
        return False

    @staticmethod
    def _report_payloads(first_payload, first_url: str, page_count: int, page) -> list[dict]:
        payloads = [first_payload]
        parts = urlsplit(first_url)
        query = parse_qs(parts.query, keep_blank_values=True)
        for page_number in range(2, page_count + 1):
            query["PAGENO"] = [str(page_number)]
            query["tab1PAGENO"] = [str(page_number)]
            next_url = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode(query, doseq=True),
                    parts.fragment,
                )
            )
            payloads.append(
                page.evaluate(
                    """async (url) => {
                        const response = await fetch(url, { credentials: 'include' });
                        if (!response.ok) throw new Error(`report request failed: ${response.status}`);
                        return response.json();
                    }""",
                    next_url,
                )
            )
        return payloads

    @staticmethod
    def _as_report_list(payload) -> list[dict]:
        return payload if isinstance(payload, list) else [payload]

    def query(self, trade_date: str, fund_code: str = "") -> list[SZSEPCFReference]:
        if self.page is None:
            raise RuntimeError("深交所 PCF 浏览器会话尚未打开")

        self.page.locator("input.query-txtJCorDH").fill(fund_code)
        self.page.locator("input.query-txtStart").fill(trade_date)
        self.page.locator("input.query-txtEnd").fill(trade_date)
        with self.page.expect_response(
            lambda response: "CATALOGID=sgshqd" in response.url,
            timeout=30000,
        ) as report_info:
            self.page.locator("button.confirm-query").click()
        report_response = report_info.value
        first_payload = report_response.json()
        report = next(
            (
                item
                for item in self._as_report_list(first_payload)
                if isinstance(item, dict) and "metadata" in item
            ),
            {},
        )
        page_count = max(1, int((report.get("metadata") or {}).get("pagecount") or 1))
        payloads = self._report_payloads(
            first_payload, report_response.url, page_count, self.page
        )
        references = []
        for payload in payloads:
            references.extend(extract_szse_pcf_references(self._as_report_list(payload)))
        return references

    def read_file(self, reference: SZSEPCFReference) -> bytes:
        if self.context is None:
            raise RuntimeError("深交所 PCF 浏览器会话尚未打开")

        download_page = None
        try:
            download_page = self.context.new_page()
            with download_page.expect_response(
                lambda response: "/files/text/ETFDown/" in urlsplit(response.url).path,
                timeout=60000,
            ) as final_response_info:
                download_page.goto(
                    reference.download_url, wait_until="networkidle", timeout=60000
                )
            final_response = final_response_info.value
            download_page.wait_for_url("**/files/text/ETFDown/**", timeout=30000)
            return final_response.body()
        except SZSEPCFPageError:
            raise
        except Exception as exc:
            if _is_browser_session_fatal(exc):
                raise SZSEPCFPageError(reference.content_date, str(exc)) from exc
            raise
        finally:
            if download_page is not None:
                try:
                    download_page.close()
                except Exception as exc:
                    if _is_browser_session_fatal(exc):
                        raise SZSEPCFPageError(reference.content_date, str(exc)) from exc
                    raise


def check_szse_pcf_connection(
    trade_date: str | None = None, session_factory=None
) -> tuple[bool, str]:
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    session_factory = session_factory or SZSEPCFBrowserSession
    try:
        with session_factory(visible=False) as session:
            references = session.query(trade_date)
            if references:
                session.read_file(references[0])
        return True, "深交所 PCF 浏览器采集连通，可以继续采集。"
    except Exception as exc:
        return False, f"深交所 PCF 浏览器仍未连通: {exc}"
