"""HTTP 接口层（标准库实现，零依赖）。

路由：
  POST /entities
  POST /ownership                      登记控股区间（自动截断旧开放区间）
  POST /ownership/{id}/correction      追溯更正
  POST /subsidiaries/{code}/exit       子公司退出
  POST /batches                        生成草案（固化组织快照）
  POST /batches/{code}/lines           法人独立分项
  POST /batches/{code}/transactions    内部交易登记/标记裁定
  POST /batches/{code}/transition      状态迁移（待核算/已确认/执行中/已封存）
  POST /batches/{code}/resnapshot      重新固化快照
  GET  /batches/{code}                 批次详情（含快照、当前序号）
  GET  /batches/{code}/expansion       集团总额/抵销项/法人独立责任展开
  GET  /batches/{code}/diffs           可审计差异（退出/追溯/重组/并发）
  GET  /batches/{code}/audit           完整审计链
  GET  /health
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .service import ConcurrentModification, DomainError, GroupFilingService, NotFound
from .storage import Store


class _Handler(BaseHTTPRequestHandler):
    service: GroupFilingService  # 由 build_server 注入

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        return

    # ---------------------------------------------------------------- 工具

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def _require(self, data: dict, *keys: str) -> None:
        missing = [k for k in keys if k not in data]
        if missing:
            raise DomainError("缺少必填字段：" + "、".join(missing))

    def _handle_errors(self, fn):
        try:
            fn()
        except NotFound as exc:
            self._send(404, {"error": "not_found", "message": str(exc)})
        except ConcurrentModification as exc:
            self._send(409, {"error": "concurrent_modification", "message": str(exc)})
        except DomainError as exc:
            self._send(400, {"error": "domain_violation", "message": str(exc)})

    # ---------------------------------------------------------------- 路由

    def do_GET(self) -> None:
        self._handle_errors(lambda: self._route_get())

    def do_POST(self) -> None:
        self._handle_errors(lambda: self._route_post())

    def _batch_detail(self, code: str) -> dict:
        conn = self.service.store.conn
        with self.service.store.lock:
            row = conn.execute("SELECT * FROM filing_batch WHERE code=?", (code,)).fetchone()
            if row is None:
                raise NotFound("批次不存在")
            return {"code": row["code"], "group_code": row["group_code"],
                    "period_start": row["period_start"], "period_end": row["period_end"],
                    "state": row["state"], "snapshot_hash": row["snapshot_hash"],
                    "snapshot": json.loads(row["snapshot_json"]),
                    "sealed_at": row["sealed_at"],
                    "current_seq": self.service.current_seq(code)}

    def _route_get(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        svc = self.service
        if path == "/health":
            self._send(200, {"status": "ok"})
        elif path.startswith("/batches/"):
            rest = path[len("/batches/"):]
            parts = rest.split("/")
            if len(parts) == 1:
                self._send(200, self._batch_detail(parts[0]))
            elif len(parts) == 2 and parts[1] == "expansion":
                self._send(200, svc.expansion(parts[0]))
            elif len(parts) == 2 and parts[1] == "diffs":
                self._send(200, {"batch_code": parts[0], "diffs": svc.diffs(parts[0])})
            elif len(parts) == 2 and parts[1] == "audit":
                self._send(200, {"batch_code": parts[0], "events": svc.audit_trail(parts[0])})
            else:
                self._send(404, {"error": "not_found", "message": "未知路由"})
        else:
            self._send(404, {"error": "not_found", "message": "未知路由"})

    def _route_post(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        data = self._read_json()
        svc = self.service

        if path == "/entities":
            self._require(data, "code", "name")
            svc.register_entity(data["code"], data["name"])
            self._send(201, {"code": data["code"], "registered": True})

        elif path == "/ownership":
            self._require(data, "group_code", "subsidiary_code", "share_pct", "valid_from", "actor")
            result = svc.put_ownership(data["group_code"], data["subsidiary_code"],
                                       float(data["share_pct"]), data["valid_from"],
                                       data["actor"], data.get("note", ""))
            self._send(201, result)

        elif path.startswith("/ownership/") and path.endswith("/correction"):
            oid = int(path.split("/")[2])
            self._require(data, "actor")
            result = svc.correct_ownership(
                oid, data["actor"],
                share_pct=float(data["share_pct"]) if data.get("share_pct") is not None else None,
                valid_from=data.get("valid_from"), valid_to=data.get("valid_to"),
                reason=data.get("reason", ""))
            self._send(200, result)

        elif path.startswith("/subsidiaries/") and path.endswith("/exit"):
            code = path.split("/")[2]
            self._require(data, "exit_date", "actor")
            svc.mark_exit(code, data["exit_date"], data["actor"])
            self._send(200, {"subsidiary_code": code, "exited_at": data["exit_date"]})

        elif path == "/batches":
            self._require(data, "code", "group_code", "period_start", "period_end", "actor")
            result = svc.create_draft(data["code"], data["group_code"],
                                      data["period_start"], data["period_end"], data["actor"])
            self._send(201, result)

        elif path.startswith("/batches/"):
            parts = path.split("/")
            if len(parts) != 4:
                raise DomainError("未知批次操作路由")
            _, _, code, action = parts
            seq = data.get("expected_seq")
            if action == "lines":
                self._require(data, "entity_code", "kind", "amount_yuan", "actor")
                line_id = svc.add_line(code, data["entity_code"], data["kind"],
                                       float(data["amount_yuan"]), data["actor"], seq)
                self._send(201, {"line_id": line_id})
            elif action == "transactions":
                self._require(data, "txn_id", "seller_code", "buyer_code",
                              "trade_date", "amount_yuan", "actor")
                result = svc.register_internal_txn(
                    code, data["txn_id"], data["seller_code"], data["buyer_code"],
                    data["trade_date"], float(data["amount_yuan"]), data["actor"], seq)
                self._send(201, result)
            elif action == "transition":
                self._require(data, "target", "actor")
                result = svc.transition(code, data["target"], data["actor"], seq)
                self._send(200, result)
            elif action == "resnapshot":
                self._require(data, "actor")
                result = svc.resnapshot(code, data["actor"], seq)
                self._send(200, result)
            else:
                raise DomainError(f"未知批次操作：{action}")
        else:
            self._send(404, {"error": "not_found", "message": "未知路由"})


def build_server(db_path: str = ":memory:", host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = GroupFilingService(store)

    handler = type("BoundHandler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.store = store          # type: ignore[attr-defined]
    httpd.service = service      # type: ignore[attr-defined]
    return httpd
