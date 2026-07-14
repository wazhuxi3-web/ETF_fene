# ETF Unified Collection UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the separate share and PCF forms with one consistent collection console that shares date/exchange inputs, exposes task-specific controls, shows per-market data coverage, and preserves all existing collection behavior.

**Architecture:** Keep the existing Tkinter application and collectors. Add one grouped database coverage query, a small pure UI-state helper, and one GUI dispatch path that routes the selected data type and exchange to the existing share/SSE-PCF/SZSE-PCF implementations. Refactor connectivity, resume, busy-state, and statistics presentation around that shared selection without changing the web server.

**Tech Stack:** Python 3, Tkinter/ttk, SQLite, `unittest`, existing SSE/SZSE fetchers and Playwright integration.

## Global Constraints

- 网页曲线区域及其地址、端口功能保持不变。
- 日期模式、日期输入和交易所下拉框在份额与成分股之间共用。
- 交易所选项固定为`上交所`、`深交所`、`沪深两市`。
- 上交所成分股只更新最新快照；日期控件保留并明确提示日期范围不适用。
- 深交所成分股单日或区间日期来自共同日期控件；历史交易日来自 `stock_daily`。
- 沪深两市成分股任务只执行一次上交所最新快照，再执行深交所所选日期。
- 基金代码留空表示全部；填写时必须是 6 位 ASCII 数字。
- 默认跳过完整成分快照；覆盖模式使用原子快照替换。
- 不新增数据库业务表或业务字段。
- 网络或页面级失败暂停当前日期，不自动进入下一日期。
- 日志区域保持独立滚动，采集期间不允许工作线程直接操作 Tk 控件。

---

### Task 1: Add grouped collection coverage statistics

**Files:**
- Modify: `etf_database.py:557-568`
- Modify: `tests/test_etf_app.py:1711-1770,1994-2070`
- Test: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: existing `ETF`, `ETF_INFO`, and `ETF_ITEM` tables.
- Produces: `ETFDatabase.get_collection_coverage() -> dict[str, dict[str, dict]]`.

- [ ] **Step 1: Write failing grouped-coverage tests**

Add a `CollectionCoverageDatabaseTests` class using a temporary SQLite database. Seed both exchanges with unequal date ranges, two PCF snapshots, and several items. Assert the complete return shape:

```python
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
```

Add an empty-database test and require all four cells to exist with `None` dates and zero counts rather than omitted keys.

- [ ] **Step 2: Run the database tests and verify RED**

Run:

```powershell
python -B -m unittest tests.test_etf_app.CollectionCoverageDatabaseTests -v
```

Expected: `AttributeError: 'ETFDatabase' object has no attribute 'get_collection_coverage'`.

- [ ] **Step 3: Implement grouped coverage without join inflation**

Add this public method to `ETFDatabase`:

```python
def get_collection_coverage(self) -> dict[str, dict[str, dict]]:
    result = {
        "share": {
            exchange: {
                "min_date": None,
                "max_date": None,
                "rows_count": 0,
                "fund_count": 0,
                "date_count": 0,
            }
            for exchange in ("SSE", "SZSE")
        },
        "component": {
            exchange: {
                "min_date": None,
                "max_date": None,
                "snapshot_count": 0,
                "fund_count": 0,
                "item_count": 0,
            }
            for exchange in ("SSE", "SZSE")
        },
    }
```

Use one grouped query over `ETF`, one grouped query over `ETF_INFO`, and one grouped query over `ETF_ITEM`. Do not join `ETF_INFO` to `ETF_ITEM`, because that would multiply snapshot counts. Merge rows into the pre-populated `SSE`/`SZSE` result and cast all counts to `int`.

The share query must compute `MIN(trade_date)`, `MAX(trade_date)`, `COUNT(*)`, `COUNT(DISTINCT fund_code)`, and `COUNT(DISTINCT trade_date)`. The info query must compute min/max content date, snapshot count, and distinct fund count. The item query supplies component row count only.

- [ ] **Step 4: Run focused and existing database tests GREEN**

