"""SQLite 存储层：建表、连接管理与进程内事务锁。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS legal_entity (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 控股关系的时态区间（左闭右开）。同一子公司同一时段只能有一个控股股东，
-- 防止新旧集团在重叠区间重复并表。
CREATE TABLE IF NOT EXISTS ownership (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_code      TEXT NOT NULL,
    subsidiary_code TEXT NOT NULL,
    share           INTEGER NOT NULL CHECK (share > 0 AND share <= 10000),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,
    note            TEXT NOT NULL DEFAULT '',
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ownership_open
    ON ownership(subsidiary_code, valid_from)
    WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS filing_batch (
    code           TEXT PRIMARY KEY,
    group_code     TEXT NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    state          TEXT NOT NULL,
    snapshot_json  TEXT NOT NULL,
    snapshot_hash  TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    sealed_at      TEXT
);

CREATE TABLE IF NOT EXISTS filing_line (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code   TEXT NOT NULL REFERENCES filing_batch(code),
    entity_code  TEXT NOT NULL,
    kind         TEXT NOT NULL,           -- 总分项标识，如 revenue / tax
    amount_cents INTEGER NOT NULL
);

-- 内部交易标记：一条物理交易只登记一次，买卖双方引用同一 txn_id；
-- is_internal 由服务端依据登记时点的控股关系裁定，不可由调用方直接指定。
CREATE TABLE IF NOT EXISTS internal_txn (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id        TEXT NOT NULL,
    batch_code    TEXT NOT NULL REFERENCES filing_batch(code),
    seller_code   TEXT NOT NULL,
    buyer_code    TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    is_internal   INTEGER NOT NULL,
    decided_by    TEXT NOT NULL,
    decided_at    TEXT NOT NULL,
    UNIQUE(txn_id, batch_code)
);

-- 合并抵销分录：每个批次内同一内部交易只允许一条抵销，杜绝重复计算。
CREATE TABLE IF NOT EXISTS elimination (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code   TEXT NOT NULL REFERENCES filing_batch(code),
    txn_id       TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(batch_code, txn_id)
);

-- 法人独立责任展开（封存时固化，便于逐法人追责）。
CREATE TABLE IF NOT EXISTS entity_liability (
    batch_code   TEXT NOT NULL REFERENCES filing_batch(code),
    entity_code  TEXT NOT NULL,
    standalone_cents  INTEGER NOT NULL,
    share_pct    INTEGER,
    attributed_cents  INTEGER NOT NULL,
    eliminated_cents  INTEGER NOT NULL,
    member_from  TEXT,
    member_to    TEXT,
    PRIMARY KEY (batch_code, entity_code)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    basis_hash  TEXT,
    detail_json TEXT NOT NULL,
    UNIQUE(batch_code, seq)
);

CREATE TABLE IF NOT EXISTS audit_diff (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code  TEXT NOT NULL,
    change_type TEXT NOT NULL,           -- exit / retroactive / restructure / concurrent
    ref_key     TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    actor       TEXT NOT NULL,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_batch ON audit_diff(batch_code, id);
"""


class Store:
    """持有单个 SQLite 连接，所有写操作串行化以保证批次级可串行化。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()
