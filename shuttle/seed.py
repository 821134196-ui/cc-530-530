"""种子数据：重置数据库并灌入演示数据。

场景设计
--------
T1 早班常规线（20 座）：少量正常预约。
T2 区间满员演示班（12 座）：12 人同乘中段[中央公园→研发中心]，
   此时中段边已满；第 13 位员工买全程 → 因覆盖满员区间进候补；
   第 14 位只坐末段 → 有余座可订。直观体现“区间容量 ≠ 整车总人数”。
T3 绕行演示班（38 座）：3 名员工覆盖不同站点，供 run.sh 一键绕行。
T4 邻线班：用于运营员线路授权（op1 只授权 R1，操作 R2 会被拒绝）。
"""
import os

from . import core, db

ROUTES = {
    "R1": ("科技园早班线", ["城南家园", "滨河路口", "中央公园",
                          "科技园南门", "研发中心", "总部大楼"]),
    "R2": ("地铁接驳线", ["地铁西站", "创业街口", "会展中心", "地铁东站"]),
}
VEHICLES = [
    ("V1", "粤B·1001", 38),
    ("V2", "粤B·2002", 20),
    ("V3", "粤B·3003", 12),
]


def seed(db_path: str | None = None) -> None:
    path = db_path or db.DB_PATH
    if os.path.exists(path):
        os.remove(path)
    db.init_db(path)

    with db.Tx() as conn:
        for rid, (rname, stops) in ROUTES.items():
            conn.execute("INSERT INTO routes(id,name) VALUES(?,?)", (rid, rname))
            for i, s in enumerate(stops):
                conn.execute("INSERT INTO stops(route_id,seq,name) VALUES(?,?,?)",
                             (rid, i, s))
        for vid, plate, seats in VEHICLES:
            conn.execute("INSERT INTO vehicles(id,plate,seats) VALUES(?,?,?)",
                         (vid, plate, seats))

        users = [("d1", "张师傅", "driver"), ("d2", "李师傅", "driver"),
                 ("op1", "王运营", "operator"), ("op2", "赵运营", "operator")]
        for i in range(1, 15):
            users.append((f"e{i}", f"员工{i:02d}", "employee"))
        for uid, name, role in users:
            conn.execute("INSERT INTO users(id,name,role) VALUES(?,?,?)",
                         (uid, name, role))

        # 运营员授权：王运营管 R1，赵运营管 R2
        conn.execute("INSERT INTO operator_routes(operator_id,route_id) VALUES('op1','R1')")
        conn.execute("INSERT INTO operator_routes(operator_id,route_id) VALUES('op2','R2')")

        trips = [
            ("T1", "R1", "R1 08:00 早班", "2026-10-03T08:00", "V2", "d1"),
            ("T2", "R1", "R1 08:30 区间满员班", "2026-10-03T08:30", "V3", "d1"),
            ("T3", "R1", "R1 09:00 绕行演示班", "2026-10-03T09:00", "V1", "d1"),
            ("T4", "R2", "R2 08:15 接驳班", "2026-10-03T08:15", "V1", "d2"),
        ]
        for tid, rid, tname, dep, vid, did in trips:
            conn.execute(
                """INSERT INTO trips(id,route_id,name,depart_at,vehicle_id,pos_seq,pos_phase)
                   VALUES(?,?,?,?,?,0,'before')""",
                (tid, rid, tname, dep, vid))
            conn.execute("INSERT INTO trip_drivers(trip_id,driver_id) VALUES(?,?)",
                         (tid, did))
            for i, s in enumerate(ROUTES[rid][1]):
                conn.execute(
                    "INSERT INTO trip_stops(trip_id,seq,name,skipped) VALUES(?,?,?,0)",
                    (tid, i, s))

    # ---- 通过业务接口下单，保证候补/区间逻辑真实生效 ----
    op1 = {"id": "op1"}

    # T1 常规预约
    for emp, b, a in [("e1", 0, 5), ("e2", 0, 3), ("e3", 2, 5), ("e4", 1, 4)]:
        core.book(None, {"id": emp}, "T1", emp, b, a)

    # T2：12 人坐中段 [2,4)；e13 全程（应候补）；e14 只坐末段 [4,5)（可订）
    for i in range(1, 13):
        core.book(None, {"id": f"e{i}"}, "T2", f"e{i}", 2, 4)
    core.book(None, {"id": "e13"}, "T2", "e13", 0, 5)
    core.book(None, {"id": "e14"}, "T2", "e14", 4, 5)

    # T3 绕行演示班：三人站点交错
    core.book(None, {"id": "e1"}, "T3", "e1", 0, 2)
    core.book(None, {"id": "e2"}, "T3", "e2", 1, 4)
    core.book(None, {"id": "e3"}, "T3", "e3", 2, 5)

    # 王运营核销 T1 张师傅出勤
    core.mark_attendance(None, op1, "T1", "d1")

    _summary()


def _summary() -> None:
    if os.environ.get("SHUTTLE_QUIET"):
        return
    with db.connect() as conn:
        for tid in ("T1", "T2", "T3", "T4"):
            d = core.trip_detail(conn, tid)
            seg = "  ".join(
                f"{s['from']}→{s['to']}:{s['load']}/{s['seats']}"
                for s in d["segments"])
            wl = [r["employee_name"] for r in d["waitlist"]]
            print(f"{tid} {d['name']}（{d['plate']}/{d['seats']}座）")
            print(f"   区间占用 {seg}")
            print(f"   候补队列 {wl or '无'}；计数 {d['counts']}")


if __name__ == "__main__":
    seed()
    print("种子数据就绪。")
