"""核心业务测试：区间重叠、递补顺序、换车缩容、扫码幂等、并发不超卖、绕行、权限。

运行：python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import threading
import unittest

_tmp = tempfile.mkdtemp(prefix="shuttle-test-")
os.environ["SHUTTLE_DB"] = os.path.join(_tmp, "test.db")
os.environ["SHUTTLE_QUIET"] = "1"

from shuttle import core, db, seed as seed_mod  # noqa: E402


def make_employees(conn, ids):
    for i in ids:
        conn.execute(
            "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?, 'employee')",
            (i, f"员工{i}"))


def make_trip(tid="T9", seats=3, n_stops=4, vehicle_id=None):
    """在 R1 模板上建自定义班次（前 n_stops 个站）。"""
    vid = vehicle_id or f"VH-{tid}"
    with db.Tx() as conn:
        conn.execute("INSERT OR REPLACE INTO vehicles(id,plate,seats) VALUES(?,?,?)",
                     (vid, f"测-{tid}", seats))
        conn.execute(
            """INSERT OR REPLACE INTO trips(id,route_id,name,depart_at,vehicle_id,
               status,pos_seq,pos_phase)
               VALUES(?,?,?, '2026-10-03T10:00',?,'scheduled',0,'before')""",
            (tid, "R1", f"测试班-{tid}", vid))
        conn.execute("DELETE FROM trip_stops WHERE trip_id=?", (tid,))
        conn.execute("INSERT OR IGNORE INTO trip_drivers(trip_id,driver_id) VALUES(?, 'd1')",
                     (tid,))
        for i in range(n_stops):
            name = conn.execute(
                "SELECT name FROM stops WHERE route_id='R1' AND seq=?", (i,)).fetchone()["name"]
            conn.execute("INSERT INTO trip_stops(trip_id,seq,name,skipped) VALUES(?,?,?,0)",
                         (tid, i, name))
    return tid


def book(emp, tid, b, a):
    return core.book(None, {"id": emp}, tid, emp, b, a)


def status_of(tid, emp):
    with db.connect() as conn:
        r = conn.execute(
            "SELECT status FROM reservations WHERE trip_id=? AND employee_id=?",
            (tid, emp)).fetchone()
        return r["status"] if r else None


def edge_loads(tid):
    with db.connect() as conn:
        trip = core.get_trip(conn, tid)
        return core.segment_load(conn, tid), trip["seats"]


class TestSegmentCapacity(unittest.TestCase):
    def setUp(self):
        seed_mod.seed()

    def test_seed_interval_saturation(self):
        """T2：中段[2,4) 12/12 满；全程候补；只坐末段可订——区间≠整车。"""
        self.assertEqual(status_of("T2", "e13"), "waitlisted")
        self.assertEqual(status_of("T2", "e14"), "booked")
        load, seats = edge_loads("T2")
        self.assertEqual(load[2], 12)
        self.assertEqual(load[3], 12)
        self.assertEqual(load[4], 1)       # 只有 e14
        self.assertLessEqual(max(load.values()), seats)

    def test_partial_interval_booking(self):
        """满的是中段时，不重叠的首段/末段仍可订，重叠则候补。"""
        make_employees_conn = None
        with db.Tx() as conn:
            make_employees(conn, ["u1", "u2", "u3"])
        self.assertEqual(book("u1", "T2", 0, 2)[1], False)   # 首段有空
        self.assertEqual(book("u2", "T2", 4, 5)[1], False)   # 末段有空
        self.assertEqual(book("u3", "T2", 1, 3)[1], True)    # 压到满边 2 → 候补
        load, seats = edge_loads("T2")
        for k, v in load.items():
            self.assertLessEqual(v, seats, f"边 {k} 超卖")

    def test_overlapping_intervals_small_trip(self):
        """3 座 4 站：3 条[0,3)占满所有边，之后任何覆盖边都候补。"""
        with db.Tx() as conn:
            make_employees(conn, [f"p{i}" for i in range(6)])
        make_trip("TS", seats=3)
        for e in ["p0", "p1", "p2"]:
            self.assertEqual(book(e, "TS", 0, 3)[1], False)
        self.assertEqual(book("p3", "TS", 0, 1)[1], True)
        self.assertEqual(book("p4", "TS", 2, 3)[1], True)
        self.assertEqual(book("p5", "TS", 0, 3)[1], True)


class TestPromotionOrder(unittest.TestCase):
    def setUp(self):
        seed_mod.seed()

    def test_fifo_on_cancel(self):
        """取消后按入列顺序递补。"""
        with db.Tx() as conn:
            make_employees(conn, ["w1", "w2"])
        make_trip("TF", seats=2, n_stops=3)
        for e in ["p", "q"]:
            with db.Tx() as conn:
                make_employees(conn, [e])
            book(e, "TF", 0, 2)
        _, wl1 = book("w1", "TF", 0, 2)
        _, wl2 = book("w2", "TF", 0, 2)
        self.assertTrue(wl1 and wl2)
        with db.connect() as conn:
            rid = conn.execute(
                "SELECT id FROM reservations WHERE trip_id='TF' AND employee_id='p'").fetchone()["id"]
        core.cancel(None, {"id": "p"}, rid)
        self.assertEqual(status_of("TF", "w1"), "booked")   # 先来先补
        self.assertEqual(status_of("TF", "w2"), "waitlisted")

    def test_skip_head_who_does_not_fit(self):
        """队首区间装不下时跳过，让更短区间的候补者递补。

        座位3：边0 由 3 条[0,1)占满，边1 由 1 条[0,2)+2 条[1,2)占满。
        wA 候补[0,2)（边0满）；wB 候补[1,2)（边1满）。
        取消一个[1,2)乘客 → 边1 有空但边0 仍满：wA 跳过，wB 递补。
        """
        with db.Tx() as conn:
            make_employees(conn, ["a0", "a1", "b1", "b2", "b3", "wA", "wB"])
        make_trip("TK", seats=3, n_stops=3)
        # 边0: a0,a1,b1 = 3；边1: b1,b2,b3 = 3
        for e in ["a0", "a1"]:
            book(e, "TK", 0, 1)
        book("b1", "TK", 0, 2)
        book("b2", "TK", 1, 2)
        book("b3", "TK", 1, 2)
        self.assertTrue(book("wA", "TK", 0, 2)[1])
        self.assertTrue(book("wB", "TK", 1, 2)[1])
        with db.connect() as conn:
            rid = conn.execute(
                "SELECT id FROM reservations WHERE trip_id='TK' AND employee_id='b3'").fetchone()["id"]
        core.cancel(None, {"id": "b3"}, rid)
        self.assertEqual(status_of("TK", "wA"), "waitlisted", "队首装不下应跳过")
        self.assertEqual(status_of("TK", "wB"), "booked", "短区间应递补")
        load, seats = edge_loads("TK")
        self.assertLessEqual(max(load.values()), seats)

    def test_demoted_have_priority(self):
        """换车缩容降补者优先级高于普通候补。"""
        with db.Tx() as conn:
            make_employees(conn, ["k0", "k1", "k2", "k3", "n1"])
        make_trip("TP", seats=4, n_stops=2)
        for e in ["k0", "k1", "k2", "k3"]:
            book(e, "TP", 0, 1)
        self.assertTrue(book("n1", "TP", 0, 1)[1])       # 普通候补
        with db.Tx() as conn:
            conn.execute("INSERT OR REPLACE INTO vehicles(id,plate,seats) VALUES('VSM','缩',3)")
        r = core.replace_vehicle(None, {"id": "op1"}, "TP", "VSM")
        self.assertEqual(len(r["demoted"]), 1)            # k3 最晚订 → 降补
        self.assertEqual(r["promoted"], [])               # 仍满，谁也补不上
        # 队列顺序：降补者 k3 在 n1 之前
        with db.connect() as conn:
            q = conn.execute(
                """SELECT employee_id FROM reservations WHERE trip_id='TP'
                   AND status='waitlisted'
                   ORDER BY waitlist_priority DESC, waitlisted_at, id""").fetchall()
        self.assertEqual(q[0]["employee_id"], "k3")
        # 再取消一人：k3（优先）递补，n1 继续候
        with db.connect() as conn:
            rid = conn.execute(
                "SELECT id FROM reservations WHERE trip_id='TP' AND employee_id='k0'").fetchone()["id"]
        core.cancel(None, {"id": "k0"}, rid)
        self.assertEqual(status_of("TP", "k3"), "booked")
        self.assertEqual(status_of("TP", "n1"), "waitlisted")


class TestReplaceVehicle(unittest.TestCase):
    def setUp(self):
        seed_mod.seed()

    def test_shrink_demotes_latest_booked_only(self):
        """缩容只把超额的最晚预订者降补，容量恰好落在新座位数。"""
        with db.Tx() as conn:
            conn.execute("INSERT OR REPLACE INTO vehicles(id,plate,seats) VALUES('V6','缩6',6)")
        # T2 中段 12 人，缩到 6 座 → 6 个最晚预订者降补
        r = core.replace_vehicle(None, {"id": "op1"}, "T2", "V6")
        self.assertEqual(len(r["demoted"]), 6)
        load, seats = edge_loads("T2")
        self.assertEqual(seats, 6)
        self.assertEqual(load[2], 6)
        self.assertEqual(load[3], 6)
        demoted = ["e7", "e8", "e9", "e10", "e11", "e12"]
        for e in demoted:
            self.assertEqual(status_of("T2", e), "waitlisted")

    def test_boarded_passengers_never_demoted(self):
        """已上车员工不能因换车被踢下车。"""
        with db.Tx() as conn:
            make_employees(conn, ["x0", "x1"])
        make_trip("TB", seats=2, n_stops=3)
        book("x0", "TB", 0, 2)
        book("x1", "TB", 0, 2)
        # 到站，x0 扫码上车
        core.advance(None, {"id": "d1"}, "TB")
        with db.connect() as conn:
            tk = conn.execute(
                "SELECT ticket FROM reservations WHERE trip_id='TB' AND employee_id='x0'").fetchone()["ticket"]
        core.scan_board(None, {"id": "d1"}, "TB", tk)
        with db.Tx() as conn:
            conn.execute("INSERT OR REPLACE INTO vehicles(id,plate,seats) VALUES('V1S','缩1',1)")
        r = core.replace_vehicle(None, {"id": "op1"}, "TB", "V1S")
        self.assertEqual(status_of("TB", "x0"), "boarded")   # 已上车保留
        self.assertEqual(status_of("TB", "x1"), "waitlisted")
        self.assertIn(
            conn_q("SELECT employee_id FROM reservations WHERE id=?", r["demoted"][0]),
            ["x1"])

    def test_expansion_promotes(self):
        """扩容后候补自动递补（T2 的 e13 全程候补）。"""
        with db.Tx() as conn:
            conn.execute("INSERT OR REPLACE INTO vehicles(id,plate,seats) VALUES('VBIG','大40',40)")
        r = core.replace_vehicle(None, {"id": "op1"}, "T2", "VBIG")
        self.assertEqual(status_of("T2", "e13"), "booked")
        self.assertIn(_id_of("T2", "e13"), r["promoted"])


def conn_q(sql, *args):
    with db.connect() as conn:
        return conn.execute(sql, args).fetchone()["employee_id"]


def _id_of(tid, emp):
    with db.connect() as conn:
        return conn.execute(
            "SELECT id FROM reservations WHERE trip_id=? AND employee_id=?",
            (tid, emp)).fetchone()["id"]


class TestScanIdempotent(unittest.TestCase):
    def setUp(self):
        seed_mod.seed()

    def test_duplicate_board_scan(self):
        """重复扫上车码幂等：流水只有 1 条，状态不翻转、不重复计数。"""
        core.advance(None, {"id": "d1"}, "T1")  # 到站 seq0
        with db.connect() as conn:
            tk = conn.execute(
                "SELECT ticket FROM reservations WHERE trip_id='T1' AND employee_id='e1'").fetchone()["ticket"]
        r1 = core.scan_board(None, {"id": "d1"}, "T1", tk)
        r2 = core.scan_board(None, {"id": "d1"}, "T1", tk)
        self.assertFalse(r1["idempotent"])
        self.assertTrue(r2["idempotent"])
        with db.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM scans WHERE reservation_id=? AND kind='board'",
                (_id_of("T1", "e1"),)).fetchone()["c"]
        self.assertEqual(n, 1)
        self.assertEqual(status_of("T1", "e1"), "boarded")

    def test_duplicate_alight_scan(self):
        core.advance(None, {"id": "d1"}, "T1")
        with db.connect() as conn:
            tk = conn.execute(
                "SELECT ticket FROM reservations WHERE trip_id='T1' AND employee_id='e1'").fetchone()["ticket"]
        core.scan_board(None, {"id": "d1"}, "T1", tk)
        # e1 [0,5)，到 seq1 后可下车
        core.advance(None, {"id": "d1"}, "T1")  # 离开 seq0
        core.advance(None, {"id": "d1"}, "T1")  # 到 seq1
        a1 = core.scan_alight(None, {"id": "d1"}, "T1", tk)
        a2 = core.scan_alight(None, {"id": "d1"}, "T1", tk)
        self.assertFalse(a1["idempotent"])
        self.assertTrue(a2["idempotent"])
        self.assertEqual(status_of("T1", "e1"), "alighted")

    def test_waitlisted_cannot_board(self):
        with db.connect() as conn:
            tk = conn.execute(
                "SELECT ticket FROM reservations WHERE trip_id='T2' AND employee_id='e13'").fetchone()["ticket"]
        core.advance(None, {"id": "d1"}, "T2")
        with self.assertRaises(core.ShuttleError) as cm:
            core.scan_board(None, {"id": "d1"}, "T2", tk)
        self.assertEqual(cm.exception.code, "not_confirmed")

    def test_wrong_stop_rejected(self):
        """在非预约上车站扫码被拒（且不留流水，之后仍可正常核验）。"""
        core.advance(None, {"id": "d1"}, "T1")
        # e3 预约 [2,5)，车还在 seq0
        with db.connect() as conn:
            tk = conn.execute(
                "SELECT ticket FROM reservations WHERE trip_id='T1' AND employee_id='e3'").fetchone()["ticket"]
        with self.assertRaises(core.ShuttleError):
            core.scan_board(None, {"id": "d1"}, "T1", tk)


class TestConcurrentBooking(unittest.TestCase):
    def test_no_oversell_under_concurrency(self):
        """20 线程并发抢 5 个座：占座恰为 5，其余候补，绝不超卖。"""
        seed_mod.seed()
        with db.Tx() as conn:
            make_employees(conn, [f"c{i:02d}" for i in range(20)])
        make_trip("TC", seats=5, n_stops=2)
        errors = []

        def worker(emp):
            try:
                book(emp, "TC", 0, 1)
            except Exception as exc:  # noqa
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"c{i:02d}",)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with db.connect() as conn:
            nb = conn.execute(
                "SELECT COUNT(*) c FROM reservations WHERE trip_id='TC' AND status='booked'").fetchone()["c"]
            nw = conn.execute(
                "SELECT COUNT(*) c FROM reservations WHERE trip_id='TC' AND status='waitlisted'").fetchone()["c"]
        self.assertEqual(nb, 5)
        self.assertEqual(nw, 15)
        load, seats = edge_loads("TC")
        self.assertEqual(load[0], 5)
        self.assertLessEqual(load[0], seats)


class TestDetour(unittest.TestCase):
    def setUp(self):
        seed_mod.seed()

    def test_skip_middle_stop_remaps(self):
        """T3 跳过 seq1：e2[1,4) 上车站前移到 seq0 并待确认；其他人不变。"""
        r = core.detour_skip_stops(None, {"id": "op1"}, "T3", [1])
        self.assertEqual(len(r["affected"]), 1)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE trip_id='T3' AND employee_id='e2'").fetchone()
        self.assertEqual(row["board_seq"], 0)
        self.assertEqual(row["alight_seq"], 4)
        self.assertEqual(row["remap_flag"], "board")
        # 模板线路与其他班次不受影响
        with db.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) c FROM trip_stops WHERE trip_id='T1' AND skipped=1").fetchone()["c"]
        self.assertEqual(n, 0)
        # 员工确认后标记清除
        core.ack_remap(None, {"id": "e2"}, row["id"])
        self.assertEqual(status_of("T3", "e2"), "booked")
        with db.connect() as conn:
            flag = conn.execute(
                "SELECT remap_flag f FROM reservations WHERE id=?", (row["id"],)).fetchone()["f"]
        self.assertIsNone(flag)

    def test_skip_first_two_cancels_unrideable(self):
        """e1[0,2) 的上车站与下车站全被跳过且无有效区间 → 自动取消。"""
        r = core.detour_skip_stops(None, {"id": "op1"}, "T3", [0, 1])
        with db.connect() as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) c FROM reservations WHERE trip_id='T3' AND employee_id='e1' "
                "AND status='cancelled'").fetchone()["c"]
        self.assertEqual(cnt, 1)
        self.assertGreaterEqual(len(r["cancelled"]), 1)


class TestNoShow(unittest.TestCase):
    def setUp(self):
        seed_mod.seed()

    def test_depart_marks_no_show_and_missed_waitlist(self):
        with db.Tx() as conn:
            make_employees(conn, ["m1"])
        # T1 已有 e1[0,5)、e2[0,3) 在 seq0 上车；再加一个 seq1 候补场景
        # e2 在 seq0 不扫码 → 爽约
        core.advance(None, {"id": "d1"}, "T1")  # 到 seq0
        with db.connect() as conn:
            tk = conn.execute(
                "SELECT ticket FROM reservations WHERE trip_id='T1' AND employee_id='e1'").fetchone()["ticket"]
        core.scan_board(None, {"id": "d1"}, "T1", tk)
        ev = core.advance(None, {"id": "d1"}, "T1")["event"]  # 离 seq0
        self.assertIn(_id_of("T1", "e2"), ev["no_show"])
        self.assertEqual(status_of("T1", "e1"), "boarded")
        self.assertEqual(status_of("T1", "e2"), "no_show")


class TestChangeStops(unittest.TestCase):
    def setUp(self):
        seed_mod.seed()

    def test_change_to_full_segment_waits_and_back_promotes(self):
        # 专用班次：2 座，g1/g2 占满 [0,2)；g3 持 [0,1)，改到 [0,2) → 候补；
        # g1 取消后 g3 按原预订时间递补
        with db.Tx() as conn:
            make_employees(conn, ["g1", "g2", "g3"])
        make_trip("TG", seats=2, n_stops=3)
        book("g1", "TG", 0, 2)   # 占边0,1
        book("g2", "TG", 1, 2)   # 边1 满（2），边0 仅 1
        self.assertEqual(book("g3", "TG", 0, 1)[1], False)  # 边0 还有座
        row, waitlisted = core.change_stops(
            None, {"id": "g3"}, _id_of("TG", "g3"), 0, 2)  # 改压满边1 → 候补
        self.assertTrue(waitlisted)
        core.cancel(None, {"id": "g1"}, _id_of("TG", "g1"))
        self.assertEqual(status_of("TG", "g3"), "booked")


class TestPermissions(unittest.TestCase):
    def setUp(self):
        seed_mod.seed()

    def test_driver_cannot_check_attendance(self):
        with self.assertRaises(core.ShuttleError) as cm:
            core.mark_attendance(None, {"id": "d1"}, "T1", "d1")
        self.assertEqual(cm.exception.code, "forbidden")

    def test_operator_restricted_to_authorized_routes(self):
        # 王运营 op1 只有 R1；操作 R2 的 T4 换车/核销/绕行均拒绝
        with db.Tx() as conn:
            conn.execute("INSERT OR REPLACE INTO vehicles(id,plate,seats) VALUES('VX','X',30)")
        with self.assertRaises(core.ShuttleError) as cm:
            core.replace_vehicle(None, {"id": "op1"}, "T4", "VX")
        self.assertEqual(cm.exception.code, "forbidden")
        with self.assertRaises(core.ShuttleError):
            core.mark_attendance(None, {"id": "op1"}, "T4", "d2")
        with self.assertRaises(core.ShuttleError):
            core.detour_skip_stops(None, {"id": "op1"}, "T4", [1])
        # 授权的 op2 可以
        out = core.detour_skip_stops(None, {"id": "op2"}, "T4", [1])
        self.assertEqual(out["skipped"], [1])

    def test_other_driver_cannot_scan(self):
        core.advance(None, {"id": "d1"}, "T1")
        with db.connect() as conn:
            tk = conn.execute(
                "SELECT ticket FROM reservations WHERE trip_id='T1' AND employee_id='e1'").fetchone()["ticket"]
        with self.assertRaises(core.ShuttleError):
            core.scan_board(None, {"id": "d2"}, "T1", tk)

    def test_employee_cannot_scan_or_swap(self):
        with self.assertRaises(core.ShuttleError):
            core.scan_board(None, {"id": "e1"}, "T1", "X")
        with self.assertRaises(core.ShuttleError):
            core.replace_vehicle(None, {"id": "e1"}, "T1", "V1")


if __name__ == "__main__":
    unittest.main()
