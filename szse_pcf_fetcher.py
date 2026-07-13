from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from sse_pcf_fetcher import normalize_pcf_info, normalize_pcf_item


@dataclass(frozen=True)
class SZSEPCFReference:
    fund_code: str
    content_date: str
    download_url: str


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
    return {
        "101": "SSE",
        "102": "SZSE",
    }.get(str(value or "").strip(), _clean_text(value))


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
        if stripped == "TAGTAG":
            in_components = True
            continue
        if not in_components:
            if "=" in line:
                key, value = line.split("=", 1)
                header[key.strip()] = value.strip()
        elif stripped and stripped not in {"ENDENDEND", "END"}:
            component_lines.append(line)

    code = _clean_text(header.get("SecurityID") or header.get("Fundid1") or header.get("FundID"))
    content_date = _format_date(header.get("TradingDay"))
    raw_info = {
        "交易所": "SZSE",
        "基金代码": code,
        "基金名称": header.get("Symbol"),
        "基金管理公司名称": header.get("FundManagementCompany"),
        "最新公告日期": content_date,
        "内容日期": content_date,
        "现金差额": header.get("CashComponent"),
        "最小申购、赎回单位净值": header.get("NAVperCU"),
        "基金份额净值": header.get("NAV"),
        "最小申购、赎回单位的预估现金部分": header.get("EstimateCashComponent"),
        "现金替代比例上限": header.get("MaxCashRatio"),
        "当日累计可申购的基金份额上限": header.get("CreationLimit"),
        "当日累计可赎回的基金份额上限": header.get("RedemptionLimit"),
        "当日净申购的基金份额上限": header.get("NetCreationLimit"),
        "当日净赎回的基金份额上限": header.get("NetRedemptionLimit"),
        "单个证券账户当日累计可申购的基金份额上限": header.get("CreationLimitPerUser"),
        "单个证券账户当日累计可赎回的基金份额上限": header.get("RedemptionLimitPerUser"),
        "单个证券账户当日净申购的基金份额上限": header.get("NetCreationLimitPerUser"),
        "单个证券账户当日净赎回的基金份额上限": header.get("NetRedemptionLimitPerUser"),
        "是否需要公布IOPV": _flag_text(header.get("Publish")),
        "最小申购、赎回单位": header.get("CreationRedemptionUnit"),
        "申购赎回的允许情况": (
            f"申购:{_allowance_value(header.get('Creation'))}；"
            f"赎回:{_allowance_value(header.get('Redemption'))}"
            if header.get("Creation") is not None or header.get("Redemption") is not None
            else None
        ),
        "申购赎回模式": header.get("Type"),
    }
    info = normalize_pcf_info(raw_info, exchange="SZSE")

    items = []
    mismatch_count = 0
    for line in component_lines:
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 4 or not fields[0]:
            continue
        fields += [""] * (9 - len(fields))
        amount, mismatch = choose_szse_substitute_amount(fields[6], fields[7])
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
            "赎回现金替代折价比例": fields[5],
            "替代金额": amount,
            "挂牌市场": _market_name(fields[8]),
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
    if "TradingDay=" in text and "TAGTAG" in text:
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
