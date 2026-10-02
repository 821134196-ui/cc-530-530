"""模拟适配器：定位与通知。真实部署时替换这两个类即可，调用方不变。"""
import json

from . import db


class LocationAdapter:
    """定位模拟：车辆位置完全由运营员/司机推进站点决定，不接 GPS。

    这里只做事件记录与位置描述，供页面展示“下一站”。
    """

    def describe(self, conn, trip) -> dict:
        stops = conn.execute(
            "SELECT seq, name, skipped FROM trip_stops WHERE trip_id=? ORDER BY seq",
            (trip["id"],),
        ).fetchall()
        active = [s for s in stops if not s["skipped"]]
        seq, phase = trip["pos_seq"], trip["pos_phase"]

        def _name(s):
            return s["name"] if s else "—"

        cur = next((s for s in active if s["seq"] == seq), None)
        upcoming = [s for s in active if s["seq"] > seq]
        if phase == "after":
            nxt = upcoming[0] if upcoming else None
            return {"state": "between", "stop": _name(cur), "next": _name(nxt)}
        if phase == "at":
            nxt = upcoming[0] if upcoming else None
            return {"state": "at", "stop": _name(cur), "next": _name(nxt)}
        # before：尚未到 seq 站
        prev = [s for s in active if s["seq"] < seq]
        nxt = cur if cur else (upcoming[0] if upcoming else None)
        return {"state": "before", "stop": _name(prev[-1] if prev else None),
                "next": _name(nxt)}


class NotificationAdapter:
    """通知模拟：写入 notifications 表（页面右上角收件箱可查）。"""

    def send(self, conn, user_id: str, title: str, body: str) -> None:
        conn.execute(
            "INSERT INTO notifications(ts, user_id, title, body) VALUES (?,?,?,?)",
            (db.now(), user_id, title, body),
        )

    def send_many(self, conn, user_ids, title: str, body: str) -> None:
        for uid in user_ids:
            self.send(conn, uid, title, body)


location_adapter = LocationAdapter()
notification_adapter = NotificationAdapter()


def log_event(conn, trip_id, type_: str, detail: dict | str) -> None:
    conn.execute(
        "INSERT INTO events(ts, trip_id, type, detail) VALUES (?,?,?,?)",
        (db.now(), trip_id, type_, json.dumps(detail, ensure_ascii=False)
         if isinstance(detail, dict) else detail),
    )
