from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


DEFAULT_DB_PATH = Path(r"E:\学习\交易\stock_data\stock_data.db")


PCF_INFO_COLUMNS = (
    "交易所", "基金代码", "基金名称", "基金管理公司名称", "最新公告日期", "内容日期",
    "现金差额", "最小申购、赎回单位净值", "基金份额净值", "最小申购、赎回单位的预估现金部分",
    "现金替代比例上限", "当日累计可申购的基金份额上限", "当日累计可赎回的基金份额上限",
    "当日净申购的基金份额上限", "当日净赎回的基金份额上限",
    "单个证券账户当日净申购的基金份额上限", "单个证券账户当日净赎回的基金份额上限",
    "单个证券账户当日累计可申购的基金份额上限", "单个证券账户当日累计可赎回的基金份额上限",
    "是否需要公布IOPV", "最小申购、赎回单位", "申购赎回的允许情况", "申购赎回模式",
)

PCF_ITEM_COLUMNS = (
    "日期", "基金代码", "基金名称", "市场", "申赎单位", "单位净值", "预估现金差额",
    "最大现金替代比例", "成分股代码", "成分股名称", "数量", "现金替代标志",
    "现金替代标志含义", "申购现金替代溢价比例", "赎回现金替代折价比例",
)

LEGACY_PCF_ITEM_COLUMNS = (
    "交易所", "基金代码", "内容日期", "证券代码", "证券简称", "股票数量", "现金替代标志",
    "申购现金替代溢价比例", "赎回现金替代折价比例", "替代金额", "挂牌市场",
)

HOLDING_COLUMNS = (
    "交易所", "基金代码", "基金名称", "报告年度", "报告季度", "报告期", "可用日期",
    "数据完整性", "序号", "股票代码", "股票名称", "占净值比例", "持股数", "持仓市值", "挂牌市场",
)


