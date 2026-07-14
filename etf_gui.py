from __future__ import annotations

import faulthandler
import os
import sys
import threading
import traceback
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from etf_database import DEFAULT_DB_PATH, ETFDatabase
from etf_fetcher import (
    ETFNetworkError,
    check_sse_connection,
    diagnose_network,
    fetch_etf_rows_for_date,
    iter_weekdays,
    normalize_worker_count,
)
from szse_download_fetcher import (
    check_szse_download_connection,
    fetch_szse_rows_via_browser_downloads,
)
from sse_pcf_fetcher import fetch_sse_pcf_for_fund
from szse_pcf_fetcher import (
    SZSEPCFPageError,
    check_szse_pcf_connection,
    collect_szse_pcf_via_browser,
)
from etf_web_app import ETFWebServer
from etf_web_app import parse_web_endpoint


ETF_CRASH_LOG = Path(__file__).with_name("etf_crash.log")
ETF_RUNTIME_LOG = Path(__file__).with_name("etf_runtime.log")
_FAULT_LOG_FILE = None


def _append_runtime_log(message: str) -> None:
    try:
        with ETF_RUNTIME_LOG.open("a", encoding="utf-8") as log:
            log.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except Exception:
        pass


def _write_crash_log(context: str, details: str | None = None) -> None:
    try:
        with ETF_CRASH_LOG.open("a", encoding="utf-8") as log:
            log.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {context}\n"
                f"{details or traceback.format_exc()}\n"
            )
    except Exception:
        pass


