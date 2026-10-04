"""HTTP 接口端到端冒烟测试（真实端口 + urllib 客户端）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from group_filing.server import build_server  # noqa: E402


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = build_server(":memory:", "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.httpd.store.close()

    def call(self, method: str, path: str, payload: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        post = lambda p, d: self.call("POST", p, d)
        get = lambda p: self.call("GET", p)

        self.assertEqual(post("/entities", {"code": "G", "name": "集团"})[0], 201)
        self.assertEqual(post("/entities", {"code": "S1", "name": "子公司"})[0], 201)
        self.assertEqual(post("/ownership", {
            "group_code": "G", "subsidiary_code": "S1", "share_pct": 80,
            "valid_from": "2026-01-01", "actor": "运营员"})[0], 201)
        self.assertEqual(post("/batches", {
            "code": "HB", "group_code": "G", "period_start": "2026-01-01",
            "period_end": "2027-01-01", "actor": "申报员"})[0], 201)

        status, batch = get("/batches/HB")
        self.assertEqual(status, 200)
        seq = batch["current_seq"]

        self.assertEqual(post("/batches/HB/lines", {
            "entity_code": "S1", "kind": "revenue", "amount_yuan": 500,
            "actor": "申报员", "expected_seq": seq})[0], 201)
        # 同一陈旧序号再次提交 -> 409
        status, body = post("/batches/HB/lines", {
            "entity_code": "G", "kind": "revenue", "amount_yuan": 100,
            "actor": "申报员", "expected_seq": seq})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "concurrent_modification")
        # 基于最新状态重试（不带序号即为强制提交），入账成功
        self.assertEqual(post("/batches/HB/lines", {
            "entity_code": "G", "kind": "revenue", "amount_yuan": 100,
            "actor": "申报员"})[0], 201)

        post("/batches/HB/transactions", {
            "txn_id": "X1", "seller_code": "S1", "buyer_code": "G",
            "trade_date": "2026-05-01", "amount_yuan": 200, "actor": "运营员"})
        for target in ("待核算", "已确认", "执行中"):
            self.assertEqual(post("/batches/HB/transition",
                                  {"target": target, "actor": "核算专员"})[0], 200)
        self.assertEqual(post("/batches/HB/transition",
                              {"target": "已封存", "actor": "审计员"})[0], 200)

        status, exp = get("/batches/HB/expansion")
        self.assertEqual(status, 200)
        self.assertEqual(exp["group_total_yuan"], 400.0)  # 500 + 100 - 200
        self.assertEqual(exp["eliminations_total_yuan"], 200.0)

        status, diffs = get("/batches/HB/diffs")
        self.assertEqual(status, 200)
        self.assertTrue(any(d["change_type"] == "concurrent" for d in diffs["diffs"]))

        status, audit = get("/batches/HB/audit")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(audit["events"]), 5)

        # 封存后继续写入 -> 400
        status, body = post("/batches/HB/lines",
                            {"entity_code": "S1", "kind": "revenue",
                             "amount_yuan": 1, "actor": "申报员"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "domain_violation")

        self.assertEqual(get("/health")[1]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
