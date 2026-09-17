from __future__ import annotations

import json
import random
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from calendar import monthrange
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://query.sse.com.cn/commonQuery.do"
SQL_ID = "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L"
SZSE_API_URL = "https://www.szse.cn/api/report/ShowReport/data"

HEADERS = {
    "Referer": "https://www.sse.com.cn/market/funddata/volumn/etfvolumn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

PAGE_URL = "https://www.sse.com.cn/market/funddata/volumn/etfvolumn/"
SZSE_PAGE_URL = "https://www.szse.cn/market/fund/volume/etf/index.html"
SSE_SHARE_MULTIPLIER = 10000


class ETFNetworkError(RuntimeError):
    pass


def parse_szse_payload(text: str) -> list[dict]:
    payload = json.loads(text.strip())
    reports = payload if isinstance(payload, list) else [payload]
    raw_rows = []
    for report in reports:
        if isinstance(report, dict):
            raw_rows.extend(report.get("data") or [])

    rows = []
    for item in raw_rows:
        code = str(item.get("fund_code") or "").strip()
        trade_date = str(item.get("size_date") or "").strip()
        share = item.get("current_size")
        if not code or not trade_date or share in (None, ""):
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "fund_code": code,
                "fund_name": str(item.get("security_short_name") or "").strip(),
                "total_share": float(str(share).replace(",", "")) * 10000,
                "exchange": "SZSE",
                "share_unit": "share",
                "source": "szse_report",
            }
        )
    return rows


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def split_date_ranges(start_date: str, end_date: str):
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if start > end:
        raise ValueError("start_date must not be after end_date")

    current = start
    while current <= end:
        chunk_end = min(_add_months(current, 6) - timedelta(days=1), end)
        yield current.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        current = chunk_end + timedelta(days=1)


def split_szse_date_ranges(start_date: str, end_date: str):
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if start > end:
        raise ValueError("start_date must not be after end_date")

    current = start
    while current <= end:
        month_end = current.replace(day=monthrange(current.year, current.month)[1])
        chunk_end = min(month_end, end)
        yield current.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        current = chunk_end + timedelta(days=1)


def _normalize_szse_range(start: datetime, end: datetime) -> tuple[datetime, datetime] | None:
    while start.weekday() >= 5:
        start += timedelta(days=1)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    return None if start > end else (start, end)