```powershell
python -B -m unittest tests.test_etf_app.CollectionCoverageDatabaseTests tests.test_etf_app.ETFDatabaseTests tests.test_etf_app.PCFDatabaseTests -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit coverage statistics**

```powershell
git add etf_database.py tests/test_etf_app.py
git -c user.name="Codex" -c user.email="codex@local" commit -m "feat: add ETF collection coverage stats"
```

---

### Task 2: Build the unified form and component dispatch

**Files:**
- Modify: `etf_gui.py:115-224`
- Modify: `tests/test_etf_app.py:2214-2547`
- Test: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: existing date/exchange/worker/PCF variables, `ETFDatabase.pcf_is_complete`, `ETFDatabase.replace_pcf_snapshot`, the SSE PCF parser, and the SZSE browser collector.
- Produces: `collection_panel_state(data_type, date_mode, exchange_label) -> dict`, `ETFApp._sync_collection_panel()`, `ETFApp.start_selected_collection()`, `ETFApp.fetch_selected_components()`, `ETFApp._fetch_selected_components(...)`, and `ETFApp._fetch_sse_components(...)`.

- [ ] **Step 1: Write failing pure UI-state tests**

Import and test a new module-level helper without constructing Tk:

```python
from etf_gui import collection_panel_state

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
```

Add source assertions that the old PCF action button labels are absent from `_build_ui`, while `ETF 份额`, `ETF 成分股`, `共同采集范围`, and `数据库覆盖范围` are present.

- [ ] **Step 2: Run the GUI state tests and verify RED**

```powershell
python -B -m unittest tests.test_etf_app.PCFGuiTests -v
```

Expected: import failure for `collection_panel_state` and missing new labels.

- [ ] **Step 3: Implement the pure state helper**

Add before `ETFApp`:

```python
def collection_panel_state(data_type: str, date_mode: str, exchange_label: str) -> dict:
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
```

- [ ] **Step 4: Replace the two collection frames with one stable form**

In `__init__`, add:

```python
self.date_mode_var = tk.StringVar(value="single")
self.data_type_var = tk.StringVar(value="share")
self.task_status_var = tk.StringVar(value="空闲")
```

In `_build_ui`:

- Rename the collection frame to `ETF 数据采集`.
- Add one `共同采集范围` band with single/range radiobuttons, shared date inputs, and the existing exchange combobox.
- Add one `采集内容` band with share/component radiobuttons.
- Add a fixed-height parameter host containing `workers_frame` and `pcf_options_frame`; switch them with `grid()`/`grid_remove()` rather than recreating widgets.
- Bind date mode, data type, and exchange changes to `_sync_collection_panel`.
- Create one `self.start_collection_button` whose command is `start_selected_collection`.
- Keep the web frame unchanged.
- Set `root.geometry("880x640")` and `root.minsize(820, 600)` so the statistics matrix and log do not overlap.

`_sync_collection_panel` must only run on the Tk thread and use `collection_panel_state` to show/hide fields, set button text, and update a dedicated notice `StringVar`. It must never clear an input value.

- [ ] **Step 5: Write failing unified dispatch tests**

Construct `ETFApp` with `__new__` and simple fake variables. Patch `_run`, `_fetch_sse_components`, `_fetch_szse_pcf_current`, and `_fetch_szse_pcf_history`. Assert:

```python
# SSE + range: latest SSE once, no SZSE call.
app.exchange_var = FakeVar("上交所")
app.date_mode_var = FakeVar("range")
app.fetch_selected_components()
run_task = app._run.call_args.args[0]
run_task()
app._fetch_sse_components.assert_called_once_with("", False)

# Both + range: SSE once, then SZSE selected range.
app.exchange_var = FakeVar("沪深两市")
app.start_var = FakeVar("2026-07-01")
app.end_var = FakeVar("2026-07-14")
run_task()
app._fetch_sse_components.assert_called_once()
app._fetch_szse_pcf_history.assert_called_once_with(
    "2026-07-01", "2026-07-14", "", False
)
```

Add tests for deep single-date dispatch, optional blank code, six ASCII digits, full-width digit rejection, invalid date order, overwrite confirmation, and stopping before SZSE if SSE creates a paused task.

- [ ] **Step 6: Run the dispatch tests and verify RED**

```powershell
python -B -m unittest tests.test_etf_app.PCFGuiTests -v
```

Expected: missing `fetch_selected_components`/`_fetch_sse_components` failures.

- [ ] **Step 7: Implement one complete top-level dispatch**

```python
def start_selected_collection(self):
    if self.data_type_var.get() == "component":
        self.fetch_selected_components()
    elif self.date_mode_var.get() == "single":
        self.fetch_single()
    else:
        self.fetch_range()
