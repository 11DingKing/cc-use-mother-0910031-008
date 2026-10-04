"""合并申报核心场景测试：

1. 年中并购/退出：期间加权并入，旧数据不整段归入新集团；
2. 内部交易标记按发生日判定控制权，避免重复抵销；
3. 组织快照固定、重新生成产生可审计差异；
4. 追溯更正与乐观锁；
5. 签署封存与冻结；
6. 跨集团重组重复并入检查；
7. 并发提交冲突。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from group_filing import ConflictError, FilingService, ValidationError
from group_filing.server import make_server


def svc() -> FilingService:
    return FilingService(":memory:")


def seed_group(s: FilingService) -> None:
    """G 集团 2026 年：S1 全年控股；S2 7 月 1 日并入；S3 7 月 1 日退出。"""
    for eid, name in (("G", "集团母公司"), ("S1", "全年子公司"),
                      ("S2", "年中并入公司"), ("S3", "年中退出公司")):
        s.create_entity({"entity_id": eid, "name": name})
    s.add_ownership_link({"parent_id": "G", "child_id": "S1", "share_pct": 100,
                          "effective_from": "2020-01-01"})
    s.add_ownership_link({"parent_id": "G", "child_id": "S2", "share_pct": 100,
                          "effective_from": "2026-07-01", "note": "年中收购"})
    s.add_ownership_link({"parent_id": "G", "child_id": "S3", "share_pct": 100,
                          "effective_from": "2020-01-01", "effective_to": "2026-07-01",
                          "note": "年中剥离"})


def file_all(s: FilingService, batch: str) -> None:
    # G=1000；S1=200；S2 申报 365；S3 退出前申报 365
    s.upsert_filing(batch, {"entity_id": "G", "declared_points": 1000})
    s.upsert_filing(batch, {"entity_id": "S1", "declared_points": 200,
                            "intercompany_sale": 100})
    s.upsert_filing(batch, {"entity_id": "S2", "declared_points": 365,
                            "intercompany_purchase": 100})
    s.upsert_filing(batch, {"entity_id": "S3", "declared_points": 365})
    for eid in ("G", "S1", "S2", "S3"):
        s.submit_filing(batch, eid, {})


class TemporalOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = svc()
        seed_group(self.s)

    def test_interval_overlap_rejected(self) -> None:
        with self.assertRaises(ConflictError):
            self.s.add_ownership_link({"parent_id": "G", "child_id": "S1",
                                       "share_pct": 80, "effective_from": "2026-06-01"})
        # 端点相接允许
        ok = self.s.add_ownership_link({"parent_id": "G", "child_id": "S3",
                                        "share_pct": 100, "effective_from": "2026-07-01"})
        self.assertEqual(ok["effective_to"], None)

    def test_midyear_weighted_snapshot(self) -> None:
        b = self.s.create_batch({"period_label": "2026", "root_entity_id": "G",
                                 "batch_id": "B-G-2026"})
        self.s.generate_draft(b["batch_id"], {})
        report = self.s.get_report(b["batch_id"])
        members = {m["entity_id"]: m for m in report["member_liabilities"]}
        self.assertEqual(members["S1"]["coverage"], 1.0)
        self.assertEqual(members["S2"]["coverage"], round(184 / 365, 2))
        self.assertEqual(members["S3"]["coverage"], round(181 / 365, 2))
        self.assertFalse(members["S2"]["active_at_period_start"])
        self.assertTrue(members["S2"]["active_at_period_end"])
        self.assertTrue(members["S3"]["active_at_period_start"])
        self.assertFalse(members["S3"]["active_at_period_end"])
        self.assertEqual(members["S2"]["controlled_intervals"],
                         [["2026-07-01", "2027-01-01"]])

    def test_point_in_time_structure(self) -> None:
        before = self.s.group_structure("G", "2026-03-01")["members"]
        after = self.s.group_structure("G", "2026-09-01")["members"]
        self.assertEqual({m["entity_id"] for m in before}, {"S1", "S3"})
        self.assertEqual({m["entity_id"] for m in after}, {"S1", "S2"})


class ConsolidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = svc()
        seed_group(self.s)
        self.batch = "B-G-2026"
        self.s.create_batch({"period_label": "2026", "root_entity_id": "G",
                             "batch_id": self.batch})
        self.s.generate_draft(self.batch, {})

    def test_txn_before_control_is_excluded(self) -> None:
        # 9 月交易：双方均受控 → 纳入抵销
        self.s.mark_intercompany_txn({"period_label": "2026", "seller_id": "S1",
                                      "buyer_id": "S2", "amount": 100,
                                      "ref_no": "IC-001", "txn_date": "2026-09-01"})
        # 3 月交易：S2 尚未并入 → 排除，不抵销
        self.s.mark_intercompany_txn({"period_label": "2026", "seller_id": "S1",
                                      "buyer_id": "S2", "amount": 50,
                                      "ref_no": "IC-002", "txn_date": "2026-03-01"})
        file_all(self.s, self.batch)
        report = self.s.get_report(self.batch)
        excluded = report["excluded_intercompany_txns"]
        self.assertEqual([e["ref_no"] for e in excluded], ["IC-002"])
        self.assertIn("buyer_not_controlled_at_txn_date", excluded[0]["reasons"])
        by_rule = report["totals"]["elimination_by_rule"]
        self.assertEqual(by_rule["IC_SALE"], 100.0)
        self.assertEqual(by_rule["IC_PURCHASE"], 100.0)
        # 集团总额 = 1000 + 200 + 365*184/365 + 365*181/365 = 1565
        self.assertEqual(report["totals"]["group_gross_points"], 1565.0)
        # 抵销 = 销售 100 + 采购 100
        self.assertEqual(report["totals"]["total_eliminations_points"], -200.0)
        self.assertEqual(report["totals"]["consolidated_points"], 1365.0)

    def test_duplicate_marking_rejected(self) -> None:
        self.s.mark_intercompany_txn({"period_label": "2026", "seller_id": "S1",
                                      "buyer_id": "S2", "amount": 10,
                                      "ref_no": "DUP", "txn_date": "2026-09-01"})
        with self.assertRaises(ConflictError):
            self.s.mark_intercompany_txn({"period_label": "2026", "seller_id": "S1",
                                          "buyer_id": "S2", "amount": 10,
                                          "ref_no": "DUP", "txn_date": "2026-09-02"})

    def test_reconciliation_mismatch_blocks_seal(self) -> None:
        self.s.mark_intercompany_txn({"period_label": "2026", "seller_id": "S1",
                                      "buyer_id": "S2", "amount": 100,
                                      "ref_no": "IC-001", "txn_date": "2026-09-01"})
        # S1 只申报 80，与标记 100 不一致
        self.s.upsert_filing(self.batch, {"entity_id": "G", "declared_points": 1000})
        self.s.upsert_filing(self.batch, {"entity_id": "S1", "declared_points": 200,
                                          "intercompany_sale": 80})
        self.s.upsert_filing(self.batch, {"entity_id": "S2", "declared_points": 365,
                                          "intercompany_purchase": 100})
        self.s.upsert_filing(self.batch, {"entity_id": "S3", "declared_points": 365})
        for eid in ("G", "S1", "S2", "S3"):
            self.s.submit_filing(self.batch, eid, {})
        self.s.submit_batch(self.batch, {})
        self.s.review_batch(self.batch, {"decision": "approve"})
        with self.assertRaises(ConflictError) as ctx:
            self.s.seal_batch(self.batch, {})
        self.assertIn("intercompany_sale_mismatch", str(ctx.exception))

    def test_minority_interest_is_memo_only(self) -> None:
        s = svc()
        s.create_entity({"entity_id": "R", "name": "母公司"})
        s.create_entity({"entity_id": "C", "name": "六成子公司"})
        s.add_ownership_link({"parent_id": "R", "child_id": "C", "share_pct": 60,
                              "effective_from": "2020-01-01"})
        s.create_batch({"period_label": "2026", "root_entity_id": "R", "batch_id": "B-R"})
        s.generate_draft("B-R", {})
        s.upsert_filing("B-R", {"entity_id": "R", "declared_points": 0})
        s.upsert_filing("B-R", {"entity_id": "C", "declared_points": 100})
        report = s.get_report("B-R")
        c = next(m for m in report["member_liabilities"] if m["entity_id"] == "C")
        self.assertEqual(c["independent_liability_points"], 100.0)
        self.assertEqual(c["minority_interest_points"], 40.0)
        self.assertEqual(report["totals"]["group_gross_points"], 100.0)
        self.assertEqual(report["totals"]["consolidated_points"], 100.0)
        minority = next(e for e in report["eliminations"] if e["rule_code"] == "MINORITY")
        self.assertTrue(minority["memo"])
        self.assertEqual(minority["amount"], 40.0)


class SnapshotAndSealTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = svc()
        seed_group(self.s)
        self.batch = "B-G-2026"
        self.s.create_batch({"period_label": "2026", "root_entity_id": "G",
                             "batch_id": self.batch})
        self.s.generate_draft(self.batch, {})

    def test_snapshot_fixed_after_submit_then_diff_on_regenerate(self) -> None:
        v1 = self.s.get_batch(self.batch)["version"]
        # 提交后新增 S4：在途报告不应变化
        self.s.create_entity({"entity_id": "S4", "name": "新增公司"})
        file_all(self.s, self.batch)
        self.s.submit_batch(self.batch, {"expected_version": v1})
        self.s.add_ownership_link({"parent_id": "G", "child_id": "S4",
                                   "share_pct": 100, "effective_from": "2025-01-01"})
        report = self.s.get_report(self.batch)
        self.assertNotIn("S4", {m["entity_id"] for m in report["member_liabilities"]})
        # 核算退回 → 重新生成 → 差异可见
        self.s.review_batch(self.batch, {"decision": "reject"})
        self.s.generate_draft(self.batch, {"actor": "企业申报员"})
        v2 = self.s.get_batch(self.batch)["version"]
        # 提交 +1、退回 +1、重新生成 +1
        self.assertEqual(v2, v1 + 3)
        diff = self.s.diff_revisions(self.batch, v1, v2)
        self.assertEqual([a["entity_id"] for a in diff["membership"]["added"]], ["S4"])
        revisions = self.s.list_revisions(self.batch)
        self.assertEqual([r["version"] for r in revisions], [v1, v2])

    def test_exit_member_filing_detached_but_audited(self) -> None:
        # 独立场景：S1 于 9 月 1 日退出，其申报保留、覆盖期按受控段计算
        s = svc()
        s.create_entity({"entity_id": "G", "name": "母公司"})
        s.create_entity({"entity_id": "S1", "name": "退出公司"})
        s.add_ownership_link({"parent_id": "G", "child_id": "S1", "share_pct": 100,
                              "effective_from": "2020-01-01",
                              "effective_to": "2026-09-01"})
        s.create_batch({"period_label": "2026", "root_entity_id": "G", "batch_id": "BX"})
        s.generate_draft("BX", {})
        s.upsert_filing("BX", {"entity_id": "G", "declared_points": 1000})
        s.upsert_filing("BX", {"entity_id": "S1", "declared_points": 365})
        s.submit_filing("BX", "S1", {})
        s1 = next(m for m in s.get_report("BX")["member_liabilities"]
                  if m["entity_id"] == "S1")
        self.assertEqual(s1["coverage"], round(243 / 365, 2))
        self.assertTrue(s1["active_at_period_start"])
        self.assertFalse(s1["active_at_period_end"])
        self.assertEqual(s1["group_attributed_points"], round(365 * 243 / 365, 2))

    def test_full_seal_freezes_report(self) -> None:
        self.s.mark_intercompany_txn({"period_label": "2026", "seller_id": "S1",
                                      "buyer_id": "S2", "amount": 100,
                                      "ref_no": "IC-001", "txn_date": "2026-09-01",
                                      "unrealized_profit": 20})
        file_all(self.s, self.batch)
        v = self.s.get_batch(self.batch)["version"]
        self.s.submit_batch(self.batch, {"expected_version": v})
        self.s.review_batch(self.batch, {"decision": "approve"})
        sealed = self.s.seal_batch(self.batch, {"actor": "监管审计员"})
        self.assertTrue(sealed["sealed"])
        self.assertEqual(sealed["sealed_by"], "监管审计员")
        # 未实现利润抵销：-100(销售)-100(采购)-20(利润)
        self.assertEqual(sealed["totals"]["total_eliminations_points"], -220.0)
        # 封存后：不能改申报、不能加同期间标记、不能重复封存
        with self.assertRaises(ConflictError):
            self.s.upsert_filing(self.batch, {"entity_id": "G", "declared_points": 1})
        with self.assertRaises(ConflictError):
            self.s.mark_intercompany_txn({"period_label": "2026", "seller_id": "S1",
                                          "buyer_id": "S2", "amount": 5,
                                          "ref_no": "IC-LATE", "txn_date": "2026-08-01"})
        with self.assertRaises(ConflictError):
            self.s.seal_batch(self.batch, {})
        # 冻结报告内容不随后续结构变动改变：新增一家次年才纳入的法人
        self.s.create_entity({"entity_id": "S5", "name": "封存后新设"})
        self.s.add_ownership_link({"parent_id": "G", "child_id": "S5", "share_pct": 100,
                                   "effective_from": "2027-01-01"})
        again = self.s.get_report(self.batch)
        self.assertEqual(again["totals"], sealed["totals"])

    def test_correction_audited_and_version_checked(self) -> None:
        file_all(self.s, self.batch)
        stale = self.s.list_filings(self.batch)
        s1_row = next(f for f in stale if f["entity_id"] == "S1")
        v = s1_row["version"]
        # 错误版本号 → 冲突
        with self.assertRaises(ConflictError):
            self.s.correct_filing(self.batch, "S1",
                                  {"declared_points": 210, "correction_reason": "追溯调增",
                                   "expected_version": v + 99})
        corrected = self.s.correct_filing(
            self.batch, "S1", {"declared_points": 210, "correction_reason": "追溯调增",
                               "expected_version": v})
        self.assertEqual(corrected["status"], "已更正")
        self.assertEqual(corrected["version"], v + 1)
        audit = self.s.audit_trail(self.batch)
        entry = next(a for a in audit if a["action"] == "filing_corrected")
        self.assertEqual(entry["detail"]["before"]["declared_points"], 200.0)
        self.assertEqual(entry["detail"]["after"]["declared_points"], 210.0)
        # 封存前补登与申报一致的内部交易标记
        self.s.mark_intercompany_txn({"period_label": "2026", "seller_id": "S1",
                                      "buyer_id": "S2", "amount": 100,
                                      "ref_no": "IC-C", "txn_date": "2026-09-01"})
        self.s.submit_batch(self.batch, {})
        self.s.review_batch(self.batch, {"decision": "approve"})
        self.s.seal_batch(self.batch, {})
        with self.assertRaises(ConflictError):
            self.s.correct_filing(self.batch, "S1",
                                  {"declared_points": 9, "correction_reason": "x"})


class CrossGroupAndConcurrencyTest(unittest.TestCase):
    def test_cross_group_restructure_overview(self) -> None:
        s = svc()
        for eid in ("GA", "GB", "X"):
            s.create_entity({"entity_id": eid, "name": eid})
        # X 上半年属 A、下半年属 B（衔接不重叠）
        s.add_ownership_link({"parent_id": "GA", "child_id": "X", "share_pct": 100,
                              "effective_from": "2020-01-01", "effective_to": "2026-07-01"})
        s.add_ownership_link({"parent_id": "GB", "child_id": "X", "share_pct": 100,
                              "effective_from": "2026-07-01"})
        s.create_batch({"period_label": "2026", "root_entity_id": "GA", "batch_id": "BA"})
        s.create_batch({"period_label": "2026", "root_entity_id": "GB", "batch_id": "BB"})
        s.generate_draft("BA", {})
        s.generate_draft("BB", {})
        overview = s.period_membership_overview("2026")
        x = next(o for o in overview["cross_group_memberships"] if o["entity_id"] == "X")
        self.assertAlmostEqual(x["total_coverage"], 1.0, places=2)
        self.assertFalse(x["potential_double_count"])

        # 制造 6 月重叠（GB 链新增一段，与 GA 的控制期并存）→ 重复并入风险
        s.add_ownership_link({"parent_id": "GB", "child_id": "X", "share_pct": 100,
                              "effective_from": "2026-06-01", "effective_to": "2026-07-01"})
        s.generate_draft("BB", {})
        overview = s.period_membership_overview("2026")
        x = next(o for o in overview["cross_group_memberships"] if o["entity_id"] == "X")
        self.assertTrue(x["potential_double_count"])
        self.assertGreater(x["total_coverage"], 1.0)

    def test_concurrent_batch_transitions_one_loses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            s = FilingService(str(Path(tmp) / "c.db"))
            seed_group(s)
            s.create_batch({"period_label": "2026", "root_entity_id": "G",
                            "batch_id": "BC"})
            s.generate_draft("BC", {})
            file_all(s, "BC")
            version = s.get_batch("BC")["version"]
            errors: list[Exception] = []

            def run() -> None:
                try:
                    s.submit_batch("BC", {"expected_version": version})
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ConflictError)
            self.assertEqual(s.get_batch("BC")["status"], "待核算")

    def test_concurrent_distinct_filings_both_succeed(self) -> None:
        s = svc()
        seed_group(s)
        s.create_batch({"period_label": "2026", "root_entity_id": "G", "batch_id": "BP"})
        s.generate_draft("BP", {})
        errors: list[Exception] = []

        def run(eid: str, points: float) -> None:
            try:
                s.upsert_filing("BP", {"entity_id": eid, "declared_points": points})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(eid, p))
                   for eid, p in (("G", 1), ("S1", 2), ("S2", 3), ("S3", 4))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        report = s.get_report("BP")
        self.assertEqual(report["totals"]["group_gross_points"],
                         round(1 + 2 + 3 * 184 / 365 + 4 * 181 / 365, 2))


class HttpApiTest(unittest.TestCase):
    def test_end_to_end_over_http(self) -> None:
        httpd = make_server("127.0.0.1", 0, ":memory:")
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            def call(method: str, path: str, body: dict | None = None,
                     expected: int = 200) -> dict:
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}", data=data, method=method,
                    headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req) as resp:
                        self.assertEqual(resp.status, expected)
                        return json.loads(resp.read())
                except urllib.error.HTTPError as exc:
                    payload = json.loads(exc.read())
                    self.assertEqual(exc.code, expected, payload)
                    return payload

            call("POST", "/entities", {"entity_id": "G", "name": "母公司"}, 201)
            call("POST", "/entities", {"entity_id": "C", "name": "子公司"}, 201)
            call("POST", "/ownership-links",
                 {"parent_id": "G", "child_id": "C", "share_pct": 100,
                  "effective_from": "2026-01-01"}, 201)
            b = call("POST", "/batches",
                     {"period_label": "2026", "root_entity_id": "G", "batch_id": "BH"}, 201)
            self.assertEqual(b["status"], "草稿")
            call("POST", "/batches/BH/draft", {}, 201)
            call("PUT", "/batches/BH/filings/G", {"declared_points": 100})
            call("PUT", "/batches/BH/filings/C", {"declared_points": 50})
            call("POST", "/batches/BH/filings/G/submit", {})
            call("POST", "/batches/BH/filings/C/submit", {})
            v = call("GET", "/batches/BH")["version"]
            call("POST", "/batches/BH/submit", {"expected_version": v})
            call("POST", "/batches/BH/review", {"decision": "approve"})
            sealed = call("POST", "/batches/BH/seal", {"actor": "监管审计员"})
            self.assertEqual(sealed["totals"]["consolidated_points"], 150.0)
            report = call("GET", "/batches/BH/report")
            self.assertEqual(len(report["member_liabilities"]), 2)
            audit = call("GET", "/batches/BH/audit")
            self.assertTrue(any(a["action"] == "batch_sealed" for a in audit))
            # 乐观锁失效返回 409
            err = call("POST", "/batches/BH/execute",
                       {"expected_version": v}, expected=409)
            self.assertEqual(err["error"]["code"], "conflict")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
