# 深交所 ETF 份额采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add official SZSE ETF scale collection while storing SSE and SZSE records together in real shares.

**Architecture:** Keep the current SSE adapter and add a separate SZSE adapter in `etf_fetcher.py`. Extend the existing SQLite schema with exchange-aware uniqueness and let the GUI select one or both exchanges. The web API keeps the same shape and receives normalized real-share values.

**Tech Stack:** Python standard library, `urllib`, `sqlite3`, Tkinter, `unittest`.

## Global Constraints

- SZSE `current_size` is in ten-thousand shares and must be multiplied by `10000`.
- SZSE requests must include `txtStart` and `txtEnd` and must not span more than six months.
- Failed dates pause collection; they are not silently skipped.
- No third-party runtime dependency is added.

### Task 1: Add failing SZSE parser tests

**Files:**
- Modify: `tests/test_etf_app.py`
- Test: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: JSON returned by the official SZSE report endpoint.
- Produces: expected `parse_szse_payload(text)` behavior and `split_date_ranges(start, end)` behavior.

- [ ] **Step 1: Write the failing test**

```python
from etf_fetcher import parse_szse_payload, split_date_ranges

def test_parses_szse_wan_share_and_normalizes_to_real_share(self):
    text = '[{"data":[{"size_date":"2026-07-08","fund_code":"159001","security_short_name":"货币ETF易方达","current_size":"1,670.34"}]}]'
    self.assertEqual(parse_szse_payload(text)[0]["total_share"], 16703400.0)
    self.assertEqual(parse_szse_payload(text)[0]["exchange"], "SZSE")

def test_splits_szse_ranges_into_at_most_six_months(self):
    ranges = list(split_date_ranges("2025-01-01", "2026-07-08"))
    self.assertEqual(ranges[0], ("2025-01-01", "2025-06-30"))
    self.assertEqual(ranges[-1][1], "2026-07-08")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -B -m unittest tests.test_etf_app`

Expected: FAIL because `parse_szse_payload` and `split_date_ranges` do not exist.

- [ ] **Step 3: Implement the minimal parser and range helper**

Add JSON parsing for the first report object, skip rows without code/date/size, parse commas, multiply `current_size` by `10000`, and emit `exchange="SZSE"`, `share_unit="share"`, and `source="szse_report"`.

- [ ] **Step 4: Run the focused tests**

Run: `python -B -m unittest tests.test_etf_app.ParseSZSEPayloadTests`

Expected: PASS.

### Task 2: Add SZSE network adapter

**Files:**
- Modify: `etf_fetcher.py`
- Modify: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: `fetch_szse_rows_for_range(start_date, end_date, timeout=20)`.
- Produces: normalized rows with real shares and a request URL containing `txtStart` and `txtEnd`.

- [ ] **Step 1: Write the failing test**

```python
def test_szse_fetch_uses_date_range_and_paginates(self):
    seen = []
    def opener(request, timeout):
        seen.append(request.full_url)
        return FakeResponse('[{"metadata":{"pagecount":1},"data":[]}]')
    self.assertEqual(fetch_szse_rows_for_range("2026-07-08", "2026-07-08", opener=opener), [])
    self.assertIn("txtStart=2026-07-08", seen[0])
    self.assertIn("txtEnd=2026-07-08", seen[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -B -m unittest tests.test_etf_app.ParseSZSEPayloadTests.test_szse_fetch_uses_date_range_and_paginates`

Expected: FAIL because the adapter does not exist.

- [ ] **Step 3: Implement the adapter**

Use `SZSE_API_URL`, the official page as `Referer`, query `PAGENO`/`tab1PAGENO`, read `metadata.pagecount`, and raise `ETFNetworkError` for transport or malformed responses. Fetch each page until `pagecount` is reached.

- [ ] **Step 4: Run focused and full tests**

Run: `python -B -m unittest tests.test_etf_app`

Expected: PASS.

### Task 3: Make SQLite storage exchange-aware

**Files:**
- Modify: `etf_database.py`
- Modify: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: rows containing optional `exchange`, `share_unit`, and `source`.
- Produces: `ETF` records unique by date/exchange/code; delta calculations scoped by exchange/code.

- [ ] **Step 1: Write the failing tests**

```python
def test_same_code_from_two_exchanges_coexists(self):
    db.upsert_rows([row("2026-07-08", "159001", 100, "SSE"), row("2026-07-08", "159001", 200, "SZSE")])
    self.assertEqual(len(db.get_history(["159001"])), 2)

def test_reimport_updates_same_exchange_row(self):
    db.upsert_rows([row("2026-07-08", "159001", 100, "SZSE")])
    db.upsert_rows([row("2026-07-08", "159001", 120, "SZSE")])
    self.assertEqual(db.get_stats()["rows_count"], 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -B -m unittest tests.test_etf_app.ETFDatabaseTests.test_same_code_from_two_exchanges_coexists tests.test_etf_app.ETFDatabaseTests.test_reimport_updates_same_exchange_row`

Expected: FAIL because the old unique constraint is only date/code.

- [ ] **Step 3: Implement migration and scoped deltas**

Add missing columns with `ALTER TABLE`, copy existing rows to `exchange='SSE'` and `share_unit='share'`, create a unique index on date/exchange/code after removing the old unique constraint through a table rebuild when needed, and use exchange/code in delta queries. Preserve existing callers by defaulting missing row metadata to SSE.

- [ ] **Step 4: Run database tests**

Run: `python -B -m unittest tests.test_etf_app.ETFDatabaseTests`

Expected: PASS.

### Task 4: Add GUI exchange selection and collection dispatch

**Files:**
- Modify: `etf_gui.py`
- Modify: `tests/test_etf_app.py`

**Interfaces:**
- Consumes: GUI choice `SSE`, `SZSE`, or `BOTH`.
- Produces: collection tasks dispatched to the matching adapter, with pause/resume state retaining exchange.

- [ ] **Step 1: Write the failing UI/source tests**

```python
def test_gui_contains_exchange_selector(self):
    source = Path("etf_gui.py").read_text(encoding="utf-8")
    self.assertIn("深交所", source)
    self.assertIn("沪深两市", source)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -B -m unittest tests.test_etf_app.ETFGuiTests.test_gui_contains_exchange_selector`

Expected: FAIL because the selector is absent.

- [ ] **Step 3: Implement dispatch**

Add a readonly combobox, route SSE to `fetch_etf_rows_for_date`, route SZSE through six-month chunks and `fetch_szse_rows_for_range`, and for BOTH submit both adapters while preserving paused task metadata. Log exchange and unit conversion without printing repeated network failures.

- [ ] **Step 4: Run GUI/source tests and full tests**

Run: `python -B -m unittest tests.test_etf_app`

Expected: PASS.

### Task 5: Verify and document the delivered change

**Files:**
- Modify: `docs/superpowers/specs/2026-07-12-szse-etf-scale-design.md`
- Modify: `docs/superpowers/plans/2026-07-12-szse-etf-scale.md`

- [ ] **Step 1: Run syntax verification**

Run: `python -B -m py_compile etf_fetcher.py etf_database.py etf_gui.py etf_web_app.py tests/test_etf_app.py`

Expected: exit code 0.

- [ ] **Step 2: Run the complete test suite**

Run: `python -B -m unittest tests.test_etf_app`

Expected: all tests pass.

- [ ] **Step 3: Inspect repository state**

Run: `git status --short`

Expected: only the planned source, test, and documentation files are changed.