def parse_sse_payload(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    if not text.startswith("{"):
        start = text.index("(") + 1
        end = text.rindex(")")
        text = text[start:end]

    payload = json.loads(text)
    raw_rows = payload.get("pageHelp", {}).get("data", []) or payload.get("result", [])
    rows = []
    for item in raw_rows:
        code = str(item.get("SEC_CODE") or item.get("FUND_CODE") or "").strip()
        share = item.get("TOT_VOL") or item.get("TOTAL_VOL") or item.get("total_share")
        if not code or share in (None, ""):
            continue
        rows.append(
            {
                "trade_date": str(item.get("STAT_DATE") or item.get("trade_date") or "").strip(),
                "fund_code": code,
                "fund_name": str(
                    item.get("SEC_NAME")
                    or item.get("SECURITY_ABBR_A")
                    or item.get("FUND_NAME")
                    or item.get("fund_name")
                    or ""
                ).strip(),
                "total_share": float(str(share).replace(",", "")) * SSE_SHARE_MULTIPLIER,
                "exchange": "SSE",
                "share_unit": "share",
                "source": "sse_commonQuery",
            }
        )
    return rows


def parse_sse_table_html(html: str) -> list[dict]:
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
        values = [_strip_tags(cell) for cell in cells]
        if len(values) < 4 or values[0] == "日期":
            continue
        date, code, name, share = values[:4]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not code:
            continue
        rows.append(
            {
                "trade_date": date,
                "fund_code": code,
                "fund_name": name,
                "total_share": float(share.replace(",", "")) * SSE_SHARE_MULTIPLIER,
                "exchange": "SSE",
                "share_unit": "share",
                "source": "sse_table",
            }
        )
    return rows


def _strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return unescape(value).strip()


def _callback_name() -> str:
    digits = "".join(str(random.randint(0, 9)) for _ in range(21))
    return f"jQuery{digits}_{int(time.time() * 1000)}"


def fetch_etf_rows_for_date(trade_date: str, page_size: int = 2000, timeout: int = 20) -> list[dict]:
    params = {
        "sqlId": SQL_ID,
        "STAT_DATE": trade_date,
        "isPagination": "true",
        "pageHelp.pageSize": str(page_size),
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.endPage": "1",
        "pageHelp.cacheSize": "1",
        "callback": _callback_name(),
        "_": str(int(time.time() * 1000)),
    }
    url = f"{API_URL}?{urlencode(params)}"
    request = Request(url, headers=HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
        return parse_sse_payload(text)
    except Exception as exc:
        raise ETFNetworkError(str(exc)) from exc


def _szse_request(start_date: str, end_date: str, page: int) -> Request:
    params = {
        "SHOWTYPE": "JSON",
        "CATALOGID": "scsj_fund_jjgm",
        "jjlb": "ETF",
        "txtStart": start_date,
        "txtEnd": end_date,
        "PAGENO": str(page),
        "tab1PAGENO": str(page),
        "_": str(int(time.time() * 1000)),
    }
    return Request(
        f"{SZSE_API_URL}?{urlencode(params)}",
        headers={**HEADERS, "Referer": SZSE_PAGE_URL},
    )


def probe_szse_connection(
    trade_date: str,
    timeout: int = 8,
    opener=None,
    attempts: int = 3,
    sleep_func=time.sleep,
) -> bool:
    opener = opener or urlopen
    probe_date = datetime.strptime(trade_date, "%Y-%m-%d")
    while probe_date.weekday() >= 5:
        probe_date -= timedelta(days=1)
    probe_date_text = probe_date.strftime("%Y-%m-%d")
    last_error = None
    for attempt in range(max(1, int(attempts))):
        try:
            with opener(
                _szse_request(probe_date_text, probe_date_text, 1), timeout=timeout
            ) as response:
                text = response.read().decode("utf-8", errors="replace")
            payload = json.loads(text)
            report = payload[0] if isinstance(payload, list) and payload else payload
            if not isinstance(report, dict) or "metadata" not in report:
                raise ValueError("深交所接口返回格式异常")
            return True
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max(1, int(attempts)):
                sleep_func(min(2**attempt, 4))
    raise ETFNetworkError(str(last_error))


def fetch_szse_rows_for_range(
    start_date: str,
    end_date: str,
    timeout: int = 20,
    opener=None,
    workers: int = 16,
    retries: int = 3,
    sleep_func=time.sleep,
) -> list[dict]:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    normalized = _normalize_szse_range(start, end)
    if normalized is None:
        return []
    start, end = normalized
    start_date = start.strftime("%Y-%m-%d")
    end_date = end.strftime("%Y-%m-%d")
    if start > end:
        raise ValueError("start_date must not be after end_date")
    if (end - start).days >= (_add_months(start, 6) - start).days:
        raise ValueError("SZSE query range must be shorter than six months")

    opener = opener or urlopen
    retries = max(1, int(retries))

    def fetch_page(page: int) -> str:
        request = _szse_request(start_date, end_date, page)
        last_error = None
        for attempt in range(retries):
            try:
                with opener(request, timeout=timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries:
                    sleep_func(min(2**attempt, 4))
        raise ETFNetworkError(str(last_error)) from last_error

    first_text = fetch_page(1)
    try:
        first_payload = json.loads(first_text)
        first_report = (
            first_payload[0]
            if isinstance(first_payload, list) and first_payload
            else first_payload
        )
        page_count = max(1, int((first_report.get("metadata") or {}).get("pagecount") or 1))
    except Exception as exc:
        raise ETFNetworkError(str(exc)) from exc

    page_texts = [first_text]
    if page_count > 1:
        with ThreadPoolExecutor(max_workers=normalize_worker_count(workers)) as executor:
            page_texts.extend(executor.map(fetch_page, range(2, page_count + 1)))
    rows = []
    for text in page_texts:
        try:
            rows.extend(parse_szse_payload(text))
        except Exception as exc:
            raise ETFNetworkError(str(exc)) from exc
    return rows


def fetch_szse_rows_with_daily_fallback(
    start_date: str,
    end_date: str,
    timeout: int = 20,
    opener=None,
    workers: int = 16,
    retries: int = 3,
    sleep_func=time.sleep,
    trade_dates: list[str] | None = None,
    range_fetcher=None,
) -> list[dict]:
    range_fetcher = range_fetcher or fetch_szse_rows_for_range
    fetch_kwargs = {
        "timeout": timeout,
        "opener": opener,
        "workers": workers,
        "retries": retries,
        "sleep_func": sleep_func,
    }
    try:
        return range_fetcher(start_date, end_date, **fetch_kwargs)
    except ETFNetworkError as chunk_error:
        if start_date == end_date:
            raise

        fallback_dates = (
            [date for date in trade_dates if start_date <= date <= end_date]
            if trade_dates is not None
            else list(iter_weekdays(start_date, end_date))
        )
        rows = []
        for trade_date in fallback_dates:
            try:
                rows.extend(range_fetcher(trade_date, trade_date, **fetch_kwargs))
            except ETFNetworkError as day_error:
                raise ETFNetworkError(
                    f"{start_date} ~ {end_date} failed; single date {trade_date} also failed: {day_error}"
                ) from day_error
        return rows


def fetch_latest_rows_from_page(timeout: int = 20) -> list[dict]:
    request = Request(PAGE_URL, headers=HEADERS)
    try:
        with urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
        return parse_sse_table_html(html)
    except Exception as exc:
        raise ETFNetworkError(str(exc)) from exc


def check_sse_connection(trade_date: str | None = None, fetch_func=None) -> tuple[bool, str]:
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    fetch_func = fetch_func or (lambda date: fetch_etf_rows_for_date(date, page_size=1, timeout=8))
    try:
        fetch_func(trade_date)
        return True, "接口连通，可以继续采集。"
    except ETFNetworkError as exc:
        return False, f"接口仍未连通: {exc}"
    except Exception as exc:
        return False, f"接口测试失败: {exc}"


def check_szse_connection(trade_date: str | None = None, fetch_func=None) -> tuple[bool, str]:
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    fetch_func = fetch_func or (lambda date_start, date_end: probe_szse_connection(date_start))
    try:
        fetch_func(trade_date, trade_date)
        return True, "深交所接口连通，可以继续采集。"
    except ETFNetworkError as exc:
        return False, f"深交所接口仍未连通: {exc}"
    except Exception as exc:
        return False, f"深交所接口测试失败: {exc}"


def normalize_worker_count(value, default: int = 16, minimum: int = 1, maximum: int = 64) -> int:
    try:
        workers = int(value)
    except (TypeError, ValueError):
        workers = default
    return max(minimum, min(maximum, workers))


def diagnose_network() -> list[str]:
    messages = []
    for host in ("www.sse.com.cn", "query.sse.com.cn", "www.szse.cn"):
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            addrs = sorted({info[4][0] for info in infos})
            messages.append(f"{host} DNS: {', '.join(addrs)}")
        except Exception as exc:
            messages.append(f"{host} DNS失败: {exc}")

    open_ports = []
    for port in (7890, 7897, 7899, 1080, 10808, 10809, 8080):
        sock = socket.socket()
        sock.settimeout(0.3)
        try:
            sock.connect(("127.0.0.1", port))
            open_ports.append(str(port))
        except OSError:
            pass
        finally:
            sock.close()
    messages.append("本机代理端口: " + (", ".join(open_ports) if open_ports else "未发现常见端口"))
    return messages


def iter_weekdays(start_date: str, end_date: str):
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current.strftime("%Y-%m-%d")
        current += timedelta(days=1)
