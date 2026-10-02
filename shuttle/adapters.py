"""定位与通知的模拟适配器（真实环境替换为 GPS/推送 SDK 即可）。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MockNotifier:
    """记录所有发出的通知；测试可断言内容。"""
    messages: list[dict[str, Any]] = field(default_factory=list)

    def send(self, employee_id: int, title: str, body: str) -> None:
        self.messages.append(
            {"employee_id": employee_id, "title": title, "body": body}
        )

    def send_many(self, employee_ids: list[int], title: str, body: str) -> None:
        for eid in employee_ids:
            self.send(eid, title, body)

    def for_employee(self, employee_id: int) -> list[dict[str, Any]]:
        return [m for m in self.messages if m["employee_id"] == employee_id]


@dataclass
class MockLocator:
    """模拟车辆定位：按已驶离站点推进，返回“下一站”。"""
    # trip_id -> 已驶离的最大站点顺序（-1 表示尚未发车）
    departed: dict[int, int] = field(default_factory=lambda: defaultdict(lambda: -1))

    def mark_departed(self, trip_id: int, stop_order: int) -> None:
        self.departed[trip_id] = max(self.departed[trip_id], stop_order)

    def reset(self, trip_id: int) -> None:
        self.departed[trip_id] = -1

    def next_stop_order(self, trip_id: int, stop_count: int) -> int | None:
        nxt = self.departed[trip_id] + 1
        return nxt if nxt < stop_count else None
