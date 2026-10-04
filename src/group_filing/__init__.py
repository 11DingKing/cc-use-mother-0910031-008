"""企业集团合并申报服务端。"""
from __future__ import annotations

from .service import DomainError, GroupFilingService
from .server import build_server

__all__ = ["DomainError", "GroupFilingService", "build_server"]
