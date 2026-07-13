# 上交所 ETF PCF 采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 ETF 工具中增加上交所 PCF 采集，写入 `ETF_INFO`、`ETF_ITEM`，并把 GUI 日志改成可读、独立滚动布局。

**Architecture:** 新增 `sse_pcf_fetcher.py`，负责上交所详情页/历史公告解析和标准化；`etf_database.py` 新增两张表及幂等写入接口；`etf_gui.py` 增加 PCF 采集入口并把控制区、统计区、日志区分开。复用现有 ETF 份额表作为上交所 ETF 代码来源，避免另造基金列表。

**Tech Stack:** Python 标准库、`urllib`、`html.parser`、`sqlite3`、Tkinter、`unittest`。

## Global Constraints

- 上交所 PCF 区间不按 5 个月切批。
- 表名固定为 `ETF_INFO`、`ETF_ITEM`。
- 公告中文表头去掉单位；缺失值保存 NULL。
- `最新公告日期` 与 `内容日期` 分开保存。
- 重复键只更新同一公告，不产生重复行；已有完整公告跳过请求。
- 网络失败暂停当前位置，不自动跳过日期，不刷屏。
- 控制区固定，只有日志区滚动。
- 不新增第三方运行依赖。

---

### Task 1: Add failing PCF normalization tests

**Files:**
- Modify: `tests/test_etf_app.py`
- Create: `sse_pcf_fetcher.py` (only test import target; no production implementation before RED)

**Interfaces:**
- `normalize_pcf_info(raw, exchange="SSE") -> dict`
- `normalize_pcf_item(raw, exchange="SSE") -> dict`

- [ ] **Step 1: Write failing tests**

```python
def test_normalizes_info_with_chinese_columns_and_null_missing_values(self):
    info = normalize_pcf_info({
        "最新公告日期": "2026-07-13",
        "内容日期": "2026-07-13",
        "基金代码": "510010",
        "基金名称": "治理ETF",
        "现金差额": "21388.57",
        "现金替代比例上限": "30%",
    })
    self.assertEqual(info["基金代码"], "510010")
    self.assertEqual(info["现金差额"], 21388.57)
    self.assertEqual(info["现金替代比例上限"], 30.0)
    self.assertIsNone(info["申购赎回模式"])
    self.assertEqual(info["交易所"], "SSE")

def test_normalizes_item_fields(self):
    item = normalize_pcf_item({
        "证券代码": "600009",
        "证券简称": "上海机场",
        "股票数量": "300",
        "现金替代标志": "允许",
        "申购现金替代溢价比例": "34%",
        "替代金额": "-",
        "挂牌市场": "上海证券交易所",
    })
    self.assertEqual(item["股票数量"], 300.0)
    self.assertEqual(item["申购现金替代溢价比例"], 34.0)
    self.assertIsNone(item["替代金额"])
```

- [ ] **Step 2: Run RED test**

Run: `python -B -m unittest tests.test_etf_app.PCFNormalizationTests -v`

Expected: FAIL because `sse_pcf_fetcher.py` and normalization functions do not exist.

- [ ] **Step 3: Implement minimal normalization**

Implement numeric parsing for commas, `%`, currency symbols, `-`, empty strings, and `None`. Preserve all required Chinese keys, fill absent keys with `None`, and add `交易所`.

- [ ] **Step 4: Run GREEN test**

Run: `python -B -m unittest tests.test_etf_app.PCFNormalizationTests -v`

Expected: PASS.

### Task 2: Parse SSE PCF HTML

**Files:**
- Modify: `tests/test_etf_app.py`
- Modify: `sse_pcf_fetcher.py`

**Interfaces:**
- `parse_sse_pcf_html(html, fund_code, content_date=None) -> tuple[dict, list[dict]]`

- [ ] **Step 1: Write failing parser test**

Use a compact HTML fixture containing the announcement table, two content-info tables, and one component table. Assert the parser returns one info row, two item rows, the correct `基金代码`, `内容日期`, `证券代码`, and no unit text in keys.

- [ ] **Step 2: Run RED test**

Run: `python -B -m unittest tests.test_etf_app.PCFParserTests -v`

Expected: FAIL because `parse_sse_pcf_html` is absent or returns no rows.

- [ ] **Step 3: Implement parser**

Use `html.parser.HTMLParser` or a bounded DOM-free parser. Read table rows by visible cell text, identify the announcement table by `最新公告日期`/`基金代码`, identify content date from headings matching `YYYY-MM-DD日内容信息`, and map component headers to the required Chinese keys. Never infer a historical date from the latest announcement date when the content date is absent.

- [ ] **Step 4: Run GREEN parser test**

Run: `python -B -m unittest tests.test_etf_app.PCFParserTests -v`

Expected: PASS.

### Task 3: Add PCF SQLite tables and idempotent storage

**Files:**
- Modify: `tests/test_etf_app.py`
- Modify: `etf_database.py`

**Interfaces:**
- `ETFDatabase.initialize()` creates/migrates `ETF_INFO` and `ETF_ITEM`.
- `ETFDatabase.existing_pcf_keys(exchange, start_date, end_date) -> set[tuple[str, str]]`.
- `ETFDatabase.upsert_pcf(info_rows, item_rows) -> tuple[int, int]`.
- `ETFDatabase.pcf_is_complete(exchange, fund_code, content_date) -> bool`.