def _enable_fault_logging() -> None:
    global _FAULT_LOG_FILE
    try:
        _FAULT_LOG_FILE = ETF_CRASH_LOG.open("a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_LOG_FILE, all_threads=True)
    except Exception:
        pass


def _install_exception_hooks() -> None:
    def handle_unhandled_exception(exc_type, exc, tb):
        _write_crash_log(
            "unhandled exception",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )
        sys.__excepthook__(exc_type, exc, tb)

    def handle_thread_exception(args):
        _write_crash_log(
            f"thread exception: {args.thread.name if args.thread else '-'}",
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        )

    sys.excepthook = handle_unhandled_exception
    threading.excepthook = handle_thread_exception


def _handle_tk_exception(exc_type, exc, tb) -> None:
    _write_crash_log(
        "tk callback exception",
        "".join(traceback.format_exception(exc_type, exc, tb)),
    )
    try:
        messagebox.showerror(
            "ETF工具运行错误",
            f"{exc}\n\n详细错误已写入:\n{ETF_CRASH_LOG}",
        )
    except Exception:
        pass


def _mark_startup() -> None:
    _append_runtime_log(
        f"startup file={Path(__file__).resolve()} cwd={Path.cwd()} "
        f"python={sys.executable} pid={os.getpid()}"
    )


def _mark_shutdown() -> None:
    _append_runtime_log("shutdown")


class ETFApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ETF份额采集与曲线")
        self.root.geometry("760x520")
        self.db = ETFDatabase(DEFAULT_DB_PATH)
        self.db.initialize()
        self.server = ETFWebServer(DEFAULT_DB_PATH)
        self.web_host_var = tk.StringVar(value="127.0.0.1")
        self.web_port_var = tk.StringVar(value="1234")
        self.exchange_var = tk.StringVar(value="上交所")
        self.pcf_code_var = tk.StringVar()
        self.pcf_replace_var = tk.BooleanVar(value=False)
        self.busy = False
        self.paused_task = None
        self.paused_pcf_task = None
        self._build_ui()
        self._refresh_stats()

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        db_text = f"数据库: {DEFAULT_DB_PATH}"
        ttk.Label(frame, text=db_text).pack(anchor="w")

        form = ttk.LabelFrame(frame, text="采集", padding=10)
        form.pack(fill=tk.X, pady=10)

        today = datetime.now().strftime("%Y-%m-%d")
        last_month = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        ttk.Label(form, text="单日").grid(row=0, column=0, padx=4, pady=4)
        self.date_var = tk.StringVar(value=today)
        ttk.Entry(form, textvariable=self.date_var, width=14).grid(row=0, column=1, padx=4)
        ttk.Button(form, text="采集单日全量ETF", command=self.fetch_single).grid(row=0, column=2, padx=4)
        ttk.Label(form, text="交易所").grid(row=0, column=3, padx=4)
        ttk.Combobox(
            form,
            textvariable=self.exchange_var,
            values=("上交所", "深交所", "沪深两市"),
            state="readonly",
            width=10,
        ).grid(row=0, column=4, padx=4)

        ttk.Label(form, text="区间").grid(row=1, column=0, padx=4, pady=4)
        self.start_var = tk.StringVar(value=last_month)
        self.end_var = tk.StringVar(value=today)
        ttk.Entry(form, textvariable=self.start_var, width=14).grid(row=1, column=1, padx=4)
        ttk.Entry(form, textvariable=self.end_var, width=14).grid(row=1, column=2, padx=4)
        ttk.Label(form, text="线程").grid(row=1, column=3, padx=4)
        self.workers_var = tk.StringVar(value="16")
        ttk.Entry(form, textvariable=self.workers_var, width=6).grid(row=1, column=4, padx=4)
        ttk.Button(form, text="多线程采集区间", command=self.fetch_range).grid(row=1, column=5, padx=4)

        pcf_form = ttk.LabelFrame(frame, text="ETF成分股（PCF）", padding=10)
        pcf_form.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(pcf_form, text="基金代码").grid(row=0, column=0, padx=4, pady=4)
        ttk.Entry(pcf_form, textvariable=self.pcf_code_var, width=14).grid(row=0, column=1, padx=4)
        ttk.Button(pcf_form, text="采集当前 PCF", command=self.fetch_pcf_single).grid(row=0, column=2, padx=4)
        ttk.Button(pcf_form, text="批量采集上交所当前 PCF", command=self.fetch_pcf_batch).grid(row=0, column=3, padx=4)
        ttk.Button(
            pcf_form,
            text="采集深交所当前 PCF",
            command=self.fetch_szse_pcf_current,
        ).grid(row=1, column=0, columnspan=2, padx=4, pady=4, sticky="w")
        ttk.Button(
            pcf_form,
            text="采集深交所历史 PCF",
            command=self.fetch_szse_pcf_history,
        ).grid(row=1, column=2, padx=4, pady=4)
        ttk.Checkbutton(
            pcf_form,
            text="重新采集已有快照",
            variable=self.pcf_replace_var,
        ).grid(row=1, column=3, padx=4, pady=4, sticky="w")

        web_form = ttk.LabelFrame(frame, text="网页", padding=10)
        web_form.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(web_form, text="地址").grid(row=0, column=0, padx=4, pady=4)
        ttk.Entry(web_form, textvariable=self.web_host_var, width=18).grid(row=0, column=1, padx=4)
        ttk.Label(web_form, text="端口").grid(row=0, column=2, padx=4, pady=4)
        ttk.Entry(web_form, textvariable=self.web_port_var, width=8).grid(row=0, column=3, padx=4)
        ttk.Button(web_form, text="打开网页曲线", command=self.open_web).grid(row=0, column=4, padx=8)

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X, pady=4)
        ttk.Button(toolbar, text="刷新统计", command=self._refresh_stats).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="测试连通性/继续采集", command=self.test_connection_and_resume).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="网络诊断", command=self.diagnose).pack(side=tk.LEFT, padx=4)
        self.stats_var = tk.StringVar()
        ttk.Label(toolbar, textvariable=self.stats_var).pack(side=tk.LEFT, padx=12)

        log_frame = ttk.LabelFrame(frame, text="运行日志", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

    def log(self, text):
        self.root.after(0, lambda: self._append_log(text))

    def _append_log(self, text):
        follow_bottom = self.log_text.yview()[1] >= 0.999
        phase = self._log_phase(text)
        line = f"[{datetime.now():%H:%M:%S}] [{phase}] {text}"
        self.log_text.insert(tk.END, line + "\n")
        if follow_bottom:
            self.log_text.see(tk.END)

    @staticmethod
    def _log_phase(text):
        if any(word in text for word in ("失败", "错误", "异常")):
            return "失败"
        if any(word in text for word in ("写入", "更新", "计算", "入库")):
            return "入库"
        if any(word in text for word in ("采集", "下载", "抓取", "PCF")):
            return "采集"
        if "网页" in text:
            return "网页"
        return "状态"

    def _run(self, func):
        if self.busy:
            messagebox.showinfo("提示", "正在采集中，请稍等。")
            return
        self.busy = True

        def worker():
            try:
                func()
            except Exception as exc:
                _write_crash_log("background task failed")
                self.log(f"失败: {exc}")
            finally:
                self.busy = False
                self.root.after(0, self._refresh_stats)

        threading.Thread(target=worker, daemon=True).start()

    def fetch_single(self):
        date = self.date_var.get().strip()
        self._validate_date(date)
        self.paused_task = None
        self._run(lambda: self._fetch_selected_single(date))

    def _selected_exchanges(self):
        return {
            "上交所": ("SSE",),
            "深交所": ("SZSE",),
            "沪深两市": ("SSE", "SZSE"),
        }.get(self.exchange_var.get(), ("SSE",))

    def _fetch_selected_single(self, date):
        for exchange in self._selected_exchanges():
            self._fetch_date(date, exchange=exchange, pause_task=("single", exchange, date, date))
            if self.paused_task:
                break

    def fetch_range(self):
        start = self.start_var.get().strip()
        end = self.end_var.get().strip()
        self._validate_date(start)
        self._validate_date(end)
        self.paused_task = None

        def task():
            workers = normalize_worker_count(self.workers_var.get())
            for exchange in self._selected_exchanges():
                if exchange == "SSE":
                    self._fetch_range_threaded(start, end, workers)
                else:
                    self._fetch_szse_range_threaded(start, end, workers)
                if self.paused_task:
                    break

        self._run(task)

    def fetch_pcf_single(self):
        code = self.pcf_code_var.get().strip()
        if not code.isdigit() or len(code) != 6:
            messagebox.showerror("基金代码错误", "请输入 6 位数字基金代码。")
            return
        self._run(lambda: self._fetch_one_pcf(code))

    def _fetch_one_pcf(self, code):
        self.log(f"正在采集上交所 {code} 当前 PCF...")
        info, items = fetch_sse_pcf_for_fund(code)
        info_count, item_count = self.db.upsert_pcf([info], items)
        self.log(
            f"上交所 {code} PCF 完成：公告日 {info.get('内容日期') or '-'}，"
            f"信息 {info_count} 行，成分 {item_count} 行。"
        )

    def fetch_pcf_batch(self):
        self._run(self._fetch_pcf_batch)

    def _fetch_pcf_batch(self):
        funds = self.db.list_fund_codes("SSE")
        if not funds:
            self.log("上交所 PCF 批量采集结束：ETF 表中没有上交所基金代码。")
            return

        configured_workers = normalize_worker_count(self.workers_var.get())
        workers = min(configured_workers, 4)
        self.log(
            f"开始批量采集上交所当前 PCF：{len(funds)} 只基金，线程 {workers}，"
            "请求间隔 0.35 秒。"
        )
        completed = 0
        written_info = 0
        written_items = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(fetch_sse_pcf_for_fund, row["fund_code"]): row["fund_code"]
                for row in funds
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    info, items = future.result()
                    info_count, item_count = self.db.upsert_pcf([info], items)
                    written_info += info_count
                    written_items += item_count
                    completed += 1
                    if completed == 1 or completed % 20 == 0 or completed == len(funds):
                        self.log(f"PCF 批量进度 {completed}/{len(funds)}，最近完成 {code}。")
                except Exception as exc:
                    completed += 1
                    self.log(f"PCF {code} 采集失败，已跳过：{exc}")
        self.log(f"上交所 PCF 批量采集完成：信息 {written_info} 行，成分 {written_items} 行。")

    def _szse_pcf_code_or_none(self):
        code = self.pcf_code_var.get().strip()
        if code and (not code.isdigit() or len(code) != 6):
            messagebox.showerror("基金代码错误", "基金代码可留空；填写时请输入 6 位数字。")
            return None
        return code

    def fetch_szse_pcf_current(self):
        code = self._szse_pcf_code_or_none()
        if code is None:
            return
        replace_existing = self.pcf_replace_var.get()
        self.paused_pcf_task = None
        self._run(lambda: self._fetch_szse_pcf_current(code, replace_existing))

    def _fetch_szse_pcf_current(self, code, replace_existing):
        today = datetime.now().strftime("%Y-%m-%d")
        summary = self._fetch_szse_pcf_dates([today], code, replace_existing)
        if summary is None or summary["discovered"] != 0:
            return summary

        latest_date = self.db.latest_stock_trading_date(today)
        if latest_date and latest_date != today:
            self.log(f"深交所当前 PCF 当日未发现文件，改采最近股票交易日 {latest_date}。")
            return self._fetch_szse_pcf_dates([latest_date], code, replace_existing)
        return summary

    def fetch_szse_pcf_history(self):
        start = self.start_var.get().strip()
        end = self.end_var.get().strip()
        self._validate_date(start)
        self._validate_date(end)
        code = self._szse_pcf_code_or_none()
        if code is None:
            return
        replace_existing = self.pcf_replace_var.get()
        self.paused_pcf_task = None
        self._run(lambda: self._fetch_szse_pcf_history(start, end, code, replace_existing))

    def _fetch_szse_pcf_history(self, start, end, code, replace_existing):
        dates = self.db.list_stock_trading_dates(start, end)
        if not dates:
            self.log("深交所历史 PCF 未找到股票交易日，未开始采集。")
            return None
        return self._fetch_szse_pcf_dates(dates, code, replace_existing)

    def _fetch_szse_pcf_dates(self, dates, code, replace_existing):
        try:
            return collect_szse_pcf_via_browser(
                dates,
                fund_code=code,
                replace_existing=replace_existing,
                is_complete=lambda fund, date: self.db.pcf_is_complete("SZSE", fund, date),
                save_snapshot=lambda info, items: self.db.replace_pcf_snapshot(
                    info, items, source="szse_pcf_browser"
                ),
                on_progress=self.log,
                visible=False,
            )
        except SZSEPCFPageError as exc:
            self.paused_pcf_task = {
                "current": exc.trade_date,
                "end": dates[-1],
                "code": code,
                "replace": replace_existing,
            }
            self.log(f"深交所 PCF {exc.trade_date} 页面查询失败，已暂停且不会跳过该日期: {exc}")
            self.log("请先点“测试连通性/继续采集”；测通后会从暂停日期继续。")
            return None

    def _fetch_range_threaded(self, start, end, workers):
        dates = list(iter_weekdays(start, end))
        if not dates:
            self.log("区间内没有工作日。")
            return 0

        existing_dates = self.db.existing_trade_dates("SSE", start, end)
        skipped = [date for date in dates if date in existing_dates]
        dates = [date for date in dates if date not in existing_dates]
        if skipped:
            self.log(f"上交所跳过数据库已有数据 {len(skipped)} 天。")
        if not dates:
            self.log("上交所所选区间数据库已有数据，跳过采集。")
            return 0

        self.log(f"开始多线程采集 {start} ~ {end}，待采工作日 {len(dates)} 天，线程 {workers}。")
        total = 0
        completed = 0
        rows_buffer = []
        touched_codes = set()
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {executor.submit(fetch_etf_rows_for_date, date): date for date in dates}
        try:
            for future in as_completed(futures):
                date = futures[future]
                try:
                    rows = future.result()
                except ETFNetworkError as exc:
                    self.paused_task = ("range", "SSE", date, end)
                    self.log(f"{date} 接口连接失败，区间采集已暂停: {exc}")
                    self.log("请先点“测试连通性/继续采集”；测通后会从暂停日期继续。")
                    break
                if not rows:
                    self.log(f"{date} 没有返回ETF数据。")
                    continue
                completed += 1
                rows_buffer.extend(rows)
                touched_codes.update(row["fund_code"] for row in rows)
                self.log(f"{date} 抓取完成 {len(rows)} 行，进度 {completed}/{len(dates)}。")
                if completed % 20 == 0:
                    count = self.db.upsert_rows(rows_buffer, recalculate=False)
                    total += count
                    self.log(f"批量写入/更新 {count} 行，稍后统一计算份额差量。")
                    rows_buffer = []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if rows_buffer:
            count = self.db.upsert_rows(rows_buffer, recalculate=False)
            total += count
            self.log(f"批量写入/更新 {count} 行，稍后统一计算份额差量。")

        if touched_codes:
            self.log(f"正在统一计算 {len(touched_codes)} 只ETF的份额差量...")
            self.db.recalculate_deltas(sorted(touched_codes))

        if self.paused_task:
            self.log(f"区间采集暂停，本次已写入/更新 {total} 行。")
        else:
            self.log(f"区间采集完成，写入/更新 {total} 行。")
        return total

    def _fetch_szse_range_threaded(self, start, end, workers):
        self.log(f"开始浏览器下载采集深交所 {start} ~ {end}，自动按 5 个月批次、每批按 15 天下载。")
        existing_dates = self.db.existing_trade_dates("SZSE", start, end)

        def should_skip_range(chunk_start, chunk_end):
            chunk_dates = list(iter_weekdays(chunk_start, chunk_end))
            return bool(chunk_dates) and all(date in existing_dates for date in chunk_dates)

        try:
            rows = fetch_szse_rows_via_browser_downloads(
                start,
                end,
                on_progress=self.log,
                should_skip_range=should_skip_range,
            )
        except Exception as exc:
            self.paused_task = ("range", "SZSE", start, end)
            self.log(f"深交所浏览器下载采集失败，已暂停: {exc}")
            return 0

        if not rows:
            self.log("深交所浏览器下载没有解析到ETF数据。")
            return 0

        rows_before_filter = len(rows)
        rows = [row for row in rows if row.get("trade_date") not in existing_dates]
        filtered = rows_before_filter - len(rows)
        if filtered:
            self.log(f"深交所过滤数据库已有日期 {filtered} 行，不覆盖旧数据。")
        if not rows:
            self.log("深交所下载数据均为数据库已有日期，跳过写入。")
            return 0

        count = self.db.upsert_rows(rows, recalculate=False)
        touched_codes = sorted({row["fund_code"] for row in rows})
        self.log(f"深交所浏览器下载解析 {len(rows)} 行，写入/更新 {count} 行。")
        if touched_codes:
            self.log(f"正在统一计算深交所 {len(touched_codes)} 只ETF的份额差量...")
            self.db.recalculate_deltas(touched_codes)
        self.log(f"深交所浏览器下载采集完成，写入/更新 {count} 行。")
        return count

    def _fetch_date(self, date, exchange="SSE", pause_task=None):
        if date in self.db.existing_trade_dates(exchange, date, date):
            self.log(f"{exchange} {date} 数据库已有数据，跳过采集。")
            return 0

        self.log(f"正在采集 {exchange} {date} 全量ETF份额...")
        try:
            if exchange == "SZSE":
                rows = fetch_szse_rows_via_browser_downloads(date, date, on_progress=self.log)
                source = "深交所浏览器下载"
            else:
                rows = fetch_etf_rows_for_date(date)
                source = "上交所接口"
        except ETFNetworkError as exc:
            self.paused_task = pause_task
            self.log(f"接口连接失败，已暂停采集: {exc}")
            self.log("请先点“测试连通性/继续采集”；测通后会从暂停日期继续。")
            return 0
        except Exception as exc:
            if exchange == "SZSE":
                self.paused_task = pause_task
                self.log(f"深交所浏览器下载失败，已暂停采集: {exc}")
                return 0
            raise
        if not rows:
            self.log(f"{date} 没有返回ETF数据。")
            return 0
        count = self.db.upsert_rows(rows)
        self.log(f"{date} 通过{source}获取 {len(rows)} 行，写入/更新 {count} 行。")
        return count

    def test_connection_and_resume(self):
        self._run(self._test_connection_and_resume)

    def _test_connection_and_resume(self):
        if self.paused_pcf_task:
            task = self.paused_pcf_task
            current = task["current"]
            ok, message = check_szse_pcf_connection(current)
            self.log(message)
            if not ok:
                return
            dates = self.db.list_stock_trading_dates(current, task["end"])
            self.paused_pcf_task = None
            if not dates:
                self.log("深交所 PCF 暂停区间未找到股票交易日，无法继续采集。")
                return
            self.log(f"继续采集深交所 PCF: {dates[0]} ~ {dates[-1]}。")
            self._fetch_szse_pcf_dates(dates, task["code"], task["replace"])
            return

        if self.paused_task and len(self.paused_task) == 4:
            test_date = self.paused_task[2]
        else:
            test_date = self.date_var.get().strip()
        try:
            self._validate_date(test_date)
        except ValueError:
            test_date = datetime.now().strftime("%Y-%m-%d")
        exchanges = (self.paused_task[1],) if self.paused_task and len(self.paused_task) == 4 else self._selected_exchanges()
        all_ok = True
        for exchange in exchanges:
            if exchange == "SZSE":
                ok, message = check_szse_download_connection(test_date)
            else:
                ok, message = check_sse_connection(test_date)
            self.log(message)
            all_ok = all_ok and ok
        if not all_ok or not self.paused_task:
            return

        if len(self.paused_task) == 3:
            mode, current, end = self.paused_task
            exchange = "SSE"
        else:
            mode, exchange, current, end = self.paused_task
        self.paused_task = None
        if mode == "single":
            self.log(f"继续采集单日 {current}。")
            self._fetch_date(
                current,
                exchange=exchange,
                pause_task=("single", exchange, current, end),
            )
            return

        self.log(f"继续采集{exchange}区间: {current} ~ {end}。")
        workers = normalize_worker_count(self.workers_var.get())
        if exchange == "SZSE":
            self._fetch_szse_range_threaded(current, end, workers)
        else:
            self._fetch_range_threaded(current, end, workers)

    def open_web(self):
        try:
            host, port = parse_web_endpoint(self.web_host_var.get(), self.web_port_var.get())
            self.server.configure(host, port)
            url = self.server.start()
        except ValueError as exc:
            messagebox.showerror("网页设置错误", str(exc))
            return
        except OSError as exc:
            self.log(f"网页启动失败: {exc}")
            messagebox.showerror("网页启动失败", "这个地址或端口可能已经被占用，请换一个端口再试。")
            return

        self.log(f"网页曲线已启动: {url}")
        self.server.open()

    def diagnose(self):
        self.log("网络诊断:")
        for line in diagnose_network():
            self.log("  " + line)

    def _refresh_stats(self):
        stats = self.db.get_stats()
        self.stats_var.set(
            f"ETF表: {stats.get('rows_count') or 0} 行 / {stats.get('fund_count') or 0} 只 "
            f"{stats.get('min_date') or '-'} ~ {stats.get('max_date') or '-'}"
        )

    @staticmethod
    def _validate_date(value):
        datetime.strptime(value, "%Y-%m-%d")


def main():
    _install_exception_hooks()
    _enable_fault_logging()
    _mark_startup()
    try:
        root = tk.Tk()
        root.report_callback_exception = _handle_tk_exception
        ETFApp(root)
        root.mainloop()
    except Exception as exc:
        _write_crash_log("startup failed")
        try:
            messagebox.showerror(
                "ETF工具启动失败",
                f"{exc}\n\n详细错误已写入:\n{ETF_CRASH_LOG}",
            )
        except Exception:
            pass
        raise
    finally:
        _mark_shutdown()


if __name__ == "__main__":
    main()

