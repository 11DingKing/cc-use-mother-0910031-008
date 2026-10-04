"""合并申报核心场景回归测试。

覆盖：
1. 年中控股变更：旧集团不得把退出后的法人数据并入；
2. 内部交易去重与合并抵销：同一交易只抵销一次；
3. 追溯更正 / 跨集团重组：形成 before/after 差异，封存不可变；
4. 并发提交：乐观序号冲突产生 409 与 concurrent 差异；
5. 封存后可展开集团总额、抵销项、各法人独立责任。
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from group_filing.service import (  # noqa: E402
    ConcurrentModification,
    DomainError,
    GroupFilingService,
)
from group_filing.storage import Store  # noqa: E402


def bootstrap() -> GroupFilingService:
    svc = GroupFilingService(Store(":memory:"))
    for code, name in [("G", "集团本部"), ("S1", "甲子公司"), ("S2", "乙子公司"),
                       ("S3", "丙子公司"), ("G2", "另一集团")]:
        svc.register_entity(code, name)
    # G 自 2026-01-01 控股 S1、S2；S3 于年中 7 月退出
    svc.put_ownership("G", "S1", 100, "2026-01-01", "运营员")
    svc.put_ownership("G", "S2", 60, "2026-01-01", "运营员")
    svc.put_ownership("G", "S3", 100, "2026-01-01", "运营员")
    return svc


class CoreScenariosTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = bootstrap()

    def test_mid_year_exit_excluded_from_old_group(self) -> None:
        """场景一：S3 年中 7 月退出；年度批次重新固化后不得并入旧集团。"""
        svc = self.svc
        svc.create_draft("B1", "G", "2026-01-01", "2027-01-01", "申报员")
        # 草案生成后 S3 于 7 月退出
        svc.mark_exit("S3", "2026-07-01", "审计员")
        diffs = svc.diffs("B1")
        self.assertTrue(any(d["change_type"] == "exit" for d in diffs))
        # 退出后的组织变更未重新固化：迁移到待核算后，封存必须被拒绝
        svc.add_line("B1", "S1", "revenue", 1000, "申报员")
        svc.transition("B1", "待核算", "核算专员")
        svc.transition("B1", "已确认", "核算专员")
        svc.transition("B1", "执行中", "核算专员")
        with self.assertRaises(DomainError):
            svc.transition("B1", "已封存", "审计员")
        # 重新固化：S3 区间 [01-01,07-01) 与年度期间相交仍保留为成员，
        # 但成员窗口被截断至退出日
        result = svc.resnapshot("B1", "申报员")
        detail = svc.expansion("B1")
        windows = {e["entity_code"]: e["member_window"] for e in detail["entities"]}
        self.assertEqual(windows["S3"], ["2026-01-01", "2026-07-01"])
        self.assertEqual(result["removed"], [])
        # 重新固化后强制退回「待核算」重新走确认链，满足签署条件后封存
        svc.transition("B1", "已确认", "核算专员")
        svc.transition("B1", "执行中", "核算专员")
        svc.transition("B1", "已封存", "审计员")
        self.assertEqual(svc.expansion("B1")["state"], "已封存")

    def test_data_after_exit_window_rejected(self) -> None:
        """退出日之后成立的新批次（如次年申报）不含 S3，强行登记被拒。"""
        svc = self.svc
        svc.mark_exit("S3", "2026-07-01", "审计员")
        svc.create_draft("B2", "G", "2027-01-01", "2028-01-01", "申报员")
        with self.assertRaises(DomainError):
            svc.add_line("B2", "S3", "revenue", 500, "申报员")
        exclusion = [d for d in svc.diffs("B2") if d["change_type"] == "exclusion"]
        self.assertEqual(len(exclusion), 1)

    def test_internal_transaction_ruled_and_eliminated_once(self) -> None:
        """场景二：内部交易自动标记、去重、仅抵销一次；外部交易不抵销。"""
        svc = self.svc
        svc.create_draft("B3", "G", "2026-01-01", "2027-01-01", "申报员")
        svc.add_line("B3", "S1", "revenue", 1000, "申报员")
        svc.add_line("B3", "S2", "revenue", 400, "申报员")
        # S1 -> S2 内部销售 300
        r1 = svc.register_internal_txn("B3", "T1", "S1", "S2", "2026-03-01", 300, "运营员")
        self.assertEqual(r1["ruling"], "internal")
        # 同一 txn_id 重复报送被拒
        with self.assertRaises(DomainError):
            svc.register_internal_txn("B3", "T1", "S1", "S2", "2026-03-01", 300, "运营员")
        # 登记一笔外部交易（买方 G2 不在集团内）—— 买方不在快照内，直接拒绝并入
        with self.assertRaises(DomainError):
            svc.register_internal_txn("B3", "T2", "S1", "G2", "2026-04-01", 200, "运营员")

        svc.transition("B3", "待核算", "核算专员")
        detail = svc.expansion("B3")
        self.assertEqual(detail["eliminations_total_yuan"], 300.0)
        self.assertEqual(len(detail["eliminations"]), 1)
        # 集团总额 = 1400 - 300
        self.assertEqual(detail["group_total_yuan"], 1100.0)
        # 重复进入待核算不产生第二条抵销
        svc.transition("B3", "已确认", "核算专员")
        svc.transition("B3", "待核算", "核算员")  # 退回
        svc.transition("B3", "已确认", "核算专员")
        detail = svc.expansion("B3")
        self.assertEqual(len(detail["eliminations"]), 1)

    def test_cross_group_restructure_old_data_not_merged(self) -> None:
        """场景三：S2 年中被 G2 收购。G 的年度批次中 S2 仅按成员窗口保留，

        G2 在收购前不拥有 S2；G2 新批次不会把旧数据并入。"""
        svc = self.svc
        svc.create_draft("B4", "G", "2026-01-01", "2027-01-01", "申报员")
        out = svc.put_ownership("G2", "S2", 100, "2026-07-01", "运营员", note="收购")
        self.assertEqual(out["change_type"], "restructure")
        # 在途批次出现重组差异
        types_ = {d["change_type"] for d in svc.diffs("B4")}
        self.assertIn("restructure", types_)
        svc.resnapshot("B4", "申报员")
        detail = svc.expansion("B4")
        win = {e["entity_code"]: e["member_window"] for e in detail["entities"]}
        self.assertEqual(win["S2"], ["2026-01-01", "2026-07-01"])
        # G2 的年度批次：收购日才开始的区间与全年期间求交
        svc.create_draft("B4G2", "G2", "2026-01-01", "2027-01-01", "申报员")
        d2 = svc.expansion("B4G2")
        win2 = {e["entity_code"]: e["member_window"] for e in d2["entities"]}
        self.assertEqual(win2["S2"], ["2026-07-01", None])
        # 同年 S2 在退出前与 S1 的交易仍属 G
        svc.add_line("B4", "S1", "revenue", 1000, "申报员")
        r = svc.register_internal_txn("B4", "T8", "S1", "S2", "2026-02-01", 200, "运营员")
        self.assertEqual(r["ruling"], "internal")
        # 而退出后的 S1->S2 交易对 G 不再是内部交易
        r2 = svc.register_internal_txn("B4", "T10", "S1", "S2", "2026-09-01", 200, "运营员")
        self.assertEqual(r2["ruling"], "external")

    def test_retroactive_correction_leaves_diff_and_blocks_seal(self) -> None:
        """场景四：追溯更正股权历史，差异可审计，封存前必须重新固化。"""
        svc = self.svc
        svc.create_draft("B5", "G", "2026-01-01", "2027-01-01", "申报员")
        oid = svc.store.conn.execute(
            "SELECT id FROM ownership WHERE subsidiary_code='S2'").fetchone()["id"]
        svc.correct_ownership(oid, "审计员", share_pct=55, reason="复核出资比例")
        d = [x for x in svc.diffs("B5") if x["change_type"] == "retroactive"]
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["before"]["share"], 6000)
        self.assertEqual(d[0]["after"]["share"], 5500)
        svc.add_line("B5", "S2", "revenue", 1000, "申报员")
        svc.resnapshot("B5", "申报员")
        # 重新固化后可走完流程；封存后法人责任按新比例展开
        svc.transition("B5", "待核算", "核算专员")
        svc.transition("B5", "已确认", "核算专员")
        svc.transition("B5", "执行中", "核算专员")
        svc.transition("B5", "已封存", "审计员")
        detail = svc.expansion("B5")
        s2 = next(e for e in detail["entities"] if e["entity_code"] == "S2")
        self.assertEqual(s2["share_pct"], 55.0)
        self.assertEqual(s2["attributed_yuan"], 550.0)
        self.assertTrue(detail["sealed"])
        # 封存为终态
        with self.assertRaises(DomainError):
            svc.transition("B5", "执行中", "审计员")
        with self.assertRaises(DomainError):
            svc.add_line("B5", "S2", "revenue", 1, "申报员")

    def test_concurrent_submit_conflict(self) -> None:
        """场景五：两个提交者基于同一序号，后到者收到 409 且冲突入差异表。"""
        svc = self.svc
        svc.create_draft("B6", "G", "2026-01-01", "2027-01-01", "申报员")
        seq = svc.current_seq("B6")
        svc.add_line("B6", "S1", "revenue", 100, "申报员", expected_seq=seq)
        with self.assertRaises(ConcurrentModification):
            svc.add_line("B6", "S2", "revenue", 200, "申报员", expected_seq=seq)
        conflicts = [d for d in svc.diffs("B6") if d["change_type"] == "concurrent"]
        self.assertEqual(len(conflicts), 1)

    def test_concurrent_threads_serialized(self) -> None:
        """多线程并发写同一批次：带序号校验时恰好一个成功，存储层不产生脏写。"""
        svc = self.svc
        svc.create_draft("B7", "G", "2026-01-01", "2027-01-01", "申报员")
        seq = svc.current_seq("B7")
        outcomes: list[str] = []

        def worker(who: str) -> None:
            try:
                svc.add_line("B7", "S1", "revenue", 10, who, expected_seq=seq)
                outcomes.append(f"{who}:ok")
            except ConcurrentModification:
                outcomes.append(f"{who}:conflict")

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for o in outcomes if o.endswith(":ok")), 1)
        self.assertEqual(sum(1 for o in outcomes if o.endswith(":conflict")), 4)

    def test_sealed_expansion_independent_liability(self) -> None:
        """封存展开：集团总额、抵销项、各法人独立责任与持股归因三层可分别核对。"""
        svc = self.svc
        svc.create_draft("B8", "G", "2026-01-01", "2027-01-01", "申报员")
        svc.add_line("B8", "G", "revenue", 100, "申报员")
        svc.add_line("B8", "S1", "revenue", 1000, "申报员")
        svc.add_line("B8", "S2", "revenue", 400, "申报员")
        svc.register_internal_txn("B8", "T1", "S1", "S2", "2026-02-01", 300, "运营员")
        svc.transition("B8", "待核算", "核算专员")
        svc.transition("B8", "已确认", "核算专员")
        svc.transition("B8", "执行中", "核算专员")
        svc.transition("B8", "已封存", "审计员")
        d = svc.expansion("B8")
        self.assertEqual(d["lines_total_yuan"], 1500.0)
        self.assertEqual(d["eliminations_total_yuan"], 300.0)
        self.assertEqual(d["group_total_yuan"], 1200.0)
        by = {e["entity_code"]: e for e in d["entities"]}
        # S1: 1000 独立责任，抵销 300，全期 100% 持股 -> 归因 700
        self.assertEqual(by["S1"]["standalone_yuan"], 1000.0)
        self.assertEqual(by["S1"]["eliminated_yuan"], 300.0)
        self.assertEqual(by["S1"]["attributed_yuan"], 700.0)
        # S2: 独立 400，60% 持股 -> 归因 240
        self.assertEqual(by["S2"]["attributed_yuan"], 240.0)
        self.assertEqual(by["G"]["standalone_yuan"], 100.0)
        # 审计链完整有序
        trail = svc.audit_trail("B8")
        seqs = [e["seq"] for e in trail]
        self.assertEqual(seqs, sorted(seqs))
        self.assertIn("封存批次", [e["action"] for e in trail])

    def test_ownership_overlap_rejected(self) -> None:
        """同一子公司的封闭历史区间不允许重叠登记。"""
        svc = self.svc
        svc.mark_exit("S1", "2026-06-01", "审计员")
        with self.assertRaises(DomainError):
            svc.put_ownership("G2", "S1", 100, "2026-03-01", "运营员")


if __name__ == "__main__":
    unittest.main()
