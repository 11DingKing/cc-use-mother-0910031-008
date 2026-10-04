"""企业集团合并申报服务端。

模块组成：

- errors：领域错误与 HTTP 状态映射；
- store：SQLite 持久化与建表；
- service：时态关系、组织快照、合并抵销、批次封存与审计差异；
- server：标准库 http.server 实现的 JSON 接口。
"""
from __future__ import annotations

from .errors import ConflictError, DomainError, NotFoundError, ValidationError
from .service import FilingService

__all__ = [
    "FilingService",
    "DomainError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
]
