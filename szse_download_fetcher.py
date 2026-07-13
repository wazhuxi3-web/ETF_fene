from __future__ import annotations

import random
import time
import warnings
from calendar import monthrange
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

import pandas as pd


SZSE_DOWNLOAD_PAGE_URL = "https://www.szse.cn/market/fund/volume/etf/index.html"
SZSE_DOWNLOAD_CHUNK_DAYS = 15
SZSE_MAX_BATCH_MONTHS = 5


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def validate_szse_download_batch_range(start_date: str, end_date: str) -> None:
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if start > end:
        raise ValueError("start_date must not be after end_date")
    max_end = _add_months(start, SZSE_MAX_BATCH_MONTHS) - timedelta(days=1)
    if end > max_end:
        raise ValueError("深交所浏览器下载模式一次最多采集5个月，请缩短日期范围。")


def split_szse_download_batch_ranges(start_date: str, end_date: str):
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if start > end:
        raise ValueError("start_date must not be after end_date")

    current = start
    while current <= end:
        batch_end = min(_add_months(current, SZSE_MAX_BATCH_MONTHS) - timedelta(days=1), end)
        yield current.strftime("%Y-%m-%d"), batch_end.strftime("%Y-%m-%d")
        current = batch_end + timedelta(days=1)


def split_szse_download_ranges(start_date: str, end_date: str):
    validate_szse_download_batch_range(start_date, end_date)
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while current <= end:
        chunk_end = min(current + timedelta(days=SZSE_DOWNLOAD_CHUNK_DAYS - 1), end)
        yield current.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        current = chunk_end + timedelta(days=1)


def _clean_text(value) -> str:
    return "" if value is None else str(value).strip()


def _to_float(value) -> float:
    if value is None or value == "":
        raise ValueError("empty number")
    return float(str(value).replace(",", "").strip())


def parse_szse_download_file(path: str | Path) -> list[dict]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Workbook contains no default style")
        df = pd.read_excel(path, engine="openpyxl")
    columns = {_clean_text(column): column for column in df.columns}
    date_col = columns.get("日期")
    code_col = columns.get("基金代码")
    name_col = columns.get("基金简称")
    size_name = next((name for name in columns if "基金规模" in name), None)
    if not all([date_col, code_col, name_col, size_name]):
        raise ValueError("深交所下载文件缺少必要列")

    size_col = columns[size_name]
    multiplier = 10000 if "万份" in size_name else 1
    rows = []
    for _, item in df.iterrows():
        code = _clean_text(item[code_col])
        trade_date = _clean_text(item[date_col])[:10]
        share = item[size_col]
        if not code or not trade_date or pd.isna(share):
            continue
        rows.append(
            {
                "trade_date": trade_date,
                "fund_code": code.zfill(6),
                "fund_name": _clean_text(item[name_col]),
                "total_share": _to_float(share) * multiplier,
                "exchange": "SZSE",
                "share_unit": "share",
                "source": "szse_download",
            }
        )
    return rows


def collect_szse_rows_from_downloads(
    start_date: str,
    end_date: str,
    downloader: Callable[[str, str], str | Path],
    on_progress: Callable[[str], None] | None = None,
    should_skip_range: Callable[[str, str], bool] | None = None,
) -> list[dict]:
    rows = []
    ranges = [
        chunk
        for batch_start, batch_end in split_szse_download_batch_ranges(start_date, end_date)
        for chunk in split_szse_download_ranges(batch_start, batch_end)
    ]
    for index, (chunk_start, chunk_end) in enumerate(ranges, start=1):
        if should_skip_range and should_skip_range(chunk_start, chunk_end):
            if on_progress:
                on_progress(f"深交所跳过已有数据 {chunk_start} ~ {chunk_end} ({index}/{len(ranges)})")
            continue
        if on_progress:
            on_progress(f"深交所下载 {chunk_start} ~ {chunk_end} ({index}/{len(ranges)})")
        rows.extend(parse_szse_download_file(downloader(chunk_start, chunk_end)))
    return rows


def _set_date_inputs(page, start_date: str, end_date: str) -> None:
    page.locator("input.query-txtStart").fill(start_date)
    page.locator("input.query-txtEnd").fill(end_date)
    page.evaluate(
        """
        ([startDate, endDate]) => {
            const start = document.querySelector("input.query-txtStart");
            const end = document.querySelector("input.query-txtEnd");
            for (const [el, value] of [[start, startDate], [end, endDate]]) {
                el.value = value;
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
            }
        }
        """,
        [start_date, end_date],
    )


def fetch_szse_rows_via_browser_downloads(
    start_date: str,
    end_date: str,
    on_progress: Callable[[str], None] | None = None,
    visible: bool = False,
    should_skip_range: Callable[[str, str], bool] | None = None,
) -> list[dict]:
    ranges = [
        chunk
        for batch_start, batch_end in split_szse_download_batch_ranges(start_date, end_date)
        for chunk in split_szse_download_ranges(batch_start, batch_end)
    ]
    if ranges and should_skip_range and all(should_skip_range(start, end) for start, end in ranges):
        if on_progress:
            on_progress("深交所所选区间数据库已有数据，跳过下载。")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError("缺少 Playwright，无法使用深交所浏览器下载模式。") from exc

    with TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        download_dir = Path(tmp)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not visible, slow_mo=120)
            page = browser.new_page(accept_downloads=True)
            page.goto(SZSE_DOWNLOAD_PAGE_URL, wait_until="networkidle", timeout=60000)

            def download_chunk(chunk_start: str, chunk_end: str) -> Path:
                _set_date_inputs(page, chunk_start, chunk_end)
                page.locator("button.confirm-query").click()
                page.wait_for_load_state("networkidle", timeout=30000)
                page.wait_for_timeout(random.randint(500, 1200))
                with page.expect_download(timeout=30000) as download_info:
                    page.locator("a.btn-default-excel").click()
                download = download_info.value
                target = download_dir / f"szse_{chunk_start}_{chunk_end}_{download.suggested_filename}"
                download.save_as(str(target))
                return target

            try:
                rows = collect_szse_rows_from_downloads(
                    start_date,
                    end_date,
                    download_chunk,
                    on_progress=on_progress,
                    should_skip_range=should_skip_range,
                )
            finally:
                browser.close()
        time.sleep(0.1)
        return rows


def check_szse_download_connection(trade_date: str | None = None, fetch_func=None) -> tuple[bool, str]:
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    fetch_func = fetch_func or fetch_szse_rows_via_browser_downloads
    try:
        fetch_func(trade_date, trade_date, visible=False)
        return True, "深交所浏览器下载连通，可以继续采集。"
    except Exception as exc:
        return False, f"深交所浏览器下载仍未连通: {exc}"