class ETFDatabase:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _etf_item_schema(conn: sqlite3.Connection) -> str:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(ETF_ITEM)")
        }
        if {"日期", "成分股代码", "市场"}.issubset(columns):
            return "flat"
        if {"交易所", "内容日期", "证券代码"}.issubset(columns):
            return "legacy"
        raise sqlite3.OperationalError("ETF_ITEM 表结构不是受支持的版本")

    @staticmethod
    def _market_for_exchange(exchange: object) -> str:
        value = str(exchange or "").strip().upper()
        if value in {"SZSE", "深交所", "深圳证券交易所"}:
            return "深圳证券交易所"
        return "上海证券交易所"

    @staticmethod
    def _cash_substitute_flag(value: object) -> tuple[int | None, str | None]:
        if value is None or str(value).strip() == "":
            return None, None
        text = str(value).strip()
        try:
            return int(float(text)), text
        except ValueError:
            pass
        meanings = {
            "禁止": (0, "禁止现金替代"),
            "禁止现金替代": (0, "禁止现金替代"),
            "允许": (1, "允许"),
            "允许现金替代": (1, "允许现金替代"),
            "必须": (2, "必须现金替代"),
            "必须现金替代": (2, "必须现金替代"),
        }
        return meanings.get(text, (None, text))

    @classmethod
    def _flatten_pcf_item(cls, info: dict, item: dict) -> dict:
        flag, meaning = cls._cash_substitute_flag(item.get("现金替代标志"))
        content_date = info.get("内容日期") or item.get("内容日期")
        return {
            "日期": content_date,
            "基金代码": info.get("基金代码") or item.get("基金代码"),
            "基金名称": info.get("基金名称") or item.get("基金名称"),
            "市场": cls._market_for_exchange(info.get("交易所") or item.get("交易所")),
            "申赎单位": info.get("最小申购、赎回单位"),
            "单位净值": info.get("基金份额净值"),
            "预估现金差额": (
                info.get("现金差额")
                if info.get("现金差额") is not None
                else info.get("最小申购、赎回单位的预估现金部分")
            ),
            "最大现金替代比例": info.get("现金替代比例上限"),
            "成分股代码": item.get("证券代码"),
            "成分股名称": item.get("证券简称"),
            "数量": item.get("股票数量"),
            "现金替代标志": flag,
            "现金替代标志含义": meaning,
            "申购现金替代溢价比例": item.get("申购现金替代溢价比例"),
            "赎回现金替代折价比例": item.get("赎回现金替代折价比例"),
        }

    @staticmethod
    def _upsert_flat_items(
        conn: sqlite3.Connection, rows: list[dict], source: str, now: str
    ) -> int:
        if not rows:
            return 0
        fields = PCF_ITEM_COLUMNS
        sql_fields = ", ".join(f'"{field}"' for field in fields)
        updates = ", ".join(
            f'"{field}" = excluded."{field}"'
            for field in fields
            if field not in {"日期", "基金代码", "成分股代码"}
        )
        placeholders = ", ".join("?" for _ in fields)
        conn.executemany(
            f"""
            INSERT INTO ETF_ITEM ({sql_fields}, source, updated_at)
            VALUES ({placeholders}, ?, ?)
            ON CONFLICT("日期", "基金代码", "成分股代码") DO UPDATE SET
                {updates}, source = excluded.source, updated_at = excluded.updated_at
            """,
            [tuple(row.get(field) for field in fields) + (source, now) for row in rows],
        )
        return len(rows)

    @staticmethod
    def _create_etf_table(conn: sqlite3.Connection, table_name: str = "ETF") -> None:
        conn.execute(
            f"""
            CREATE TABLE {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT 'SSE',
                fund_code TEXT NOT NULL,
                fund_name TEXT NOT NULL,
                total_share REAL NOT NULL,
                share_delta REAL,
                share_unit TEXT NOT NULL DEFAULT 'share',
                source TEXT NOT NULL DEFAULT 'sse_commonQuery',
                updated_at TEXT NOT NULL,
                UNIQUE(trade_date, exchange, fund_code)
            )
            """
        )

    def _migrate_etf_schema(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(ETF)").fetchall()
        }
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ETF'"
        ).fetchone()["sql"] or ""
        compact_sql = "".join(table_sql.lower().split())
        needs_rebuild = (
            "exchange" not in columns
            or "share_unit" not in columns
            or "unique(trade_date,fund_code)" in compact_sql
        )
        if not needs_rebuild:
            conn.execute("UPDATE ETF SET exchange = 'SSE' WHERE exchange IS NULL OR exchange = ''")
            conn.execute("UPDATE ETF SET share_unit = 'share' WHERE share_unit IS NULL OR share_unit = ''")
            return

        conn.execute("ALTER TABLE ETF RENAME TO ETF_legacy")
        self._create_etf_table(conn)
        source_expr = '"source"' if "source" in columns else "'sse_commonQuery'"
        exchange_expr = '"exchange"' if "exchange" in columns else "'SSE'"
        unit_expr = '"share_unit"' if "share_unit" in columns else "'share'"
        conn.execute(
            f"""
            INSERT INTO ETF (
                id, trade_date, exchange, fund_code, fund_name, total_share,
                share_delta, share_unit, source, updated_at
            )
            SELECT id, trade_date, COALESCE({exchange_expr}, 'SSE'), fund_code,
                   fund_name, total_share, share_delta, COALESCE({unit_expr}, 'share'),
                   COALESCE({source_expr}, 'sse_commonQuery'), updated_at
            FROM ETF_legacy
            """
        )
        conn.execute("DROP TABLE ETF_legacy")

    @staticmethod
    def _create_pcf_tables(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ETF_INFO (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                "交易所" TEXT NOT NULL DEFAULT 'SSE', "基金代码" TEXT NOT NULL,
                "基金名称" TEXT, "基金管理公司名称" TEXT, "最新公告日期" TEXT,
                "内容日期" TEXT NOT NULL, "现金差额" REAL,
                "最小申购、赎回单位净值" REAL, "基金份额净值" REAL,
                "最小申购、赎回单位的预估现金部分" REAL, "现金替代比例上限" REAL,
                "当日累计可申购的基金份额上限" REAL, "当日累计可赎回的基金份额上限" REAL,
                "当日净申购的基金份额上限" REAL, "当日净赎回的基金份额上限" REAL,
                "单个证券账户当日净申购的基金份额上限" REAL,
                "单个证券账户当日净赎回的基金份额上限" REAL,
                "单个证券账户当日累计可申购的基金份额上限" REAL,
                "单个证券账户当日累计可赎回的基金份额上限" REAL,
                "是否需要公布IOPV" TEXT, "最小申购、赎回单位" REAL,
                "申购赎回的允许情况" TEXT, "申购赎回模式" TEXT,
                source TEXT NOT NULL DEFAULT 'sse_pcf', updated_at TEXT NOT NULL,
                UNIQUE("交易所", "基金代码", "内容日期")
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ETF_ITEM (
                "日期" TEXT NOT NULL, "基金代码" TEXT NOT NULL, "基金名称" TEXT,
                "市场" TEXT, "申赎单位" REAL, "单位净值" REAL, "预估现金差额" REAL,
                "最大现金替代比例" REAL, "成分股代码" TEXT NOT NULL,
                "成分股名称" TEXT, "数量" REAL, "现金替代标志" INTEGER,
                "现金替代标志含义" TEXT, "申购现金替代溢价比例" REAL,
                "赎回现金替代折价比例" REAL,
                source TEXT NOT NULL DEFAULT 'sse_pcf', updated_at TEXT NOT NULL,
                PRIMARY KEY("日期", "基金代码", "成分股代码")
            )
            """
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_etf_info_date ON ETF_INFO("交易所", "内容日期")'
        )
        item_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(ETF_ITEM)")
        }
        if "日期" in item_columns and "成分股代码" in item_columns:
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_etf_item_date '
                'ON ETF_ITEM("日期", "基金代码")'
            )
        else:
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_etf_item_date_legacy '
                'ON ETF_ITEM("交易所", "内容日期")'
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ETF_HOLDING (
                "编号" INTEGER PRIMARY KEY AUTOINCREMENT,
                "交易所" TEXT NOT NULL DEFAULT 'SSE',
                "基金代码" TEXT NOT NULL,
                "基金名称" TEXT,
                "报告年度" INTEGER NOT NULL,
                "报告季度" INTEGER NOT NULL,
                "报告期" TEXT NOT NULL,
                "可用日期" TEXT,
                "数据完整性" TEXT,
                "序号" INTEGER,
                "股票代码" TEXT NOT NULL,
                "股票名称" TEXT,
                "占净值比例" REAL,
                "持股数" REAL,
                "持仓市值" REAL,
                "挂牌市场" TEXT,
                "来源" TEXT NOT NULL DEFAULT 'eastmoney_holding',
                "更新时间" TEXT NOT NULL,
                UNIQUE("交易所", "基金代码", "报告期", "股票代码")
            )
            """
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_etf_holding_period '
            'ON ETF_HOLDING("交易所", "报告期")'
        )
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_etf_holding_fund '
            'ON ETF_HOLDING("交易所", "基金代码", "报告期")'
        )

    def initialize(self) -> None:
        with closing(self.connect()) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ETF'"
            ).fetchone()
            if exists:
                self._migrate_etf_schema(conn)
            else:
                self._create_etf_table(conn)
            self._create_pcf_tables(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_etf_code_date ON ETF(fund_code, trade_date)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_etf_date_share ON ETF(trade_date, total_share)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_etf_exchange_code_date "
                "ON ETF(exchange, fund_code, trade_date)"
            )
            conn.commit()

    def existing_pcf_keys(
        self, exchange: str, start_date: str, end_date: str
    ) -> set[tuple[str, str]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT "基金代码" AS fund_code, "内容日期" AS content_date
                FROM ETF_INFO
                WHERE "交易所" = ? AND "内容日期" BETWEEN ? AND ?
                """,
                (str(exchange).strip().upper(), start_date, end_date),
            ).fetchall()
        return {(str(row["fund_code"]), str(row["content_date"])) for row in rows}

    def pcf_is_complete(self, exchange: str, fund_code: str, content_date: str) -> bool:
        key = (str(exchange).strip().upper(), str(fund_code).strip(), content_date)
        with closing(self.connect()) as conn:
            info_exists = conn.execute(
                """
                SELECT 1 FROM ETF_INFO
                WHERE "交易所" = ? AND "基金代码" = ? AND "内容日期" = ?
                """,
                key,
            ).fetchone()
            if self._etf_item_schema(conn) == "flat":
                item_count = conn.execute(
                    'SELECT COUNT(*) FROM ETF_ITEM '
                    'WHERE "基金代码" = ? AND "日期" = ?',
                    (key[1], key[2]),
                ).fetchone()[0]
            else:
                item_count = conn.execute(
                    """
                    SELECT COUNT(*) FROM ETF_ITEM
                    WHERE "交易所" = ? AND "基金代码" = ? AND "内容日期" = ?
                    """,
                    key,
                ).fetchone()[0]
        return info_exists is not None and item_count > 0

    @staticmethod
    def _format_stock_trading_date(value: object) -> str:
        digits = str(value).split(".", 1)[0].zfill(8)
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"

    def list_stock_trading_dates(self, start_date: str, end_date: str) -> list[str]:
        start_value = int(start_date.replace("-", ""))
        end_value = int(end_date.replace("-", ""))
        with closing(self.connect()) as conn:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stock_daily'"
            ).fetchone()
            if table_exists is None:
                return []
            rows = conn.execute(
                'SELECT DISTINCT "日期" AS trade_date FROM stock_daily '
                'WHERE "日期" BETWEEN ? AND ? ORDER BY "日期"',
                (start_value, end_value),
            ).fetchall()
        return [self._format_stock_trading_date(row["trade_date"]) for row in rows]

    def latest_stock_trading_date(self, on_or_before: str) -> str | None:
        end_value = int(on_or_before.replace("-", ""))
        with closing(self.connect()) as conn:
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stock_daily'"
            ).fetchone()
            if table_exists is None:
                return None
            row = conn.execute(
                'SELECT MAX("日期") AS trade_date FROM stock_daily WHERE "日期" <= ?',
                (end_value,),
            ).fetchone()
        if row["trade_date"] is None:
            return None
        return self._format_stock_trading_date(row["trade_date"])

    def replace_pcf_snapshot(
        self,
        info: dict,
        items: list[dict],
        source: str = "sse_pcf",
    ) -> tuple[int, int]:
        key_fields = ("交易所", "基金代码", "内容日期")
        if not isinstance(info, dict):
            raise ValueError("info must be a dict")
        if not items or not all(isinstance(item, dict) for item in items):
            raise ValueError("items must contain at least one row")

        info_key = tuple(info.get(field) for field in key_fields)
        item_keys = {tuple(item.get(field) for field in key_fields) for item in items}
        if any(not all(value is not None and value != "" for value in key) for key in (info_key, *item_keys)):
            raise ValueError("PCF snapshot keys must be present")
        if item_keys != {info_key}:
            raise ValueError("PCF snapshot keys must match")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        info_fields = PCF_INFO_COLUMNS
        info_sql_fields = ", ".join(f'"{field}"' for field in info_fields)
        info_updates = ", ".join(
            f'"{field}" = excluded."{field}"'
            for field in info_fields
            if field not in key_fields
        )
        info_values = tuple(info.get(field) for field in info_fields)

        with closing(self.connect()) as conn:
            with conn:
                item_schema = self._etf_item_schema(conn)
                if item_schema == "flat":
                    conn.execute(
                        'DELETE FROM ETF_ITEM WHERE "基金代码" = ? AND "日期" = ?',
                        (info_key[1], info_key[2]),
                    )
                else:
                    conn.execute(
                        'DELETE FROM ETF_ITEM WHERE "交易所" = ? AND "基金代码" = ? AND "内容日期" = ?',
                        info_key,
                    )
                placeholders = ", ".join("?" for _ in info_fields)
                conn.execute(
                    f"""
                    INSERT INTO ETF_INFO ({info_sql_fields}, source, updated_at)
                    VALUES ({placeholders}, ?, ?)
                    ON CONFLICT("交易所", "基金代码", "内容日期") DO UPDATE SET
                        {info_updates}, source = excluded.source, updated_at = excluded.updated_at
                    """,
                    info_values + (source, now),
                )
                if item_schema == "flat":
                    self._upsert_flat_items(
                        conn,
                        [self._flatten_pcf_item(info, item) for item in items],
                        source,
                        now,
                    )
                else:
                    item_fields = LEGACY_PCF_ITEM_COLUMNS
                    item_sql_fields = ", ".join(f'"{field}"' for field in item_fields)
                    item_updates = ", ".join(
                        f'"{field}" = excluded."{field}"'
                        for field in item_fields
                        if field not in ("交易所", "基金代码", "内容日期", "证券代码", "挂牌市场")
                    )
                    placeholders = ", ".join("?" for _ in item_fields)
                    conn.executemany(
                        f"""
                        INSERT INTO ETF_ITEM ({item_sql_fields}, source, updated_at)
                        VALUES ({placeholders}, ?, ?)
                        ON CONFLICT("交易所", "基金代码", "内容日期", "证券代码", "挂牌市场") DO UPDATE SET
                            {item_updates}, source = excluded.source, updated_at = excluded.updated_at
                        """,
                        [
                            tuple(item.get(field) for field in item_fields) + (source, now)
                            for item in items
                        ],
                    )
        return 1, len(items)

    def upsert_pcf(
        self,
        info_rows: list[dict],
        item_rows: list[dict],
        source: str = "sse_pcf",
    ) -> tuple[int, int]:
        if not info_rows and not item_rows:
            return 0, 0

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        info_fields = PCF_INFO_COLUMNS
        info_sql_fields = ", ".join(f'"{field}"' for field in info_fields)
        info_updates = ", ".join(
            f'"{field}" = excluded."{field}"'
            for field in info_fields
            if field not in {"交易所", "基金代码", "内容日期"}
        )
        info_values = [
            tuple(row.get(field) for field in info_fields)
            for row in info_rows
            if row.get("基金代码") and row.get("内容日期")
        ]
        info_by_key = {
            (
                str(row.get("交易所") or "SSE").strip().upper(),
                str(row.get("基金代码")).strip(),
                str(row.get("内容日期")),
            ): row
            for row in info_rows
            if row.get("基金代码") and row.get("内容日期")
        }

        with closing(self.connect()) as conn:
            item_schema = self._etf_item_schema(conn)
            if info_values:
                placeholders = ", ".join("?" for _ in info_fields)
                conn.executemany(
                    f"""
                    INSERT INTO ETF_INFO ({info_sql_fields}, source, updated_at)
                    VALUES ({placeholders}, ?, ?)
                    ON CONFLICT("交易所", "基金代码", "内容日期") DO UPDATE SET
                        {info_updates}, source = excluded.source, updated_at = excluded.updated_at
                    """,
                    [values + (source, now) for values in info_values],
                )
            if item_schema == "flat":
                flat_items = []
                for item in item_rows:
                    key = (
                        str(item.get("交易所") or "SSE").strip().upper(),
                        str(item.get("基金代码") or "").strip(),
                        str(item.get("内容日期") or ""),
                    )
                    info = info_by_key.get(key, item)
                    flat = self._flatten_pcf_item(info, item)
                    if flat["基金代码"] and flat["日期"] and flat["成分股代码"]:
                        flat_items.append(flat)
                item_count = self._upsert_flat_items(conn, flat_items, source, now)
            else:
                item_fields = LEGACY_PCF_ITEM_COLUMNS
                item_sql_fields = ", ".join(f'"{field}"' for field in item_fields)
                item_updates = ", ".join(
                    f'"{field}" = excluded."{field}"'
                    for field in item_fields
                    if field not in {"交易所", "基金代码", "内容日期", "证券代码", "挂牌市场"}
                )
                item_values = [
                    tuple(row.get(field) for field in item_fields)
                    for row in item_rows
                    if row.get("基金代码") and row.get("内容日期") and row.get("证券代码")
                ]
                if item_values:
                    placeholders = ", ".join("?" for _ in item_fields)
                    conn.executemany(
                        f"""
                        INSERT INTO ETF_ITEM ({item_sql_fields}, source, updated_at)
                        VALUES ({placeholders}, ?, ?)
                        ON CONFLICT("交易所", "基金代码", "内容日期", "证券代码", "挂牌市场") DO UPDATE SET
                            {item_updates}, source = excluded.source, updated_at = excluded.updated_at
                        """,
                        [values + (source, now) for values in item_values],
                    )
                item_count = len(item_values)
            conn.commit()
        return len(info_values), item_count

    def upsert_rows(self, rows: list[dict], recalculate: bool = True) -> int:
        if not rows:
            return 0

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        normalized = [
            (
                str(row["trade_date"]),
                str(row.get("exchange") or "SSE").strip().upper(),
                str(row["fund_code"]).strip(),
                str(row.get("fund_name") or "").strip(),
                float(row["total_share"]),
                str(row.get("share_unit") or "share").strip(),
                str(
                    row.get("source")
                    or ("szse_report" if str(row.get("exchange") or "SSE").upper() == "SZSE" else "sse_commonQuery")
                ).strip(),
                now,
            )
            for row in rows
        ]
        codes = sorted({row[2] for row in normalized})

        with closing(self.connect()) as conn:
            conn.executemany(
                """
                INSERT INTO ETF (
                    trade_date, exchange, fund_code, fund_name, total_share,
                    share_unit, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, exchange, fund_code) DO UPDATE SET
                    fund_name = excluded.fund_name,
                    total_share = excluded.total_share,
                    share_unit = excluded.share_unit,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                normalized,
            )
            if recalculate:
                self._recalculate_deltas(conn, codes)
            conn.commit()

        return len(normalized)

    def recalculate_deltas(self, fund_codes: list[str]) -> None:
        codes = sorted({str(code).strip() for code in fund_codes if str(code).strip()})
        if not codes:
            return
        with closing(self.connect()) as conn:
            self._recalculate_deltas(conn, codes)
            conn.commit()

    def existing_trade_dates(self, exchange: str, start_date: str, end_date: str) -> set[str]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT trade_date
                FROM ETF
                WHERE exchange = ?
                  AND trade_date BETWEEN ? AND ?
                """,
                (str(exchange).strip().upper(), start_date, end_date),
            ).fetchall()
        return {str(row["trade_date"]) for row in rows}

    def list_fund_codes(self, exchange: str = "SSE") -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT fund_code, MAX(fund_name) AS fund_name
                FROM ETF
                WHERE exchange = ?
                GROUP BY fund_code
                ORDER BY fund_code
                """,
                (str(exchange).strip().upper(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def fund_exchanges(self, fund_code: str) -> tuple[str, ...]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT exchange FROM ETF WHERE fund_code = ? ORDER BY exchange",
                (str(fund_code).strip(),),
            ).fetchall()
        return tuple(str(row["exchange"]).upper() for row in rows)

    def holding_snapshot_exists(
        self, exchange: str, fund_code: str, report_period: str
    ) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                'SELECT 1 FROM ETF_HOLDING '
                'WHERE "交易所" = ? AND "基金代码" = ? AND "报告期" = ? LIMIT 1',
                (str(exchange).strip().upper(), str(fund_code).strip(), report_period),
            ).fetchone()
        return row is not None

    def replace_holding_snapshot(
        self, rows: list[dict], source: str = "eastmoney_holding"
    ) -> tuple[int, int]:
        if not rows or not all(isinstance(row, dict) for row in rows):
            raise ValueError("holding snapshot must contain at least one row")
        key_fields = ("交易所", "基金代码", "报告期")
        keys = {tuple(row.get(field) for field in key_fields) for row in rows}
        if len(keys) != 1 or any(not all(value is not None and value != "" for value in key) for key in keys):
            raise ValueError("holding snapshot keys must be present and identical")
        if any(not row.get("股票代码") for row in rows):
            raise ValueError("holding rows must contain stock codes")

        exchange, fund_code, report_period = next(iter(keys))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fields = HOLDING_COLUMNS
        sql_fields = ", ".join(f'"{field}"' for field in fields)
        values = [tuple(row.get(field) for field in fields) for row in rows]
        updates = ", ".join(
            f'"{field}" = excluded."{field}"'
            for field in fields
            if field not in {"交易所", "基金代码", "报告期", "股票代码"}
        )

        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    'DELETE FROM ETF_HOLDING WHERE "交易所" = ? AND "基金代码" = ? AND "报告期" = ?',
                    (exchange, fund_code, report_period),
                )
                placeholders = ", ".join("?" for _ in fields)
                conn.executemany(
                    f"""
                    INSERT INTO ETF_HOLDING ({sql_fields}, "来源", "更新时间")
                    VALUES ({placeholders}, ?, ?)
                    ON CONFLICT("交易所", "基金代码", "报告期", "股票代码") DO UPDATE SET
                        {updates}, "来源" = excluded."来源", "更新时间" = excluded."更新时间"
                    """,
                    [item + (source, now) for item in values],
                )
        return 1, len(values)

    def _recalculate_deltas(self, conn: sqlite3.Connection, fund_codes: list[str]) -> None:
        code_placeholders = ",".join("?" for _ in fund_codes)
        groups = conn.execute(
            f"""
            SELECT DISTINCT exchange, fund_code
            FROM ETF
            WHERE fund_code IN ({code_placeholders})
            """,
            fund_codes,
        ).fetchall()
        for group in groups:
            rows = conn.execute(
                """
                SELECT id, total_share
                FROM ETF
                WHERE exchange = ? AND fund_code = ?
                ORDER BY trade_date, id
                """,
                (group["exchange"], group["fund_code"]),
            ).fetchall()
            previous = None
            for row in rows:
                delta = None if previous is None else float(row["total_share"]) - previous
                conn.execute("UPDATE ETF SET share_delta = ? WHERE id = ?", (delta, row["id"]))
                previous = float(row["total_share"])

    def list_latest_etfs(self, query: str = "", sort: str = "share_desc") -> list[dict]:
        query = (query or "").strip()
        order_by = {
            "share_asc": "e.total_share ASC",
            "code": "e.fund_code ASC",
            "name": "e.fund_name ASC",
        }.get(sort, "e.total_share DESC")

        where = ""
        params: list[str] = []
        if query:
            where = "WHERE e.fund_code LIKE ? OR e.fund_name LIKE ?"
            like = f"%{query}%"
            params.extend([like, like])

        sql = f"""
            SELECT e.trade_date, e.exchange, e.fund_code, e.fund_name,
                   e.total_share, e.share_delta, e.share_unit, e.source
            FROM ETF e
            JOIN (
                SELECT exchange, fund_code, MAX(trade_date) AS trade_date
                FROM ETF
                GROUP BY exchange, fund_code
            ) latest
              ON latest.exchange = e.exchange
             AND latest.fund_code = e.fund_code
             AND latest.trade_date = e.trade_date
            {where}
            ORDER BY {order_by}
        """
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def get_history(self, fund_codes: list[str], limit_days: int | None = None) -> list[dict]:
        codes = [str(code).strip() for code in fund_codes if str(code).strip()]
        if not codes:
            return []

        placeholders = ",".join(["?"] * len(codes))
        params: list[object] = list(codes)
        date_filter = ""
        if limit_days:
            date_filter = """
                AND e.trade_date IN (
                    SELECT trade_date FROM ETF GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?
                )
            """
            params.append(int(limit_days))

        with closing(self.connect()) as conn:
            has_stock_daily = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stock_daily'"
                ).fetchone()
                is not None
            )
            if has_stock_daily:
                price_select = """
                    s."开盘价" AS open_price,
                    s."最高价" AS high_price,
                    s."最低价" AS low_price,
                    s."收盘价" AS close_price,
                    s."成交量" AS volume
                """
                price_join = """
                    LEFT JOIN stock_daily s
                      ON s."股票代码" = e.fund_code
                     AND s."日期" = CAST(REPLACE(e.trade_date, '-', '') AS INTEGER)
                """
            else:
                price_select = """
                    NULL AS open_price,
                    NULL AS high_price,
                    NULL AS low_price,
                    NULL AS close_price,
                    NULL AS volume
                """
                price_join = ""

            sql = f"""
                SELECT e.trade_date, e.exchange, e.fund_code, e.fund_name,
                       e.total_share, e.share_delta, e.share_unit, e.source,
                       {price_select}
                FROM ETF e
                {price_join}
                WHERE e.fund_code IN ({placeholders})
                {date_filter}
                ORDER BY e.trade_date ASC, e.fund_code ASC
            """
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def get_stats(self) -> dict:
        with closing(self.connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS rows_count,
                       COUNT(DISTINCT fund_code) AS fund_count,
                       MIN(trade_date) AS min_date,
                       MAX(trade_date) AS max_date
                FROM ETF
                """
            ).fetchone()
            return dict(row)

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
            "holding": {
                exchange: {
                    "min_date": None,
                    "max_date": None,
                    "report_count": 0,
                    "fund_count": 0,
                    "item_count": 0,
                    "full_report_count": 0,
                    "partial_report_count": 0,
                }
                for exchange in ("SSE", "SZSE")
            },
        }

        with closing(self.connect()) as conn:
            share_rows = conn.execute(
                """
                SELECT exchange,
                       MIN(trade_date) AS min_date,
                       MAX(trade_date) AS max_date,
                       COUNT(*) AS rows_count,
                       COUNT(DISTINCT fund_code) AS fund_count,
                       COUNT(DISTINCT trade_date) AS date_count
                FROM ETF
                GROUP BY exchange
                """
            ).fetchall()
            item_schema = self._etf_item_schema(conn)
            if item_schema == "flat":
                component_rows = conn.execute(
                    """
                    SELECT CASE
                               WHEN "市场" IN ('深圳证券交易所', '深交所', 'SZSE')
                               THEN 'SZSE' ELSE 'SSE' END AS exchange,
                           MIN("日期") AS min_date,
                           MAX("日期") AS max_date,
                           COUNT(DISTINCT "日期" || '|' || "基金代码") AS snapshot_count,
                           COUNT(DISTINCT "基金代码") AS fund_count,
                           COUNT(*) AS item_count
                    FROM ETF_ITEM
                    GROUP BY CASE
                               WHEN "市场" IN ('深圳证券交易所', '深交所', 'SZSE')
                               THEN 'SZSE' ELSE 'SSE' END
                    """
                ).fetchall()
            else:
                info_rows = conn.execute(
                    """
                    SELECT "交易所" AS exchange,
                           MIN("内容日期") AS min_date,
                           MAX("内容日期") AS max_date,
                           COUNT(*) AS snapshot_count,
                           COUNT(DISTINCT "基金代码") AS fund_count
                    FROM ETF_INFO
                    GROUP BY "交易所"
                    """
                ).fetchall()
                item_rows = conn.execute(
                    """
                    SELECT "交易所" AS exchange, COUNT(*) AS item_count
                    FROM ETF_ITEM
                    GROUP BY "交易所"
                    """
                ).fetchall()
                component_rows = []
                item_count_by_exchange = {
                    row["exchange"]: int(row["item_count"]) for row in item_rows
                }
                for row in info_rows:
                    component_rows.append(
                        {
                            "exchange": row["exchange"],
                            "min_date": row["min_date"],
                            "max_date": row["max_date"],
                            "snapshot_count": row["snapshot_count"],
                            "fund_count": row["fund_count"],
                            "item_count": item_count_by_exchange.get(row["exchange"], 0),
                        }
                    )
            holding_rows = conn.execute(
                """
                SELECT "交易所" AS exchange,
                       MIN("报告期") AS min_date,
                       MAX("报告期") AS max_date,
                       COUNT(DISTINCT "基金代码" || '|' || "报告期") AS report_count,
                       COUNT(DISTINCT "基金代码") AS fund_count,
                       COUNT(*) AS item_count,
                       COUNT(DISTINCT CASE WHEN "数据完整性" = '完整披露'
                                           THEN "基金代码" || '|' || "报告期" END) AS full_report_count,
                       COUNT(DISTINCT CASE WHEN "数据完整性" = '部分披露'
                                           THEN "基金代码" || '|' || "报告期" END) AS partial_report_count
                FROM ETF_HOLDING
                GROUP BY "交易所"
                """
            ).fetchall()

        for row in share_rows:
            exchange = row["exchange"]
            if exchange not in result["share"]:
                continue
            result["share"][exchange].update(
                min_date=row["min_date"],
                max_date=row["max_date"],
                rows_count=int(row["rows_count"]),
                fund_count=int(row["fund_count"]),
                date_count=int(row["date_count"]),
            )

        for row in component_rows:
            exchange = row["exchange"]
            if exchange not in result["component"]:
                continue
            result["component"][exchange].update(
                min_date=row["min_date"],
                max_date=row["max_date"],
                snapshot_count=int(row["snapshot_count"]),
                fund_count=int(row["fund_count"]),
            )

            result["component"][exchange]["item_count"] = int(row["item_count"])

        for row in holding_rows:
            exchange = row["exchange"]
            if exchange not in result["holding"]:
                continue
            result["holding"][exchange].update(
                min_date=row["min_date"],
                max_date=row["max_date"],
                report_count=int(row["report_count"]),
                fund_count=int(row["fund_count"]),
                item_count=int(row["item_count"]),
                full_report_count=int(row["full_report_count"]),
                partial_report_count=int(row["partial_report_count"]),
            )

        return result
