# 深交所 ETF PCF 采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 GUI 中增加后台浏览器方式的深交所当前及历史 PCF 采集，并把每日完整快照安全写入现有 `ETF_INFO`、`ETF_ITEM`。

**Architecture:** 新增独立 `szse_pcf_fetcher.py`，把纯解析、浏览器会话和按日流式采集分开；`etf_database.py` 提供真实交易日查询与单快照原子替换；`etf_gui.py` 只负责输入校验、调用采集器、暂停续采和汇总日志。采集器通过隐藏 Playwright 页面完成查询及下载，不并发下载，每只基金解析后立即提交。

**Tech Stack:** Python 3、标准库 `xml.etree.ElementTree`/`re`/`dataclasses`、Playwright sync API、SQLite、Tkinter、`unittest`。

## Global Constraints

- 数据源固定为 `https://www.szse.cn/disclosure/fund/currency/index.html`，报表 `sgshqd`。
- 继续使用现有 `ETF_INFO`、`ETF_ITEM` 业务列，不新增深交所专有列。
- `交易所` 固定写 `SZSE`，`source` 固定写 `szse_pcf_browser`。
- 当前日和历史区间都支持；历史交易日来自 `stock_daily`。
- 后台浏览器固定 `headless=True`，除非测试显式传入 `visible=True`。
- 文件按顺序下载，不使用并发；文件间隔 0.8 至 1.8 秒。
- 单文件最多重试三次，退避为 2、5、10 秒。
- 默认跳过完整快照；强制重采时原子替换同基金同日期全部成分。
- 同一天页面级失败后暂停，不自动前进到下一交易日。

---

### Task 1: Add SZSE PCF pure parsers

**Files:**
- Create: `szse_pcf_fetcher.py`
- Modify: `tests/test_etf_app.py:1-45`
- Test: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: `normalize_pcf_info(raw, exchange="SZSE")`、`normalize_pcf_item(raw, exchange="SZSE")` from `sse_pcf_fetcher.py`.
- Produces: `SZSEPCFReference`、`extract_szse_pcf_references(payload)`、`choose_szse_substitute_amount(creation, redemption)`、`parse_szse_pcf_download(raw)`。

- [ ] **Step 1: Write failing XML and reference tests**

Add imports:

```python
from szse_pcf_fetcher import (
    SZSEPCFReference,
    choose_szse_substitute_amount,
    extract_szse_pcf_references,
    parse_szse_pcf_download,
)
```

Add `SZSEPCFParserTests` with a namespaced XML fixture containing `SecurityID=159915`, `TradingDay=20260714`, `SecurityIDSource=102`, one component, `CreationCashSubstitute=12.5`, and `RedemptionCashSubstitute=12.5`. Assert:

```python
info, items, mismatches = parse_szse_pcf_download(xml)
self.assertEqual(info["交易所"], "SZSE")
self.assertEqual(info["基金代码"], "159915")
self.assertEqual(info["内容日期"], "2026-07-14")
self.assertEqual(items[0]["证券代码"], "300001")
self.assertEqual(items[0]["挂牌市场"], "SZSE")
self.assertEqual(items[0]["替代金额"], 12.5)
self.assertEqual(mismatches, 0)
```

Test report reference extraction with:

```python
payload = [{"data": [{"jjdm": (
    "<a href='/modules/report/views/eft_download_new.html?"
    "path=%2Ffiles%2Ftext%2FETFDown%2F&"
    "filename=pcf_159915_20260714%3B159915ETF20260714&"
    "opencode=ETF15991520260714.txt'>下载</a>"
)}]}]
refs = extract_szse_pcf_references(payload)
self.assertEqual(refs, [SZSEPCFReference("159915", "2026-07-14", refs[0].download_url)])
self.assertIn("eft_download_new.html", refs[0].download_url)
```

- [ ] **Step 2: Run the parser tests and verify RED**

Run:

```powershell
python -B -m unittest tests.test_etf_app.SZSEPCFParserTests -v
```

Expected: import failure because `szse_pcf_fetcher.py` does not exist.

- [ ] **Step 3: Implement XML parsing and reference extraction**

Create these exact public definitions:

```python
@dataclass(frozen=True)
class SZSEPCFReference:
    fund_code: str
    content_date: str
    download_url: str

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

def extract_szse_pcf_references(payload: list[dict]) -> list[SZSEPCFReference]:
    # Read every jjdm HTML fragment, extract the official download-page href,
    # and derive the six-digit fund code and YYYY-MM-DD date from filename.
```