```

`fetch_selected_components` must validate shared dates, reject `start > end`, validate the optional code, request confirmation when overwrite is selected, clear only `paused_pcf_task`, capture all current values, and pass one closure to `_run`.

`_fetch_selected_components` must iterate `_selected_exchanges()` in order and call:

```python
if exchange == "SSE":
    self._fetch_sse_components(code, replace_existing)
elif date_mode == "single":
    self._fetch_szse_pcf_current(
        code, replace_existing, current_date=single_date, fallback_pending=True
    )
else:
    self._fetch_szse_pcf_history(start_date, end_date, code, replace_existing)
```

Break immediately if `paused_pcf_task` is set.

- [ ] **Step 8: Refactor SSE single/all collection into one method**

`_fetch_sse_components(code, replace_existing)` must derive the code list from the explicit code or `db.list_fund_codes("SSE")`. Preserve the existing maximum four-worker behavior and 0.35-second request pacing.

For each downloaded `(info, items)`:

```python
content_date = info.get("内容日期")
fund_code = info.get("基金代码")
if (
    not replace_existing
    and self.db.pcf_is_complete("SSE", fund_code, content_date)
):
    skipped += 1
else:
    info_count, item_count = self.db.replace_pcf_snapshot(
        info, items, source="sse_pcf"
    )
```

This makes both single and all-fund SSE paths idempotent and removes stale components during overwrite. Log requested, skipped, succeeded, failed, information rows, and item rows. Remove the obsolete public PCF button handlers after no caller remains.

- [ ] **Step 9: Run focused GUI and collector regressions GREEN**

```powershell
python -B -m unittest tests.test_etf_app.PCFGuiTests tests.test_etf_app.ETFGuiTests tests.test_etf_app.PCFParserTests tests.test_etf_app.SZSEPCFCollectorTests -v
```

Expected: helper, layout, share dispatch, component dispatch, and collector tests all pass.

- [ ] **Step 10: Commit the unified form and dispatch**

```powershell
git add etf_gui.py tests/test_etf_app.py
git -c user.name="Codex" -c user.email="codex@local" commit -m "feat: unify ETF collection controls"
```

---

### Task 3: Display coverage and separate connection/resume actions

**Files:**
- Modify: `etf_gui.py:190-224,232-263,569-687`
- Modify: `tests/test_etf_app.py:2214-2700`
- Test: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: `ETFDatabase.get_collection_coverage()`, selected content/exchange, existing network checks, `paused_task`, and `paused_pcf_task`.
- Produces: `format_coverage_cell(...)`, `ETFApp.test_selected_connection()`, `ETFApp.continue_paused_task()`, and `ETFApp._set_busy_ui(...)`.

- [ ] **Step 1: Write failing coverage-format tests**

Add and import:

```python
from etf_gui import format_coverage_cell
```

Assert:

```python
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
    format_coverage_cell("component", {"min_date": None}),
    "暂无数据",
)
```

- [ ] **Step 2: Write failing toolbar behavior tests**

Use fake vars and patched connection functions to verify:

- Share mode tests SSE/SZSE share sources according to the selected exchange.
- Component mode with SSE uses the entered fund code or the first `db.list_fund_codes("SSE")` code for an in-memory `fetch_sse_pcf_for_fund` probe and performs no database write.
- Component mode with SZSE calls `check_szse_pcf_connection` using the shared single/end date.
- `continue_paused_task` resumes PCF before share only when PCF is actually paused; otherwise it resumes the existing share tuple.
- The continue button is disabled when neither pause state exists.

- [ ] **Step 3: Run focused tests and verify RED**

```powershell
python -B -m unittest tests.test_etf_app.PCFGuiTests tests.test_etf_app.ETFGuiTests -v
```

Expected: missing formatter and split action methods.

- [ ] **Step 4: Add the four-cell coverage matrix**

Add four `StringVar` cells keyed by `(share/component, SSE/SZSE)` and create a compact two-row, two-market matrix under the dynamic parameters. Preserve the manual `刷新统计` button.

Implement:

```python
def format_coverage_cell(kind: str, stats: dict) -> str:
    if not stats or not stats.get("min_date"):
        return "暂无数据"
    if kind == "share":
        return (
            f"{stats['min_date']} ~ {stats['max_date']} | "
            f"{stats['rows_count']} 行 / {stats['fund_count']} 只 / "
            f"{stats['date_count']} 日"
        )
    return (
        f"{stats['min_date']} ~ {stats['max_date']} | "
        f"{stats['snapshot_count']} 快照 / {stats['fund_count']} 只 / "
        f"{stats['item_count']} 成分"
    )
