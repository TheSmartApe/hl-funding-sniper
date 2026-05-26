"""Position persistence layer (SQLite).

Tracks every entry/exit + cumulative funding collected. Survives bot restarts.
The DB is the single source of truth for "what positions do we currently hold,
per the bot's view of the world". Cross-checked against HL user_state at every
tick by the risk manager.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .signal_engine import PositionState

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('short','long')),
    size_usd REAL NOT NULL,
    size_base REAL,                       -- coin units (for trigger cancels)
    leverage INTEGER NOT NULL,

    entry_price REAL NOT NULL,
    entry_timestamp TEXT NOT NULL,
    entry_funding_apr REAL,
    entry_zscore REAL,
    entry_reason TEXT,

    sl_oid INTEGER,                       -- HL order id of the SL trigger
    tp_oid INTEGER,                       -- HL order id of the TP trigger

    exit_price REAL,
    exit_timestamp TEXT,
    exit_reason TEXT,
    realized_pnl_usd REAL,
    funding_collected_usd REAL DEFAULT 0,

    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED','ERROR'))
);

CREATE INDEX IF NOT EXISTS idx_positions_coin_status
    ON positions(coin, status);

CREATE TABLE IF NOT EXISTS exits_history (
    -- Per-coin exit log to enforce reentry cooldowns
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    exit_timestamp TEXT NOT NULL,
    exit_reason TEXT NOT NULL,
    realized_pnl_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_exits_coin_ts
    ON exits_history(coin, exit_timestamp);
"""

# Idempotent migrations for DBs created before columns existed
MIGRATIONS = [
    "ALTER TABLE positions ADD COLUMN sl_oid INTEGER",
    "ALTER TABLE positions ADD COLUMN tp_oid INTEGER",
    "ALTER TABLE positions ADD COLUMN size_base REAL",
]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PositionManager:
    def __init__(self, db_path: str | Path = "state/positions.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            # Apply migrations idempotently (ignore "duplicate column" errors)
            for stmt in MIGRATIONS:
                try:
                    c.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ── opens ──────────────────────────────────────────────────────────────

    def record_open(
        self,
        coin: str,
        direction: str,
        size_usd: float,
        size_base: float,
        leverage: int,
        entry_price: float,
        entry_funding_apr: float,
        entry_zscore: float,
        entry_reason: str,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO positions
                   (coin, direction, size_usd, size_base, leverage, entry_price,
                    entry_timestamp, entry_funding_apr, entry_zscore,
                    entry_reason, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
                (
                    coin, direction, size_usd, size_base, leverage, entry_price,
                    _utcnow_iso(), entry_funding_apr, entry_zscore,
                    entry_reason,
                ),
            )
            pid = cur.lastrowid
            log.info("Recorded OPEN id=%d %s %s $%.2f (base=%.6f) @ %.6f",
                     pid, direction, coin, size_usd, size_base, entry_price)
            return pid

    def update_trigger_oids(
        self, position_id: int, sl_oid: Optional[int], tp_oid: Optional[int],
    ) -> None:
        """Attach HL trigger order ids to a position after they're placed."""
        with self._conn() as c:
            c.execute(
                "UPDATE positions SET sl_oid=?, tp_oid=? WHERE id=?",
                (sl_oid, tp_oid, position_id),
            )
            log.info("Recorded triggers position_id=%d sl_oid=%s tp_oid=%s",
                     position_id, sl_oid, tp_oid)

    def clear_trigger_oids(self, position_id: int) -> None:
        """Forget triggers after cancellation (or after they fired)."""
        with self._conn() as c:
            c.execute(
                "UPDATE positions SET sl_oid=NULL, tp_oid=NULL WHERE id=?",
                (position_id,),
            )

    # ── closes ─────────────────────────────────────────────────────────────

    def record_close(
        self,
        position_id: int,
        exit_price: float,
        exit_reason: str,
        realized_pnl_usd: float,
        funding_collected_usd: float,
    ) -> None:
        with self._conn() as c:
            row = c.execute(
                "SELECT coin FROM positions WHERE id=?", (position_id,)
            ).fetchone()
            if row is None:
                log.error("record_close: position %d not found", position_id)
                return
            coin = row["coin"]
            ts = _utcnow_iso()
            c.execute(
                """UPDATE positions
                   SET exit_price=?, exit_timestamp=?, exit_reason=?,
                       realized_pnl_usd=?, funding_collected_usd=?, status='CLOSED'
                   WHERE id=?""",
                (
                    exit_price, ts, exit_reason,
                    realized_pnl_usd, funding_collected_usd, position_id,
                ),
            )
            c.execute(
                """INSERT INTO exits_history
                   (coin, exit_timestamp, exit_reason, realized_pnl_usd)
                   VALUES (?, ?, ?, ?)""",
                (coin, ts, exit_reason, realized_pnl_usd),
            )
            log.info("Recorded CLOSE id=%d %s reason=%s pnl=$%.2f funding=$%.2f",
                     position_id, coin, exit_reason, realized_pnl_usd,
                     funding_collected_usd)

    def mark_error(self, position_id: int, note: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE positions SET status='ERROR', exit_reason=? WHERE id=?",
                (f"ERROR: {note}", position_id),
            )

    # ── queries ────────────────────────────────────────────────────────────

    def list_open(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM positions WHERE status='OPEN' ORDER BY id"
            ))

    def get_open_by_coin(self, coin: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM positions WHERE coin=? AND status='OPEN' LIMIT 1",
                (coin,),
            ).fetchone()

    def open_coins(self) -> List[str]:
        with self._conn() as c:
            return [r["coin"] for r in c.execute(
                "SELECT DISTINCT coin FROM positions WHERE status='OPEN'"
            )]

    def last_exit(self, coin: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                """SELECT * FROM exits_history WHERE coin=?
                   ORDER BY exit_timestamp DESC LIMIT 1""",
                (coin,),
            ).fetchone()

    def realized_pnl_since(self, hours: int) -> float:
        """Sum realized PnL of closes within the last N hours."""
        with self._conn() as c:
            cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
            cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
            row = c.execute(
                """SELECT COALESCE(SUM(realized_pnl_usd), 0) AS s
                   FROM exits_history WHERE exit_timestamp >= ?""",
                (cutoff_iso,),
            ).fetchone()
            return float(row["s"]) if row else 0.0

    def total_realized_pnl(self) -> float:
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0) AS s FROM exits_history"
            ).fetchone()
            return float(row["s"]) if row else 0.0

    # ── adapter for signal engine ──────────────────────────────────────────

    @staticmethod
    def to_position_state(row: sqlite3.Row) -> PositionState:
        return PositionState(
            coin=row["coin"],
            direction=row["direction"],
            entry_price=row["entry_price"],
            entry_timestamp_iso=row["entry_timestamp"],
            entry_funding_apr=row["entry_funding_apr"] or 0.0,
        )

    # ── funding accrual update ─────────────────────────────────────────────

    def add_funding_collected(self, position_id: int, amount_usd: float) -> None:
        """Increment cumulative funding for an open position."""
        with self._conn() as c:
            c.execute(
                "UPDATE positions SET funding_collected_usd = "
                "COALESCE(funding_collected_usd, 0) + ? WHERE id=?",
                (amount_usd, position_id),
            )