Use namespace-agnostic XML local names and map exactly the fields documented in the design. `UnderlyingSecurityIDSource` maps `101 -> SSE`, `102 -> SZSE`, otherwise the original value. Build `申购赎回的允许情况` as `申购:Y/N；赎回:Y/N`. Preserve `SubstituteFlag` as source text. Return `(info, items, mismatch_count)`.

- [ ] **Step 4: Add failing substitute and legacy TXT tests**

Add three assertions:

```python
self.assertEqual(choose_szse_substitute_amount("10", "10"), (10.0, False))
self.assertEqual(choose_szse_substitute_amount("0", "12"), (12.0, False))
self.assertEqual(choose_szse_substitute_amount("10", "12"), (10.0, True))
```

Add a `Version=2.0`/`TAGTAG`/pipe-delimited TXT fixture with one component and assert the same normalized fields as XML.

- [ ] **Step 5: Run tests and verify RED for legacy TXT**

Run the parser test class again. Expected: XML/reference tests pass and legacy TXT test fails with unsupported format.

- [ ] **Step 6: Implement legacy TXT detection and parsing**

`parse_szse_pcf_download(raw)` must decode bytes as UTF-8 BOM first, then GB18030 fallback; dispatch XML when trimmed content starts with `<`, and dispatch legacy format when it contains both `TradingDay=` and `TAGTAG`. Any other payload raises `ValueError("深交所 PCF 文件格式无法识别")`.

- [ ] **Step 7: Run parser tests GREEN**

Run:

```powershell
python -B -m unittest tests.test_etf_app.SZSEPCFParserTests -v
```

Expected: all parser tests pass.

- [ ] **Step 8: Commit parser work**

```powershell
git add szse_pcf_fetcher.py tests/test_etf_app.py
git -c user.name="Codex" -c user.email="codex@local" commit -m "feat: parse SZSE ETF PCF files"
```

---

### Task 2: Add trading-day lookup and atomic snapshot replacement

**Files:**
- Modify: `etf_database.py:163-251`
- Modify: `tests/test_etf_app.py:947-1039`
- Test: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: existing `PCF_INFO_COLUMNS`、`PCF_ITEM_COLUMNS` and SQLite connection helper.
- Produces: `list_stock_trading_dates(start_date, end_date)`、`latest_stock_trading_date(on_or_before)`、`replace_pcf_snapshot(info, items, source)`。

- [ ] **Step 1: Write failing trading-date tests**

Create a temporary `stock_daily("日期" INTEGER)` table with duplicate rows for `20260710` and `20260713`. Assert:

```python
self.assertEqual(
    db.list_stock_trading_dates("2026-07-10", "2026-07-14"),
    ["2026-07-10", "2026-07-13"],
)
self.assertEqual(db.latest_stock_trading_date("2026-07-14"), "2026-07-13")
```

Also assert an absent `stock_daily` table returns an empty list/`None`, allowing the GUI to report a clear message rather than crashing.

- [ ] **Step 2: Run trading-date tests RED**

Run:

```powershell
python -B -m unittest tests.test_etf_app.PCFDatabaseTests -v
```

Expected: `AttributeError` for the two missing database methods.

- [ ] **Step 3: Implement trading-date queries**

Use integer bounds `int(start_date.replace("-", ""))` and `int(end_date.replace("-", ""))`. Convert each `日期` value with:

```python
digits = str(row["trade_date"]).split(".", 1)[0].zfill(8)
formatted = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
```

Query `SELECT DISTINCT "日期" AS trade_date ... ORDER BY "日期"`; `latest_stock_trading_date` uses `MAX("日期")` with the same conversion.

- [ ] **Step 4: Write failing snapshot replacement test**

Insert one info row and two item rows with `replace_pcf_snapshot`. Replace the same key with one remaining item and assert:

```python
self.assertTrue(db.pcf_is_complete("SZSE", "159915", "2026-07-14"))
self.assertEqual(item_codes, ["300001"])
self.assertEqual(info_source, "szse_pcf_browser")
self.assertEqual(item_source, "szse_pcf_browser")
```

Add a second date before replacement and assert it remains unchanged.

- [ ] **Step 5: Run snapshot test RED**

Expected: `AttributeError` for `replace_pcf_snapshot`.

- [ ] **Step 6: Implement atomic replacement and generic source**

Add:

```python
def replace_pcf_snapshot(
    self,
    info: dict,
    items: list[dict],
    source: str = "sse_pcf",
) -> tuple[int, int]:
```

Validate one info row, at least one item, and identical `(交易所, 基金代码, 内容日期)` keys before opening the transaction. Inside one connection: delete matching `ETF_ITEM`, upsert `ETF_INFO` using `source`, insert/upsert every item using the same `source`, then commit. Roll back automatically on any exception. Refactor `upsert_pcf` only enough to stop hardcoding `sse_pcf`; preserve all existing callers and behavior.

