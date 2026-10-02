"""核心领域测试：区间重叠、候补 FIFO 递补、换车缩容、扫码幂等、并发防超卖。

直接运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest

from shuttle.adapters import MockLocator, MockNotifier
from shuttle.db import connect, init_db
from shuttle.errors import AuthzError, CapacityError, ConflictError, ValidationError
from shuttle.service import ShuttleService


class World:
    """最小测试世界：1 条线 6 站（按字母 A..F），2 辆车(3座/2座)，
    司机 d1=1，运营 op_ok=2(授权)、op_no=3(未授权)，员工 10..40。"""

    def __init__(self, db: str = ":memory:"):
        self.conn = connect(db)
        init_db(self.conn)
        c = self.conn
        c.execute("INSERT INTO routes(id,name) VALUES('R','测试线')")
        self.stop_ids = {}
        for seq, code in enumerate("ABCDEF"):
            cur = c.execute(
                "INSERT INTO stops(route_id,code,name,seq) VALUES(?,?,?,?)",
                ("R", code, f"站{code}", seq))
            self.stop_ids[code] = cur.lastrowid
        c.execute("INSERT INTO vehicles(id,plate,seats) VALUES('V3','x',3)")
        c.execute("INSERT INTO vehicles(id,plate,seats) VALUES('V2','y',2)")
        for eid, role in [(1, "driver"), (2, "operator"), (3, "operator"),
                          (9, "admin")]:
            c.execute("INSERT INTO employees(id,name,role) VALUES(?,?,?)",
                      (eid, str(eid), role))
        c.execute("INSERT INTO drivers(id,license_no) VALUES(1,'L')")
        c.execute("INSERT INTO operator_routes(operator_id,route_id) VALUES(2,'R')")
        for eid in range(10, 41):
            c.execute("INSERT INTO employees(id,name,role) VALUES(?,?, 'staff')",
                      (eid, str(eid)))
        self.notifier = MockNotifier()
        self.locator = MockLocator()
        self.svc = ShuttleService(self.conn, self.notifier, self.locator)
        self._trip_n = 0

    def trip(self, vehicle: str = "V3", driver: int = 1) -> int:
        self._trip_n += 1
        cur = self.conn.execute(
            "INSERT INTO trips(route_id,trip_date,depart_at,vehicle_id,driver_id) "
            "VALUES('R','2026-10-02',?,?,?)",
            (f"08:{self._trip_n:02d}", vehicle, driver))
        return cur.lastrowid

    def status(self, booking_id: int) -> str:
        return self.conn.execute(
            "SELECT status FROM bookings WHERE id=?", (booking_id,)
        ).fetchone()["status"]

    def load(self, trip_id: int) -> list[int]:
        return self.svc.edge_load(trip_id)

    def effective_stops(self, *a, **k):
        return self.svc.effective_stops(*a, **k)

    # 服务方法代理（测试里可写 w.skip_stop(...)）
    def skip_stop(self, *a, **k):
        return self.svc.skip_stop(*a, **k)

    def add_temporary_stop(self, *a, **k):
        return self.svc.add_temporary_stop(*a, **k)

    def clear_detour(self, *a, **k):
        return self.svc.clear_detour(*a, **k)

    def change_vehicle(self, *a, **k):
        return self.svc.change_vehicle(*a, **k)

    def mark_noshow(self, *a, **k):
        return self.svc.mark_noshow(*a, **k)

    def verify_checkin(self, *a, **k):
        return self.svc.verify_checkin(*a, **k)

    def driver_self_checkin(self, *a, **k):
        return self.svc.driver_self_checkin(*a, **k)


class SegmentCapacityTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.t = self.w.trip()  # 3 座

    def test_overlapping_segments_share_capacity(self):
        """A→D 3 人后，任何与 A-D 重叠的区间都满；
        但 A→B 满不代表 B→C 满，短区间可错开售卖。"""
        s, w = self.w.stop_ids, self.w
        for emp in (10, 11, 12):
            self.assertEqual(w.svc.book(self.t, emp, s["A"], s["D"])["status"],
                             "confirmed")
        # 边 AB/BC/CD 均 3
        self.assertEqual(w.load(self.t), [3, 3, 3, 0, 0])
        # 与满区间重叠 → 候补
        r = w.svc.book(self.t, 13, s["B"], s["E"])
        self.assertEqual(r["status"], "waitlisted")
        r = w.svc.book(self.t, 14, s["A"], s["B"])
        self.assertEqual(r["status"], "waitlisted")
        # 不重叠的纯后段区间仍可订（区间容量而非整车人数）
        r = w.svc.book(self.t, 15, s["D"], s["F"])
        self.assertEqual(r["status"], "confirmed")
        # 车上总人数此刻可超过 3，但每条边不超过 3
        self.assertEqual(w.load(self.t), [3, 3, 3, 1, 1])

    def test_mixed_hauls_fill_precisely(self):
        """1 个全程 A→F + 2 个 A→B 后，AB 边满，B→F 仍有 2 座。"""
        s, w = self.w.stop_ids, self.w
        w.svc.book(self.t, 10, s["A"], s["F"])
        w.svc.book(self.t, 11, s["A"], s["B"])
        w.svc.book(self.t, 12, s["A"], s["B"])
        self.assertEqual(w.load(self.t), [3, 1, 1, 1, 1])
        # B→C 还有 2
        self.assertEqual(w.svc.book(self.t, 13, s["B"], s["C"])["status"],
                         "confirmed")
        self.assertEqual(w.svc.book(self.t, 14, s["B"], s["C"])["status"],
                         "confirmed")
        self.assertEqual(w.svc.book(self.t, 15, s["B"], s["C"])["status"],
                         "waitlisted")
        self.assertEqual(w.load(self.t), [3, 3, 1, 1, 1])

    def test_board_after_alight_rejected(self):
        s, w = self.w.stop_ids, self.w
        with self.assertRaises(ValidationError):
            w.svc.book(self.t, 10, s["C"], s["A"])

    def test_duplicate_booking_rejected(self):
        s, w = self.w.stop_ids, self.w
        w.svc.book(self.t, 10, s["A"], s["B"])
        with self.assertRaises(ConflictError):
            w.svc.book(self.t, 10, s["A"], s["C"])


class WaitlistPromotionTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.t = self.w.trip()  # 3 座

    def test_fifo_promotion_on_cancel(self):
        s, w = self.w.stop_ids, self.w
        ids = []
        for emp in (10, 11, 12):
            ids.append(w.svc.book(self.t, emp, s["A"], s["F"])["booking_id"])
        h1 = w.svc.book(self.t, 13, s["A"], s["F"])  # 候补#1
        h2 = w.svc.book(self.t, 14, s["A"], s["F"])  # 候补#2
        self.assertEqual(h1["status"], "waitlisted")
        w.svc.cancel(ids[0], 10)
        self.assertEqual(self.w.status(h1["booking_id"]), "confirmed")
        self.assertEqual(self.w.status(h2["booking_id"]), "waitlisted")
        # 递补者带标记
        row = self.w.conn.execute(
            "SELECT promoted_from_waitlist p FROM bookings WHERE id=?",
            (h1["booking_id"],)).fetchone()
        self.assertEqual(row["p"], 1)

    def test_interval_aware_promotion_skips_blocked_head(self):
        """队首候补的区间仍满时，跳过队首，递补区间有空的后到者；
        且队首不会因此被同区间后到者插队。"""
        s, w = self.w.stop_ids, self.w
        # AB 满：2 个 A→B + 1 个 A→F；BC 满：再加 2 个 B→C；
        # DE/EF 满：A→F 之外再加 2 个 D→F；中间 CD 仍空
        w.svc.book(self.t, 10, s["A"], s["B"])
        w.svc.book(self.t, 11, s["A"], s["B"])
        w.svc.book(self.t, 12, s["A"], s["F"])
        w.svc.book(self.t, 15, s["B"], s["C"])
        w.svc.book(self.t, 16, s["B"], s["C"])
        w.svc.book(self.t, 17, s["D"], s["F"])
        w.svc.book(self.t, 18, s["D"], s["F"])
        self.assertEqual(w.load(self.t), [3, 3, 1, 3, 3])
        head = w.svc.book(self.t, 13, s["A"], s["C"])   # 候补#1：被 AB 堵
        later = w.svc.book(self.t, 14, s["D"], s["F"])  # 候补#2：被 DE 堵
        # 后段先腾出座位：队首仍被 AB 堵住，跳过队首递补后到者
        w.svc.cancel(self.w.conn.execute(
            "SELECT id FROM bookings WHERE employee_id=17").fetchone()["id"], 17)
        self.assertEqual(self.w.status(head["booking_id"]), "waitlisted")
        self.assertEqual(self.w.status(later["booking_id"]), "confirmed")
        # 前段再腾出：队首严格按 FIFO 优先递补
        w.svc.cancel(self.w.conn.execute(
            "SELECT id FROM bookings WHERE employee_id=15").fetchone()["id"], 15)
        self.assertEqual(self.w.status(head["booking_id"]), "waitlisted")  # AB 仍满
        w.svc.cancel(self.w.conn.execute(
            "SELECT id FROM bookings WHERE employee_id=10").fetchone()["id"], 10)
        self.assertEqual(self.w.status(head["booking_id"]), "confirmed")

    def test_change_stop_may_promote_others(self):
        s, w = self.w.stop_ids, self.w
        b1 = w.svc.book(self.t, 10, s["A"], s["F"])["booking_id"]
        w.svc.book(self.t, 11, s["A"], s["F"])
        w.svc.book(self.t, 12, s["A"], s["F"])
        waiter = w.svc.book(self.t, 13, s["A"], s["B"])  # 被堵
        self.assertEqual(waiter["status"], "waitlisted")
        # 持全程票者改到后段 D→F，释放前段，候补者递补
        w.svc.change_stops(self.t, 10, s["D"], s["F"])
        self.assertEqual(self.w.status(waiter["booking_id"]), "confirmed")

    def test_change_stop_to_full_interval_falls_back_to_queue_tail(self):
        s, w = self.w.stop_ids, self.w
        mine = w.svc.book(self.t, 10, s["A"], s["B"])["booking_id"]
        # C→D 区间恰好卖满 3 张
        w.svc.book(self.t, 11, s["C"], s["D"])
        w.svc.book(self.t, 12, s["C"], s["D"])
        w.svc.book(self.t, 13, s["C"], s["D"])
        self.assertEqual(w.load(self.t)[2], 3)
        # 改到已满区间 → 退回候补队尾（原 AB 座位释放）
        r = w.svc.change_stops(self.t, 10, s["C"], s["D"])
        self.assertEqual(r["status"], "waitlisted")
        self.assertEqual(self.w.status(mine), "waitlisted")
        self.assertIsNotNone(self.w.conn.execute(
            "SELECT queue_seq FROM bookings WHERE id=?", (mine,)
        ).fetchone()["queue_seq"])
        # 原 AB 边已释放（load[0]==0），且候补者改回空区间可立即递补
        self.assertEqual(w.load(self.t)[0], 0)
        r2 = w.svc.change_stops(self.t, 10, s["A"], s["B"])
        self.assertEqual(r2["status"], "confirmed")


class ChangeVehicleTests(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_shrink_demotes_and_never_oversells(self):
        t = self.w.trip("V3")  # 3 座
        s, w = self.w.stop_ids, self.w
        # 交错售卖使边负载不均：AB=2, BC..EF=3
        w.svc.book(t, 10, s["A"], s["F"])
        w.svc.book(t, 11, s["B"], s["F"])
        w.svc.book(t, 12, s["B"], s["F"])
        self.assertEqual(w.load(t), [1, 3, 3, 3, 3])
        h = w.svc.book(t, 13, s["A"], s["B"])  # AB 还有 2 座 → 应确认，不是候补
        self.assertEqual(h["status"], "confirmed")
        self.assertEqual(w.load(t), [2, 3, 3, 3, 3])
        h2 = w.svc.book(t, 14, s["C"], s["F"])  # 后段满 → 候补
        self.assertEqual(h2["status"], "waitlisted")
        # 缩容到 2 座：BC..EF 超员，需降级 1 人
        r = w.svc.change_vehicle(t, 2, "V2")
        self.assertEqual(r["seats"], 2)
        for n in w.load(t):
            self.assertLessEqual(n, 2)
        waiters = self.w.conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE trip_id=? AND status='waitlisted'",
            (t,)).fetchone()["c"]
        self.assertGreaterEqual(waiters, 1)
        titles = [m["title"] for m in self.w.notifier.messages]
        self.assertIn("车辆临时调整，您已转入候补", titles)

    def test_grow_vehicle_promotes_waiters(self):
        t = self.w.trip("V2")  # 2 座
        s, w = self.w.stop_ids, self.w
        for e in (10, 11):
            w.svc.book(t, e, s["A"], s["F"])
        h = w.svc.book(t, 12, s["A"], s["F"])
        self.assertEqual(h["status"], "waitlisted")
        r = w.svc.change_vehicle(t, 2, "V3")  # 3 座
        self.assertEqual(self.w.status(h["booking_id"]), "confirmed")
        self.assertEqual(w.load(t), [3, 3, 3, 3, 3])


class ScanAndRideTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.t = self.w.trip()
        s = self.w.stop_ids
        self.bk = self.w.svc.book(self.t, 10, s["A"], s["C"])["booking_id"]

    def test_scan_idempotent(self):
        r1 = self.w.svc.boarding_scan(self.t, 1, self.bk)
        self.assertEqual(r1["action"], "boarded")
        r2 = self.w.svc.boarding_scan(self.t, 1, self.bk)
        r3 = self.w.svc.boarding_scan(self.t, 1, self.bk)
        self.assertEqual(r2["action"], "already")
        self.assertEqual(r3["action"], "already")
        row = self.w.conn.execute(
            "SELECT scan_count, boarded_at, board_trip_id FROM bookings WHERE id=?",
            (self.bk,)).fetchone()
        self.assertEqual(row["scan_count"], 3)
        self.assertIsNotNone(row["boarded_at"])
        self.assertEqual(row["board_trip_id"], self.t)
        # 乘坐记录只有一条
        records = [r for r in self.w.svc.rider_records(self.t)
                   if r["booking_id"] == self.bk and r["boarded_at"]]
        self.assertEqual(len(records), 1)

    def test_scan_wrong_trip_and_driver(self):
        other = self.w.trip()
        self.assertEqual(
            self.w.svc.boarding_scan(other, 1, self.bk)["action"], "wrong_trip")
        with self.assertRaises(AuthzError):
            self.w.svc.boarding_scan(self.t, 4, self.bk)  # 非该班次司机

    def test_late_noshow_on_departure_promotes_later_waiter(self):
        s, w = self.w.stop_ids, self.w
        # AB 满：10(A→C)+11(A→B)+12(A→B)；BC 满：再加 14、15 两个 B→C
        w.svc.book(self.t, 11, s["A"], s["B"])
        w.svc.book(self.t, 12, s["A"], s["B"])
        w.svc.book(self.t, 14, s["B"], s["C"])
        w.svc.book(self.t, 15, s["B"], s["C"])
        self.assertEqual(w.load(self.t), [3, 3, 0, 0, 0])
        waiter = w.svc.book(self.t, 13, s["B"], s["D"])   # BC 满 → 候补
        past = w.svc.book(self.t, 16, s["A"], s["B"])     # AB 满 → 候补
        self.assertEqual(waiter["status"], "waitlisted")
        self.assertEqual(past["status"], "waitlisted")
        # 司机只扫了 11、12，没扫 10；14/15 在 B 站以后才上
        for emp in (11, 12):
            bid = self.w.conn.execute(
                "SELECT id FROM bookings WHERE employee_id=?", (emp,)
            ).fetchone()["id"]
            w.svc.boarding_scan(self.t, 1, bid)
        r = w.svc.depart_stop(self.t, 1, s["A"])
        # 10 迟到未上车
        self.assertEqual(self.w.status(self.bk), "noshow")
        # B 站候补递补成功；A 站候补因错过站点不递补
        self.assertIn(waiter["booking_id"], r["promoted"])
        self.assertNotIn(past["booking_id"], r["promoted"])
        self.assertEqual(self.w.status(waiter["booking_id"]), "confirmed")
        self.assertEqual(self.w.status(past["booking_id"]), "waitlisted")
        # 再驶离 B 站，错过 A 站的人依旧不递补
        r2 = w.svc.depart_stop(self.t, 1, s["B"])
        self.assertNotIn(past["booking_id"], r2["promoted"])

    def test_waitlist_scan_at_gate_uses_fifo(self):
        s, w = self.w.stop_ids, self.w
        w.svc.book(self.t, 11, s["A"], s["F"])
        w.svc.book(self.t, 12, s["A"], s["F"])
        # self.bk 是 A→C 已确认；再填满
        self.assertEqual(w.load(self.t)[0], 3)
        h1 = w.svc.book(self.t, 13, s["A"], s["B"])
        h2 = w.svc.book(self.t, 14, s["A"], s["B"])
        # 队首未取消前扫队尾 → 不放行
        self.assertEqual(
            w.svc.boarding_scan(self.t, 1, h2["booking_id"])["action"], "waitlist")
        # 一张已确认票取消腾出空位后，再扫队首 → 统一 FIFO 递补成功
        bid11 = self.w.conn.execute(
            "SELECT id FROM bookings WHERE employee_id=11").fetchone()["id"]
        w.svc.cancel(bid11, 11)
        r = w.svc.boarding_scan(self.t, 1, h1["booking_id"])
        self.assertEqual(r["action"], "boarded")
        self.assertEqual(self.w.status(h2["booking_id"]), "waitlisted")


class DetourTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.t = self.w.trip()
        self.s = self.w.stop_ids

    def test_skip_boarding_stop_moves_to_waitlist(self):
        w = self.w
        w.svc.book(self.t, 10, self.s["C"], self.s["E"])
        w.skip_stop(self.t, 2, self.s["C"], "施工")
        bk = self.w.conn.execute(
            "SELECT status FROM bookings WHERE employee_id=10").fetchone()
        self.assertEqual(bk["status"], "waitlisted")
        self.assertNotIn("站C", [x["name"] for x in w.effective_stops(self.t)])
        self.assertIn("线路绕行通知",
                      [m["title"] for m in self.w.notifier.messages])

    def test_skip_alighting_stop_extends_occupancy(self):
        w = self.w
        # C 是下车站将被飞：乘客实际坐到 D，合并后的 B→D 边仍被其占用
        w.svc.book(self.t, 10, self.s["A"], self.s["C"])
        for e in (11, 12):
            w.svc.book(self.t, e, self.s["B"], self.s["D"])
        # 飞站前：AB=1, BC=3, CD=2
        self.assertEqual(w.load(self.t), [1, 3, 2, 0, 0])
        w.skip_stop(self.t, 2, self.s["C"])
        # 飞站后有效边：A→B / B→D(合并) / D→E / E→F
        # 10 号下车站被飞，坐到 D：B→D 边 = 10+11+12 = 3，不能当空座售出
        self.assertEqual(w.load(self.t), [1, 3, 0, 0])
        # B→D 满，新来 B→D 只能候补；D 以后不重叠仍可订
        self.assertEqual(
            w.svc.book(self.t, 13, self.s["B"], self.s["D"])["status"],
            "waitlisted")
        self.assertEqual(
            w.svc.book(self.t, 14, self.s["D"], self.s["F"])["status"],
            "confirmed")

    def test_temporary_stop_bookable_and_scoped(self):
        w = self.w
        tmp = w.add_temporary_stop(self.t, 2, "临停X", self.s["B"])
        names = [s["name"] for s in w.effective_stops(self.t)]
        self.assertEqual(names, ["站A", "站B", "临停X", "站C", "站D", "站E", "站F"])
        # 临停可预约
        r = w.svc.book(self.t, 10, tmp, self.s["D"])
        self.assertEqual(r["status"], "confirmed")
        # 临停是班次专属，不污染其他班次
        t2 = self.w.trip()
        self.assertEqual(
            [s["name"] for s in w.effective_stops(t2)],
            ["站A", "站B", "站C", "站D", "站E", "站F"])
        # 解除绕行后恢复原线
        w.clear_detour(self.t, 2)
        self.assertEqual(
            [s["name"] for s in w.effective_stops(self.t)],
            ["站A", "站B", "站C", "站D", "站E", "站F"])


class AuthzTests(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.t = self.w.trip()

    def test_operator_route_authorization(self):
        s, w = self.w.stop_ids, self.w
        # 未授权运营 3 不能换车/飞站/核销/no-show
        with self.assertRaises(AuthzError):
            w.change_vehicle(self.t, 3, "V2")
        with self.assertRaises(AuthzError):
            w.skip_stop(self.t, 3, s["A"])
        with self.assertRaises(AuthzError):
            w.mark_noshow(self.t, 3)
        # 授权运营 2 可以
        w.change_vehicle(self.t, 2, "V2")

    def test_driver_cannot_verify_own_checkin(self):
        w = self.w
        w.driver_self_checkin(self.t, 1)
        with self.assertRaises(AuthzError):
            w.verify_checkin(self.t, 1)
        # 运营可核销
        w.verify_checkin(self.t, 2, "ok")
        with self.assertRaises(ConflictError):
            w.verify_checkin(self.t, 2)  # 重复核销

    def test_driver_checkin_guard(self):
        t2 = self.w.trip(driver=1)
        # 构造另一个司机尝试签到别人的班次：司机4不存在于 drivers 表 → 用非司机员工
        with self.assertRaises(AuthzError):
            self.w.svc.driver_self_checkin(t2, 10)


class ConcurrencyTests(unittest.TestCase):
    """多线程 + 多连接争抢同一区间最后座位，验证绝不超卖。"""

    def test_concurrent_booking_no_oversell(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            setup = connect(path)
            init_db(setup)
            setup.execute("INSERT INTO routes(id,name) VALUES('R','x')")
            stop_a = setup.execute(
                "INSERT INTO stops(route_id,code,name,seq) VALUES('R','A','A',0)"
            ).lastrowid
            stop_b = setup.execute(
                "INSERT INTO stops(route_id,code,name,seq) VALUES('R','B','B',1)"
            ).lastrowid
            setup.execute("INSERT INTO vehicles(id,plate,seats) VALUES('V','v',5)")
            for eid in range(100, 130):
                setup.execute(
                    "INSERT INTO employees(id,name,role) VALUES(?,?,'staff')",
                    (eid, str(eid)))
            trip_id = setup.execute(
                "INSERT INTO trips(route_id,trip_date,depart_at,vehicle_id) "
                "VALUES('R','2026-10-02','09:00','V')").lastrowid
            setup.close()

            results: list[str] = []
            barrier = threading.Barrier(20)

            def worker(eid: int):
                conn = connect(path)
                svc = ShuttleService(conn)
                barrier.wait()
                try:
                    r = svc.book(trip_id, eid, stop_a, stop_b)
                    results.append(r["status"])
                except Exception as e:  # noqa: BLE001
                    results.append(f"ERR:{e}")
                finally:
                    conn.close()

            threads = [threading.Thread(target=worker, args=(100 + i,))
                       for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            check = connect(path)
            confirmed = check.execute(
                "SELECT COUNT(*) c FROM bookings WHERE trip_id=? AND status='confirmed'",
                (trip_id,)).fetchone()["c"]
            waitlisted = check.execute(
                "SELECT COUNT(*) c FROM bookings WHERE trip_id=? AND status='waitlisted'",
                (trip_id,)).fetchone()["c"]
            self.assertEqual(confirmed, 5)
            self.assertEqual(waitlisted, 15)
            self.assertFalse(any(x.startswith("ERR") for x in results))
            check.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                p = path + suffix
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    unittest.main()
