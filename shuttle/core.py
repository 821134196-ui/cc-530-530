"""业务内核：区间容量、候补递补、换车缩容、绕行改站、扫码核验、权限。

容量模型
--------
一条班次按站点快照 0..N-1 排列。预约 [board_seq, alight_seq) 占用
边（区间）board_seq, board_seq+1, ..., alight_seq-1。某条边的占用为
所有 status IN ('booked','boarded') 且覆盖该边的预约数；不得超过车辆座位。
因此“整车没满但某区间满了”也会进候补。

并发
----
所有写方法在 db.Tx()（BEGIN IMMEDIATE + 进程写锁）中执行，
区间余量的“检查—插入—递补”是原子的，并发预约不会超卖。
"""
import secrets

from . import db
from .adapters import location_adapter, notification_adapter, log_event

ACTIVE = ("booked", "boarded")          # 占座的状态
RIDING = ("booked", "waitlisted", "boarded")  # 仍有效、未结束的状态


class ShuttleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 基础查询

def get_user(conn, uid: str):
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise ShuttleError("user_not_found", f"用户不存在：{uid}")
    return row


def list_routes(conn):
    return conn.execute("SELECT * FROM routes ORDER BY id").fetchall()


def list_stops(conn, route_id: str):
    return conn.execute(
        "SELECT seq, name FROM stops WHERE route_id=? ORDER BY seq", (route_id,)
    ).fetchall()


def trip_stops(conn, trip_id: str, include_skipped=True):
    sql = "SELECT seq, name, skipped FROM trip_stops WHERE trip_id=?"
    if not include_skipped:
        sql += " AND skipped=0"
    return conn.execute(sql + " ORDER BY seq", (trip_id,)).fetchall()


def get_trip(conn, trip_id: str):
    row = conn.execute(
        """SELECT t.*, v.plate, v.seats, r.name AS route_name
           FROM trips t JOIN vehicles v ON v.id=t.vehicle_id
           JOIN routes r ON r.id=t.route_id WHERE t.id=?""",
        (trip_id,),
    ).fetchone()
    if not row:
        raise ShuttleError("trip_not_found", f"班次不存在：{trip_id}")
    return row


def list_trips(conn):
    return conn.execute(
        """SELECT t.*, v.plate, v.seats, r.name AS route_name
           FROM trips t JOIN vehicles v ON v.id=t.vehicle_id
           JOIN routes r ON r.id=t.route_id ORDER BY t.depart_at"""
    ).fetchall()


# ---------------------------------------------------------------- 权限

def assert_route_operator(conn, actor, route_id: str):
    """运营员只能操作被授权的线路。"""
    if actor["role"] != "operator":
        raise ShuttleError("forbidden", "仅运营员可执行该操作")
    ok = conn.execute(
        "SELECT 1 FROM operator_routes WHERE operator_id=? AND route_id=?",
        (actor["id"], route_id),
    ).fetchone()
    if not ok:
        raise ShuttleError("forbidden", "未授权该线路，禁止操作")


def assert_trip_driver(conn, actor, trip_id: str):
    if actor["role"] != "driver":
        raise ShuttleError("forbidden", "仅司机可执行该操作")
    ok = conn.execute(
        "SELECT 1 FROM trip_drivers WHERE trip_id=? AND driver_id=?",
        (trip_id, actor["id"]),
    ).fetchone()
    if not ok:
        raise ShuttleError("forbidden", "您不是该班次司机")


def can_operate_trip(conn, actor, trip) -> bool:
    if actor["role"] == "operator":
        return bool(conn.execute(
            "SELECT 1 FROM operator_routes WHERE operator_id=? AND route_id=?",
            (actor["id"], trip["route_id"]),
        ).fetchone())
    if actor["role"] == "driver":
        return bool(conn.execute(
            "SELECT 1 FROM trip_drivers WHERE trip_id=? AND driver_id=?",
            (trip["id"], actor["id"]),
        ).fetchone())
    return False


def assert_can_scan(conn, actor, trip):
    if actor["role"] == "driver":
        if not conn.execute(
            "SELECT 1 FROM trip_drivers WHERE trip_id=? AND driver_id=?",
            (trip["id"], actor["id"]),
        ).fetchone():
            raise ShuttleError("forbidden", "您不是该班次司机")
    elif actor["role"] == "operator":
        assert_route_operator(conn, actor, trip["route_id"])
    else:
        raise ShuttleError("forbidden", "司机或运营员才可核验")