- [ ] **Step 7: Run database tests GREEN**

```powershell
python -B -m unittest tests.test_etf_app.PCFDatabaseTests -v
```

Expected: all PCF database tests pass.

- [ ] **Step 8: Commit database work**

```powershell
git add etf_database.py tests/test_etf_app.py
git -c user.name="Codex" -c user.email="codex@local" commit -m "feat: replace PCF snapshots atomically"
```

---

### Task 3: Add hidden browser collection and retry flow

**Files:**
- Modify: `szse_pcf_fetcher.py`
- Modify: `tests/test_etf_app.py`
- Test: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: parser interfaces from Task 1 and callbacks from Task 2.
- Produces: `SZSEPCFBrowserSession`、`collect_szse_pcf_via_browser(...)`、`check_szse_pcf_connection(...)`、`SZSEPCFPageError`。

- [ ] **Step 1: Write failing collector tests with a fake session**

The fake session returns two references for one date, records `read_file` calls, and returns valid XML bytes. Test:

```python
summary = collect_szse_pcf_via_browser(
    ["2026-07-14"],
    is_complete=lambda code, date: code == "159001",
    save_snapshot=save_snapshot,
    session_factory=lambda visible=False: fake_session,
    sleep_func=lambda seconds: sleeps.append(seconds),
    delay_func=lambda: 0.8,
)
self.assertEqual(summary["discovered"], 2)
self.assertEqual(summary["skipped"], 1)
self.assertEqual(summary["succeeded"], 1)
self.assertEqual(summary["failed"], 0)
self.assertEqual(len(saved), 1)
self.assertEqual(sleeps, [0.8])
```

Add a force-replace test where `replace_existing=True` ignores `is_complete`. Add a file failure test where three retries occur with sleep values `2`, `5`, `10`, the failure is counted, and collection continues to the next reference.

- [ ] **Step 2: Run collector tests RED**

Expected: missing collector/session symbols.

- [ ] **Step 3: Implement pure orchestration**

Add:

```python
class SZSEPCFPageError(RuntimeError):
    def __init__(self, trade_date: str, message: str):
        super().__init__(message)
        self.trade_date = trade_date

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
```

Return totals `dates`, `discovered`, `skipped`, `succeeded`, `failed`, `items`, `mismatched_amounts`, and `failures`. Query errors must close the session and raise `SZSEPCFPageError(current_date, message)`; file errors stay in the summary. Emit exactly one discovery and one completion progress message per date, plus individual failure lines.

- [ ] **Step 4: Run orchestration tests GREEN**

Run the collector test class and verify all fake-session tests pass before adding Playwright code.

- [ ] **Step 5: Write failing browser-session source tests**

Assert production source contains:

```python
"playwright.chromium.launch(headless=not visible)"
"input.query-txtJCorDH"
"input.query-txtStart"
"input.query-txtEnd"
"button.confirm-query"
"CATALOGID=sgshqd"
```

Also assert `check_szse_pcf_connection` accepts an injected session factory and reports success after querying one day and reading the first reference.

- [ ] **Step 6: Implement Playwright browser session**

`SZSEPCFBrowserSession` must:

- Enter `sync_playwright()` in `__enter__`, launch Chromium with `headless=not visible` and `slow_mo=120`, and open the PCF page.
- Fill the three query inputs, click `button.confirm-query`, and wait for report data.
- Read first-page report metadata, then request remaining `PAGENO` values through `page.evaluate(fetch)` in the same browser context; pass all returned JSON through `extract_szse_pcf_references`.
- Open each official `eft_download_new.html` URL in a second page, wait until it resolves to `/files/text/ETFDown/`, then read the final response bytes. Accept TXT or XML.
- Close browser and Playwright in `__exit__` even after errors.

`check_szse_pcf_connection` queries a single date, reads at most the first reference, and returns `(True, "深交所 PCF 浏览器采集连通，可以继续采集。")` or `(False, f"深交所 PCF 浏览器仍未连通: {exc}")`.

- [ ] **Step 7: Run all SZSE PCF fetcher tests GREEN**

```powershell
python -B -m unittest tests.test_etf_app.SZSEPCFParserTests tests.test_etf_app.SZSEPCFCollectorTests -v
```

- [ ] **Step 8: Commit browser collector**

```powershell
git add szse_pcf_fetcher.py tests/test_etf_app.py
git -c user.name="Codex" -c user.email="codex@local" commit -m "feat: collect SZSE PCF in background browser"
```

