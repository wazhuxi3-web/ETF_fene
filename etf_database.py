from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


DEFAULT_DB_PATH = Path(r"E:\学习\交易\stock_data\stock_data.db")


class ETFDatabase:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

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

    def initialize(self) -> None:
        with closing(self.connect()) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ETF'"
            ).fetchone()
            if exists:
                self._migrate_etf_schema(conn)
            else:
                self._create_etf_table(conn)
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