# ---------------------------------------------------------------- 区间容量

def segment_load(conn, trip_id: str) -> dict:
    """返回 {边起点 seq: 占用人数}，只统计占座状态。"""
    load: dict = {}
    rows = conn.execute(
        """SELECT board_seq, alight_seq FROM reservations
           WHERE trip_id=? AND status IN ('booked','boarded')""",
        (trip_id,),
    ).fetchall()
    for r in rows:
        for k in range(r["board_seq"], r["alight_seq"]):
            load[k] = load.get(k, 0) + 1
    return load


def interval_fits(load: dict, seats: int, b: int, a: int) -> bool:
    return all(load.get(k, 0) < seats for k in range(b, a))


def _active_stop_seqs(conn, trip_id: str) -> list[int]:
    return [r["seq"] for r in trip_stops(conn, trip_id, include_skipped=False)]


def _validate_interval(conn, trip_id: str, b: int, a: int):
    seqs = {r["seq"]: r for r in trip_stops(conn, trip_id)}
    if b not in seqs or a not in seqs:
        raise ShuttleError("bad_stop", "上/下车站点不在该班次")
    if seqs[b]["skipped"] or seqs[a]["skipped"]:
        raise ShuttleError("stop_skipped", "该站点已绕行跳过，请改站")
    if b >= a:
        raise ShuttleError("bad_interval", "下车站必须在上车站之后")


# ---------------------------------------------------------------- 候补递补

def _promote(conn, trip_id: str, seats: int | None = None) -> list[int]:
    """按优先级把候补预约填入区间空位。返回被递补的预约 id。

    顺序：降补优先(waitlist_priority) → 入列时间 → id。
    每个候选都做区间容量检查；装不下就跳过（后续更短区间的人可能能上）。
    """
    trip = get_trip(conn, trip_id)
    seats = seats if seats is not None else trip["seats"]
    load = segment_load(conn, trip_id)
    promoted = []
    queue = conn.execute(
        """SELECT * FROM reservations WHERE trip_id=? AND status='waitlisted'
           ORDER BY waitlist_priority DESC, waitlisted_at ASC, id ASC""",
        (trip_id,),
    ).fetchall()
    for r in queue:
        b, a = r["board_seq"], r["alight_seq"]
        # 车辆已驶过该上车站，递补也无意义
        if trip["status"] != "scheduled" and b < trip["pos_seq"]:
            continue
        if interval_fits(load, seats, b, a):
            for k in range(b, a):
                load[k] = load.get(k, 0) + 1
            conn.execute(
                """UPDATE reservations SET status='booked', waitlist_priority=0
                   WHERE id=?""", (r["id"],))
            promoted.append(r["id"])
            notification_adapter.send(
                conn, r["employee_id"], "候补成功",
                f"班次 {trip['name']} 候补递补成功，请按预约站点乘车。")
    if promoted:
        log_event(conn, trip_id, "promoted", {"ids": promoted})
    return promoted


# ---------------------------------------------------------------- 预约 / 取消 / 改站

def _gen_ticket(trip_id: str) -> str:
    return f"{trip_id}-{secrets.token_hex(3).upper()}"


