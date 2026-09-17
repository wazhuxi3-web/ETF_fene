from __future__ import annotations

import faulthandler
import json
import os
import sys
import threading
import traceback
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
from eastmoney_holding_fetcher import (
    EastmoneyHoldingError,
    EastmoneyHoldingNoDataError,
    check_eastmoney_holding_connection,
    fetch_eastmoney_holdings,
)
from szse_pcf_fetcher import (
    SZSEPCFPageError,
    check_szse_pcf_connection,
    collect_szse_pcf_via_browser,
)
from etf_web_app import ETFWebServer
from etf_web_app import parse_web_endpoint


ETF_CRASH_LOG = Path(__file__).with_name("etf_crash.log")
ETF_RUNTIME_LOG = Path(__file__).with_name("etf_runtime.log")
ETF_CONFIG_PATH = Path(__file__).with_name("etf_gui_config.json")
_FAULT_LOG_FILE = None


def load_database_path(
    config_path: str | Path = ETF_CONFIG_PATH,
    default_path: str | Path = DEFAULT_DB_PATH,
) -> Path:
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        value = config.get("database_path") if isinstance(config, dict) else None
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    except (OSError, TypeError, ValueError):
        pass
    return Path(default_path)


def save_database_path(
    db_path: str | Path,
    config_path: str | Path = ETF_CONFIG_PATH,
) -> None:
    Path(config_path).write_text(
        json.dumps({"database_path": str(Path(db_path))}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


def collection_panel_state(
    data_type: str, date_mode: str, exchange_label: str
) -> dict:
    if data_type == "holding":
        return {
            "show_single_date": False,
            "show_range_dates": False,
            "show_holding_years": True,
            "show_workers": True,
            "show_pcf_options": True,
            "button_text": "开始采集季度持仓",
            "notice": "东方财富按年度返回季度报告；一、三季度通常不是完整持仓，回测请按可用日期过滤。",
        }
    is_component = data_type == "component"
    notice = ""
    if is_component and exchange_label == "上交所":
        notice = "上交所成分股仅更新最新快照，所选日期范围不适用。"
    elif is_component and exchange_label == "沪深两市":
        notice = "选择沪深两市时：上交所更新一次最新快照；深交所按所选日期区间采集。"
    return {
        "show_single_date": date_mode == "single",
        "show_range_dates": date_mode == "range",
        "show_workers": not is_component,
        "show_pcf_options": is_component,
        "button_text": "开始采集成分股" if is_component else "开始采集份额",
        "notice": notice,
    }


def format_coverage_cell(kind: str, stats: dict) -> str:
    if not stats or not stats.get("min_date"):
        return "暂无数据"
    if kind == "share":
        return (
            f"{stats['min_date']} ~ {stats['max_date']} | "
            f"{stats['rows_count']} 行 / {stats['fund_count']} 只 / "
            f"{stats['date_count']} 日"
        )
    if kind == "holding":
        return (
            f"{stats['min_date']} ~ {stats['max_date']} | "
            f"{stats['report_count']} 份报告 / {stats['fund_count']} 只 / "
            f"{stats['item_count']} 条 / 完整 {stats['full_report_count']}"
        )
    return (
        f"{stats['min_date']} ~ {stats['max_date']} | "
        f"{stats['snapshot_count']} 快照 / {stats['fund_count']} 只 / "
        f"{stats['item_count']} 成分"
    )


class ETFApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ETF份额采集与曲线")
        self.root.geometry("880x640")
        self.root.minsize(820, 600)
        self.db_path = load_database_path()
        self.db = ETFDatabase(self.db_path)
        self.db.initialize()
        self.server = ETFWebServer(self.db_path)
        self.web_host_var = tk.StringVar(value="127.0.0.1")
        self.web_port_var = tk.StringVar(value="1234")
        self.exchange_var = tk.StringVar(value="上交所")
        self.date_mode_var = tk.StringVar(value="single")
        self.data_type_var = tk.StringVar(value="share")
        self.task_status_var = tk.StringVar(value="空闲")
        self.collection_notice_var = tk.StringVar()
        self.workers_hint_var = tk.StringVar(value="深交所份额下载由浏览器自动分批")
        self.pcf_code_var = tk.StringVar()
        self.pcf_replace_var = tk.BooleanVar(value=False)
        self.busy = False
        self.paused_task = None
        self.paused_pcf_task = None
        self.paused_holding_task = None
        self._build_ui()
        self._refresh_stats()

    def _build_ui(self):
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        db_frame = ttk.Frame(frame)
        db_frame.pack(fill=tk.X)
        ttk.Label(db_frame, text="数据库").pack(side=tk.LEFT, padx=(0, 6))
        self.db_path_var = tk.StringVar(value=str(self.db_path))
        self.db_path_entry = ttk.Entry(
            db_frame, textvariable=self.db_path_var, state="readonly"
        )
        self.db_path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.db_path_button = ttk.Button(
            db_frame, text="选择数据库", command=self.choose_database
        )
        self.db_path_button.pack(side=tk.LEFT, padx=(8, 0))

        today = datetime.now().strftime("%Y-%m-%d")
        last_month = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        self.date_var = tk.StringVar(value=today)
        self.start_var = tk.StringVar(value=last_month)
        self.end_var = tk.StringVar(value=today)
        self.export_start_var = tk.StringVar(value=last_month)
        self.export_end_var = tk.StringVar(value=today)
        self.export_dir_var = tk.StringVar()
        self.holding_start_year_var = tk.StringVar(value="2016")
        self.holding_end_year_var = tk.StringVar(value=str(datetime.now().year))
        self.workers_var = tk.StringVar(value="16")

        collection_form = ttk.LabelFrame(frame, text="ETF 数据采集", padding=10)
        collection_form.pack(fill=tk.X, pady=10)
        collection_form.columnconfigure(0, weight=1)

        content_frame = ttk.LabelFrame(collection_form, text="采集内容", padding=6)
        content_frame.grid(row=0, column=0, sticky="ew")
        self.share_type_button = ttk.Radiobutton(
            content_frame,
            text="ETF 份额",
            value="share",
            variable=self.data_type_var,
            command=self._sync_collection_panel,
        )
        self.share_type_button.pack(side=tk.LEFT, padx=(2, 14))
        self.component_type_button = ttk.Radiobutton(
            content_frame,
            text="ETF 成分股",
            value="component",
            variable=self.data_type_var,
            command=self._sync_collection_panel,
        )
        self.component_type_button.pack(side=tk.LEFT)
        self.holding_type_button = ttk.Radiobutton(
            content_frame,
            text="基金季度持仓",
            value="holding",
            variable=self.data_type_var,
            command=self._sync_collection_panel,
        )
        self.holding_type_button.pack(side=tk.LEFT, padx=(14, 0))

        scope_frame = ttk.LabelFrame(collection_form, text="共同采集范围", padding=6)
        scope_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.single_mode_button = ttk.Radiobutton(
            scope_frame,
            text="单日",
            value="single",
            variable=self.date_mode_var,
            command=self._sync_collection_panel,
        )
        self.single_mode_button.grid(row=0, column=0, padx=(2, 8), pady=2)
        self.range_mode_button = ttk.Radiobutton(
            scope_frame,
            text="日期区间",
            value="range",
            variable=self.date_mode_var,
            command=self._sync_collection_panel,
        )
        self.range_mode_button.grid(row=0, column=1, padx=(0, 12), pady=2)

        self.date_input_host = ttk.Frame(scope_frame)
        self.date_input_host.grid(row=0, column=2, sticky="w")
        self.single_date_frame = ttk.Frame(self.date_input_host)
        ttk.Label(self.single_date_frame, text="日期").pack(side=tk.LEFT, padx=(0, 4))
        self.single_date_entry = ttk.Entry(
            self.single_date_frame, textvariable=self.date_var, width=14
        )
        self.single_date_entry.pack(side=tk.LEFT)
        self.range_date_frame = ttk.Frame(self.date_input_host)
        ttk.Label(self.range_date_frame, text="开始").pack(side=tk.LEFT, padx=(0, 4))
        self.start_date_entry = ttk.Entry(
            self.range_date_frame, textvariable=self.start_var, width=14
        )
        self.start_date_entry.pack(side=tk.LEFT)
        ttk.Label(self.range_date_frame, text="结束").pack(side=tk.LEFT, padx=(10, 4))
        self.end_date_entry = ttk.Entry(
            self.range_date_frame, textvariable=self.end_var, width=14
        )
        self.end_date_entry.pack(side=tk.LEFT)
        self.single_date_frame.grid(row=0, column=0, sticky="w")
        self.range_date_frame.grid(row=0, column=0, sticky="w")

        self.holding_year_frame = ttk.Frame(scope_frame)
        ttk.Label(self.holding_year_frame, text="报告年度").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Entry(
            self.holding_year_frame,
            textvariable=self.holding_start_year_var,
            width=7,
        ).pack(side=tk.LEFT)
        ttk.Label(self.holding_year_frame, text="至").pack(side=tk.LEFT, padx=6)
        ttk.Entry(
            self.holding_year_frame,
            textvariable=self.holding_end_year_var,
            width=7,
        ).pack(side=tk.LEFT)

        ttk.Label(scope_frame, text="交易所").grid(row=0, column=3, padx=(18, 4))
        self.exchange_combo = ttk.Combobox(
            scope_frame,
            textvariable=self.exchange_var,
            values=("上交所", "深交所", "沪深两市"),
            state="readonly",
            width=10,
        )
        self.exchange_combo.grid(row=0, column=4, padx=4)
        self.exchange_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_collection_panel()
        )

        parameter_host = ttk.LabelFrame(collection_form, text="采集参数", padding=6)
        parameter_host.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        parameter_host.columnconfigure(0, weight=1)
        parameter_host.columnconfigure(1, weight=1)
        parameter_host.rowconfigure(0, minsize=34)

        self.workers_frame = ttk.Frame(parameter_host)
        ttk.Label(self.workers_frame, text="并发线程").pack(side=tk.LEFT, padx=(2, 4))
        self.workers_entry = ttk.Entry(
            self.workers_frame, textvariable=self.workers_var, width=7
        )
        self.workers_entry.pack(side=tk.LEFT)
        ttk.Label(self.workers_frame, textvariable=self.workers_hint_var).pack(
            side=tk.LEFT, padx=12
        )
        self.workers_frame.grid(row=0, column=0, sticky="w")

        self.pcf_options_frame = ttk.Frame(parameter_host)
        ttk.Label(self.pcf_options_frame, text="基金代码（留空为全部）").pack(
            side=tk.LEFT, padx=(2, 4)
        )
        self.pcf_code_entry = ttk.Entry(
            self.pcf_options_frame, textvariable=self.pcf_code_var, width=14
        )
        self.pcf_code_entry.pack(side=tk.LEFT)
        self.pcf_replace_check = ttk.Checkbutton(
            self.pcf_options_frame,
            text="重新采集已有快照",
            variable=self.pcf_replace_var,
        )
        self.pcf_replace_check.pack(side=tk.LEFT, padx=14)
        self.pcf_options_frame.grid(row=0, column=0, sticky="w")

        action_frame = ttk.Frame(collection_form)
        action_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        action_frame.columnconfigure(0, weight=1)
        ttk.Label(
            action_frame,
            textvariable=self.collection_notice_var,
            foreground="#8a5a00",
        ).grid(row=0, column=0, sticky="w")
        self.start_collection_button = ttk.Button(
            action_frame,
            text="开始采集份额",
            command=self.start_selected_collection,
        )
        self.start_collection_button.grid(row=0, column=1, sticky="e", padx=(12, 0))

        coverage_frame = ttk.LabelFrame(
            collection_form, text="数据库覆盖范围", padding=6
        )
        coverage_frame.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        coverage_frame.columnconfigure(1, weight=1)
        coverage_frame.columnconfigure(2, weight=1)
        ttk.Label(coverage_frame, text="数据类型").grid(
            row=0, column=0, sticky="w", padx=(0, 10)
        )
        ttk.Label(coverage_frame, text="上交所").grid(
            row=0, column=1, sticky="w", padx=4
        )
        ttk.Label(coverage_frame, text="深交所").grid(
            row=0, column=2, sticky="w", padx=4
        )
        self.coverage_vars = {
            (kind, exchange): tk.StringVar(value="正在读取...")
            for kind in ("share", "component", "holding")
            for exchange in ("SSE", "SZSE")
        }
        for row, (kind, label) in enumerate(
            (("share", "ETF 份额"), ("component", "ETF 成分股"), ("holding", "基金季度持仓")), start=1
        ):
            ttk.Label(coverage_frame, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=2
            )
            for column, exchange in enumerate(("SSE", "SZSE"), start=1):
                ttk.Label(
                    coverage_frame,
                    textvariable=self.coverage_vars[(kind, exchange)],
                ).grid(row=row, column=column, sticky="w", padx=4, pady=2)

        export_form = ttk.LabelFrame(frame, text="ETF 份额导出", padding=10)
        export_form.pack(fill=tk.X, pady=(0, 10))
        export_form.columnconfigure(5, weight=1)
        ttk.Label(export_form, text="日期范围").grid(
            row=0, column=0, padx=(0, 4), pady=3, sticky="w"
        )
        self.export_start_entry = ttk.Entry(
            export_form, textvariable=self.export_start_var, width=14
        )
        self.export_start_entry.grid(row=0, column=1, padx=4, pady=3, sticky="w")
        ttk.Label(export_form, text="至").grid(row=0, column=2, padx=4, pady=3)
        self.export_end_entry = ttk.Entry(
            export_form, textvariable=self.export_end_var, width=14
        )
        self.export_end_entry.grid(row=0, column=3, padx=4, pady=3, sticky="w")
        ttk.Label(export_form, text="导出目录").grid(
            row=0, column=4, padx=(14, 4), pady=3, sticky="w"
        )
        self.export_dir_entry = ttk.Entry(
            export_form, textvariable=self.export_dir_var, state="readonly"
        )
        self.export_dir_entry.grid(row=0, column=5, padx=4, pady=3, sticky="ew")
        self.export_dir_button = ttk.Button(
            export_form, text="选择文件夹", command=self.choose_export_directory
        )
        self.export_dir_button.grid(row=0, column=6, padx=4, pady=3)
        self.export_button = ttk.Button(
            export_form, text="导出份额 CSV", command=self.start_share_export
        )
        self.export_button.grid(row=0, column=7, padx=(8, 0), pady=3)
        ttk.Label(
            export_form,
            text="按日生成：YYYY-MM-DD_上交所.csv / YYYY-MM-DD_深交所.csv；无数据日期不生成空文件",
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(3, 0))

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
        self.test_connection_button = ttk.Button(
            toolbar, text="测试连接", command=self.test_selected_connection
        )
        self.test_connection_button.pack(side=tk.LEFT, padx=4)
        self.continue_button = ttk.Button(
            toolbar, text="继续暂停任务", command=self.continue_paused_task
        )
        self.continue_button.pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="网络诊断", command=self.diagnose).pack(side=tk.LEFT, padx=4)
        ttk.Label(toolbar, text="状态:").pack(side=tk.LEFT, padx=(14, 4))
        ttk.Label(toolbar, textvariable=self.task_status_var).pack(side=tk.LEFT)

        log_frame = ttk.LabelFrame(frame, text="运行日志", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=12, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

        self.task_input_widgets = [
            self.share_type_button,
            self.component_type_button,
            self.holding_type_button,
            self.single_mode_button,
            self.range_mode_button,
            self.single_date_entry,
            self.start_date_entry,
            self.end_date_entry,
            *self.holding_year_frame.winfo_children(),
            self.exchange_combo,
            self.workers_entry,
            self.pcf_code_entry,
            self.pcf_replace_check,
            self.start_collection_button,
            self.test_connection_button,
            self.db_path_entry,
            self.db_path_button,
            self.export_start_entry,
            self.export_end_entry,
            self.export_dir_entry,
            self.export_dir_button,
            self.export_button,
        ]
        self._sync_collection_panel()
        self._update_continue_button()

    def _sync_collection_panel(self):
        state = collection_panel_state(
            self.data_type_var.get(),
            self.date_mode_var.get(),
            self.exchange_var.get(),
        )
        self.holding_year_frame.grid_remove()
        if state.get("show_holding_years"):
            self.single_mode_button.grid_remove()
            self.range_mode_button.grid_remove()
            self.single_date_frame.grid_remove()
            self.range_date_frame.grid_remove()
            self.holding_year_frame.grid(row=0, column=0, sticky="w")
        elif state["show_single_date"]:
            self.single_mode_button.grid()
            self.range_mode_button.grid()
            self.range_date_frame.grid_remove()
            self.single_date_frame.grid()
        else:
            self.single_mode_button.grid()
            self.range_mode_button.grid()
            self.single_date_frame.grid_remove()
            self.range_date_frame.grid()
        if self.data_type_var.get() == "holding":
            self.workers_frame.grid(row=0, column=0, sticky="w")
            self.pcf_options_frame.grid(row=0, column=1, sticky="w", padx=(18, 0))
        elif state["show_workers"]:
            self.pcf_options_frame.grid_remove()
            self.workers_frame.grid(row=0, column=0, sticky="w")
        else:
            self.workers_frame.grid_remove()
            self.pcf_options_frame.grid(row=0, column=0, sticky="w")
        self.start_collection_button.configure(text=state["button_text"])
        self.collection_notice_var.set(state["notice"])
        if self.data_type_var.get() == "holding":
            self.workers_hint_var.set("东方财富请求建议 2~4 线程，按年度返回季度表")
        else:
            self.workers_hint_var.set("深交所份额下载由浏览器自动分批")

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

    def _run(self, func, busy_status="采集中"):
        if self.busy:
            messagebox.showinfo("提示", "正在采集中，请稍等。")
            return
        self._set_busy_ui(True, busy_status)

        def worker():
            try:
                func()
            except Exception as exc:
                _write_crash_log("background task failed")
                self.log(f"失败: {exc}")
            finally:
                status = "已暂停" if self.paused_task or self.paused_pcf_task or getattr(self, "paused_holding_task", None) else "空闲"
                self.root.after(0, lambda: self._finish_background_task(status))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_background_task(self, status):
        self._set_busy_ui(False, status)
        self._refresh_stats()

    def _set_busy_ui(self, busy: bool, status: str | None = None):
        self.busy = busy
        self.task_status_var.set(status or ("采集中" if busy else "空闲"))
        for widget in self.task_input_widgets:
            if busy:
                widget.configure(state="disabled")
            elif widget is self.exchange_combo or widget is getattr(
                self, "db_path_entry", None
            ) or widget is getattr(self, "export_dir_entry", None):
                widget.configure(state="readonly")
            else:
                widget.configure(state="normal")
        self._update_continue_button()

    def _update_continue_button(self):
        state = (
            "normal"
            if not self.busy and (self.paused_pcf_task or getattr(self, "paused_holding_task", None) or self.paused_task)
            else "disabled"
        )
        self.continue_button.configure(state=state)

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

    def start_selected_collection(self):
        if self.data_type_var.get() == "holding":
            self.fetch_selected_holdings()
        elif self.data_type_var.get() == "component":
            self.fetch_selected_components()
        elif self.date_mode_var.get() == "single":
            self.fetch_single()
        else:
            self.fetch_range()

    def _pcf_code_or_none(self):
        code = self.pcf_code_var.get().strip()
        if code and not (len(code) == 6 and code.isascii() and code.isdigit()):
            messagebox.showerror("基金代码错误", "基金代码可留空；填写时请输入 6 位数字。")
            return None
        return code

    def _holding_years_or_none(self):
        start_text = self.holding_start_year_var.get().strip()
        end_text = self.holding_end_year_var.get().strip()
        if not (start_text.isdigit() and end_text.isdigit() and len(start_text) == 4 and len(end_text) == 4):
            messagebox.showerror("报告年度错误", "请输入 4 位数字的起止年度。")
            return None
        start_year, end_year = int(start_text), int(end_text)
        if start_year < 1990 or end_year > datetime.now().year + 1 or start_year > end_year:
            messagebox.showerror("报告年度错误", "起止年度无效，且开始年度不能晚于结束年度。")
            return None
        return start_year, end_year

    def fetch_selected_holdings(self):
        years = self._holding_years_or_none()
        if years is None:
            return
        code = self._pcf_code_or_none()
        if code is None:
            return
        replace_existing = bool(self.pcf_replace_var.get())
        if replace_existing and not messagebox.askyesno(
            "确认重新采集",
            "已有基金季度持仓快照将按基金和报告期整体替换，是否继续？",
        ):
            return
        self.paused_holding_task = None
        exchanges = tuple(self._selected_exchanges())
        self._run(
            lambda: self._fetch_selected_holdings(
                exchanges, years[0], years[1], code, replace_existing
            )
        )

    def _holding_tasks(self, exchanges, start_year, end_year, code):
        tasks = []
        for exchange in exchanges:
            if code:
                funds = [{"fund_code": code, "fund_name": ""}]
                known_exchanges = self.db.fund_exchanges(code)
                if known_exchanges and exchange not in known_exchanges:
                    continue
            else:
                funds = self.db.list_fund_codes(exchange)
            for year in range(start_year, end_year + 1):
                for fund in funds:
                    tasks.append((exchange, fund["fund_code"], fund.get("fund_name") or "", year))
        return tasks

    def _fetch_selected_holdings(self, exchanges, start_year, end_year, code, replace_existing):
        tasks = self._holding_tasks(exchanges, start_year, end_year, code)
        if not tasks:
            self.log("季度持仓采集没有找到符合条件的基金代码。")
            return
        workers = min(normalize_worker_count(self.workers_var.get()), 4)
        self.log(
            f"开始采集基金季度持仓：{len(tasks)} 个基金年度任务，线程 {workers}；"
            "东方财富每次年度请求可能返回多个季度。"
        )
        completed = 0
        written_reports = 0
        written_items = 0
        skipped_reports = 0
        completed_tasks = set()
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {
            executor.submit(
                fetch_eastmoney_holdings,
                fund_code,
                year,
                exchange=exchange,
                fund_name=fund_name,
                include_report_dates=True,
            ): task
            for task in tasks
            for exchange, fund_code, fund_name, year in [task]
        }
        try:
            for future in as_completed(futures):
                task = futures[future]
                exchange, fund_code, fund_name, year = task
                try:
                    rows = future.result()
                    grouped = {}
                    for row in rows:
                        grouped.setdefault(row["报告期"], []).append(row)
                    for report_period, snapshot in sorted(grouped.items()):
                        if not replace_existing and self.db.holding_snapshot_exists(
                            exchange, fund_code, report_period
                        ):
                            skipped_reports += 1
                            continue
                        report_count, item_count = self.db.replace_holding_snapshot(
                            snapshot, source="eastmoney_holding"
                        )
                        written_reports += report_count
                        written_items += item_count
                    completed += 1
                    completed_tasks.add(task)
                    if completed == 1 or completed % 25 == 0 or completed == len(tasks):
                        self.log(
                            f"基金季度持仓进度 {completed}/{len(tasks)}，最近处理 {fund_code} {year} 年。"
                        )
                except EastmoneyHoldingNoDataError as exc:
                    completed += 1
                    completed_tasks.add(task)
                    self.log(f"基金季度持仓 {fund_code} {year} 年重试后仍无股票持仓，已跳过：{exc}")
                    if completed == 1 or completed % 25 == 0 or completed == len(tasks):
                        self.log(f"基金季度持仓进度 {completed}/{len(tasks)}，最近跳过 {fund_code} {year} 年。")
                except EastmoneyHoldingError as exc:
                    self.paused_holding_task = {
                        "tasks": [item for item in tasks if item not in completed_tasks],
                        "replace": replace_existing,
                    }
                    self.log(f"基金季度持仓 {fund_code} {year} 年请求失败，已暂停：{exc}")
                    self.log("请先点“测试连接”；测通后点“继续暂停任务”，不会跳过失败任务。")
                    break
                except Exception as exc:
                    self.paused_holding_task = {
                        "tasks": [item for item in tasks if item not in completed_tasks],
                        "replace": replace_existing,
                    }
                    self.log(f"基金季度持仓 {fund_code} {year} 年入库失败，已暂停：{exc}")
                    break
        finally:
            for future in futures:
                if not future.done():
                    future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
        if self.paused_holding_task:
            self.log(
                f"基金季度持仓已暂停：写入 {written_reports} 份报告、{written_items} 条持仓，"
                f"跳过已有 {skipped_reports} 份报告。"
            )
        else:
            self.log(
                f"基金季度持仓采集完成：写入 {written_reports} 份报告、{written_items} 条持仓，"
                f"跳过已有 {skipped_reports} 份报告。"
            )

    def _szse_pcf_code_or_none(self):
        return self._pcf_code_or_none()

    def fetch_selected_components(self):
        date_mode = self.date_mode_var.get()
        single_date = self.date_var.get().strip()
        start_date = self.start_var.get().strip()
        end_date = self.end_var.get().strip()
        try:
            if date_mode == "single":
                self._validate_date(single_date)
            else:
                self._validate_date(start_date)
                self._validate_date(end_date)
        except ValueError:
            messagebox.showerror("日期错误", "请输入 YYYY-MM-DD 格式的日期。")
            return
        if date_mode == "range" and start_date > end_date:
            messagebox.showerror("日期错误", "开始日期不能晚于结束日期。")
            return

        code = self._pcf_code_or_none()
        if code is None:
            return
        replace_existing = bool(self.pcf_replace_var.get())
        if replace_existing and not messagebox.askyesno(
            "确认重新采集",
            "已有成分股快照将按基金和日期整体替换，是否继续？",
        ):
            return

        exchanges = tuple(self._selected_exchanges())
        self.paused_pcf_task = None
        self._run(
            lambda: self._fetch_selected_components(
                exchanges,
                date_mode,
                single_date,
                start_date,
                end_date,
                code,
                replace_existing,
            )
        )

    def _fetch_selected_components(
        self,
        exchanges,
        date_mode,
        single_date,
        start_date,
        end_date,
        code,
        replace_existing,
    ):
        for exchange in exchanges:
            if exchange == "SSE":
                self._fetch_sse_components(code, replace_existing)
            elif date_mode == "single":
                self._fetch_szse_pcf_current(
                    code,
                    replace_existing,
                    current_date=single_date,
                    fallback_pending=False,
                )
            else:
                self._fetch_szse_pcf_history(
                    start_date, end_date, code, replace_existing
                )
            if self.paused_pcf_task:
                break

    def _fetch_sse_components(self, code, replace_existing):
        funds = (
            [{"fund_code": code, "fund_name": ""}]
            if code
            else self.db.list_fund_codes("SSE")
        )
        if not funds:
            self.log("上交所成分股采集结束：ETF 表中没有上交所基金代码。")
            return

        configured_workers = normalize_worker_count(self.workers_var.get())
        workers = min(configured_workers, 4)
        self.log(
            f"开始采集上交所最新成分股：{len(funds)} 只基金，线程 {workers}，"
            "请求间隔 0.35 秒。"
        )
        completed = 0
        skipped = 0
        succeeded = 0
        failed = 0
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
                    content_date = info.get("内容日期")
                    fund_code = info.get("基金代码") or code
                    if (
                        not replace_existing
                        and self.db.pcf_is_complete("SSE", fund_code, content_date)
                    ):
                        skipped += 1
                        info_count = item_count = 0
                    else:
                        info_count, item_count = self.db.replace_pcf_snapshot(
                            info, items, source="sse_pcf"
                        )
                        succeeded += 1
                    written_info += info_count
                    written_items += item_count
                    completed += 1
                    if completed == 1 or completed % 20 == 0 or completed == len(funds):
                        self.log(
                            f"上交所成分股进度 {completed}/{len(funds)}，最近处理 {code}。"
                        )
                except Exception as exc:
                    completed += 1
                    failed += 1
                    self.log(f"上交所成分股 {code} 采集失败，已跳过：{exc}")
        self.log(
            f"上交所成分股采集完成：请求 {len(funds)} 只，成功 {succeeded} 只，"
            f"跳过完整快照 {skipped} 只，失败 {failed} 只；"
            f"信息 {written_info} 行，成分 {written_items} 行。"
        )

    def _fetch_szse_pcf_current(
        self, code, replace_existing, current_date=None, fallback_pending=True
    ):
        today = current_date or datetime.now().strftime("%Y-%m-%d")
        summary = self._fetch_szse_pcf_dates(
            [today],
            code,
            replace_existing,
            mode="current",
            fallback_pending=fallback_pending,
        )
        if summary is None or summary["discovered"] != 0 or not fallback_pending:
            return summary

        latest_date = self.db.latest_stock_trading_date(today)
        if latest_date and latest_date != today:
            self.log(f"深交所当前 PCF 当日未发现文件，改采最近股票交易日 {latest_date}。")
            return self._fetch_szse_pcf_dates(
                [latest_date],
                code,
                replace_existing,
                mode="current",
                fallback_pending=False,
            )
        return summary

    def _fetch_szse_pcf_history(self, start, end, code, replace_existing):
        dates = self.db.list_stock_trading_dates(start, end)
        if not dates:
            self.log("深交所历史 PCF 未找到股票交易日，未开始采集。")
            return None
        return self._fetch_szse_pcf_dates(
            dates, code, replace_existing, mode="history", fallback_pending=False
        )

    def _fetch_szse_pcf_dates(
        self, dates, code, replace_existing, *, mode="history", fallback_pending=False
    ):
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
            failure_index = dates.index(exc.trade_date) if exc.trade_date in dates else 0
            self.paused_pcf_task = {
                "mode": mode,
                "dates": list(dates[failure_index:]),
                "code": code,
                "replace": replace_existing,
                "fallback_pending": fallback_pending,
            }
            self.log(f"深交所 PCF {exc.trade_date} 页面查询失败，已暂停且不会跳过该日期: {exc}")
            self.log("请先点“测试连接”；测通后点“继续暂停任务”从该日期继续。")
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
                    self.log("请先点“测试连接”；测通后点“继续暂停任务”从该日期继续。")
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
            self.log("请先点“测试连接”；测通后点“继续暂停任务”从该日期继续。")
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

    def test_selected_connection(self):
        if self.data_type_var.get() == "holding":
            years = self._holding_years_or_none()
            if years is None:
                return
            test_date = str(years[1])
        else:
            test_date = (
                self.date_var.get().strip()
                if self.date_mode_var.get() == "single"
                else self.end_var.get().strip()
            )
            try:
                self._validate_date(test_date)
            except ValueError:
                messagebox.showerror("日期错误", "请输入 YYYY-MM-DD 格式的日期。")
                return
        code = ""
        if self.data_type_var.get() in {"component", "holding"}:
            code = self._pcf_code_or_none()
            if code is None:
                return
        data_type = self.data_type_var.get()
        exchanges = tuple(self._selected_exchanges())
        self._run(
            lambda: self._test_selected_connection(
                data_type, exchanges, test_date, code
            )
        )

    def _test_selected_connection(self, data_type, exchanges, test_date, code):
        if data_type == "holding":
            probe_year = int(test_date)
            for exchange in exchanges:
                probe_code = code
                if not probe_code:
                    funds = self.db.list_fund_codes(exchange)
                    if not funds:
                        self.log(f"{exchange} 季度持仓连接测试失败：ETF 表中没有基金代码。")
                        continue
                    probe_code = funds[0]["fund_code"]
                ok, message = check_eastmoney_holding_connection(probe_code, probe_year)
                self.log(f"{exchange} {message}")
            return
        for exchange in exchanges:
            if data_type == "share":
                if exchange == "SZSE":
                    _ok, message = check_szse_download_connection(test_date)
                else:
                    _ok, message = check_sse_connection(test_date)
                self.log(message)
                continue

            if exchange == "SZSE":
                _ok, message = check_szse_pcf_connection(test_date)
                self.log(message)
                continue

            probe_code = code
            if not probe_code:
                funds = self.db.list_fund_codes("SSE")
                if not funds:
                    self.log("上交所成分股连接测试失败：ETF 表中没有上交所基金代码。")
                    continue
                probe_code = funds[0]["fund_code"]
            try:
                info, items = fetch_sse_pcf_for_fund(probe_code)
                self.log(
                    f"上交所成分股连接正常：{probe_code}，"
                    f"内容日期 {info.get('内容日期') or '-'}，成分 {len(items)} 行。"
                )
            except Exception as exc:
                self.log(f"上交所成分股连接失败：{probe_code}，{exc}")

    def continue_paused_task(self):
        if not self.paused_pcf_task and not getattr(self, "paused_holding_task", None) and not self.paused_task:
            return
        self._run(self._continue_paused_task)

    def _continue_paused_task(self):
        if self.paused_pcf_task:
            task = self.paused_pcf_task
            dates = list(task["dates"])
            if not dates:
                self.log("深交所 PCF 暂停任务没有待采日期，无法继续采集。")
                return
            current = dates[0]
            ok, message = check_szse_pcf_connection(current)
            self.log(message)
            if not ok:
                return
            self.log(f"继续采集深交所 PCF: {dates[0]} ~ {dates[-1]}。")
            mode = task.get("mode", "history")
            fallback_pending = task.get("fallback_pending", False)
            if mode == "current":
                self._fetch_szse_pcf_current(
                    task["code"],
                    task["replace"],
                    current_date=dates[0],
                    fallback_pending=fallback_pending,
                )
            else:
                self._fetch_szse_pcf_dates(
                    dates,
                    task["code"],
                    task["replace"],
                    mode=mode,
                    fallback_pending=fallback_pending,
                )
            if self.paused_pcf_task is task:
                self.paused_pcf_task = None
            return

        if getattr(self, "paused_holding_task", None):
            task = self.paused_holding_task
            pending = list(task.get("tasks") or [])
            if not pending:
                self.log("季度持仓暂停任务没有待采任务，无法继续采集。")
                return
            exchange, fund_code, _fund_name, year = pending[0]
            ok, message = check_eastmoney_holding_connection(fund_code, year)
            self.log(message)
            if not ok:
                return
            self.log(f"继续采集基金季度持仓：剩余 {len(pending)} 个基金年度任务。")
            self.paused_holding_task = None
            self._fetch_holding_tasks(pending, task.get("replace", False))
            return

        if not self.paused_task:
            return

        if len(self.paused_task) == 3:
            mode, current, end = self.paused_task
            exchange = "SSE"
        else:
            mode, exchange, current, end = self.paused_task
        if exchange == "SZSE":
            ok, message = check_szse_download_connection(current)
        else:
            ok, message = check_sse_connection(current)
        self.log(message)
        if not ok:
            return

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

    def _fetch_holding_tasks(self, tasks, replace_existing):
        if not tasks:
            return
        exchanges = tuple(dict.fromkeys(task[0] for task in tasks))
        start_year = min(task[3] for task in tasks)
        end_year = max(task[3] for task in tasks)
        # 续采使用精确的剩余任务列表，避免根据当前数据库状态重新推导而漏掉失败任务。
        workers = min(normalize_worker_count(self.workers_var.get()), 4)
        completed = 0
        completed_tasks = set()
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {
            executor.submit(
                fetch_eastmoney_holdings,
                fund_code,
                year,
                exchange=exchange,
                fund_name=fund_name,
                include_report_dates=True,
            ): task
            for task in tasks
            for exchange, fund_code, fund_name, year in [task]
        }
        try:
            for future in as_completed(futures):
                task = futures[future]
                exchange, fund_code, _fund_name, year = task
                try:
                    rows = future.result()
                    grouped = {}
                    for row in rows:
                        grouped.setdefault(row["报告期"], []).append(row)
                    for report_period, snapshot in grouped.items():
                        if not replace_existing and self.db.holding_snapshot_exists(
                            exchange, fund_code, report_period
                        ):
                            continue
                        self.db.replace_holding_snapshot(snapshot, source="eastmoney_holding")
                    completed += 1
                    completed_tasks.add(task)
                    if completed == 1 or completed % 25 == 0 or completed == len(tasks):
                        self.log(f"季度持仓续采进度 {completed}/{len(tasks)}，最近处理 {fund_code} {year} 年。")
                except EastmoneyHoldingNoDataError as exc:
                    completed += 1
                    completed_tasks.add(task)
                    self.log(f"季度持仓续采 {fund_code} {year} 年重试后仍无股票持仓，已跳过：{exc}")
                except Exception as exc:
                    self.paused_holding_task = {
                        "tasks": [item for item in tasks if item not in completed_tasks],
                        "replace": replace_existing,
                    }
                    self.log(f"季度持仓续采在 {fund_code} {year} 年暂停：{exc}")
                    self.log("失败任务已保留，不会自动跳过。")
                    break
        finally:
            for future in futures:
                if not future.done():
                    future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
        if not self.paused_holding_task:
            self.log(f"季度持仓续采完成：处理 {completed} 个基金年度任务。")

    def choose_export_directory(self):
        current = Path(self.export_dir_var.get().strip()).expanduser()
        initial_dir = current if current.is_dir() else self.db_path.parent
        selected = filedialog.askdirectory(
            title="选择 ETF 份额导出目录",
            initialdir=str(initial_dir),
            mustexist=True,
        )
        if selected:
            self.export_dir_var.set(str(Path(selected).expanduser().resolve()))

    def start_share_export(self):
        start = self.export_start_var.get().strip()
        end = self.export_end_var.get().strip()
        try:
            self._validate_date(start)
            self._validate_date(end)
        except ValueError:
            messagebox.showerror("日期错误", "请输入 YYYY-MM-DD 格式的日期。")
            return
        if start > end:
            messagebox.showerror("日期错误", "导出开始日期不能晚于结束日期。")
            return

        output_dir = Path(self.export_dir_var.get().strip()).expanduser()
        if not self.export_dir_var.get().strip() or not output_dir.is_dir():
            messagebox.showerror("导出目录错误", "请先选择一个存在的导出文件夹。")
            return

        self._run(
            lambda: self._export_share_data(start, end, output_dir),
            busy_status="导出中",
        )

    def _export_share_data(self, start: str, end: str, output_dir: Path):
        self.log(f"开始导出 ETF 份额：{start} ~ {end}。")
        self.log(f"导出目录：{output_dir}")
        stats = self.db.export_share_csv_files(start, end, output_dir)
        labels = {"SSE": "上交所", "SZSE": "深交所"}
        total_files = 0
        total_rows = 0
        for exchange in ("SSE", "SZSE"):
            files = stats[exchange]["files"]
            rows = stats[exchange]["rows"]
            total_files += files
            total_rows += rows
            self.log(f"{labels[exchange]}导出完成：{files} 个 CSV 文件，{rows} 行。")
        if total_files:
            self.log(f"ETF 份额导出完成：共 {total_files} 个文件，{total_rows} 行。")
        else:
            self.log("所选日期范围没有可导出的 ETF 份额数据。")

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

    def choose_database(self):
        if self.busy:
            messagebox.showinfo("提示", "正在采集中，请先等待任务结束。")
            return
        if self.paused_task or self.paused_pcf_task or self.paused_holding_task:
            messagebox.showinfo("提示", "当前有暂停任务，请先继续或结束任务后再切换数据库。")
            return

        selected = filedialog.askopenfilename(
            title="选择 ETF 数据库",
            initialdir=str(self.db_path.parent),
            initialfile=self.db_path.name,
            filetypes=[
                ("SQLite 数据库", "*.db *.sqlite *.sqlite3"),
                ("所有文件", "*.*"),
            ],
        )
        if not selected:
            return

        new_path = Path(selected).expanduser().resolve()
        if new_path == self.db_path.expanduser().resolve():
            return
        try:
            new_db = ETFDatabase(new_path)
            new_db.initialize()
        except Exception as exc:
            messagebox.showerror("数据库切换失败", f"无法打开数据库：{exc}")
            return

        try:
            if self.server.server:
                self.server.stop()
            self.server.db_path = new_path
            self.db_path = new_path
            self.db = new_db
            self.db_path_var.set(str(new_path))
            save_database_path(new_path)
        except Exception as exc:
            messagebox.showerror("数据库切换失败", f"数据库已打开，但保存设置失败：{exc}")
            self.log(f"数据库已切换，但保存路径设置失败：{exc}")
        else:
            self.log(f"数据库已切换：{new_path}")
        self._refresh_stats()

    def diagnose(self):
        self.log("网络诊断:")
        for line in diagnose_network():
            self.log("  " + line)

    def _refresh_stats(self):
        if hasattr(self, "root") and hasattr(self, "coverage_vars"):
            if getattr(self, "_stats_loading", False):
                return
            self._stats_loading = True
            for variable in self.coverage_vars.values():
                variable.set("统计加载中...")

            def load_coverage():
                try:
                    coverage = self.db.get_collection_coverage()
                    error = None
                except Exception as exc:
                    coverage = None
                    error = exc
                try:
                    self.root.after(0, lambda: self._apply_coverage(coverage, error))
                except Exception:
                    pass

            threading.Thread(target=load_coverage, daemon=True).start()
            return

        coverage = self.db.get_collection_coverage()
        self._apply_coverage(coverage, None)

    def _apply_coverage(self, coverage, error):
        self._stats_loading = False
        if error is not None:
            for variable in self.coverage_vars.values():
                variable.set("读取失败")
            self.log(f"覆盖范围统计失败：{error}")
            return
        for kind in ("share", "component", "holding"):
            for exchange in ("SSE", "SZSE"):
                variable = self.coverage_vars.get((kind, exchange))
                if variable is not None:
                    variable.set(format_coverage_cell(kind, coverage[kind][exchange]))

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