```

Refactor `_refresh_stats` to call `get_collection_coverage` once and update all four vars. It must run only through `root.after` when called from a worker completion path.

- [ ] **Step 5: Split connection testing from pause continuation**

Replace the combined toolbar command with:

```python
ttk.Button(..., text="测试连接", command=self.test_selected_connection)
self.continue_button = ttk.Button(
    ..., text="继续暂停任务", command=self.continue_paused_task
)
```

`test_selected_connection` starts one background task that probes the currently selected content type and exchange(s), logs each result, and never clears or resumes a paused task.

`continue_paused_task` preserves the existing tested resume algorithms, including PCF mode/fallback state and share date tuples. It may perform the existing connectivity probe before resuming, but it must not use current UI values to replace saved pause values.

- [ ] **Step 6: Add main-thread busy/status control**

Refactor `_run` so status changes are scheduled on Tk's main thread:

```python
def _set_busy_ui(self, busy: bool, status: str | None = None):
    self.busy = busy
    self.task_status_var.set(status or ("采集中" if busy else "空闲"))
    state = "disabled" if busy else "normal"
    for widget in self.task_input_widgets:
        widget.configure(state=state)
    self._update_continue_button()
```

When a worker finishes, derive final status as `已暂停` if either pause object exists, otherwise `空闲`; schedule `_set_busy_ui(False, status)` and `_refresh_stats` with `root.after`. The log widget and its scrollbar must not be disabled.

- [ ] **Step 7: Run GUI and database tests GREEN**

```powershell
python -B -m unittest tests.test_etf_app.CollectionCoverageDatabaseTests tests.test_etf_app.PCFGuiTests tests.test_etf_app.ETFGuiTests -v
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit coverage display and toolbar behavior**

```powershell
git add etf_gui.py tests/test_etf_app.py
git -c user.name="Codex" -c user.email="codex@local" commit -m "feat: show ETF coverage and task status"
```

---

### Task 4: Complete regression and GUI smoke verification

**Files:**
- Verify: `etf_database.py`
- Verify: `etf_gui.py`
- Verify: `etf_web_app.py`
- Verify: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: all completed tasks.
- Produces: evidence that the unified GUI is syntactically valid, behaviorally tested, and does not alter the web module.

- [ ] **Step 1: Compile all application modules**

```powershell
python -B -m py_compile etf_database.py etf_fetcher.py szse_download_fetcher.py sse_pcf_fetcher.py szse_pcf_fetcher.py etf_web_app.py etf_gui.py
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Run the complete unit suite**

```powershell
python -B -m unittest tests.test_etf_app -v
```

Expected: every test passes with no traceback or warning introduced by the GUI work.

- [ ] **Step 3: Run non-destructive GUI construction smoke test**

Use a temporary database and patch `DEFAULT_DB_PATH` before constructing the app. Create `tk.Tk()`, call `root.update_idletasks()`, and assert:

```python
self.assertEqual(app.root.title(), "ETF份额采集与曲线")
self.assertEqual(app.start_collection_button.cget("text"), "开始采集份额")
self.assertEqual(app.task_status_var.get(), "空闲")
self.assertEqual(app.web_host_var.get(), "127.0.0.1")
self.assertEqual(app.web_port_var.get(), "1234")
```

Switch to component mode, call `_sync_collection_panel`, and assert the button changes to `开始采集成分股` while all date values remain unchanged. Destroy the root in `finally`. Skip this smoke only when Tk reports that no display is available; unit behavior tests remain mandatory.

- [ ] **Step 4: Verify no web behavior changed**

```powershell
python -B -m unittest tests.test_etf_app.ETFWebServerTests -v
```

Expected: all web server/chart tests pass.

- [ ] **Step 5: Check repository cleanliness**

```powershell
git diff --check
git status --short
```

Expected: no cache files, temporary databases, browser profiles, or uncommitted source changes.
