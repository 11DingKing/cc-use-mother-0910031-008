"""核心领域服务：时态股权、批次状态机、内部交易、合并抵销、快照封存、审计差异。"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from .models import STATE_TRANSITIONS, FilingState
from .storage import Store

def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class DomainError(Exception):
    """业务规则冲突（4xx）。"""


class NotFound(DomainError):
    """资源不存在（404）。"""


class ConcurrentModification(DomainError):
    """并发提交：期望序号与服务端当前序号不一致（409）。"""


class GroupFilingService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ 基础

    @staticmethod
    def _today() -> str:
        return datetime.now().date().isoformat()

    def _audit(self, conn, batch: str, actor: str, action: str, detail: dict,
               basis_hash: str | None = None) -> int:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM audit_log WHERE batch_code=?", (batch,)).fetchone()
        seq = row["m"] + 1
        conn.execute(
            "INSERT INTO audit_log(batch_code, seq, ts, actor, action, basis_hash, detail_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (batch, seq, datetime.now().isoformat(timespec="seconds"), actor, action,
             basis_hash, canonical(detail)),
        )
        return seq

    def _diff(self, conn, batch: str, change_type: str, ref_key: str,
              before: Any, after: Any, actor: str) -> None:
        conn.execute(
            "INSERT INTO audit_diff(batch_code, change_type, ref_key, before_json, after_json, actor, ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (batch, change_type, ref_key,
             canonical(before) if before is not None else None,
             canonical(after) if after is not None else None,
             actor, datetime.now().isoformat(timespec="seconds")),
        )

    def register_entity(self, code: str, name: str) -> None:
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO legal_entity(code, name) VALUES(?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name",
                (code, name),
            )

    # ----------------------------------------------------------- 时态股权关系

    def put_ownership(self, group_code: str, subsidiary_code: str, share_pct: float,
                      valid_from: str, actor: str, note: str = "") -> dict:
        """登记一段新的控股关系。若该子公司存在开放区间，则在 valid_from 处截断，

        保证同一子公司任意时点至多一个控股股东。集团变更即跨集团重组。"""
        if not (0 < share_pct <= 100):
            raise DomainError("持股比例必须在 (0,100] 区间")
        date.fromisoformat(valid_from)
        share = int(round(share_pct * 100))
        conn = self.store.conn
        with self.store.lock:
            open_row = conn.execute(
                "SELECT * FROM ownership WHERE subsidiary_code=? AND valid_to IS NULL",
                (subsidiary_code,),
            ).fetchone()
            if open_row is not None and open_row["valid_from"] >= valid_from:
                raise DomainError("新生效日不得早于既有开放区间的生效日；如需改写历史请使用追溯更正")
            # 与历史闭区间重叠校验
            hit = conn.execute(
                "SELECT 1 FROM ownership WHERE subsidiary_code=? AND valid_to IS NOT NULL "
                "AND valid_from < ? AND valid_to > ?",
                (subsidiary_code, valid_from, valid_from),
            ).fetchone()
            if hit:
                raise DomainError("与已封闭的历史区间重叠，需走追溯更正流程")

            change_type = "restructure" if (open_row and open_row["group_code"] != group_code) else "ownership"
            if open_row is not None:
                conn.execute("UPDATE ownership SET valid_to=? WHERE id=?", (valid_from, open_row["id"]))
            cur = conn.execute(
                "INSERT INTO ownership(group_code, subsidiary_code, share, valid_from, valid_to, note) "
                "VALUES(?,?,?,?,NULL,?)",
                (group_code, subsidiary_code, share, valid_from, note),
            )
            # 重组/退出对在途草稿形成差异
            if change_type == "restructure":
                self._diff_drafts_for_subsidiary(conn, subsidiary_code, "restructure",
                                                 {"group_code": open_row["group_code"],
                                                  "valid_to": valid_from},
                                                 {"group_code": group_code, "valid_from": valid_from},
                                                 actor)
            return {"ownership_id": cur.lastrowid, "change_type": change_type}

    def correct_ownership(self, ownership_id: int, actor: str, *,
                          share_pct: float | None = None,
                          valid_from: str | None = None,
                          valid_to: str | None = None,
                          reason: str = "") -> dict:
        """追溯更正已登记的股权区间。历史被改写本身必须留下 before/after 差异，

        且只影响之后重新固化的快照，已封存批次永不改变。"""
        conn = self.store.conn
        with self.store.lock:
            row = conn.execute("SELECT * FROM ownership WHERE id=?", (ownership_id,)).fetchone()
            if row is None:
                raise NotFound("股权区间不存在")
            before = {k: row[k] for k in row.keys()}
            new_share = int(round(share_pct * 100)) if share_pct is not None else row["share"]
            new_from = valid_from or row["valid_from"]
            new_to = valid_to if valid_to is not None else row["valid_to"]
            if new_to is not None and new_to <= new_from:
                raise DomainError("生效区间结束日必须晚于开始日")
            conn.execute("UPDATE ownership SET share=?, valid_from=?, valid_to=? WHERE id=?",
                         (new_share, new_from, new_to, ownership_id))
            after = dict(before, share=new_share, valid_from=new_from, valid_to=new_to)
            for b in self._open_batches(conn, row["group_code"]):
                self._diff(conn, b["code"], "retroactive",
                           f"ownership:{ownership_id}", before, after, actor)
                self._audit(conn, b["code"], actor, "追溯更正",
                            {"ownership_id": ownership_id, "reason": reason,
                             "before": before, "after": after})
            return {"before": before, "after": after}

    def mark_exit(self, subsidiary_code: str, exit_date: str, actor: str) -> None:
        """子公司退出：关闭开放区间，并在所有受影响在途批次中登记 exit 差异。"""
        date.fromisoformat(exit_date)
        conn = self.store.conn
        with self.store.lock:
            row = conn.execute(
                "SELECT * FROM ownership WHERE subsidiary_code=? AND valid_to IS NULL",
                (subsidiary_code,),
            ).fetchone()
            if row is None:
                raise DomainError("该子公司没有开放的控股区间")
            if exit_date <= row["valid_from"]:
                raise DomainError("退出日必须晚于当前控股生效日")
            conn.execute("UPDATE ownership SET valid_to=? WHERE id=?", (exit_date, row["id"]))
            before = {"group_code": row["group_code"], "valid_to": None}
            after = {"group_code": row["group_code"], "valid_to": exit_date}
            self._diff_drafts_for_subsidiary(conn, subsidiary_code, "exit", before, after, actor)

    def _diff_drafts_for_subsidiary(self, conn, subsidiary_code: str, change_type: str,
                                    before: dict, after: dict, actor: str) -> None:
        rows = conn.execute(
            "SELECT b.code FROM filing_batch b WHERE b.state != ? "
            "AND EXISTS (SELECT 1 FROM ownership o WHERE o.group_code=b.group_code "
            "            AND o.subsidiary_code=? "
            "            AND o.valid_from < b.period_end "
            "            AND (o.valid_to IS NULL OR o.valid_to > b.period_start))",
            (FilingState.SEALED.value, subsidiary_code),
        ).fetchall()
        for r in rows:
            self._diff(conn, r["code"], change_type, f"subsidiary:{subsidiary_code}",
                       before, after, actor)
            self._audit(conn, r["code"], actor,
                       {"exit": "子公司退出", "restructure": "跨集团重组"}[change_type],
                       {"subsidiary": subsidiary_code, "before": before, "after": after})

    def _open_batches(self, conn, group_code: str):
        return conn.execute(
            "SELECT * FROM filing_batch WHERE group_code=? AND state != ?",
            (group_code, FilingState.SEALED.value),
        ).fetchall()

    def _owner_at(self, conn, entity_code: str, day: str, group_code: str | None = None) -> str | None:
        """交易日控股股东。集团本部法人视为始终归属本集团。"""
        if group_code is not None and entity_code == group_code:
            return group_code
        row = conn.execute(
            "SELECT group_code FROM ownership WHERE subsidiary_code=? "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
            (entity_code, day, day),
        ).fetchone()
        return row["group_code"] if row else None

    def _membership(self, conn, group_code: str, period_start: str, period_end: str) -> list[dict]:
        """集团在申报期内的成员窗口（区间与申报期求交），含集团本部法人。"""
        members: list[dict] = []
        parent = conn.execute("SELECT 1 FROM legal_entity WHERE code=?", (group_code,)).fetchone()
        if parent:
            members.append({"entity_code": group_code, "share": 10000,
                            "member_from": period_start, "member_to": None,
                            "parent": True})
        rows = conn.execute(
            "SELECT subsidiary_code, share, valid_from, valid_to FROM ownership "
            "WHERE group_code=? AND valid_from < ? AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY subsidiary_code, valid_from",
            (group_code, period_end, period_start),
        ).fetchall()
        for r in rows:
            mfrom = max(r["valid_from"], period_start)
            mto = min(r["valid_to"], period_end) if r["valid_to"] else None
            members.append({"entity_code": r["subsidiary_code"], "share": r["share"],
                            "member_from": mfrom, "member_to": mto, "parent": False})
        return members

    def _ownership_basis(self, conn, group_code: str, period_start: str, period_end: str) -> str:
        rows = conn.execute(
            "SELECT group_code, subsidiary_code, share, valid_from, valid_to FROM ownership "
            "WHERE group_code=? AND valid_from < ? AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY subsidiary_code, valid_from",
            (group_code, period_end, period_start),
        ).fetchall()
        return digest([dict(r) for r in rows])

    # --------------------------------------------------------------- 申报批次

    def create_draft(self, code: str, group_code: str, period_start: str,
                     period_end: str, actor: str) -> dict:
        date.fromisoformat(period_start)
        date.fromisoformat(period_end)
        if period_end <= period_start:
            raise DomainError("申报期结束日必须晚于开始日")
        conn = self.store.conn
        with self.store.lock:
            if conn.execute("SELECT 1 FROM filing_batch WHERE code=?", (code,)).fetchone():
                raise DomainError("批次编号已存在")
            members = self._membership(conn, group_code, period_start, period_end)
            snapshot = {"group_code": group_code, "period": [period_start, period_end],
                        "members": members, "fixed_at": datetime.now().isoformat(timespec="seconds")}
            h = digest({"members": members, "basis": self._ownership_basis(
                conn, group_code, period_start, period_end)})
            conn.execute(
                "INSERT INTO filing_batch(code, group_code, period_start, period_end, state, "
                "snapshot_json, snapshot_hash, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (code, group_code, period_start, period_end, FilingState.DRAFT.value,
                 canonical(snapshot), h, actor, datetime.now().isoformat(timespec="seconds")),
            )
            self._audit(conn, code, actor, "生成草案", {"snapshot": snapshot}, h)
            return {"code": code, "state": FilingState.DRAFT.value,
                    "member_count": len(members), "snapshot_hash": h}

    def _get_batch(self, conn, code: str):
        row = conn.execute("SELECT * FROM filing_batch WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return row

    def _require_state(self, row, *allowed: FilingState) -> None:
        cur = FilingState(row["state"])
        if cur not in allowed:
            raise DomainError(f"当前状态为「{cur.value}」，不允许该操作")

    def _snapshot_members(self, row) -> dict[str, dict]:
        snap = json.loads(row["snapshot_json"])
        return {m["entity_code"]: m for m in snap["members"]}

    def add_line(self, batch_code: str, entity_code: str, kind: str,
                 amount_yuan: float, actor: str, expected_seq: int | None = None) -> int:
        """登记法人独立申报分项。法人不在固化快照内（如已转投新集团）一律拒绝并入。"""
        conn = self.store.conn
        with self.store.lock:
            row = self._get_batch(conn, batch_code)
            self._require_state(row, FilingState.DRAFT)
            self._check_seq(conn, batch_code, expected_seq, actor)
            members = self._snapshot_members(row)
            if entity_code not in members:
                owner = self._owner_at(conn, entity_code, row["period_end"], row["group_code"])
                self._diff(conn, batch_code, "exclusion", f"entity:{entity_code}:{kind}",
                           None, {"reason": "not_in_snapshot", "current_owner": owner}, actor)
                raise DomainError(
                    f"法人 {entity_code} 不在批次组织快照内，不得并入本集团（现属：{owner or '无'}）")
            cur = conn.execute(
                "INSERT INTO filing_line(batch_code, entity_code, kind, amount_cents) "
                "VALUES(?,?,?,?)",
                (batch_code, entity_code, kind, int(round(amount_yuan * 100))),
            )
            self._audit(conn, batch_code, actor, "申报分项登记",
                        {"line_id": cur.lastrowid, "entity": entity_code,
                         "kind": kind, "amount_yuan": amount_yuan})
            return cur.lastrowid

    # ------------------------------------------------------------- 内部交易

    def register_internal_txn(self, batch_code: str, txn_id: str, seller_code: str,
                              buyer_code: str, trade_date: str, amount_yuan: float,
                              actor: str, expected_seq: int | None = None) -> dict:
        """登记一笔集团内部候选交易。是否内部由系统按交易日控股关系裁定。

        同一 txn_id 只能登记一次，防止买卖双方重复报送导致重复抵销。"""
        date.fromisoformat(trade_date)
        conn = self.store.conn
        with self.store.lock:
            row = self._get_batch(conn, batch_code)
            self._require_state(row, FilingState.DRAFT, FilingState.PENDING)
            self._check_seq(conn, batch_code, expected_seq, actor)
            members = self._snapshot_members(row)
            for side, code in (("卖方", seller_code), ("买方", buyer_code)):
                if code not in members:
                    raise DomainError(f"{side} {code} 不在批次组织快照内")
            seller_owner = self._owner_at(conn, seller_code, trade_date, row["group_code"])
            buyer_owner = self._owner_at(conn, buyer_code, trade_date, row["group_code"])
            is_internal = seller_owner == row["group_code"] and buyer_owner == row["group_code"]
            decided = "internal" if is_internal else "external"
            try:
                conn.execute(
                    "INSERT INTO internal_txn(txn_id, batch_code, seller_code, buyer_code, "
                    "trade_date, amount_cents, is_internal, decided_by, decided_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (txn_id, batch_code, seller_code, buyer_code, trade_date,
                     int(round(amount_yuan * 100)), int(is_internal),
                      decided, datetime.now().isoformat(timespec="seconds")),
                )
            except Exception as exc:  # sqlite3.IntegrityError
                raise DomainError(f"交易 {txn_id} 已登记，不得重复报送") from exc
            self._audit(conn, batch_code, actor, "内部交易标记",
                        {"txn_id": txn_id, "seller": seller_code, "buyer": buyer_code,
                         "trade_date": trade_date, "amount_yuan": amount_yuan,
                         "ruling": decided,
                         "owners": {"seller": seller_owner, "buyer": buyer_owner}})
            return {"txn_id": txn_id, "ruling": decided}

    # ------------------------------------------------------------- 状态迁移

    def transition(self, batch_code: str, target: str, actor: str,
                   expected_seq: int | None = None) -> dict:
        conn = self.store.conn
        with self.store.lock:
            row = self._get_batch(conn, batch_code)
            cur = FilingState(row["state"])
            try:
                tgt = FilingState(target)
            except ValueError as exc:
                raise DomainError(f"未知目标状态：{target}") from exc
            if tgt not in STATE_TRANSITIONS[cur]:
                raise DomainError(f"不允许从「{cur.value}」迁移到「{tgt.value}」")
            self._check_seq(conn, batch_code, expected_seq, actor)

            if tgt == FilingState.PENDING:
                # 进入核算：固定内部交易抵销；每笔交易仅生成一条抵销分录。
                self._build_eliminations(conn, row, actor)
            if tgt == FilingState.SEALED:
                self._seal(conn, row, actor)

            conn.execute("UPDATE filing_batch SET state=? WHERE code=?", (tgt.value, batch_code))
            detail = {"from": cur.value, "to": tgt.value}
            seq = self._audit(conn, batch_code, actor, f"状态迁移:{cur.value}->{tgt.value}", detail)
            return {"code": batch_code, "state": tgt.value, "seq": seq}

    def _build_eliminations(self, conn, row, actor: str) -> list[dict]:
        batch = row["code"]
        txns = conn.execute(
            "SELECT txn_id, amount_cents FROM internal_txn "
            "WHERE batch_code=? AND is_internal=1", (batch,),
        ).fetchall()
        created = []
        for t in txns:
            cur = conn.execute(
                "INSERT OR IGNORE INTO elimination(batch_code, txn_id, amount_cents, reason, created_at) "
                "VALUES(?,?,?,?,?)",
                (batch, t["txn_id"], t["amount_cents"], "集团内部交易合并抵销",
                 datetime.now().isoformat(timespec="seconds")),
            )
            if cur.rowcount:
                created.append({"txn_id": t["txn_id"], "amount_cents": t["amount_cents"]})
        if created:
            self._audit(conn, batch, actor, "生成合并抵销",
                        {"eliminations": created, "count": len(created)})
        return created

    def _seal(self, conn, row, actor: str) -> None:
        batch = row["code"]
        members = self._snapshot_members(row)
        # 封存前重新比对组织基础：期间发生过退出/重组/追溯更正且未重新固化则拒绝封存。
        basis = self._ownership_basis(conn, row["group_code"], row["period_start"], row["period_end"])
        stored = json.loads(row["snapshot_json"])
        if digest({"members": stored["members"], "basis": basis}) != row["snapshot_hash"]:
            raise DomainError("组织关系自草案固化后已变更，请先重新固化组织快照再封存")

        lines = {c: 0 for c in members}
        for r in conn.execute(
            "SELECT entity_code, SUM(amount_cents) AS s FROM filing_line WHERE batch_code=? GROUP BY entity_code",
            (batch,),
        ):
            lines[r["entity_code"]] = r["s"]
        eliminated = {c: 0 for c in members}
        for r in conn.execute(
            "SELECT seller_code, SUM(amount_cents) AS s FROM internal_txn "
            "WHERE batch_code=? AND is_internal=1 GROUP BY seller_code", (batch,),
        ):
            if r["seller_code"] in eliminated:
                eliminated[r["seller_code"]] = r["s"]

        conn.execute("DELETE FROM entity_liability WHERE batch_code=?", (batch,))
        for code, m in members.items():
            standalone = lines.get(code, 0)
            elim = eliminated.get(code, 0)
            attributed = round(standalone * m["share"] / 10000) - elim
            conn.execute(
                "INSERT INTO entity_liability(batch_code, entity_code, standalone_cents, "
                "share_pct, attributed_cents, eliminated_cents, member_from, member_to) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (batch, code, standalone, m["share"], attributed, elim,
                 m["member_from"], m["member_to"]),
            )
        conn.execute("UPDATE filing_batch SET sealed_at=? WHERE code=?",
                     (datetime.now().isoformat(timespec="seconds"), batch))
        self._audit(conn, batch, actor, "封存批次", {"members": len(members)})

    def resnapshot(self, batch_code: str, actor: str, expected_seq: int | None = None) -> dict:
        """重新固化组织快照（退出/重组/追溯更正后使用），新旧快照形成可审计差异。

        已登记但不再属于快照的法人分项将被摘除并逐条留痕。"""
        conn = self.store.conn
        with self.store.lock:
            row = self._get_batch(conn, batch_code)
            self._require_state(row, FilingState.DRAFT, FilingState.PENDING,
                                FilingState.CONFIRMED, FilingState.EXECUTING)
            self._check_seq(conn, batch_code, expected_seq, actor)
            old_state = FilingState(row["state"])
            old = json.loads(row["snapshot_json"])
            new_members = self._membership(conn, row["group_code"], row["period_start"], row["period_end"])
            new_snap = {**old, "members": new_members,
                        "fixed_at": datetime.now().isoformat(timespec="seconds")}
            h = digest({"members": new_members, "basis": self._ownership_basis(
                conn, row["group_code"], row["period_start"], row["period_end"])})
            conn.execute("UPDATE filing_batch SET snapshot_json=?, snapshot_hash=? WHERE code=?",
                         (canonical(new_snap), h, batch_code))

            old_codes = {m["entity_code"] for m in old["members"]}
            new_codes = {m["entity_code"] for m in new_members}
            for gone in sorted(old_codes - new_codes):
                removed = conn.execute(
                    "DELETE FROM filing_line WHERE batch_code=? AND entity_code=?",
                    (batch_code, gone),
                ).rowcount
                conn.execute(
                    "DELETE FROM internal_txn WHERE batch_code=? AND (seller_code=? OR buyer_code=?)",
                    (batch_code, gone, gone),
                )
                self._diff(conn, batch_code, "exit", f"member:{gone}",
                           {"member": gone, "removed_lines": removed}, None, actor)
            # 清理失去交易支撑的抵销分录
            conn.execute(
                "DELETE FROM elimination WHERE batch_code=? AND txn_id NOT IN "
                "(SELECT txn_id FROM internal_txn WHERE batch_code=?)",
                (batch_code, batch_code),
            )
            # 股权历史被追溯改写后，逐笔按交易日重新裁定内部/外部，反转处留差异
            for t in conn.execute(
                "SELECT * FROM internal_txn WHERE batch_code=?", (batch_code,)
            ).fetchall():
                s_owner = self._owner_at(conn, t["seller_code"], t["trade_date"], row["group_code"])
                b_owner = self._owner_at(conn, t["buyer_code"], t["trade_date"], row["group_code"])
                now_internal = int(s_owner == row["group_code"] and b_owner == row["group_code"])
                if now_internal != t["is_internal"]:
                    self._diff(conn, batch_code, "retroactive", f"txn:{t['txn_id']}",
                               {"is_internal": bool(t["is_internal"])},
                               {"is_internal": bool(now_internal),
                                "owners": {"seller": s_owner, "buyer": b_owner}}, actor)
                    conn.execute("UPDATE internal_txn SET is_internal=?, decided_by=? WHERE id=?",
                                 (now_internal, "re-ruled", t["id"]))
            if old_state in (FilingState.CONFIRMED, FilingState.EXECUTING):
                # 组织基础变了，核算结论作废，强制退回待核算重新确认
                conn.execute("UPDATE filing_batch SET state=? WHERE code=?",
                             (FilingState.PENDING.value, batch_code))
            self._build_eliminations(conn, row, actor)
            for added in sorted(new_codes - old_codes):
                self._diff(conn, batch_code, "restructure", f"member:{added}",
                           None, {"member": added}, actor)
            for m in new_members:
                old_m = next((x for x in old["members"] if x["entity_code"] == m["entity_code"]), None)
                if old_m and old_m != m:
                    self._diff(conn, batch_code, "retroactive", f"member:{m['entity_code']}",
                               old_m, m, actor)
            self._audit(conn, batch_code, actor, "重新固化组织快照",
                        {"old_hash": row["snapshot_hash"], "new_hash": h,
                         "removed": sorted(old_codes - new_codes),
                         "added": sorted(new_codes - old_codes)}, h)
            return {"snapshot_hash": h,
                    "removed": sorted(old_codes - new_codes),
                    "added": sorted(new_codes - old_codes)}

    def _check_seq(self, conn, batch: str, expected_seq: int | None, actor: str) -> None:
        """乐观并发：expected_seq 与当前审计序号不一致即 409，并记录并发冲突差异。"""
        if expected_seq is None:
            return
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM audit_log WHERE batch_code=?",
                           (batch,)).fetchone()
        if row["m"] != expected_seq:
            self._diff(conn, batch, "concurrent", f"seq:{expected_seq}",
                       {"expected_seq": expected_seq, "actual_seq": row["m"]},
                       {"rejected": True}, actor)
            self._audit(conn, batch, actor, "并发提交冲突",
                        {"expected_seq": expected_seq, "actual_seq": row["m"]})
            raise ConcurrentModification(
                f"批次已被其他提交更新（期望序号 {expected_seq}，实际 {row['m']}）")

    # ----------------------------------------------------------------- 查询

    def expansion(self, batch_code: str) -> dict:
        """展开集团总额、抵销项与各法人独立责任。"""
        conn = self.store.conn
        with self.store.lock:
            row = self._get_batch(conn, batch_code)
            lines_total = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS s FROM filing_line WHERE batch_code=?",
                (batch_code,),
            ).fetchone()["s"]
            elim_rows = conn.execute(
                "SELECT txn_id, amount_cents, reason FROM elimination WHERE batch_code=? ORDER BY txn_id",
                (batch_code,),
            ).fetchall()
            elim_total = sum(r["amount_cents"] for r in elim_rows)

            pending_internal = conn.execute(
                "SELECT txn_id, amount_cents FROM internal_txn "
                "WHERE batch_code=? AND is_internal=1 AND txn_id NOT IN "
                "(SELECT txn_id FROM elimination WHERE batch_code=?)",
                (batch_code, batch_code),
            ).fetchall()

            liab = []
            use_table = row["state"] == FilingState.SEALED.value
            if use_table:
                src = conn.execute(
                    "SELECT * FROM entity_liability WHERE batch_code=? ORDER BY entity_code",
                    (batch_code,),
                ).fetchall()
                for r in src:
                    liab.append({"entity_code": r["entity_code"],
                                 "standalone_yuan": r["standalone_cents"] / 100,
                                 "share_pct": (r["share_pct"] or 10000) / 100,
                                 "eliminated_yuan": r["eliminated_cents"] / 100,
                                 "attributed_yuan": r["attributed_cents"] / 100,
                                 "member_window": [r["member_from"], r["member_to"]]})
            else:
                members = self._snapshot_members(row)
                sums: dict[str, int] = {}
                for r in conn.execute(
                    "SELECT entity_code, SUM(amount_cents) AS s FROM filing_line "
                    "WHERE batch_code=? GROUP BY entity_code", (batch_code,),
                ):
                    sums[r["entity_code"]] = r["s"]
                elims: dict[str, int] = {}
                for r in conn.execute(
                    "SELECT seller_code, SUM(amount_cents) AS s FROM internal_txn "
                    "WHERE batch_code=? AND is_internal=1 GROUP BY seller_code", (batch_code,),
                ):
                    elims[r["seller_code"]] = r["s"]
                for code, m in sorted(members.items()):
                    std = sums.get(code, 0)
                    em = elims.get(code, 0)
                    liab.append({"entity_code": code,
                                 "standalone_yuan": std / 100,
                                 "share_pct": m["share"] / 100,
                                 "eliminated_yuan": em / 100,
                                 "attributed_yuan": round(std * m["share"] / 10000 - em) / 100,
                                 "member_window": [m["member_from"], m["member_to"]]})

            return {"code": batch_code, "state": row["state"],
                    "group_total_yuan": (lines_total - elim_total) / 100,
                    "lines_total_yuan": lines_total / 100,
                    "eliminations": [{"txn_id": r["txn_id"], "amount_yuan": r["amount_cents"] / 100,
                                      "reason": r["reason"]} for r in elim_rows],
                    "eliminations_total_yuan": elim_total / 100,
                    "pending_internal_count": len(pending_internal),
                    "entities": liab,
                    "sealed": use_table}

    def diffs(self, batch_code: str) -> list[dict]:
        conn = self.store.conn
        with self.store.lock:
            self._get_batch(conn, batch_code)
            rows = conn.execute(
                "SELECT id, change_type, ref_key, before_json, after_json, actor, ts "
                "FROM audit_diff WHERE batch_code=? ORDER BY id", (batch_code,),
            ).fetchall()
            out = []
            for r in rows:
                out.append({"id": r["id"], "change_type": r["change_type"], "ref_key": r["ref_key"],
                            "before": json.loads(r["before_json"]) if r["before_json"] else None,
                            "after": json.loads(r["after_json"]) if r["after_json"] else None,
                            "actor": r["actor"], "ts": r["ts"]})
            return out

    def audit_trail(self, batch_code: str) -> list[dict]:
        conn = self.store.conn
        with self.store.lock:
            self._get_batch(conn, batch_code)
            rows = conn.execute(
                "SELECT seq, ts, actor, action, basis_hash, detail_json "
                "FROM audit_log WHERE batch_code=? ORDER BY seq", (batch_code,),
            ).fetchall()
            return [{"seq": r["seq"], "ts": r["ts"], "actor": r["actor"],
                     "action": r["action"], "basis_hash": r["basis_hash"],
                     "detail": json.loads(r["detail_json"])} for r in rows]

    def current_seq(self, batch_code: str) -> int:
        conn = self.store.conn
        with self.store.lock:
            self._get_batch(conn, batch_code)
            return conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM audit_log WHERE batch_code=?",
                (batch_code,),
            ).fetchone()["m"]
