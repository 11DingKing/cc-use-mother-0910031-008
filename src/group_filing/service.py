"""合并申报领域服务。

设计要点
========

* **时态股权**：每条控股关系是半开区间 ``[effective_from, effective_to)``；
  同一父-子链区间不得重叠，新增开放区间自动截断旧的开放区间，全部追加留痕。
* **期间加权合并**：成员资格按「申报年度内被集团控制的日期段」判定，
  集团总额按覆盖天数加权，年中并购/退出不会把旧数据整段并入新集团。
* **内部交易标记**：交易按发生日判定双方是否同属一个集团；
  发生日不受控制的交易列入 ``excluded`` 并给出原因，绝不参与抵销。
* **组织快照**：草案生成时把成员、控制区间、路径、阈值固化为快照；
  每次重新生成产生不可变修订版本，版本间可结构化 diff。
* **签署封存**：批次走完 草稿→待核算→已确认→执行中→已封存；
  封存校验全部成员已申报、标记与申报数核对一致，封存后报告永久冻结。
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from .errors import ConflictError, NotFoundError, ValidationError
from .store import DEFAULT_RULES, Store, dumps, loads

# ---------- 常量 ----------

FILING_DRAFT, FILING_SUBMITTED, FILING_CORRECTED = "草案", "已申报", "已更正"
CONTROL_THRESHOLD_PCT = 50.0
EPS = 0.01  # 积分/金额核对容差

# 允许的批次状态流转
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "草稿": ("待核算",),
    "待核算": ("已确认", "草稿"),
    "已确认": ("执行中", "已封存"),
    "执行中": ("已封存",),
    "已封存": (),
}


# ---------- 时间与字段工具 ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str, field: str = "date") -> date:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 文本")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 日期格式无效：{value!r}") from exc


def period_bounds(period_label: str) -> tuple[date, date]:
    """年度期间标签 ``YYYY`` → 半开区间 [1月1日, 次年1月1日)。"""
    if not (isinstance(period_label, str) and len(period_label) == 4 and period_label.isdigit()):
        raise ValidationError("期间标签必须是四位年度，例如 2026")
    year = int(period_label)
    return date(year, 1, 1), date(year + 1, 1, 1)


def _require(payload: dict, key: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise ValidationError(f"缺少必填字段：{key}")
    return payload[key]


def _share(payload: dict, key: str = "share_pct") -> float:
    value = _require(payload, key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 < value <= 100):
        raise ValidationError(f"{key} 必须是 0 到 100 之间的数值（不含 0）")
    return float(value)


def _amount(payload: dict, key: str) -> float:
    value = _require(payload, key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{key} 必须是非负数")
    return float(value)


def _round(value: float) -> float:
    return round(float(value) + 0.0, 2)


# ---------- 股权图 ----------

def _active_links(conn: sqlite3.Connection, on: date) -> list[tuple[str, str, float]]:
    rows = conn.execute(
        """SELECT parent_id, child_id, share_pct FROM ownership_links
           WHERE effective_from <= ? AND (effective_to IS NULL OR ? < effective_to)""",
        (on.isoformat(), on.isoformat()),
    ).fetchall()
    return [(r["parent_id"], r["child_id"], r["share_pct"]) for r in rows]


def _control_graph(
    links: Iterable[tuple[str, str, float]], root: str
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """从 root 出发求每个法人的最大累计持股比例（多路径取最大）与对应路径。"""
    out: dict[str, list[tuple[str, float]]] = {}
    for parent, child, share in links:
        out.setdefault(parent, []).append((child, share / 100.0))
    best_share: dict[str, float] = {root: 1.0}
    best_path: dict[str, list[str]] = {root: [root]}
    stack = [root]
    while stack:
        node = stack.pop()
        base = best_share[node]
        for child, share in out.get(node, ()):  # 环不会使最大比例增大，松弛自然终止
            candidate = base * share
            if candidate > best_share.get(child, -1.0) + 1e-12:
                best_share[child] = candidate
                best_path[child] = best_path[node] + [child]
                stack.append(child)
    return best_share, best_path


def _merge_intervals(segments: list[tuple[date, date]]) -> list[list[str]]:
    if not segments:
        return []
    segments = sorted(segments)
    merged: list[list[date]] = [list(segments[0])]
    for start, end in segments[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [[a.isoformat(), b.isoformat()] for a, b in merged]


# ====================================================================
# 服务
# ====================================================================

class FilingService:
    def __init__(self, store: Store | str | None = None) -> None:
        self.store = store if isinstance(store, Store) else Store(store or ":memory:")
        with self.store.txn_lock():
            conn = self.store.connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for code, name, basis, sign, memo in DEFAULT_RULES:
                    conn.execute(
                        "INSERT OR IGNORE INTO elimination_rules"
                        "(code,name,basis,sign,memo,created_at) VALUES(?,?,?,?,?,?)",
                        (code, name, basis, sign, memo, now_iso()),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _txn(self, write: bool = True):
        conn = self.store.connect()
        with self.store.txn_lock():
            if write:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                if write:
                    conn.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                if write:
                    conn.execute("ROLLBACK")
                if "locked" in str(exc).lower():
                    raise ConflictError("数据库正被其他提交占用，请重试（乐观冲突）") from exc
                raise
            except sqlite3.IntegrityError as exc:
                if write:
                    conn.execute("ROLLBACK")
                raise ConflictError(f"唯一性/引用约束冲突：{exc}") from exc
            except Exception:
                if write:
                    conn.execute("ROLLBACK")
                raise

    # ---------------- 审计 ----------------

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        batch_id: str | None = None,
        version_from: int | None = None,
        version_to: int | None = None,
        detail: Any = None,
    ) -> None:
        conn.execute(
            """INSERT INTO audit_log(ts,actor,action,entity_type,entity_id,batch_id,
                                     version_from,version_to,detail_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (now_iso(), actor, action, entity_type, str(entity_id), batch_id,
             version_from, version_to, dumps(detail or {})),
        )

    # ---------------- 法人 ----------------

    def create_entity(self, payload: dict) -> dict:
        entity_id = _require(payload, "entity_id")
        name = _require(payload, "name")
        actor = payload.get("actor") or payload.get("created_by") or "企业申报员"
        with self._txn() as conn:
            if conn.execute("SELECT 1 FROM legal_entities WHERE entity_id=?", (entity_id,)).fetchone():
                raise ConflictError(f"法人已存在：{entity_id}")
            conn.execute(
                "INSERT INTO legal_entities(entity_id,name,created_at,detail_json) VALUES(?,?,?,?)",
                (entity_id, name, now_iso(), dumps(payload.get("detail") or {})),
            )
            self._audit(conn, actor=actor, action="entity_created",
                        entity_type="legal_entity", entity_id=entity_id, detail={"name": name})
        return {"entity_id": entity_id, "name": name}

    def list_entities(self) -> list[dict]:
        with self._txn(write=False) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT entity_id,name,created_at FROM legal_entities ORDER BY entity_id")]

    # ---------------- 时态控股关系 ----------------

    def add_ownership_link(self, payload: dict) -> dict:
        parent_id = _require(payload, "parent_id")
        child_id = _require(payload, "child_id")
        if parent_id == child_id:
            raise ValidationError("父级与子级不能是同一法人")
        share = _share(payload)
        effective_from = parse_date(_require(payload, "effective_from"), "effective_from")
        effective_to = (parse_date(payload["effective_to"], "effective_to")
                        if payload.get("effective_to") else None)
        if effective_to and effective_to <= effective_from:
            raise ValidationError("effective_to 必须晚于 effective_from")
        actor = payload.get("actor") or "交易运营员"

        with self._txn() as conn:
            for eid in (parent_id, child_id):
                if not conn.execute("SELECT 1 FROM legal_entities WHERE entity_id=?", (eid,)).fetchone():
                    raise ValidationError(f"法人不存在：{eid}")
            rows = conn.execute(
                """SELECT id, effective_from, effective_to FROM ownership_links
                   WHERE parent_id=? AND child_id=? ORDER BY effective_from""",
                (parent_id, child_id),
            ).fetchall()
            for row in rows:
                start = date.fromisoformat(row["effective_from"])
                end = date.fromisoformat(row["effective_to"]) if row["effective_to"] else None
                # 半开区间重叠判定（端点相接不算重叠）
                if effective_from < (end or date.max) and start < (effective_to or date.max):
                    raise ConflictError(
                        f"控股区间重叠：{parent_id}→{child_id} 已有 "
                        f"{start.isoformat()}~{end.isoformat() if end else '至今'} 的记录")
            supersedes = None
            closed: list[dict] = []
            if effective_to is None:
                # 新增开放区间时，自动截断同链旧的开放区间
                for row in rows:
                    if row["effective_to"] is None:
                        old_start = date.fromisoformat(row["effective_from"])
                        if old_start < effective_from:
                            conn.execute(
                                "UPDATE ownership_links SET effective_to=? WHERE id=?",
                                (effective_from.isoformat(), row["id"]))
                            supersedes = row["id"]
                            closed.append({"link_id": row["id"],
                                           "closed_from": effective_from.isoformat()})
            cur = conn.execute(
                """INSERT INTO ownership_links(parent_id,child_id,share_pct,
                       effective_from,effective_to,supersedes_id,created_at,note)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (parent_id, child_id, share, effective_from.isoformat(),
                 effective_to.isoformat() if effective_to else None,
                 supersedes, now_iso(), payload.get("note", "")))
            link_id = cur.lastrowid
            self._audit(conn, actor=actor, action="ownership_link_added",
                        entity_type="ownership_link", entity_id=link_id,
                        detail={"parent_id": parent_id, "child_id": child_id,
                                "share_pct": share,
                                "effective_from": effective_from.isoformat(),
                                "effective_to": effective_to.isoformat() if effective_to else None,
                                "supersedes_id": supersedes, "closed": closed})
        return {"link_id": link_id, "parent_id": parent_id, "child_id": child_id,
                "share_pct": share, "effective_from": effective_from.isoformat(),
                "effective_to": effective_to.isoformat() if effective_to else None,
                "supersedes_id": supersedes, "closed_intervals": closed}

    def list_ownership_links(self) -> list[dict]:
        with self._txn(write=False) as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id,parent_id,child_id,share_pct,effective_from,effective_to,
                          supersedes_id FROM ownership_links ORDER BY effective_from,id""")]

    def group_structure(self, root_id: str, on: str | None = None) -> dict:
        """某一时点的集团股权结构（审计用）。"""
        on_date = parse_date(on, "date") if on else date.today()
        with self._txn(write=False) as conn:
            if not conn.execute("SELECT 1 FROM legal_entities WHERE entity_id=?", (root_id,)).fetchone():
                raise NotFoundError(f"法人不存在：{root_id}")
            links = _active_links(conn, on_date)
        shares, paths = _control_graph(links, root_id)
        nodes = []
        for eid in sorted(shares):
            if eid == root_id:
                continue
            pct = shares[eid] * 100
            nodes.append({
                "entity_id": eid,
                "cumulative_share_pct": _round(pct),
                "controlled": pct >= CONTROL_THRESHOLD_PCT - EPS,
                "path": paths[eid],
            })
        return {"root_entity_id": root_id, "as_of": on_date.isoformat(),
                "control_threshold_pct": CONTROL_THRESHOLD_PCT, "members": nodes}

    # ---------------- 内部交易标记 ----------------

    def mark_intercompany_txn(self, payload: dict) -> dict:
        period = _require(payload, "period_label")
        start, end = period_bounds(period)
        seller_id = _require(payload, "seller_id")
        buyer_id = _require(payload, "buyer_id")
        if seller_id == buyer_id:
            raise ValidationError("内部交易买卖双方不能是同一法人")
        amount = _amount(payload, "amount")
        unrealized = float(payload.get("unrealized_profit") or 0.0)
        if unrealized < 0:
            raise ValidationError("unrealized_profit 必须是非负数")
        ref_no = _require(payload, "ref_no")
        txn_date = parse_date(payload.get("txn_date") or start, "txn_date")
        if not (start <= txn_date < end):
            raise ValidationError(f"txn_date 必须落在期间 {period} 内")
        actor = payload.get("actor") or "交易运营员"

        with self._txn() as conn:
            for eid in (seller_id, buyer_id):
                if not conn.execute("SELECT 1 FROM legal_entities WHERE entity_id=?", (eid,)).fetchone():
                    raise ValidationError(f"法人不存在：{eid}")
            # 已封存批次固化后，涉及其成员的期间标记不得再追加，保证封存可复核
            sealed = conn.execute(
                """SELECT b.batch_id FROM filing_batches b
                   JOIN batch_members m ON m.batch_id=b.batch_id
                   WHERE b.period_label=? AND b.status='已封存'
                     AND m.entity_id IN (?,?) LIMIT 1""",
                (period, seller_id, buyer_id),
            ).fetchone()
            if sealed:
                raise ConflictError(
                    f"期间 {period} 已有封存批次 {sealed['batch_id']} 涉及该交易双方法人，"
                    "封存口径内的内部交易标记不可再追加")
            note = dumps({"txn_date": txn_date.isoformat(),
                          "unrealized_profit": unrealized,
                          "note": payload.get("note", "")})
            cur = conn.execute(
                """INSERT INTO intercompany_txns(period_label,seller_id,buyer_id,amount,
                       ref_no,marked_by,marked_at,note)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (period, seller_id, buyer_id, amount, ref_no, actor, now_iso(), note))
            txn_id = cur.lastrowid
            self._audit(conn, actor=actor, action="ic_txn_marked",
                        entity_type="intercompany_txn", entity_id=txn_id,
                        detail={"period_label": period, "seller_id": seller_id,
                                "buyer_id": buyer_id, "amount": amount, "ref_no": ref_no,
                                "txn_date": txn_date.isoformat(),
                                "unrealized_profit": unrealized})
        return {"txn_id": txn_id, "period_label": period, "seller_id": seller_id,
                "buyer_id": buyer_id, "amount": amount, "ref_no": ref_no,
                "txn_date": txn_date.isoformat(), "unrealized_profit": unrealized}

    def list_intercompany_txns(self, period: str) -> list[dict]:
        period_bounds(period)
        with self._txn(write=False) as conn:
            rows = conn.execute(
                "SELECT * FROM intercompany_txns WHERE period_label=? ORDER BY id", (period,))
            result = []
            for r in rows:
                item = {k: r[k] for k in ("id", "period_label", "seller_id", "buyer_id",
                                          "amount", "ref_no", "marked_by", "marked_at")}
                item.update(loads(r["note"]))
                result.append(item)
            return result

    # ---------------- 抵销规则 ----------------

    def list_rules(self) -> list[dict]:
        with self._txn(write=False) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT code,name,basis,sign,memo,active FROM elimination_rules ORDER BY id")]

    # ---------------- 批次 ----------------

    def create_batch(self, payload: dict) -> dict:
        period = _require(payload, "period_label")
        period_bounds(period)
        root_entity_id = _require(payload, "root_entity_id")
        actor = payload.get("actor") or payload.get("created_by") or "企业申报员"
        batch_id = payload.get("batch_id") or f"B-{period}-{root_entity_id}-{uuid.uuid4().hex[:8]}"
        with self._txn() as conn:
            if conn.execute("SELECT 1 FROM filing_batches WHERE batch_id=?", (batch_id,)).fetchone():
                raise ConflictError(f"批次已存在：{batch_id}")
            if not conn.execute("SELECT 1 FROM legal_entities WHERE entity_id=?", (root_entity_id,)).fetchone():
                raise ValidationError(f"根法人不存在：{root_entity_id}")
            conn.execute(
                """INSERT INTO filing_batches(batch_id,period_label,root_entity_id,status,
                       version,created_by,created_at)
                   VALUES(?,?,?,?,1,?,?)""",
                (batch_id, period, root_entity_id, "草稿", actor, now_iso()))
            conn.execute("INSERT INTO batch_members(batch_id,entity_id) VALUES(?,?)",
                         (batch_id, root_entity_id))
            self._audit(conn, actor=actor, action="batch_created",
                        entity_type="batch", entity_id=batch_id,
                        detail={"period_label": period, "root_entity_id": root_entity_id})
        return self.get_batch(batch_id)

    def _get_batch(self, conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM filing_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise NotFoundError(f"批次不存在：{batch_id}")
        return row

    def get_batch(self, batch_id: str) -> dict:
        with self._txn(write=False) as conn:
            row = self._get_batch(conn, batch_id)
            data = {k: row[k] for k in row.keys() if not k.endswith("_json")}
            data["has_snapshot"] = row["snapshot_json"] is not None
            return data

    def list_batches(self) -> list[dict]:
        with self._txn(write=False) as conn:
            return [dict(r) for r in conn.execute(
                """SELECT batch_id,period_label,root_entity_id,status,version,
                          created_at,submitted_at,sealed_at,sealed_by
                   FROM filing_batches ORDER BY id""")]

    # ---------------- 组织快照 ----------------

    def _build_snapshot(self, conn: sqlite3.Connection, batch: sqlite3.Row) -> dict:
        period = batch["period_label"]
        root = batch["root_entity_id"]
        start, end = period_bounds(period)
        period_days = (end - start).days

        # 收集期间内所有可能改变控制权的边界点，逐段切片
        boundaries = {start, end}
        for r in conn.execute("SELECT effective_from, effective_to FROM ownership_links"):
            d = date.fromisoformat(r["effective_from"])
            if start < d < end:
                boundaries.add(d)
            if r["effective_to"]:
                d2 = date.fromisoformat(r["effective_to"])
                if start < d2 < end:
                    boundaries.add(d2)
        grid = sorted(boundaries)

        weighted_days: dict[str, float] = {}
        controlled_days: dict[str, int] = {}
        controlled_segments: dict[str, list[tuple[date, date]]] = {}
        for seg_start, seg_end in zip(grid, grid[1:]):
            days = (seg_end - seg_start).days
            shares, _ = _control_graph(_active_links(conn, seg_start), root)
            for eid, share in shares.items():
                if eid == root:
                    continue
                if share * 100 >= CONTROL_THRESHOLD_PCT - EPS:
                    weighted_days[eid] = weighted_days.get(eid, 0.0) + share * days
                    controlled_days[eid] = controlled_days.get(eid, 0) + days
                    controlled_segments.setdefault(eid, []).append((seg_start, seg_end))

        end_shares, end_paths = _control_graph(_active_links(conn, end - timedelta(days=1)), root)
        start_shares, _ = _control_graph(_active_links(conn, start), root)
        names = {r["entity_id"]: r["name"]
                 for r in conn.execute("SELECT entity_id,name FROM legal_entities")}

        members = []
        for eid in sorted(weighted_days):
            avg_share = weighted_days[eid] / controlled_days[eid]
            members.append({
                "entity_id": eid,
                "name": names.get(eid, eid),
                "coverage": _round(controlled_days[eid] / period_days),
                # 精确因子供合并计算使用，coverage 仅用于展示
                "attribution_factor": controlled_days[eid] / period_days,
                # 受控期间的平均持股（用于少数股东权益），不受覆盖率稀释
                "period_weighted_share_pct": _round(avg_share * 100),
                "period_weighted_share": avg_share,
                "controlled_days": controlled_days[eid],
                "period_days": period_days,
                "active_at_period_start":
                    start_shares.get(eid, 0) * 100 >= CONTROL_THRESHOLD_PCT - EPS,
                "active_at_period_end":
                    end_shares.get(eid, 0) * 100 >= CONTROL_THRESHOLD_PCT - EPS,
                "share_at_period_end_pct":
                    _round(end_shares[eid] * 100) if eid in end_shares else None,
                "controlled_intervals": _merge_intervals(controlled_segments[eid]),
                "path_at_period_end": end_paths.get(eid),
            })
        return {
            "schema_version": 1,
            "period_label": period,
            "root_entity_id": root,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "control_threshold_pct": CONTROL_THRESHOLD_PCT,
            "generated_at": now_iso(),
            "members": members,
        }

    def generate_draft(self, batch_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        actor = payload.get("actor") or "企业申报员"
        with self._txn() as conn:
            batch = self._get_batch(conn, batch_id)
            if batch["status"] != "草稿":
                raise ConflictError(f"批次当前为 {batch['status']}，仅草稿状态可（重新）生成草案")
            old_snapshot = loads(batch["snapshot_json"])
            snapshot = self._build_snapshot(conn, batch)

            member_ids = {snapshot["root_entity_id"], *(m["entity_id"] for m in snapshot["members"])}
            conn.execute("DELETE FROM batch_members WHERE batch_id=?", (batch_id,))
            conn.executemany(
                "INSERT INTO batch_members(batch_id,entity_id) VALUES(?,?)",
                [(batch_id, eid) for eid in sorted(member_ids)])
            # 退出成员的申报数据不属于本快照，但保留为审计痕迹（status 置为「已剔除」）
            conn.execute(
                "UPDATE filings SET status='已剔除', version=version+1 "
                "WHERE batch_id=? AND entity_id NOT IN (%s) AND status!='已剔除'"
                % ",".join("?" * len(member_ids)),
                [batch_id, *sorted(member_ids)])
            # 成员重新进入快照（如重组回退）：恢复可编辑，版本递增可在审计中追溯
            conn.execute(
                "UPDATE filings SET status='草案', version=version+1 "
                "WHERE batch_id=? AND entity_id IN (%s) AND status='已剔除'"
                % ",".join("?" * len(member_ids)),
                [batch_id, *sorted(member_ids)])

            report = self._compute_report(conn, batch, snapshot)
            new_version = batch["version"] if old_snapshot is None else batch["version"] + 1
            conn.execute(
                "UPDATE filing_batches SET version=?, snapshot_json=?, totals_json=? WHERE batch_id=?",
                (new_version, dumps(snapshot), dumps(report["totals"]), batch_id))
            conn.execute(
                """INSERT INTO batch_revisions(batch_id,version,created_at,actor,
                       snapshot_json,totals_json) VALUES(?,?,?,?,?,?)""",
                (batch_id, new_version, now_iso(), actor,
                 dumps(snapshot), dumps(report["totals"])))
            self._audit(conn, actor=actor,
                        action="draft_generated" if old_snapshot is None else "draft_regenerated",
                        entity_type="batch", entity_id=batch_id, batch_id=batch_id,
                        version_from=batch["version"], version_to=new_version,
                        detail={"old_members": [m["entity_id"]
                                                for m in (old_snapshot or {}).get("members", [])],
                                "new_members": [m["entity_id"] for m in snapshot["members"]]})
        return self.get_report(batch_id)

    # ---------------- 成员申报 ----------------

    def _find_filing(self, conn: sqlite3.Connection, batch_id: str,
                     entity_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM filings WHERE batch_id=? AND entity_id=?",
            (batch_id, entity_id)).fetchone()

    def _assert_member(self, conn: sqlite3.Connection, batch_id: str, entity_id: str) -> None:
        if not conn.execute(
            "SELECT 1 FROM batch_members WHERE batch_id=? AND entity_id=?",
            (batch_id, entity_id)).fetchone():
            raise ValidationError(f"法人 {entity_id} 不在批次 {batch_id} 的组织快照内")

    def _filing_view(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        return {k: row[k] for k in row.keys()}

    def upsert_filing(self, batch_id: str, payload: dict) -> dict:
        entity_id = _require(payload, "entity_id")
        points = _amount(payload, "declared_points")
        sale = float(payload.get("intercompany_sale") or 0)
        purchase = float(payload.get("intercompany_purchase") or 0)
        if min(sale, purchase) < 0:
            raise ValidationError("内部交易金额必须是非负数")
        actor = payload.get("actor") or "企业申报员"
        with self._txn() as conn:
            batch = self._get_batch(conn, batch_id)
            if batch["status"] != "草稿":
                raise ConflictError(f"批次当前为 {batch['status']}，草案填报已关闭，请走追溯更正")
            self._assert_member(conn, batch_id, entity_id)
            existing = self._find_filing(conn, batch_id, entity_id)
            if existing and existing["status"] not in (FILING_DRAFT,):
                raise ConflictError("该法人申报已提交或被剔除，请走追溯更正流程")
            if existing:
                conn.execute(
                    """UPDATE filings SET declared_points=?, intercompany_sale=?,
                           intercompany_purchase=?, version=version+1 WHERE id=?""",
                    (points, sale, purchase, existing["id"]))
                version = existing["version"] + 1
                action = "filing_updated"
            else:
                cur = conn.execute(
                    """INSERT INTO filings(batch_id,entity_id,declared_points,
                           intercompany_purchase,intercompany_sale,status,version)
                       VALUES(?,?,?,?,?,?,1)""",
                    (batch_id, entity_id, points, purchase, sale, FILING_DRAFT))
                version = 1
                action = "filing_drafted"
            self._audit(conn, actor=actor, action=action,
                        entity_type="filing", entity_id=entity_id, batch_id=batch_id,
                        version_to=version,
                        detail={"declared_points": points,
                                "intercompany_sale": sale,
                                "intercompany_purchase": purchase})
            row = self._find_filing(conn, batch_id, entity_id)
            view = self._filing_view(conn, row)
        return view

    def submit_filing(self, batch_id: str, entity_id: str, payload: dict) -> dict:
        actor = payload.get("actor") or "企业申报员"
        expected = payload.get("expected_version")
        with self._txn() as conn:
            batch = self._get_batch(conn, batch_id)
            if batch["status"] not in ("草稿", "待核算"):
                raise ConflictError(f"批次当前为 {batch['status']}，成员申报不能提交")
            self._assert_member(conn, batch_id, entity_id)
            filing = self._find_filing(conn, batch_id, entity_id)
            if not filing:
                raise NotFoundError("该法人尚未起草申报")
            if expected is not None and int(expected) != filing["version"]:
                raise ConflictError(
                    f"申报版本冲突：期望 {expected}，当前 {filing['version']}")
            if filing["status"] == FILING_DRAFT:
                conn.execute(
                    """UPDATE filings SET status=?, submitted_by=?, submitted_at=?,
                           version=version+1 WHERE id=?""",
                    (FILING_SUBMITTED, actor, now_iso(), filing["id"]))
                self._audit(conn, actor=actor, action="filing_submitted",
                            entity_type="filing", entity_id=entity_id, batch_id=batch_id,
                            version_from=filing["version"],
                            version_to=filing["version"] + 1, detail={})
            row = self._find_filing(conn, batch_id, entity_id)
            view = self._filing_view(conn, row)
        return view

    def correct_filing(self, batch_id: str, entity_id: str, payload: dict) -> dict:
        """追溯更正：仅 草稿/待核算 批次允许；原值进入审计日志。"""
        actor = payload.get("actor") or "企业申报员"
        reason = _require(payload, "correction_reason")
        expected = payload.get("expected_version")
        with self._txn() as conn:
            batch = self._get_batch(conn, batch_id)
            if batch["status"] not in ("草稿", "待核算"):
                raise ConflictError(f"批次当前为 {batch['status']}，追溯更正已关闭")
            self._assert_member(conn, batch_id, entity_id)
            filing = self._find_filing(conn, batch_id, entity_id)
            if not filing:
                raise NotFoundError("该法人尚未起草申报")
            if expected is not None and int(expected) != filing["version"]:
                raise ConflictError(
                    f"申报版本冲突：期望 {expected}，当前 {filing['version']}")
            points = float(payload.get("declared_points", filing["declared_points"]))
            sale = float(payload.get("intercompany_sale", filing["intercompany_sale"]))
            purchase = float(payload.get("intercompany_purchase", filing["intercompany_purchase"]))
            if min(points, sale, purchase) < 0:
                raise ValidationError("积分与内部交易金额必须是非负数")
            before = {k: filing[k] for k in
                      ("declared_points", "intercompany_sale", "intercompany_purchase", "status")}
            conn.execute(
                """UPDATE filings SET declared_points=?, intercompany_sale=?,
                       intercompany_purchase=?, status=?, corrected_by=?, corrected_at=?,
                       correction_reason=?, version=version+1 WHERE id=?""",
                (points, sale, purchase, FILING_CORRECTED, actor, now_iso(),
                 reason, filing["id"]))
            self._audit(conn, actor=actor, action="filing_corrected",
                        entity_type="filing", entity_id=entity_id, batch_id=batch_id,
                        version_from=filing["version"],
                        version_to=filing["version"] + 1,
                        detail={"before": before,
                                "after": {"declared_points": points,
                                          "intercompany_sale": sale,
                                          "intercompany_purchase": purchase},
                                "reason": reason})
            row = self._find_filing(conn, batch_id, entity_id)
            view = self._filing_view(conn, row)
        return view

    def list_filings(self, batch_id: str) -> list[dict]:
        with self._txn(write=False) as conn:
            self._get_batch(conn, batch_id)
            return [{k: r[k] for k in r.keys()} for r in conn.execute(
                "SELECT * FROM filings WHERE batch_id=? ORDER BY entity_id", (batch_id,))]

    # ---------------- 合并报告（确定性） ----------------

    @staticmethod
    def _intervals_cover(intervals: list[list[str]], day: date) -> bool:
        iso = day.isoformat()
        return any(a <= iso < b for a, b in intervals)

    def _compute_report(self, conn: sqlite3.Connection, batch: sqlite3.Row,
                        snapshot: dict) -> dict:
        """依据固定快照 + 当前申报/标记计算合并报告。

        成员控制区间取自快照（而非最新股权表），因此提交后股权再变化
        不影响在途批次；差异通过退回草稿并重新生成体现。
        """
        period = snapshot["period_label"]
        members: dict[str, dict] = {m["entity_id"]: m for m in snapshot["members"]}
        members[snapshot["root_entity_id"]] = {
            "entity_id": snapshot["root_entity_id"],
            "name": snapshot["root_entity_id"],
            "coverage": 1.0, "attribution_factor": 1.0,
            "period_weighted_share_pct": 100.0, "period_weighted_share": 1.0,
            "active_at_period_start": True, "active_at_period_end": True,
            "share_at_period_end_pct": 100.0,
            "controlled_intervals": [[snapshot["period_start"], snapshot["period_end"]]],
        }
        filings = {r["entity_id"]: r for r in conn.execute(
            "SELECT * FROM filings WHERE batch_id=?", (batch["batch_id"],))}
        txns = conn.execute(
            "SELECT * FROM intercompany_txns WHERE period_label=? ORDER BY id", (period,))
        rules = {r["code"]: r for r in conn.execute(
            "SELECT * FROM elimination_rules WHERE active=1 ORDER BY id")}

        sale_items: list[dict] = []
        purchase_items: list[dict] = []
        profit_items: list[dict] = []
        excluded: list[dict] = []
        marked_sale: dict[str, float] = {}
        marked_purchase: dict[str, float] = {}

        for t in txns:
            tdetail = loads(t["note"], {})
            tday = parse_date(tdetail.get("txn_date"), "txn_date")
            seller_m = members.get(t["seller_id"])
            buyer_m = members.get(t["buyer_id"])
            seller_in = bool(seller_m) and self._intervals_cover(
                seller_m["controlled_intervals"], tday)
            buyer_in = bool(buyer_m) and self._intervals_cover(
                buyer_m["controlled_intervals"], tday)
            item = {"ref_no": t["ref_no"], "seller_id": t["seller_id"],
                    "buyer_id": t["buyer_id"], "amount": _round(t["amount"]),
                    "txn_date": tday.isoformat()}
            if seller_in and buyer_in:
                sale_items.append(item)
                purchase_items.append(item)
                marked_sale[t["seller_id"]] = marked_sale.get(t["seller_id"], 0.0) + t["amount"]
                marked_purchase[t["buyer_id"]] = marked_purchase.get(t["buyer_id"], 0.0) + t["amount"]
                profit = float(tdetail.get("unrealized_profit") or 0.0)
                if profit > 0:
                    profit_items.append({**item, "unrealized_profit": _round(profit)})
            else:
                reasons = []
                if seller_m is None:
                    reasons.append("seller_not_in_snapshot")
                elif not seller_in:
                    reasons.append("seller_not_controlled_at_txn_date")
                if buyer_m is None:
                    reasons.append("buyer_not_in_snapshot")
                elif not buyer_in:
                    reasons.append("buyer_not_controlled_at_txn_date")
                excluded.append({**item, "reasons": reasons})

        member_views = []
        gross = 0.0
        minority_total = 0.0
        flags: list[dict] = []
        for eid in sorted(members):
            m = members[eid]
            f = filings.get(eid)
            coverage = float(m["coverage"])
            factor = float(m.get("attribution_factor", coverage))
            declared = float(f["declared_points"]) if f else 0.0
            attributed = declared * factor
            gross += attributed
            share = float(m.get("period_weighted_share",
                               float(m["period_weighted_share_pct"]) / 100.0))
            nci = (attributed * max(0.0, 1.0 - share)
                   if eid != snapshot["root_entity_id"] else 0.0)
            minority_total += nci
            member_views.append({
                "entity_id": eid,
                "name": m.get("name", eid),
                "filing_status": f["status"] if f else None,
                "filing_version": f["version"] if f else None,
                "coverage": _round(coverage),
                "period_weighted_share_pct": _round(share * 100.0),
                "independent_liability_points": _round(declared),
                "group_attributed_points": _round(attributed),
                "minority_interest_points": _round(nci),
                "intercompany_sale_filed": _round(f["intercompany_sale"]) if f else 0.0,
                "intercompany_purchase_filed": _round(f["intercompany_purchase"]) if f else 0.0,
                "intercompany_sale_marked": _round(marked_sale.get(eid, 0.0)),
                "intercompany_purchase_marked": _round(marked_purchase.get(eid, 0.0)),
                "active_at_period_start": m["active_at_period_start"],
                "active_at_period_end": m["active_at_period_end"],
                "controlled_intervals": m["controlled_intervals"],
            })
            if f is None:
                flags.append({"type": "missing_filing", "entity_id": eid})
            elif f["status"] == FILING_DRAFT:
                flags.append({"type": "unsubmitted_filing", "entity_id": eid})
            if f:
                for side, filed_key, marked in (
                    ("sale", "intercompany_sale", marked_sale),
                    ("purchase", "intercompany_purchase", marked_purchase),
                ):
                    filed_val = float(f[filed_key])
                    marked_val = marked.get(eid, 0.0)
                    if abs(filed_val - marked_val) > EPS:
                        flags.append({"type": f"intercompany_{side}_mismatch",
                                      "entity_id": eid, "filed": _round(filed_val),
                                      "marked": _round(marked_val),
                                      "delta": _round(filed_val - marked_val)})

        sale_total = sum(i["amount"] for i in sale_items)
        purchase_total = sum(i["amount"] for i in purchase_items)
        profit_total = sum(i["unrealized_profit"] for i in profit_items)

        def line(code: str, amount: float, items: list[dict]) -> dict:
            rule = rules[code]
            return {"rule_code": code, "name": rule["name"], "basis": rule["basis"],
                    "sign": rule["sign"], "memo": bool(rule["memo"]),
                    "amount": _round(amount), "entry_count": len(items), "items": items}

        eliminations = [
            line("IC_SALE", sale_total, sale_items),
            line("IC_PURCHASE", purchase_total, purchase_items),
            line("IC_PROFIT", profit_total, profit_items),
            line("MINORITY", minority_total, []),
        ]
        total_elim = sum(e["sign"] * e["amount"] for e in eliminations if not e["memo"])
        consolidated = gross + total_elim
        totals = {
            "period_label": period,
            "group_gross_points": _round(gross),
            "total_eliminations_points": _round(total_elim),
            "consolidated_points": _round(consolidated),
            "minority_interest_points": _round(minority_total),
            "elimination_by_rule": {e["rule_code"]: e["amount"] for e in eliminations},
            "member_count": len(members),
            "missing_filing_count": sum(1 for fl in flags if fl["type"] == "missing_filing"),
            "reconciliation_flag_count": len(flags),
            "computed_at": now_iso(),
        }
        return {
            "batch_id": batch["batch_id"],
            "status": batch["status"],
            "sealed": False,
            "snapshot_version": batch["version"],
            "snapshot": snapshot,
            "totals": totals,
            "eliminations": eliminations,
            "member_liabilities": member_views,
            "excluded_intercompany_txns": excluded,
            "reconciliation_flags": flags,
        }

    def get_report(self, batch_id: str) -> dict:
        with self._txn(write=False) as conn:
            batch = self._get_batch(conn, batch_id)
            if not batch["snapshot_json"]:
                raise ConflictError("批次尚未生成草案组织快照")
            snapshot = loads(batch["snapshot_json"])
            if batch["status"] == "已封存":
                frozen = loads(batch["frozen_report_json"])
                frozen["sealed_at"] = batch["sealed_at"]
                frozen["sealed_by"] = batch["sealed_by"]
                return frozen
            return self._compute_report(conn, batch, snapshot)

    # ---------------- 批次流转与封存 ----------------

    def _check_version(self, batch: sqlite3.Row, expected_version: Any) -> None:
        if expected_version is not None and int(expected_version) != batch["version"]:
            raise ConflictError(
                f"批次版本冲突：期望 {expected_version}，当前 {batch['version']}")

    def submit_batch(self, batch_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        actor = payload.get("actor") or "企业申报员"
        with self._txn() as conn:
            batch = self._get_batch(conn, batch_id)
            if not batch["snapshot_json"]:
                raise ConflictError("请先生成草案组织快照再提交")
            self._check_version(batch, payload.get("expected_version"))
            if "待核算" not in TRANSITIONS[batch["status"]]:
                raise ConflictError(f"批次状态不能从 {batch['status']} 提交")
            conn.execute(
                "UPDATE filing_batches SET status='待核算', version=version+1, submitted_at=? "
                "WHERE batch_id=?",
                (now_iso(), batch_id))
            self._audit(conn, actor=actor, action="batch_submitted",
                        entity_type="batch", entity_id=batch_id, batch_id=batch_id,
                        version_from=batch["version"], version_to=batch["version"] + 1)
        return self.get_batch(batch_id)

    def review_batch(self, batch_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        actor = payload.get("actor") or "核算专员"
        decision = payload.get("decision", "approve")
        if decision not in ("approve", "reject"):
            raise ValidationError("decision 只能是 approve 或 reject")
        target = "已确认" if decision == "approve" else "草稿"
        with self._txn() as conn:
            batch = self._get_batch(conn, batch_id)
            self._check_version(batch, payload.get("expected_version"))
            if target not in TRANSITIONS[batch["status"]]:
                raise ConflictError(f"批次状态不能从 {batch['status']} 流转到 {target}")
            conn.execute(
                "UPDATE filing_batches SET status=?, version=version+1 WHERE batch_id=?",
                (target, batch_id))
            self._audit(conn, actor=actor,
                        action=f"batch_review_{decision}",
                        entity_type="batch", entity_id=batch_id, batch_id=batch_id,
                        version_from=batch["version"], version_to=batch["version"] + 1,
                        detail={"decision": decision, "note": payload.get("note", "")})
        return self.get_batch(batch_id)

    def execute_batch(self, batch_id: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        with self._txn() as conn:
            batch = self._get_batch(conn, batch_id)
            self._check_version(batch, payload.get("expected_version"))
            if "执行中" not in TRANSITIONS[batch["status"]]:
                raise ConflictError(f"批次状态不能从 {batch['status']} 进入执行中")
            conn.execute(
                "UPDATE filing_batches SET status='执行中', version=version+1 WHERE batch_id=?",
                (batch_id,))
            self._audit(conn, actor=payload.get("actor") or "交易运营员",
                        action="batch_execute_started",
                        entity_type="batch", entity_id=batch_id, batch_id=batch_id,
                        version_from=batch["version"], version_to=batch["version"] + 1)
        return self.get_batch(batch_id)

    def seal_batch(self, batch_id: str, payload: dict | None = None) -> dict:
        """达到签署条件后封存：成员全部已申报、内部交易标记与申报数核对一致。"""
        payload = payload or {}
        actor = payload.get("actor") or "监管审计员"
        with self._txn() as conn:
            batch = self._get_batch(conn, batch_id)
            if batch["status"] not in ("已确认", "执行中"):
                raise ConflictError(f"批次当前为 {batch['status']}，不满足封存前置状态")
            self._check_version(batch, payload.get("expected_version"))
            snapshot = loads(batch["snapshot_json"])
            report = self._compute_report(conn, batch, snapshot)
            blocking = list(report["reconciliation_flags"])
            if blocking:
                raise ConflictError("未达到封存条件：" + dumps(blocking))
            sealed_at = now_iso()
            report["status"] = "已封存"
            report["sealed"] = True
            report["sealed_at"] = sealed_at
            report["sealed_by"] = actor
            conn.execute(
                """UPDATE filing_batches SET status='已封存', version=version+1,
                       sealed_at=?, sealed_by=?, frozen_report_json=? WHERE batch_id=?""",
                (sealed_at, actor, dumps(report), batch_id))
            conn.execute(
                "INSERT INTO batch_revisions(batch_id,version,created_at,actor,"
                "snapshot_json,totals_json) VALUES(?,?,?,?,?,?)",
                (batch_id, batch["version"] + 1, sealed_at, actor,
                 dumps(snapshot), dumps(report["totals"])))
            self._audit(conn, actor=actor, action="batch_sealed",
                        entity_type="batch", entity_id=batch_id, batch_id=batch_id,
                        version_from=batch["version"], version_to=batch["version"] + 1,
                        detail={"consolidated_points": report["totals"]["consolidated_points"]})
        return self.get_report(batch_id)

    # ---------------- 修订与差异 ----------------

    def list_revisions(self, batch_id: str) -> list[dict]:
        with self._txn(write=False) as conn:
            self._get_batch(conn, batch_id)
            rows = conn.execute(
                """SELECT version,created_at,actor,snapshot_json,totals_json
                   FROM batch_revisions WHERE batch_id=? ORDER BY version""",
                (batch_id,)).fetchall()
            return [{"version": r["version"], "created_at": r["created_at"], "actor": r["actor"],
                     "member_count": len(loads(r["snapshot_json"])["members"]),
                     "consolidated_points": loads(r["totals_json"])["consolidated_points"]}
                    for r in rows]

    def diff_revisions(self, batch_id: str, from_version: int, to_version: int) -> dict:
        with self._txn(write=False) as conn:
            self._get_batch(conn, batch_id)
            snaps: dict[int, dict] = {}
            totals: dict[int, dict] = {}
            for v in (from_version, to_version):
                row = conn.execute(
                    "SELECT snapshot_json,totals_json FROM batch_revisions "
                    "WHERE batch_id=? AND version=?",
                    (batch_id, v)).fetchone()
                if not row:
                    raise NotFoundError(f"批次修订版本不存在：{v}")
                snaps[v] = loads(row["snapshot_json"])
                totals[v] = loads(row["totals_json"])

        old_m = {m["entity_id"]: m for m in snaps[from_version]["members"]}
        new_m = {m["entity_id"]: m for m in snaps[to_version]["members"]}
        added, removed, changed = [], [], []
        for eid in sorted(set(old_m) | set(new_m)):
            if eid in old_m and eid not in new_m:
                removed.append({"entity_id": eid,
                                "old_coverage": old_m[eid]["coverage"],
                                "old_weighted_share_pct":
                                    old_m[eid]["period_weighted_share_pct"]})
            elif eid not in old_m:
                added.append({"entity_id": eid,
                              "new_coverage": new_m[eid]["coverage"],
                              "new_weighted_share_pct":
                                  new_m[eid]["period_weighted_share_pct"]})
            else:
                a, b = old_m[eid], new_m[eid]
                member_diff = {}
                for key in ("coverage", "period_weighted_share_pct", "controlled_days"):
                    if a.get(key) != b.get(key):
                        member_diff[key] = {"old": a.get(key), "new": b.get(key)}
                if a.get("controlled_intervals") != b.get("controlled_intervals"):
                    member_diff["controlled_intervals"] = {
                        "old": a.get("controlled_intervals"),
                        "new": b.get("controlled_intervals")}
                if member_diff:
                    changed.append({"entity_id": eid, "changes": member_diff})

        totals_diff = []
        for key in ("group_gross_points", "total_eliminations_points",
                    "consolidated_points", "minority_interest_points"):
            if totals[from_version].get(key) != totals[to_version].get(key):
                totals_diff.append({"field": key,
                                    "old": totals[from_version].get(key),
                                    "new": totals[to_version].get(key)})
        elim_diff = []
        rules = set(totals[from_version].get("elimination_by_rule", {})) | \
            set(totals[to_version].get("elimination_by_rule", {}))
        for code in sorted(rules):
            a = totals[from_version].get("elimination_by_rule", {}).get(code, 0.0)
            b = totals[to_version].get("elimination_by_rule", {}).get(code, 0.0)
            if _round(a) != _round(b):
                elim_diff.append({"rule_code": code, "old": _round(a), "new": _round(b)})
        return {
            "batch_id": batch_id,
            "from_version": from_version,
            "to_version": to_version,
            "membership": {"added": added, "removed": removed, "changed": changed},
            "totals": totals_diff,
            "eliminations": elim_diff,
            "flags": {
                "from": {k: totals[from_version].get(k) for k in
                         ("missing_filing_count", "reconciliation_flag_count")},
                "to": {k: totals[to_version].get(k) for k in
                       ("missing_filing_count", "reconciliation_flag_count")},
            },
        }

    # ---------------- 审计 ----------------

    def audit_trail(self, batch_id: str | None = None, limit: int = 500) -> list[dict]:
        with self._txn(write=False) as conn:
            if batch_id:
                self._get_batch(conn, batch_id)
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE batch_id=? ORDER BY id DESC LIMIT ?",
                    (batch_id, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            result = []
            for r in rows:
                item = {k: r[k] for k in r.keys() if k != "detail_json"}
                item["detail"] = loads(r["detail_json"])
                result.append(item)
            return result

    # ---------------- 跨集团视角 ----------------

    def period_membership_overview(self, period: str) -> dict:
        """同一年度多个集团快照对同一法人的覆盖一览。

        同一法人出现在多个批次中时列出；覆盖率合计超过 100% 标记
        ``potential_double_count=true``，供审计识别跨集团重组重复并入风险。
        """
        period_bounds(period)
        with self._txn(write=False) as conn:
            rows = conn.execute(
                "SELECT batch_id,root_entity_id,snapshot_json FROM filing_batches "
                "WHERE period_label=? ORDER BY batch_id",
                (period,)).fetchall()
        per_entity: dict[str, list[dict]] = {}
        batches = []
        for r in rows:
            snap = loads(r["snapshot_json"])
            if not snap:
                continue
            batches.append({"batch_id": r["batch_id"], "root_entity_id": r["root_entity_id"]})
            for m in snap["members"]:
                per_entity.setdefault(m["entity_id"], []).append({
                    "batch_id": r["batch_id"],
                    "root_entity_id": r["root_entity_id"],
                    "coverage": m["coverage"],
                    "weighted_share_pct": m["period_weighted_share_pct"],
                })
        overlaps = []
        for eid, occ in sorted(per_entity.items()):
            if len(occ) > 1:
                total_cov = sum(o["coverage"] for o in occ)
                overlaps.append({"entity_id": eid, "total_coverage": _round(total_cov),
                                 "potential_double_count": total_cov > 1.0 + EPS,
                                 "in_batches": occ})
        return {"period_label": period, "batches": batches,
                "cross_group_memberships": overlaps}
