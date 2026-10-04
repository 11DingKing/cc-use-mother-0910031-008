"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期领域错误的基类。"""

    http_status = 400
    code = "domain_error"


class ValidationError(DomainError):
    """请求数据不满足字段或角色约束。"""

    http_status = 400
    code = "validation_error"


class NotFoundError(DomainError):
    """引用的聚合或记录不存在。"""

    http_status = 404
    code = "not_found"


class ConflictError(DomainError):
    """状态流转、时态区间或乐观锁冲突。"""

    http_status = 409
    code = "conflict"
