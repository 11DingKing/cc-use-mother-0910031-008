"""基于标准库 http.server 的 JSON 接口。

路由（全部返回 JSON；写接口使用 POST/PUT，请求体为 JSON）::

    POST   /entities                          登记法人
    GET    /entities
    POST   /ownership-links                   登记/截断时态控股关系
    GET    /ownership-links
    GET    /groups/{root}/structure?date=     任一时点的集团结构
    POST   /intercompany-txns                 标记内部交易
    GET    /intercompany-txns?period=YYYY
    GET    /rules                             合并抵销规则
    POST   /batches
    GET    /batches
    GET    /batches/{id}
    POST   /batches/{id}/draft                生成/重新生成草案（固定组织快照）
    POST   /batches/{id}/submit               提交批次（expected_version 乐观锁）
    POST   /batches/{id}/review               核算 approve/reject
    POST   /batches/{id}/execute
    POST   /batches/{id}/seal                 满足签署条件后封存
    GET    /batches/{id}/report               展开总额/抵销项/法人独立责任
    PUT    /batches/{id}/filings/{entity}
    POST   /batches/{id}/filings/{entity}/submit
    POST   /batches/{id}/filings/{entity}/correct
    GET    /batches/{id}/filings
    GET    /batches/{id}/revisions
    GET    /batches/{id}/diff?from=N&to=M     修订间可审计差异
    GET    /batches/{id}/audit
    GET    /audit
    GET    /periods/{period}/membership       跨集团重复并入检查
"""
from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import DomainError
from .service import FilingService
from .store import Store


