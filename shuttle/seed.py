"""种子数据：线路/站点/车辆/人员 + 三个演示班次。

演示设计（见启动时打印的说明）：
- 班次 08:00 滨江线：9 人确认但 8 座车不超载（短区间乘客与全程乘客错开），
  候补队列同时存在“内层区间被堵”和“全程被堵”两种人，演示区间感知递补。
- 班次 08:10 西山线：20 座大巴 10 人，运营可一键换成 8 座中巴演示缩容降级。
- 班次 07:40 滨江线：绕行样例——江景公寓飞站、滨江花园后加“桥东岸临停”，
  飞站上的乘客自动转候补并收到通知。
"""
from __future__ import annotations

import sqlite3
from datetime import date

from .service import ShuttleService

ROUTES = {
    "R1": ("滨江科技园线", [
        ("S101", "科技园北门"),
        ("S102", "滨江花园"),
        ("S103", "江景公寓"),
        ("S104", "新城枢纽"),
        ("S105", "总部大楼"),
    ]),
    "R2": ("西山居住区线", [
        ("S201", "西山宿舍"),
        ("S202", "龙泉桥"),
        ("S203", "大学城"),
        ("S204", "中心广场"),
        ("S205", "总部大楼"),
    ]),
}

VEHICLES = [
    ("BUS01", "沪A·D1001", 20),
    ("BUS02", "沪A·D2008", 8),
    ("VAN01", "沪A·D306", 6),
]

# id, 姓名, 角色
EMPLOYEES = [
    (1, "张伟(司机)", "driver"),
    (4, "刘勇(司机)", "driver"),
    (2, "李娜(运营·仅滨江线)", "operator"),
    (3, "王强(运营·全线)", "operator"),
    (9, "赵安(管理员)", "admin"),
] + [(i, name, "staff") for i, name in enumerate([
    "陈晨", "刘洋", "杨帆", "黄磊", "周婷", "吴昊", "徐静",
    "孙磊", "马超", "朱琳", "胡军", "林峰", "郭涛", "何雪",
], start=10)]


def seed(conn: sqlite3.Connection, svc: ShuttleService | None = None) -> dict:
    today = date.today().isoformat()
    cur = conn.cursor()

    for rid, (rname, stops) in ROUTES.items():
        cur.execute("INSERT INTO routes(id,name) VALUES(?,?)", (rid, rname))
        for seq, (code, name) in enumerate(stops):
            cur.execute(
                "INSERT INTO stops(route_id,code,name,seq) VALUES(?,?,?,?)",
                (rid, code, name, seq),
            )
    for vid, plate, seats in VEHICLES:
        cur.execute("INSERT INTO vehicles(id,plate,seats) VALUES(?,?,?)",
                    (vid, plate, seats))
    for eid, name, role in EMPLOYEES:
        cur.execute("INSERT INTO employees(id,name,role) VALUES(?,?,?)",
                    (eid, name, role))
        if role == "driver":
            cur.execute("INSERT INTO drivers(id,license_no) VALUES(?,?)",
                        (eid, f"LIC-{eid}-2026"))
    cur.execute("INSERT INTO operator_routes(operator_id,route_id) VALUES(2,'R1')")
    cur.execute("INSERT INTO operator_routes(operator_id,route_id) VALUES(3,'R1')")
    cur.execute("INSERT INTO operator_routes(operator_id,route_id) VALUES(3,'R2')")

    def stops(route: str):
        rows = cur.execute(
            "SELECT id,name FROM stops WHERE route_id=? ORDER BY seq", (route,)
        ).fetchall()
        return {r["name"]: r["id"] for r in rows}

    r1, r2 = stops("R1"), stops("R2")

    def new_trip(route, depart, vehicle, driver):
        cur.execute(
            "INSERT INTO trips(route_id,trip_date,depart_at,vehicle_id,driver_id) "
            "VALUES(?,?,?,?,?)", (route, today, depart, vehicle, driver),
        )
        return cur.lastrowid

    trip_c = new_trip("R1", "07:40", "BUS02", 1)   # 绕行样例
    trip_a = new_trip("R1", "08:00", "BUS02", 1)   # 区间满员+候补
    trip_b = new_trip("R2", "08:10", "BUS01", 4)   # 换车缩容演示

    svc = svc or ShuttleService(conn)

    # ---- 班次 A（R1 08:00，8 座）----
    A, B, C, D, E = (r1["科技园北门"], r1["滨江花园"], r1["江景公寓"],
                     r1["新城枢纽"], r1["总部大楼"])
    for emp in range(10, 17):          # 7 人全程
        svc.book(trip_a, emp, A, E)
    svc.book(trip_a, 17, B, E)         # 内层乘客：边1-3 满
    svc.book(trip_a, 18, A, B)         # 短区间乘客：边0 满 → 9 张票/最多 8 人同车
    svc.book(trip_a, 19, B, D)         # 候补#1：内层区间被堵
    svc.book(trip_a, 20, A, E)         # 候补#2：全程被堵（边0 满）

    # ---- 班次 B（R2 08:10，20 座 10 人，留给运营现场换 8 座）----
    P, Q, R, S, T = (r2["西山宿舍"], r2["龙泉桥"], r2["大学城"],
                     r2["中心广场"], r2["总部大楼"])
    for emp in range(10, 17):          # 7 人全程
        svc.book(trip_b, emp, P, T)
    svc.book(trip_b, 17, P, Q)
    svc.book(trip_b, 18, Q, T)
    svc.book(trip_b, 20, P, R)

    # ---- 班次 C（R1 07:40，绕行样例）----
    svc.book(trip_c, 10, A, E)
    svc.book(trip_c, 11, A, E)
    svc.book(trip_c, 12, B, E)
    svc.book(trip_c, 13, A, C)         # 下车站江景公寓将被飞 → 实际坐到新城枢纽
    svc.book(trip_c, 14, C, E)         # 上车站江景公寓将被飞 → 转候补
    tmp_id = svc.add_temporary_stop(
        trip_c, 9, "桥东岸(临停)", B, note="滨江路施工绕行"
    )
    svc.skip_stop(trip_c, 9, C, note="滨江路施工，江景公寓暂不停靠")

    # ---- 司机出勤状态 ----
    svc.driver_self_checkin(trip_a, 1)     # 已签到、待核销
    svc.driver_self_checkin(trip_c, 1)
    svc.verify_checkin(trip_c, 3, "早班已核销样例")

    return {
        "date": today,
        "trips": {"detour": trip_c, "full": trip_a, "shrink": trip_b},
        "stops": {"R1": r1, "R2": r2},
        "temp_stop_id": tmp_id,
    }