---

### Task 4: Integrate current/history PCF collection into GUI

**Files:**
- Modify: `etf_gui.py:12-31,108-190,273-326,462-505`
- Modify: `tests/test_etf_app.py:1040-1050,1106-1174`
- Test: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: `collect_szse_pcf_via_browser`、`check_szse_pcf_connection`、`SZSEPCFPageError` and Task 2 database methods.
- Produces: `fetch_szse_pcf_current()`、`fetch_szse_pcf_history()`、`_fetch_szse_pcf_dates(...)` and resumable GUI behavior.

- [ ] **Step 1: Write failing GUI integration tests**

Assert `etf_gui.py` contains:

```python
"ETF成分股（PCF）"
"采集深交所当前 PCF"
"采集深交所历史 PCF"
"重新采集已有快照"
"collect_szse_pcf_via_browser"
"check_szse_pcf_connection"
"list_stock_trading_dates"
"replace_pcf_snapshot"
```

Assert the previous SSE button labels and functions remain present.

- [ ] **Step 2: Run GUI tests RED**

```powershell
python -B -m unittest tests.test_etf_app.PCFGuiTests tests.test_etf_app.ETFGuiTests -v
```

Expected: new SZSE PCF strings/imports are absent.

- [ ] **Step 3: Add controls and input validation**

In `__init__` add:

```python
self.pcf_replace_var = tk.BooleanVar(value=False)
self.paused_pcf_task = None
```

Rename the frame, preserve SSE controls on row 0, add SZSE current/history buttons on row 1, and add a `ttk.Checkbutton` bound to `pcf_replace_var`. SZSE code may be empty; if nonempty it must be exactly six digits.

- [ ] **Step 4: Implement current and historical entry points**

`fetch_szse_pcf_current` queries today first. If summary discovery count is zero, call `latest_stock_trading_date(today)` and retry once when the date differs. `fetch_szse_pcf_history` calls `list_stock_trading_dates(start, end)` and stops with one log line when no dates exist.

Both route through:

```python
def _fetch_szse_pcf_dates(self, dates, code, replace_existing):
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
```

Catch `SZSEPCFPageError`, store `paused_pcf_task = {"current": exc.trade_date, "end": dates[-1], "code": code, "replace": replace_existing}`, and log that the current date is paused without advancing.

- [ ] **Step 5: Integrate connectivity resume**

At the start of `_test_connection_and_resume`, handle `paused_pcf_task` before existing share-collection tuples. Test `check_szse_pcf_connection(current)`; when successful, rebuild remaining dates with `list_stock_trading_dates(current, end)`, clear the paused task, and call `_fetch_szse_pcf_dates`. Existing SSE/SZSE share resume behavior must remain unchanged.

- [ ] **Step 6: Run GUI tests GREEN**

```powershell
python -B -m unittest tests.test_etf_app.PCFGuiTests tests.test_etf_app.ETFGuiTests -v
```

- [ ] **Step 7: Run complete regression suite**

```powershell
python -B -m unittest tests.test_etf_app -v
```

Expected: all tests pass with no traceback or warning introduced by PCF work.

- [ ] **Step 8: Commit GUI integration**

```powershell
git add etf_gui.py tests/test_etf_app.py
git -c user.name="Codex" -c user.email="codex@local" commit -m "feat: add SZSE PCF GUI collection"
```

---

### Task 5: Verify live data without modifying the production database

**Files:**
- Verify: `szse_pcf_fetcher.py`
- Verify: `etf_database.py`
- Verify: `etf_gui.py`

**Interfaces:**
- Consumes: completed collector and parser.
- Produces: evidence that current official SZSE data parses correctly and the GUI code is syntactically valid.

- [ ] **Step 1: Compile all Python modules**

```powershell
python -B -m py_compile etf_database.py etf_fetcher.py szse_download_fetcher.py sse_pcf_fetcher.py szse_pcf_fetcher.py etf_web_app.py etf_gui.py
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Run a read-only live smoke test for 159915**

Use `SZSEPCFBrowserSession` to query the latest trading date for code `159915`, read the first official file, and parse it in memory. Do not call any database write method. Assert:

```text
交易所 = SZSE
基金代码 = 159915
内容日期 = requested/latest returned date
ETF_ITEM rows > 0
every item has 证券代码 and 挂牌市场
```

- [ ] **Step 3: Re-run full tests after live verification**

```powershell
python -B -m unittest tests.test_etf_app -v
git status --short
```

Expected: all tests pass; worktree contains no cache, downloaded XML/TXT, temporary browser profile, or uncommitted source changes.