def book(conn_ignored, actor, trip_id: str, employee_id: str,
         board_seq: int, alight_seq: int):
    """员工预约（或进候补）。返回 (reservation_row, waitlisted: bool)。"""
    with db.Tx() as conn:
        actor = get_user(conn, actor["id"])
        employee = get_user(conn, employee_id)
        if employee["role"] != "employee":
            raise ShuttleError("forbidden", "只有员工可预约")
        if actor["id"] != employee["id"] and actor["role"] != "operator":
            raise ShuttleError("forbidden", "不能替他人预约")
        trip = get_trip(conn, trip_id)
        if trip["status"] != "scheduled":
            raise ShuttleError("trip_closed", "班次已发车或结束")
        board_seq, alight_seq = int(board_seq), int(alight_seq)
        _validate_interval(conn, trip_id, board_seq, alight_seq)

        dup = conn.execute(
            """SELECT id FROM reservations
               WHERE trip_id=? AND employee_id=? AND status IN ('booked','waitlisted','boarded')""",
            (trip_id, employee_id),
        ).fetchone()
        if dup:
            raise ShuttleError("duplicate_booking", "该班次已有有效预约，请先取消或改站")

        load = segment_load(conn, trip_id)
        fits = interval_fits(load, trip["seats"], board_seq, alight_seq)
        ts = db.now()
        ticket = _gen_ticket(trip_id)
        if fits:
            status, wl_at, wl_pri = "booked", None, 0
        else:
            status, wl_at, wl_pri = "waitlisted", ts, 0

        cur = conn.execute(
            """INSERT INTO reservations
               (ticket, trip_id, employee_id, board_seq, alight_seq, status,
                booked_at, waitlisted_at, waitlist_priority)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ticket, trip_id, employee_id, board_seq, alight_seq, status,
             ts, wl_at, wl_pri),
        )
        rid = cur.lastrowid
        log_event(conn, trip_id, "book",
                  {"id": rid, "employee": employee_id, "interval": [board_seq, alight_seq],
                   "status": status})
        if status == "waitlisted":
            notification_adapter.send(
                conn, employee_id, "已进入候补",
                f"班次 {trip['name']} 您选择的区间已满，已为您排入候补队列。")
        row = conn.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
        return dict(row), status == "waitlisted"


def cancel(conn_ignored, actor, reservation_id: int, reason: str = "员工取消"):
    with db.Tx() as conn:
        res = conn.execute("SELECT * FROM reservations WHERE id=?",
                           (reservation_id,)).fetchone()
        if not res:
            raise ShuttleError("not_found", "预约不存在")
        actor = get_user(conn, actor["id"])
        if actor["id"] != res["employee_id"]:
            if actor["role"] != "operator":
                raise ShuttleError("forbidden", "只能取消本人预约")
            trip = get_trip(conn, res["trip_id"])
            assert_route_operator(conn, actor, trip["route_id"])
        if res["status"] in ("cancelled", "no_show", "alighted"):
            raise ShuttleError("bad_state", f"预约已结束（{res['status']}）")
        conn.execute(
            "UPDATE reservations SET status='cancelled', cancelled_at=?, cancel_reason=? WHERE id=?",
            (db.now(), reason, res["id"]))
        log_event(conn, res["trip_id"], "cancel",
                  {"id": res["id"], "by": actor["id"], "reason": reason})
        promoted = _promote(conn, res["trip_id"])
        return {"cancelled": res["id"], "promoted": promoted}


def change_stops(conn_ignored, actor, reservation_id: int,
                 new_board: int, new_alight: int):
    """员工改站：按新区间重新检查容量，装不下则自动转候补（不占旧座）。"""
    with db.Tx() as conn:
        res = conn.execute("SELECT * FROM reservations WHERE id=?",
                           (reservation_id,)).fetchone()
        if not res:
            raise ShuttleError("not_found", "预约不存在")
        actor = get_user(conn, actor["id"])
        if actor["id"] != res["employee_id"]:
            raise ShuttleError("forbidden", "只能修改本人预约")
        if res["status"] not in ("booked", "waitlisted"):
            raise ShuttleError("bad_state", "当前状态不可改站")
        trip = get_trip(conn, res["trip_id"])
        new_board, new_alight = int(new_board), int(new_alight)
        _validate_interval(conn, res["trip_id"], new_board, new_alight)

        old_status = res["status"]
        # 先把自己从占座集中摘掉，再按新区间判断；改站者按原预订时间排队，
        # 不插队到其他候补者之前
        if old_status == "booked":
            conn.execute(
                "UPDATE reservations SET status='waitlisted', "
                "waitlisted_at=COALESCE(waitlisted_at, ?) WHERE id=?",
                (db.now(), res["id"]))
        conn.execute("UPDATE reservations SET board_seq=?, alight_seq=?, remap_flag=NULL WHERE id=?",
                     (new_board, new_alight, res["id"]))
        promoted = _promote(conn, res["trip_id"])
        row = conn.execute("SELECT * FROM reservations WHERE id=?", (res["id"],)).fetchone()
        log_event(conn, res["trip_id"], "change_stops",
                  {"id": res["id"], "interval": [new_board, new_alight],
                   "status": row["status"]})
        return dict(row), row["status"] == "waitlisted"


# ---------------------------------------------------------------- 换车缩容

def replace_vehicle(conn_ignored, actor, trip_id: str, new_vehicle_id: str):
    """临时换车。缩容时按规则把多余占座者降为高优先级候补，再统一递补。

    已上车(boarded)的员工绝不被降补；在 booked 中优先降补最晚预约、
    且覆盖超载区间的人。
    """
    with db.Tx() as conn:
        actor = get_user(conn, actor["id"])
        trip = get_trip(conn, trip_id)
        assert_route_operator(conn, actor, trip["route_id"])
        veh = conn.execute("SELECT * FROM vehicles WHERE id=?",
                           (new_vehicle_id,)).fetchone()
        if not veh:
            raise ShuttleError("vehicle_not_found", "车辆不存在")
        if veh["id"] == trip["vehicle_id"]:
            raise ShuttleError("bad_vehicle", "与当前车辆相同")

        conn.execute("UPDATE trips SET vehicle_id=? WHERE id=?",
                     (new_vehicle_id, trip_id))
        log_event(conn, trip_id, "replace_vehicle",
                  {"old": trip["vehicle_id"], "new": new_vehicle_id,
                   "seats": [trip["seats"], veh["seats"]]})

        demoted = []
        if veh["seats"] < trip["seats"]:
            load = segment_load(conn, trip_id)
            while any(v > veh["seats"] for v in load.values()):
                over_edges = [k for k, v in load.items() if v > veh["seats"]]
                candidate = conn.execute(
                    """SELECT * FROM reservations WHERE trip_id=? AND status='booked'
                       ORDER BY booked_at DESC, id DESC""",
                    (trip_id,),
                ).fetchall()
                victim = next(
                    (r for r in candidate
                     if any(r["board_seq"] <= k < r["alight_seq"] for k in over_edges)),
                    None,
                )
                if victim is None:
                    break  # 理论上不会发生
                for k in range(victim["board_seq"], victim["alight_seq"]):
                    load[k] -= 1
                conn.execute(
                    """UPDATE reservations SET status='waitlisted',
                       waitlist_priority=1, waitlisted_at=? WHERE id=?""",
                    (db.now(), victim["id"]))
                demoted.append(victim["id"])
                notification_adapter.send(
                    conn, victim["employee_id"], "临时换车，座位调整",
                    f"班次 {trip['name']} 临时更换为 {veh['seats']} 座车辆，"
                    "您已被优先排入候补，空位将第一时间递补。")
        promoted = _promote(conn, trip_id, veh["seats"])
        return {"old_vehicle": trip["vehicle_id"], "new_vehicle": new_vehicle_id,
                "seats": veh["seats"], "demoted": demoted, "promoted": promoted}


# ---------------------------------------------------------------- 绕行

def _nearest_active(conn, trip_id: str, seq: int, direction: int):
    seqs = _active_stop_seqs(conn, trip_id)
    if direction < 0:
        prev = [s for s in seqs if s < seq]
        return prev[-1] if prev else None
    nxt = [s for s in seqs if s > seq]
    return nxt[0] if nxt else None


def detour_skip_stops(conn_ignored, actor, trip_id: str, seqs_to_skip: list[int]):
    """线路绕行：在本班次快照上跳过站点（不影响线路模板/其他班次）。

    受影响预约自动改到最近的可服务站点并要求员工确认；
    绕行后不存在可乘区间的预约直接取消并通知。
    """
    with db.Tx() as conn:
        actor = get_user(conn, actor["id"])
        trip = get_trip(conn, trip_id)
        assert_route_operator(conn, actor, trip["route_id"])
        if trip["status"] != "scheduled":
            raise ShuttleError("trip_started", "发车后不可设置绕行")
        seqs_to_skip = sorted({int(s) for s in seqs_to_skip})
        valid = {r["seq"] for r in trip_stops(conn, trip_id)}
        if not set(seqs_to_skip) <= valid:
            raise ShuttleError("bad_stop", "含不存在的站点")

        for s in seqs_to_skip:
            conn.execute("UPDATE trip_stops SET skipped=1 WHERE trip_id=? AND seq=?",
                         (trip_id, s))
        log_event(conn, trip_id, "detour", {"skipped": seqs_to_skip})

        affected, cancelled = [], []
        rows = conn.execute(
            """SELECT * FROM reservations WHERE trip_id=?
               AND status IN ('booked','waitlisted')""", (trip_id,)).fetchall()
        stopmap = {r["seq"]: r for r in trip_stops(conn, trip_id)}

        for r in rows:
            b, a, flag = r["board_seq"], r["alight_seq"], None
            if stopmap[b]["skipped"]:
                nb = _nearest_active(conn, trip_id, b, -1)
                if nb is None:
                    nb = _nearest_active(conn, trip_id, b, +1)
                    flag = "board"
                else:
                    flag = "board"
                b = nb
            if stopmap[a]["skipped"]:
                na = _nearest_active(conn, trip_id, a, +1)
                if na is None:
                    na = _nearest_active(conn, trip_id, a, -1)
                a = na
                flag = flag or "alight"
            if b is None or a is None or b >= a:
                conn.execute(
                    """UPDATE reservations SET status='cancelled', cancelled_at=?,
                       cancel_reason='绕行后无可乘区间' WHERE id=?""",
                    (db.now(), r["id"]))
                cancelled.append(r["id"])
                notification_adapter.send(
                    conn, r["employee_id"], "绕行通知：预约取消",
                    f"班次 {trip['name']} 绕行跳过您的乘车站点且无替代站点，预约已取消。")
                continue
            if b != r["board_seq"] or a != r["alight_seq"]:
                conn.execute(
                    "UPDATE reservations SET board_seq=?, alight_seq=?, remap_flag=? WHERE id=?",
                    (b, a, flag, r["id"]))
                affected.append(r["id"])
                which = "上车站" if flag == "board" else "下车站"
                notification_adapter.send(
                    conn, r["employee_id"], "绕行改站，请确认",
                    f"班次 {trip['name']} 绕行跳过站点，您的{which}已调整为"
                    f"{stopmap[b]['name'] if flag=='board' else stopmap[a]['name']}，请打开预约确认。")

        promoted = _promote(conn, trip_id)
        return {"skipped": seqs_to_skip, "affected": affected,
                "cancelled": cancelled, "promoted": promoted}


def restore_stops(conn_ignored, actor, trip_id: str, seqs_to_restore: list[int]):
    """恢复绕行跳过的站点。"""
    with db.Tx() as conn:
        actor = get_user(conn, actor["id"])
        trip = get_trip(conn, trip_id)
        assert_route_operator(conn, actor, trip["route_id"])
        for s in seqs_to_restore:
            conn.execute("UPDATE trip_stops SET skipped=0 WHERE trip_id=? AND seq=?",
                         (trip_id, int(s)))
        log_event(conn, trip_id, "detour_restore", {"restored": seqs_to_restore})
        promoted = _promote(conn, trip_id)
        return {"restored": seqs_to_restore, "promoted": promoted}


def ack_remap(conn_ignored, actor, reservation_id: int,
              new_board: int | None = None, new_alight: int | None = None):
    """员工确认绕行后的改站；也可顺带改成其他可服务站点（需满足区间容量）。"""
    with db.Tx() as conn:
        res = conn.execute("SELECT * FROM reservations WHERE id=?",
                           (reservation_id,)).fetchone()
        if not res:
            raise ShuttleError("not_found", "预约不存在")
        actor = get_user(conn, actor["id"])
        if actor["id"] != res["employee_id"]:
            raise ShuttleError("forbidden", "只能确认本人预约")
        if new_board is not None and new_alight is not None:
            nb, na = int(new_board), int(new_alight)
            _validate_interval(conn, res["trip_id"], nb, na)
            was_booked = res["status"] == "booked"
            if was_booked:
                conn.execute("UPDATE reservations SET status='waitlisted', waitlist_priority=1 WHERE id=?",
                             (res["id"],))
            conn.execute("UPDATE reservations SET board_seq=?, alight_seq=? WHERE id=?",
                         (nb, na, res["id"]))
            _promote(conn, res["trip_id"])
        res = conn.execute("SELECT * FROM reservations WHERE id=?",
                           (reservation_id,)).fetchone()
        conn.execute("UPDATE reservations SET remap_flag=NULL WHERE id=?", (res["id"],))
        return dict(conn.execute("SELECT * FROM reservations WHERE id=?",
                                 (reservation_id,)).fetchone())


# ---------------------------------------------------------------- 车辆运行 / 核验

def advance(conn_ignored, actor, trip_id: str):
    """推进到站/离站。离站时把该站未上车者判爽约，随后递补后续区间的候补。"""
    with db.Tx() as conn:
        actor = get_user(conn, actor["id"])
        trip = get_trip(conn, trip_id)
        if not can_operate_trip(conn, actor, trip):
            raise ShuttleError("forbidden", "无权推进该班次")
        if trip["status"] == "completed":
            raise ShuttleError("trip_done", "班次已结束")

        seq, phase = trip["pos_seq"], trip["pos_phase"]
        active = _active_stop_seqs(conn, trip_id)
        if phase == "before":
            conn.execute(
                "UPDATE trips SET pos_phase='at', "
                "status=CASE WHEN status='scheduled' THEN 'active' ELSE status END WHERE id=?",
                (trip_id,))
            log_event(conn, trip_id, "arrive", {"seq": seq})
            event = {"at": seq}
        elif phase == "at":
            # 离站：爽约处理
            no_shows = [r["id"] for r in conn.execute(
                """SELECT id FROM reservations WHERE trip_id=? AND status='booked'
                   AND board_seq=?""", (trip_id, seq)).fetchall()]
            for rid in no_shows:
                conn.execute(
                    "UPDATE reservations SET status='no_show', no_show_at=? WHERE id=?",
                    (db.now(), rid))
            missed = [r["id"] for r in conn.execute(
                """SELECT id FROM reservations WHERE trip_id=? AND status='waitlisted'
                   AND board_seq<=?""", (trip_id, seq)).fetchall()]
            for rid in missed:
                conn.execute(
                    """UPDATE reservations SET status='cancelled', cancelled_at=?,
                       cancel_reason='候补未递补，车辆已驶过上车站' WHERE id=?""",
                    (db.now(), rid))
            log_event(conn, trip_id, "depart",
                      {"seq": seq, "no_show": no_shows, "waitlist_missed": missed})

            nxt = [s for s in active if s > seq]
            if nxt:
                conn.execute("UPDATE trips SET pos_seq=?, pos_phase='before' WHERE id=?",
                             (nxt[0], trip_id))
            else:
                conn.execute("UPDATE trips SET status='completed', pos_phase='after' WHERE id=?",
                             (trip_id,))
            promoted = _promote(conn, trip_id)
            event = {"departed": seq, "no_show": no_shows,
                     "waitlist_missed": missed, "promoted": promoted,
                     "completed": not nxt}
        else:
            raise ShuttleError("trip_done", "班次已结束")
        trip2 = get_trip(conn, trip_id)
        return {"trip": dict(trip2), "event": event,
                "location": location_adapter.describe(conn, trip2)}


def _find_boardable(conn, res, trip) -> int | None:
    """扫码上车时实际所在站：取当前有效站序号（必须在乘车间区内）。"""
    seq = trip["pos_seq"]
    if trip["pos_phase"] != "at":
        return None
    if res["board_seq"] <= seq < res["alight_seq"]:
        return seq
    return None


def scan_board(conn_ignored, actor, trip_id: str, ticket: str):
    """司机核验上车。重复扫码幂等：命中 scans 唯一约束即返回首次记录。"""
    with db.Tx() as conn:
        actor = get_user(conn, actor["id"])
        trip = get_trip(conn, trip_id)
        assert_can_scan(conn, actor, trip)
        res = conn.execute("SELECT * FROM reservations WHERE ticket=?",
                           (ticket,)).fetchone()
        if not res or res["trip_id"] != trip_id:
            raise ShuttleError("bad_ticket", "车票无效或不属于本班车")

        existing = conn.execute(
            "SELECT * FROM scans WHERE trip_id=? AND reservation_id=? AND kind='board'",
            (trip_id, res["id"])).fetchone()
        if existing:
            return {"idempotent": True, "kind": "board",
                    "message": "重复扫码：上车已核验，请勿重复操作",
                    "scanned_at": existing["scanned_at"],
                    "reservation": dict(res)}

        if res["status"] == "alighted":
            return {"idempotent": True, "kind": "board",
                    "message": "该员工已完成行程（已下车）",
                    "reservation": dict(res)}
        if res["status"] in ("cancelled", "no_show"):
            raise ShuttleError("ticket_void", f"车票已失效（{res['status']}）")
        if res["status"] == "waitlisted":
            raise ShuttleError("not_confirmed", "候补尚未递补成功，不能上车")
        # booked
        at = _find_boardable(conn, res, trip)
        if at is None:
            if trip["pos_seq"] >= res["alight_seq"]:
                raise ShuttleError("wrong_stop", "已驶过下车站，无法上车")
            raise ShuttleError("wrong_stop",
                               f"当前不在可上车站点（预约 {res['board_seq']} 站上车）")

        ts = db.now()
        conn.execute(
            """INSERT INTO scans(trip_id, reservation_id, kind, stop_seq, actor_id, scanned_at, note)
               VALUES (?,?,?,?,?,?,?)""",
            (trip_id, res["id"], "board", at, actor["id"], ts, "上车核验"))
        conn.execute(
            "UPDATE reservations SET status='boarded', boarded_at=? WHERE id=?",
            (ts, res["id"]))
        log_event(conn, trip_id, "scan_board",
                  {"reservation": res["id"], "stop": at, "actor": actor["id"]})
        return {"idempotent": False, "kind": "board", "message": "上车核验通过",
                "stop_seq": at, "reservation": dict(
                    conn.execute("SELECT * FROM reservations WHERE id=?",
                                 (res["id"],)).fetchone())}


def scan_alight(conn_ignored, actor, trip_id: str, ticket: str):
    with db.Tx() as conn:
        actor = get_user(conn, actor["id"])
        trip = get_trip(conn, trip_id)
        assert_can_scan(conn, actor, trip)
        res = conn.execute("SELECT * FROM reservations WHERE ticket=?",
                           (ticket,)).fetchone()
        if not res or res["trip_id"] != trip_id:
            raise ShuttleError("bad_ticket", "车票无效或不属于本班车")

        existing = conn.execute(
            "SELECT * FROM scans WHERE trip_id=? AND reservation_id=? AND kind='alight'",
            (trip_id, res["id"])).fetchone()
        if existing:
            return {"idempotent": True, "kind": "alight",
                    "message": "重复扫码：下车已登记", "scanned_at": existing["scanned_at"],
                    "reservation": dict(res)}

        if res["status"] in ("cancelled", "no_show"):
            raise ShuttleError("ticket_void", f"车票已失效（{res['status']}）")
        if res["status"] != "boarded":
            raise ShuttleError("not_boarded", "尚未核验上车，不能登记下车")
        seq = trip["pos_seq"]
        if trip["pos_phase"] != "at" or not (res["board_seq"] < seq <= res["alight_seq"]):
            raise ShuttleError("wrong_stop", "当前不在可下车站点")

        ts = db.now()
        conn.execute(
            """INSERT INTO scans(trip_id, reservation_id, kind, stop_seq, actor_id, scanned_at, note)
               VALUES (?,?,?,?,?,?,?)""",
            (trip_id, res["id"], "alight", seq, actor["id"], ts, "下车登记"))
        conn.execute(
            "UPDATE reservations SET status='alighted', alighted_at=? WHERE id=?",
            (ts, res["id"]))
        log_event(conn, trip_id, "scan_alight",
                  {"reservation": res["id"], "stop": seq, "actor": actor["id"]})
        promoted = _promote(conn, trip_id)
        return {"idempotent": False, "kind": "alight", "message": "下车已登记",
                "stop_seq": seq, "promoted": promoted, "reservation": dict(
                    conn.execute("SELECT * FROM reservations WHERE id=?",
                                 (res["id"],)).fetchone())}


# ---------------------------------------------------------------- 出勤核销

def mark_attendance(conn_ignored, actor, trip_id: str, driver_id: str):
    """司机出勤核销只能由运营员登记；司机不能核销自己（或任何人）。"""
    with db.Tx() as conn:
        actor = get_user(conn, actor["id"])
        trip = get_trip(conn, trip_id)
        assert_route_operator(conn, actor, trip["route_id"])
        driver = get_user(conn, driver_id)
        if driver["role"] != "driver":
            raise ShuttleError("bad_driver", "目标用户不是司机")
        if not conn.execute(
            "SELECT 1 FROM trip_drivers WHERE trip_id=? AND driver_id=?",
            (trip_id, driver_id),
        ).fetchone():
            raise ShuttleError("bad_driver", "该司机不属于本班次")
        try:
            conn.execute(
                """INSERT INTO driver_attendance(trip_id, driver_id, checked_at, operator_id)
                   VALUES (?,?,?,?)""",
                (trip_id, driver_id, db.now(), actor["id"]))
        except Exception:
            raise ShuttleError("already_checked", "该司机出勤已核销")
        log_event(conn, trip_id, "attendance",
                  {"driver": driver_id, "operator": actor["id"]})
        notification_adapter.send(
            conn, driver_id, "出勤已核销",
            f"运营员已核销您在班次 {trip['name']} 的出勤。")
        return {"trip_id": trip_id, "driver_id": driver_id, "checked_by": actor["id"]}


# ---------------------------------------------------------------- 视图组装

def trip_detail(conn, trip_id: str, viewer_id: str | None = None) -> dict:
    trip = dict(get_trip(conn, trip_id))
    stops = [dict(s) for s in trip_stops(conn, trip_id)]
    load = segment_load(conn, trip_id)
    segments = []
    for i, s in enumerate(stops[:-1]):
        nxt = stops[i + 1]
        segments.append({
            "edge": s["seq"], "from": s["name"], "to": nxt["name"],
            "from_skipped": bool(s["skipped"]), "to_skipped": bool(nxt["skipped"]),
            "load": load.get(s["seq"], 0), "seats": trip["seats"],
            "free": max(0, trip["seats"] - load.get(s["seq"], 0)),
        })
    reservations = [dict(r) for r in conn.execute(
        """SELECT rv.*, u.name AS employee_name FROM reservations rv
           JOIN users u ON u.id=rv.employee_id
           WHERE rv.trip_id=? ORDER BY rv.id""", (trip_id,)).fetchall()]
    waitlist = [dict(r) for r in conn.execute(
        """SELECT rv.*, u.name AS employee_name FROM reservations rv
           JOIN users u ON u.id=rv.employee_id
           WHERE rv.trip_id=? AND rv.status='waitlisted'
           ORDER BY waitlist_priority DESC, waitlisted_at ASC, id""",
        (trip_id,)).fetchall()]
    drivers = [dict(r) for r in conn.execute(
        """SELECT u.id, u.name,
                  (SELECT checked_at FROM driver_attendance a
                   WHERE a.trip_id=td.trip_id AND a.driver_id=u.id) AS checked_at,
                  (SELECT operator_id FROM driver_attendance a
                   WHERE a.trip_id=td.trip_id AND a.driver_id=u.id) AS checked_by
           FROM trip_drivers td JOIN users u ON u.id=td.driver_id
           WHERE td.trip_id=?""", (trip_id,)).fetchall()]
    my = None
    if viewer_id:
        row = conn.execute(
            "SELECT * FROM reservations WHERE trip_id=? AND employee_id=? "
            "AND status IN ('booked','waitlisted','boarded')",
            (trip_id, viewer_id)).fetchone()
        my = dict(row) if row else None
    trip.update({
        "stops": stops, "segments": segments,
        "reservations": reservations, "waitlist": waitlist,
        "drivers": drivers,
        "location": location_adapter.describe(conn, get_trip(conn, trip_id)),
        "my_reservation": my,
        "counts": {
            "booked": sum(1 for r in reservations if r["status"] == "booked"),
            "boarded": sum(1 for r in reservations if r["status"] == "boarded"),
            "alighted": sum(1 for r in reservations if r["status"] == "alighted"),
            "waitlisted": len(waitlist),
            "no_show": sum(1 for r in reservations if r["status"] == "no_show"),
            "cancelled": sum(1 for r in reservations if r["status"] == "cancelled"),
        },
    })
    return trip


def inbox(conn, user_id: str, limit=30):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit)).fetchall()]


def events(conn, trip_id: str | None = None, limit=100):
    if trip_id:
        rows = conn.execute(
            "SELECT * FROM events WHERE trip_id=? ORDER BY id DESC LIMIT ?",
            (trip_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def ride_records(conn, trip_id: str | None = None):
    """实际乘坐记录：以扫码流水为准，按预约聚合上车站/下车站与时间。"""
    where = "WHERE rv.trip_id=?" if trip_id else ""
    args = (trip_id,) if trip_id else ()
    sql = f"""
    SELECT rv.id AS reservation_id, rv.ticket, rv.trip_id,
           rv.employee_id, u.name AS employee_name,
           rv.board_seq, rv.alight_seq,
           sb.name AS plan_board_stop, sa.name AS plan_alight_stop,
           b.scanned_at AS boarded_at, b.stop_seq AS board_seq_real,
           sbr.name AS real_board_stop,
           a.scanned_at AS alighted_at, a.stop_seq AS alight_seq_real,
           sar.name AS real_alight_stop,
           rv.status
    FROM reservations rv
    JOIN users u ON u.id=rv.employee_id
    LEFT JOIN scans b ON b.reservation_id=rv.id AND b.kind='board'
    LEFT JOIN scans a ON a.reservation_id=rv.id AND a.kind='alight'
    LEFT JOIN trip_stops sbr ON sbr.trip_id=rv.trip_id AND sbr.seq=b.stop_seq
    LEFT JOIN trip_stops sar ON sar.trip_id=rv.trip_id AND sar.seq=a.stop_seq
    LEFT JOIN trip_stops sb ON sb.trip_id=rv.trip_id AND sb.seq=rv.board_seq
    LEFT JOIN trip_stops sa ON sa.trip_id=rv.trip_id AND sa.seq=rv.alight_seq
    {where}
    ORDER BY rv.trip_id, rv.id"""
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
