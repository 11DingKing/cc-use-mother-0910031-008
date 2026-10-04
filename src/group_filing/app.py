"""服务端启动入口：python -m group_filing.app [--db data.db] [--port 8080]"""
from __future__ import annotations

import argparse

from .server import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="企业集团合并申报服务端")
    parser.add_argument("--db", default=":memory:", help="SQLite 路径，默认内存库")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    httpd = build_server(args.db, args.host, args.port)
    print(f"合并申报服务已启动：http://{args.host}:{args.port}  (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