class _Handler(BaseHTTPRequestHandler):
    service: FilingService  # 由 make_server 注入类属性

    # ---- 基础收发 ----

    def _send(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def log_message(self, fmt: str, *args) -> None:  # 安静输出
        return

    # ---- 入口 ----

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        try:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            for pattern, verbs, handler in ROUTES:
                match = pattern.fullmatch(path)
                if match and method in verbs:
                    getattr(self, handler)(match.groupdict(), self._body() if method != "GET" else {})
                    return
            self._send({"error": {"code": "not_found", "message": f"无此接口：{method} {path}"}},
                       HTTPStatus.NOT_FOUND)
        except DomainError as exc:
            self._send({"error": {"code": exc.code, "message": str(exc)}}, exc.http_status)
        except Exception as exc:  # 兜底，避免连接挂死
            self._send({"error": {"code": "internal_error", "message": repr(exc)}}, 500)

    # ---- 处理器 ----

    def h_create_entity(self, p, b):
        self._send(self.service.create_entity(b), HTTPStatus.CREATED)

    def h_list_entities(self, p, b):
        self._send(self.service.list_entities())

    def h_add_link(self, p, b):
        self._send(self.service.add_ownership_link(b), HTTPStatus.CREATED)

    def h_list_links(self, p, b):
        self._send(self.service.list_ownership_links())

    def h_structure(self, p, b):
        self._send(self.service.group_structure(p["root"], self._query().get("date")))

    def h_mark_txn(self, p, b):
        self._send(self.service.mark_intercompany_txn(b), HTTPStatus.CREATED)

    def h_list_txns(self, p, b):
        period = self._query().get("period")
        if not period:
            raise DomainError("缺少 period 查询参数")
        self._send(self.service.list_intercompany_txns(period))

    def h_rules(self, p, b):
        self._send(self.service.list_rules())

    def h_create_batch(self, p, b):
        self._send(self.service.create_batch(b), HTTPStatus.CREATED)

    def h_list_batches(self, p, b):
        self._send(self.service.list_batches())

    def h_get_batch(self, p, b):
        self._send(self.service.get_batch(p["batch"]))

    def h_draft(self, p, b):
        self._send(self.service.generate_draft(p["batch"], b), HTTPStatus.CREATED)

    def h_submit_batch(self, p, b):
        self._send(self.service.submit_batch(p["batch"], b))

    def h_review_batch(self, p, b):
        self._send(self.service.review_batch(p["batch"], b))

    def h_execute_batch(self, p, b):
        self._send(self.service.execute_batch(p["batch"], b))

    def h_seal_batch(self, p, b):
        self._send(self.service.seal_batch(p["batch"], b))

    def h_report(self, p, b):
        self._send(self.service.get_report(p["batch"]))

    def h_upsert_filing(self, p, b):
        self._send(self.service.upsert_filing(p["batch"], {**b, "entity_id": p["entity"]}))

    def h_submit_filing(self, p, b):
        self._send(self.service.submit_filing(p["batch"], p["entity"], b))

    def h_correct_filing(self, p, b):
        self._send(self.service.correct_filing(p["batch"], p["entity"], b))

    def h_list_filings(self, p, b):
        self._send(self.service.list_filings(p["batch"]))

    def h_revisions(self, p, b):
        self._send(self.service.list_revisions(p["batch"]))

    def h_diff(self, p, b):
        q = self._query()
        if "from" not in q or "to" not in q:
            raise DomainError("diff 需要 from 与 to 查询参数")
        self._send(self.service.diff_revisions(p["batch"], int(q["from"]), int(q["to"])))

    def h_batch_audit(self, p, b):
        self._send(self.service.audit_trail(p["batch"]))

    def h_global_audit(self, p, b):
        limit = int(self._query().get("limit", "500"))
        self._send(self.service.audit_trail(None, limit=limit))

    def h_period_membership(self, p, b):
        self._send(self.service.period_membership_overview(p["period"]))


def _route(methods: str, pattern: str, handler: str) -> tuple[re.Pattern, frozenset, str]:
    return re.compile(pattern.replace("{batch}", r"(?P<batch>[^/]+)")
                      .replace("{entity}", r"(?P<entity>[^/]+)")
                      .replace("{root}", r"(?P<root>[^/]+)")
                      .replace("{period}", r"(?P<period>\d{4})") + "$"), \
        frozenset(methods.split()), handler


ROUTES = (
    _route("POST", r"/entities", "h_create_entity"),
    _route("GET", r"/entities", "h_list_entities"),
    _route("POST", r"/ownership-links", "h_add_link"),
    _route("GET", r"/ownership-links", "h_list_links"),
    _route("GET", r"/groups/{root}/structure", "h_structure"),
    _route("POST", r"/intercompany-txns", "h_mark_txn"),
    _route("GET", r"/intercompany-txns", "h_list_txns"),
    _route("GET", r"/rules", "h_rules"),
    _route("POST", r"/batches", "h_create_batch"),
    _route("GET", r"/batches", "h_list_batches"),
    _route("POST", r"/batches/{batch}/draft", "h_draft"),
    _route("POST", r"/batches/{batch}/submit", "h_submit_batch"),
    _route("POST", r"/batches/{batch}/review", "h_review_batch"),
    _route("POST", r"/batches/{batch}/execute", "h_execute_batch"),
    _route("POST", r"/batches/{batch}/seal", "h_seal_batch"),
    _route("GET", r"/batches/{batch}/report", "h_report"),
    _route("PUT", r"/batches/{batch}/filings/{entity}", "h_upsert_filing"),
    _route("POST", r"/batches/{batch}/filings/{entity}/submit", "h_submit_filing"),
    _route("POST", r"/batches/{batch}/filings/{entity}/correct", "h_correct_filing"),
    _route("GET", r"/batches/{batch}/filings", "h_list_filings"),
    _route("GET", r"/batches/{batch}/revisions", "h_revisions"),
    _route("GET", r"/batches/{batch}/diff", "h_diff"),
    _route("GET", r"/batches/{batch}/audit", "h_batch_audit"),
    _route("GET", r"/batches/{batch}", "h_get_batch"),
    _route("GET", r"/audit", "h_global_audit"),
    _route("GET", r"/periods/{period}/membership", "h_period_membership"),
)


def make_server(host: str, port: int, db_path: str = ":memory:") -> ThreadingHTTPServer:
    service = FilingService(Store(db_path))

    handler = type("BoundHandler", (_Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # type: ignore[attr-defined]
    return httpd


def main(argv: list[str] | None = None) -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="企业集团合并申报服务端")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/group_filing.db"))
    args = parser.parse_args(argv)

    httpd = make_server(args.host, args.port, args.db)
    print(f"合并申报服务监听 http://{args.host}:{args.port} （数据库 {args.db}）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
