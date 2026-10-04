"""SQLite 持久化层。

仅负责连接、建表与基础访问；所有领域判断位于 :mod:`group_filing.service`。
时间统一存储为 ISO-8601 文本，字典以 JSON 文本存储。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS legal_entities (
    entity_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

-- 控股关系的时态区间：[effective_from, effective_to)，NULL 表示至今（开区间）。
-- 同一父-子链在时间轴上不得重叠，由服务层在事务内保证。
CREATE TABLE IF NOT EXISTS ownership_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id       TEXT NOT NULL REFERENCES legal_entities(entity_id),
    child_id        TEXT NOT NULL REFERENCES legal_entities(entity_id),
    share_pct       REAL NOT NULL,
    effective_from  TEXT NOT NULL,
    effective_to    TEXT,
    supersedes_id   INTEGER REFERENCES ownership_links(id),
    created_at      TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    UNIQUE(parent_id, child_id, effective_from)
);

CREATE TABLE IF NOT EXISTS filing_batches (
    batch_id        TEXT PRIMARY KEY,
    period_label    TEXT NOT NULL,
    root_entity_id  TEXT NOT NULL REFERENCES legal_entities(entity_id),
    status          TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    submitted_at    TEXT,
    sealed_at       TEXT,
    sealed_by       TEXT,
    snapshot_json   TEXT,
    totals_json     TEXT,
    frozen_report_json TEXT
);

CREATE TABLE IF NOT EXISTS batch_members (
    batch_id    TEXT NOT NULL REFERENCES filing_batches(batch_id),
    entity_id   TEXT NOT NULL REFERENCES legal_entities(entity_id),
    PRIMARY KEY (batch_id, entity_id)
);

-- 草案每次（重新）生成写入一行不可变修订，支撑版本间结构化差异。
CREATE TABLE IF NOT EXISTS batch_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL REFERENCES filing_batches(batch_id),
    version       INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    actor         TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    totals_json   TEXT NOT NULL,
    UNIQUE(batch_id, version)
);

CREATE TABLE IF NOT EXISTS filings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id                TEXT NOT NULL REFERENCES filing_batches(batch_id),
    entity_id               TEXT NOT NULL REFERENCES legal_entities(entity_id),
    declared_points         REAL NOT NULL,
    intercompany_purchase   REAL NOT NULL DEFAULT 0,
    intercompany_sale       REAL NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT '草案',
    version                 INTEGER NOT NULL DEFAULT 1,
    submitted_by            TEXT,
    submitted_at            TEXT,
    corrected_by            TEXT,
    corrected_at            TEXT,
    correction_reason       TEXT NOT NULL DEFAULT '',
    UNIQUE(batch_id, entity_id)
);

-- 内部交易标记：同一期间同一对法人同一业务编号仅允许登记一次。
-- txn_date / unrealized_profit / 备注统一收纳在 note 的 JSON 中。
CREATE TABLE IF NOT EXISTS intercompany_txns (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    period_label            TEXT NOT NULL,
    seller_id               TEXT NOT NULL REFERENCES legal_entities(entity_id),
    buyer_id                TEXT NOT NULL REFERENCES legal_entities(entity_id),
    amount                  REAL NOT NULL,
    ref_no                  TEXT NOT NULL,
    marked_by               TEXT NOT NULL,
    marked_at               TEXT NOT NULL,
    note                    TEXT NOT NULL DEFAULT '{}',
    UNIQUE(period_label, seller_id, buyer_id, ref_no)
);

-- memo=1 表示仅列示（如少数股东权益），不进入合并积分抵减。
CREATE TABLE IF NOT EXISTS elimination_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    basis       TEXT NOT NULL,
    sign        INTEGER NOT NULL,
    memo        INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    batch_id     TEXT,
    version_from INTEGER,
    version_to   INTEGER,
    detail_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_log(batch_id);
CREATE INDEX IF NOT EXISTS idx_links_child ON ownership_links(child_id, effective_from);
CREATE INDEX IF NOT EXISTS idx_filings_batch ON filings(batch_id);
CREATE INDEX IF NOT EXISTS idx_txns_period ON intercompany_txns(period_label);
CREATE INDEX IF NOT EXISTS idx_revisions_batch ON batch_revisions(batch_id, version);
"""

# (code, 名称, 基数, 方向, 是否仅列示)
DEFAULT_RULES: Sequence[tuple[str, str, str, int, int]] = (
    ("IC_SALE", "内部销售收入抵销", "sale", -1, 0),
    ("IC_PURCHASE", "内部采购成本抵销", "purchase", -1, 0),
    ("IC_PROFIT", "内部交易未实现损益抵销", "profit", -1, 0),
    ("MINORITY", "少数股东权益分拆（仅列示，不抵减积分）", "minority", 1, 1),
)


class Store:
    """数据访问封装。

    文件库每个工作线程取独立连接，依靠 WAL + busy_timeout 串行化写入；
    内存库共享单连接并以进程锁串行化（主要供测试使用）。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.is_memory = self.path == ":memory:"
        if not self.is_memory:
            parent = Path(self.path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        self._mem: sqlite3.Connection | None = None
        self._mem_lock = threading.RLock()
        self._local = threading.local()
        if self.is_memory:
            with self._mem_lock:
                self._mem = self._connect()
                self._mem.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,  # 显式 BEGIN/COMMIT
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def connect(self) -> sqlite3.Connection:
        if self.is_memory:
            return self._mem  # type: ignore[return-value]
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            conn.executescript(SCHEMA)
            self._local.conn = conn
        return conn

    def txn_lock(self):
        """内存库返回进程锁作为事务互斥手段；文件库返回空上下文。"""
        if self.is_memory:
            return self._mem_lock

        class _Null:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Null()

    def close(self) -> None:
        if self._mem is not None:
            self._mem.close()
            self._mem = None


# ---------- JSON 辅助 ----------

def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
