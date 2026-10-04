"""值对象与枚举。"""
from __future__ import annotations

import dataclasses
import enum


class FilingState(str, enum.Enum):
    """申报批次状态（对齐 domain/contract.json）。"""

    DRAFT = "草稿"
    PENDING = "待核算"
    CONFIRMED = "已确认"
    EXECUTING = "执行中"
    SEALED = "已封存"


# 合法状态迁移：封存前可退回前一阶段；封存为终态。
STATE_TRANSITIONS: dict[FilingState, frozenset[FilingState]] = {
    FilingState.DRAFT: frozenset({FilingState.PENDING}),
    FilingState.PENDING: frozenset({FilingState.CONFIRMED, FilingState.DRAFT}),
    FilingState.CONFIRMED: frozenset({FilingState.EXECUTING, FilingState.PENDING}),
    FilingState.EXECUTING: frozenset({FilingState.SEALED, FilingState.CONFIRMED}),
    FilingState.SEALED: frozenset(),
}


@dataclasses.dataclass(frozen=True)
class DateRange:
    """左闭右开的生效区间 [start, end)，end=None 表示至今。"""

    start: str
    end: str | None = None

    def overlaps(self, other: "DateRange") -> bool:
        return self.start < (other.end or "9999-12-31") and other.start < (self.end or "9999-12-31")

    def days(self) -> int:
        from datetime import date

        s = date.fromisoformat(self.start)
        e = date.fromisoformat(self.end) if self.end else date(9999, 12, 31)
        return (e - s).days


@dataclasses.dataclass(frozen=True)
class Amount:
    """金额，统一以整数分存储，避免浮点误差。"""

    cents: int

    @classmethod
    def of(cls, yuan: float | str) -> "Amount":
        return cls(int(round(float(yuan) * 100)))

    @property
    def yuan(self) -> float:
        return round(self.cents / 100, 2)