- [ ] **Step 1: Write failing database tests**

Test table creation, Chinese column names, duplicate upsert returning one info row and one item row, different dates coexisting, different funds coexisting, and a missing item causing `pcf_is_complete` to return false.

- [ ] **Step 2: Run RED tests**

Run: `python -B -m unittest tests.test_etf_app.PCFDatabaseTests -v`

Expected: FAIL because tables and methods are absent.

- [ ] **Step 3: Implement schema**

Create `ETF_INFO` and `ETF_ITEM` with quoted Chinese identifiers, nullable value columns, `交易所` default `SSE`, `基金代码`, `内容日期`, and unique constraints from the approved design. Add indexes on `(交易所, 基金代码, 内容日期)` and `(交易所, 内容日期)`.

- [ ] **Step 4: Implement idempotent upsert**

Use SQLite `ON CONFLICT` on the approved keys. Upsert info and items in one transaction. Return inserted/updated counts. Do not delete existing items during a partial import; caller must only write a complete parsed announcement.

- [ ] **Step 5: Run GREEN database tests**

Run: `python -B -m unittest tests.test_etf_app.PCFDatabaseTests -v`

Expected: PASS.

### Task 4: Add SSE PCF fetch adapter and date-range behavior

**Files:**
- Modify: `tests/test_etf_app.py`
- Modify: `sse_pcf_fetcher.py`

**Interfaces:**
- `fetch_sse_pcf_for_fund(fund_code, content_date=None, opener=None) -> tuple[dict, list[dict]]`
- `iter_sse_pcf_dates(start_date, end_date) -> iterator[str]`

- [ ] **Step 1: Write failing adapter tests**

Test that the request uses `fundid=<code>`, that a requested historical content date is passed through the official history route, that the parser result is returned, and that a range from `2026-01-01` to `2026-07-13` remains one date iterator with no five-month split.

- [ ] **Step 2: Run RED tests**

Run: `python -B -m unittest tests.test_etf_app.PCFFetcherTests -v`

Expected: FAIL because the fetch adapter and date iterator are absent.

- [ ] **Step 3: Implement fetch adapter**

Use the official SSE detail URL for current data and the confirmed official historical route for historical content. Keep HTTP headers consistent with the existing SSE fetcher. Raise `ETFNetworkError` on transport errors, non-HTML responses, missing key fields, or a page that returns a different content date than requested.

- [ ] **Step 4: Run GREEN adapter tests**

Run: `python -B -m unittest tests.test_etf_app.PCFFetcherTests -v`

Expected: PASS.

### Task 5: Integrate PCF collection into GUI

**Files:**
- Modify: `tests/test_etf_app.py`
- Modify: `etf_gui.py`

**Interfaces:**
- `ETFApp.fetch_pcf_range()` starts a background PCF collection using existing date inputs.
- `ETFApp._append_log(text, level="INFO", phase="日志")` appends one formatted line without moving controls.

- [ ] **Step 1: Write failing GUI/source tests**

Assert the GUI has a PCF collection button, imports `fetch_sse_pcf_for_fund`, does not call a five-month splitter for SSE PCF, creates a separate log frame with a scrollbar, and binds the log text widget rather than the whole root frame to scrolling.

- [ ] **Step 2: Run RED tests**

Run: `python -B -m unittest tests.test_etf_app.PCFGuiTests -v`

Expected: FAIL because PCF controls and independent log layout are absent.

- [ ] **Step 3: Implement fixed controls + scrollable log**

Build the root with a fixed top control frame and a bottom expandable log frame. Create `Text(..., yscrollcommand=scrollbar.set)`, `Scrollbar(command=text.yview)`, and only bind mouse-wheel handling to the log widget. Track whether the view is at the bottom before appending; call `see(tk.END)` only when it was already following the bottom.

- [ ] **Step 4: Implement PCF worker**

Use known SSE fund codes from `ETF` for each requested date. Check `pcf_is_complete` per fund/date before network access. Parse and transactionally upsert complete results. Track the current date and fund index in `paused_task`; a connection failure logs one concise failure and pauses. Completion logs aggregate info/item counts.

- [ ] **Step 5: Run GREEN GUI tests**

Run: `python -B -m unittest tests.test_etf_app.PCFGuiTests -v`

Expected: PASS.

### Task 6: Full verification and baseline commit

**Files:**
- Modify: `tests/test_etf_app.py` only if a regression test is needed.

- [ ] **Step 1: Run full unit tests**

Run: `python -B -m unittest tests.test_etf_app -v`

Expected: all tests pass.

- [ ] **Step 2: Run syntax check**

Run: `python -B -m py_compile etf_database.py etf_fetcher.py sse_pcf_fetcher.py etf_gui.py etf_web_app.py tests/test_etf_app.py`

Expected: exit code 0.

- [ ] **Step 3: Remove generated bytecode**

Run: `Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force`

Expected: no `__pycache__` remains.

- [ ] **Step 4: Commit implementation**

```powershell
git add etf_database.py sse_pcf_fetcher.py etf_gui.py tests/test_etf_app.py docs/superpowers/specs/2026-07-13-sse-etf-pcf-design.md docs/superpowers/plans/2026-07-13-sse-etf-pcf.md
git -c user.name="Codex" -c user.email="codex@local" commit -m "feat: collect SSE ETF PCF data"
```

Expected: one clean implementation commit after the baseline commit.
