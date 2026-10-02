"""领域服务：区间容量模型 + 预约/候补/递补/换车/绕行/司机核验/运营核销。

容量模型
========
线路站点按序为 s0..sn-1，“边” ei 表示从 si 到 s(i+1) 的区段。
一条 [board, alight) 的预约占用 board..alight-1 的每条边各 1 个座位。
某预约区间可订 ⇔ 其覆盖的每条边 confirmed 占用 < 车辆座位数。
因此“后半段满、前半段空”时，只走前半段的人仍可订，只看整车总人数会误判。

并发安全
========
所有写操作在 BEGIN IMMEDIATE 事务内进行（SQLite 单写者），
容量校验与写入在同一事务，天然杜绝两个并发请求同时通过校验导致超卖。
trips.version 在换车/绕行等结构变更时递增，供 UI/乐观锁使用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .adapters import MockLocator, MockNotifier
from .db import transaction
from .errors import AuthzError, CapacityError, ConflictError, ValidationError


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ShuttleService:
    def __init__(self, conn: sqlite3.Connection,
                 notifier: MockNotifier | None = None,
                 locator: MockLocator | None = None):
        self.conn = conn
        self.notifier = notifier or MockNotifier()
        self.locator = locator or MockLocator()

    # ------------------------------------------------------------------ #
    # 基础读取
    # ------------------------------------------------------------------ #
    def get_route(self, route_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM routes WHERE id=?", (route_id,)
        ).fetchone()
        if not row:
            raise ValidationError(f"线路不存在: {route_id}")
        return row

    def list_stops(self, route_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM stops WHERE route_id=? AND trip_scope IS NULL "
            "ORDER BY seq", (route_id,)
        ).fetchall()

    def get_trip(self, trip_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
        if not row:
            raise ValidationError(f"班次不存在: {trip_id}")
        return row

    def _ordered_all_stops(self, trip_id: int
                           ) -> tuple[list[sqlite3.Row], list[bool], set[int]]:
        """返回 (顺序站列表, 是否被飞, 飞站id集合)：包含模板站与该班次临停，
        skipped 站保留在顺序中但标记。临停按 after_stop_id 插入。"""
        trip = self.get_trip(trip_id)
        base = self.conn.execute(
            "SELECT * FROM stops WHERE route_id=? AND "
            "(trip_scope IS NULL OR trip_scope=?) ORDER BY seq",
            (trip["route_id"], trip_id),
        ).fetchall()
        overrides = self.conn.execute(
            "SELECT * FROM trip_stop_overrides WHERE trip_id=?", (trip_id,)
        ).fetchall()
        skip_ids = {o["stop_id"] for o in overrides if o["kind"] == "skipped"}
        added = {o["stop_id"]: o["after_stop_id"]
                 for o in overrides if o["kind"] == "added"}

        template = [s for s in base if s["trip_scope"] is None]
        ordered: list[sqlite3.Row] = list(template)
        scoped = {s["id"]: s for s in base if s["trip_scope"] is not None}
        for stop_id, anchor in added.items():
            if any(s["id"] == stop_id for s in ordered):
                continue
            stop = scoped.get(stop_id) or self.conn.execute(
                "SELECT * FROM stops WHERE id=?", (stop_id,)
            ).fetchone()
            idx = next((i for i, s in enumerate(ordered) if s["id"] == anchor),
                       len(ordered) - 1)
            ordered.insert(idx + 1, stop)
        flags = [s["id"] in skip_ids for s in ordered]
        return ordered, flags, skip_ids

    def effective_stops(self, trip_id: int) -> list[sqlite3.Row]:
        """班次实际停靠站：模板站 - skipped + added（added 按 after_stop_id 插入）。"""
        ordered, flags, _ = self._ordered_all_stops(trip_id)
        return [s for s, sk in zip(ordered, flags) if not sk]

    def _stop_index_map(self, trip_id: int) -> tuple[list[sqlite3.Row], dict[int, int]]:
        stops = self.effective_stops(trip_id)
        return stops, {s["id"]: i for i, s in enumerate(stops)}

    def _edge_loads(self, trip_id: int, ignore_booking: int | None = None,
                    status: str = "confirmed") -> list[int]:
        """规范的边占用计算。

        ordered 含飞站（标记）与临停；有效边 = 相邻两个未飞站之间的区段。
        乘客在边的“起点”仍在车上即占用该边：
          - 上车站被飞的 confirmed 不应存在（skip_stop 会转候补）；
          - 下车站被飞 → 实际坐到飞站之后的第一个有效站，该段仍占用，
            避免把有人的座位当成空座而超卖。
        """
        ordered, flags, _ = self._ordered_all_stops(trip_id)
        pos = {s["id"]: i for i, s in enumerate(ordered)}
        eff = [(i, s) for i, (s, sk) in enumerate(zip(ordered, flags)) if not sk]
        # 有效边 e 连接 eff[e] 与 eff[e+1]，起点在 ordered 中的位置：
        edge_start_pos = [eff[e][0] for e in range(len(eff) - 1)]
        load = [0] * len(edge_start_pos)
        rows = self.conn.execute(
            "SELECT id, boarding_stop_id b, alighting_stop_id a FROM bookings "
            "WHERE trip_id=? AND status=?", (trip_id, status),
        ).fetchall()
        for r in rows:
            if ignore_booking is not None and r["id"] == ignore_booking:
                continue
            pb = pos.get(r["b"])
            pa = pos.get(r["a"])
            if pb is None or pa is None or pb >= pa:
                continue
            # 下车站被飞 → 离车位置延后到其后第一个未飞站
            leave = pa
            if flags[pa]:
                k = pa + 1
                while k < len(ordered) and flags[k]:
                    k += 1
                leave = k  # 可能等于 len(ordered)（终点被飞，坐到完）
            for e, sp in enumerate(edge_start_pos):
                if pb <= sp < leave:
                    load[e] += 1
        return load

    def _vehicle_seats(self, vehicle_id: str) -> int:
        row = self.conn.execute(
            "SELECT seats FROM vehicles WHERE id=? AND active=1", (vehicle_id,)
        ).fetchone()
        if not row:
            raise ValidationError(f"车辆不可用: {vehicle_id}")
        return row["seats"]

    # ------------------------------------------------------------------ #
    # 区间占用
    # ------------------------------------------------------------------ #
    def edge_load(self, trip_id: int) -> list[int]:
        """返回每条有效边的 confirmed 占用。"""
        return self._edge_loads(trip_id)

    def segment_availability(self, trip_id: int) -> list[dict]:
        """每条边的余座（预订页“剩余座位按区间展示”）。"""
        trip = self.get_trip(trip_id)
        seats = self._vehicle_seats(trip["vehicle_id"])
        stops = self.effective_stops(trip_id)
        load = self.edge_load(trip_id)
        out = []
        for e, n in enumerate(load):
            out.append({
                "edge": e,
                "from_stop": stops[e]["name"],
                "to_stop": stops[e + 1]["name"],
                "seats": seats,
                "used": n,
                "free": seats - n,
            })
        return out

    def can_book_interval(self, trip_id: int, board_id: int, alight_id: int,
                          seats: int | None = None,
                          ignore_booking: int | None = None) -> bool:
        """区间是否可订：覆盖的每条有效边占用都 < 座位数。"""
        trip = self.get_trip(trip_id)
        seats = seats or self._vehicle_seats(trip["vehicle_id"])
        stops, idx = self._stop_index_map(trip_id)
        if board_id not in idx or alight_id not in idx:
            raise ValidationError("上/下车站不在本班次停靠范围内")
        bi, ai = idx[board_id], idx[alight_id]
        if bi >= ai:
            raise ValidationError("下车站必须在上车站之后")
        load = self._edge_loads(trip_id, ignore_booking=ignore_booking)
        return all(load[e] + 1 <= seats for e in range(bi, ai))

    # ------------------------------------------------------------------ #
    # 预约 / 改站 / 取消
    # ------------------------------------------------------------------ #
    def _next_created_seq(self, conn: sqlite3.Connection, trip_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(created_seq),0)+1 n FROM bookings WHERE trip_id=?",
            (trip_id,),
        ).fetchone()
        return row["n"]

    def _next_queue_seq(self, conn: sqlite3.Connection, trip_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(queue_seq),0)+1 n FROM bookings "
            "WHERE trip_id=? AND status='waitlisted'", (trip_id,),
        ).fetchone()
        return row["n"]

    def book(self, trip_id: int, employee_id: int,
             board_stop_id: int, alight_stop_id: int) -> dict:
        """员工预约。区间满员则进候补队列（FIFO）。返回 {status, booking_id}。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            if trip["status"] in ("completed", "cancelled"):
                raise ConflictError("该班次已结束或取消，无法预约")
            existing = self.conn.execute(
                "SELECT * FROM bookings WHERE trip_id=? AND employee_id=?",
                (trip_id, employee_id),
            ).fetchone()
            if existing and existing["status"] in ("confirmed", "waitlisted"):
                raise ConflictError("您已预约该班次，如需调整请使用改站")
            seq = self._next_created_seq(self.conn, trip_id)
            if self.can_book_interval(trip_id, board_stop_id, alight_stop_id):
                cur = self.conn.execute(
                    "INSERT INTO bookings(trip_id,employee_id,boarding_stop_id,"
                    "alighting_stop_id,status,created_seq) "
                    "VALUES(?,?,?,?,?,?)",
                    (trip_id, employee_id, board_stop_id, alight_stop_id,
                     "confirmed", seq),
                )
                bid = cur.lastrowid
                self._log(employee_id, "book",
                          f"trip={trip_id} booking={bid} confirmed")
                return {"status": "confirmed", "booking_id": bid}
            qseq = self._next_queue_seq(self.conn, trip_id)
            cur = self.conn.execute(
                "INSERT INTO bookings(trip_id,employee_id,boarding_stop_id,"
                "alighting_stop_id,status,created_seq,queue_seq) "
                "VALUES(?,?,?,?,?,?,?)",
                (trip_id, employee_id, board_stop_id, alight_stop_id,
                 "waitlisted", seq, qseq),
            )
            bid = cur.lastrowid
            self._log(employee_id, "book",
                      f"trip={trip_id} booking={bid} waitlisted seq={qseq}")
            self.notifier.send(
                employee_id, "已进入候补",
                f"班次 {trip_id} 您的上下车区间已满，候补序号 {qseq}，"
                "有座位时将按区间容量自动递补。",
            )
            return {"status": "waitlisted", "booking_id": bid, "queue_seq": qseq}

    def change_stops(self, trip_id: int, employee_id: int,
                     new_board_id: int, new_alight_id: int) -> dict:
        """员工改站。已确认者改站后若新区间满，则退回候补队尾（公平）。"""
        with transaction(self.conn):
            bk = self.conn.execute(
                "SELECT * FROM bookings WHERE trip_id=? AND employee_id=?",
                (trip_id, employee_id),
            ).fetchone()
            if not bk or bk["status"] not in ("confirmed", "waitlisted"):
                raise ConflictError("没有可改站的有效预约")
            if self.can_book_interval(trip_id, new_board_id, new_alight_id,
                                      ignore_booking=bk["id"]):
                if bk["status"] == "waitlisted":
                    # 候补者改站：更新区间但保留候补序号，交由统一 FIFO 递补，
                    # 避免凭“改站”插到同区间更早候补者前面
                    self.conn.execute(
                        "UPDATE bookings SET boarding_stop_id=?, alighting_stop_id=? "
                        "WHERE id=?", (new_board_id, new_alight_id, bk["id"]),
                    )
                else:
                    self.conn.execute(
                        "UPDATE bookings SET boarding_stop_id=?, alighting_stop_id=? "
                        "WHERE id=?", (new_board_id, new_alight_id, bk["id"]),
                    )
                self._log(employee_id, "change_stops",
                          f"booking={bk['id']} -> stops {new_board_id}/{new_alight_id}")
                # 改站可能腾出原区间，统一按 FIFO 重算递补
                promoted = self._promote_waitlist(trip_id)
                fresh = self.conn.execute(
                    "SELECT status FROM bookings WHERE id=?", (bk["id"],)
                ).fetchone()
                return {"status": fresh["status"], "booking_id": bk["id"],
                        "promoted": promoted}
            # 新区间满：confirmed 也需退回候补队尾，腾出原区间触发递补
            qseq = self._next_queue_seq(self.conn, trip_id)
            self.conn.execute(
                "UPDATE bookings SET boarding_stop_id=?, alighting_stop_id=?, "
                "status='waitlisted', queue_seq=? WHERE id=?",
                (new_board_id, new_alight_id, qseq, bk["id"]),
            )
            self._log(employee_id, "change_stops",
                      f"booking={bk['id']} falls back to waitlist seq={qseq}")
            promoted = self._promote_waitlist(trip_id)
            self.notifier.send(
                employee_id, "改站后进入候补",
                "新的上下车区间暂无余座，已为您排入候补队尾。",
            )
            return {"status": "waitlisted", "booking_id": bk["id"],
                    "queue_seq": qseq, "promoted": promoted}

    def cancel(self, booking_id: int, actor_id: int) -> None:
        """取消预约并立即按区间 FIFO 递补。"""
        with transaction(self.conn):
            bk = self.conn.execute(
                "SELECT * FROM bookings WHERE id=?", (booking_id,)
            ).fetchone()
            if not bk:
                raise ValidationError("预约不存在")
            if bk["employee_id"] != actor_id:
                # 运营员可代取消（授权线路校验在 operator 入口完成）
                op = self.conn.execute(
                    "SELECT role FROM employees WHERE id=?", (actor_id,)
                ).fetchone()
                if op is None or op["role"] not in ("operator", "admin"):
                    raise AuthzError("只能取消本人预约")
            if bk["status"] not in ("confirmed", "waitlisted"):
                raise ConflictError("该预约已结束，无法取消")
            self.conn.execute(
                "UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,)
            )
            self._log(actor_id, "cancel", f"booking={booking_id}")
            self._promote_waitlist(bk["trip_id"])

    # ------------------------------------------------------------------ #
    # 候补递补（区间感知 FIFO）
    # ------------------------------------------------------------------ #
    def _promote_waitlist(self, trip_id: int,
                          min_board_order: int | None = None) -> list[int]:
        """按候补序号逐个递补；某人区间仍满则跳过（不阻塞后续可订的人）。

        min_board_order：车辆已驶离站点后传入，仅递补上车站顺序 ≥ 该值的候补
        （已错过上车站的人不再自动递补）。
        一轮扫描可能跳过多个；为稳妥循环扫描直到无新增递补（数据量小，
        且严格保持 FIFO 语义：被跳过者不会让位给同区间的后到者，
        只会被不同区间、互不重叠的后到者超越）。
        """
        trip = self.conn.execute(
            "SELECT * FROM trips WHERE id=?", (trip_id,)
        ).fetchone()
        seats = self._vehicle_seats(trip["vehicle_id"])
        stops, idx = self._stop_index_map(trip_id)
        promoted: list[int] = []
        while True:
            waiting = self.conn.execute(
                "SELECT * FROM bookings WHERE trip_id=? AND status='waitlisted' "
                "ORDER BY queue_seq", (trip_id,),
            ).fetchall()
            progress = False
            for bk in waiting:
                if bk["id"] in promoted:
                    continue
                board_order = idx.get(bk["boarding_stop_id"])
                if board_order is None:
                    continue  # 上车站被飞站，等运营处理
                if min_board_order is not None and board_order < min_board_order:
                    continue  # 已错过该站
                if self.can_book_interval(trip_id, bk["boarding_stop_id"],
                                          bk["alighting_stop_id"], seats=seats,
                                          ignore_booking=bk["id"]):
                    self.conn.execute(
                        "UPDATE bookings SET status='confirmed', queue_seq=NULL, "
                        "promoted_from_waitlist=1 WHERE id=?", (bk["id"],)
                    )
                    promoted.append(bk["id"])
                    progress = True
                    self._log(None, "promote",
                              f"trip={trip_id} booking={bk['id']}")
                    self.notifier.send(
                        bk["employee_id"], "候补成功",
                        f"班次 {trip_id} 已有座位，您已候补成功，请按时乘车。",
                    )
            if not progress:
                break
        return promoted

    def promote_now(self, trip_id: int) -> list[int]:
        """手动触发递补（一般无需，系统在取消/改站/换车后自动触发）。"""
        with transaction(self.conn):
            return self._promote_waitlist(trip_id)

    # ------------------------------------------------------------------ #
    # 运营：授权校验 / 换车 / 绕行
    # ------------------------------------------------------------------ #
    def _require_route_operator(self, operator_id: int, route_id: str) -> None:
        emp = self.conn.execute(
            "SELECT * FROM employees WHERE id=?", (operator_id,)
        ).fetchone()
        if not emp:
            raise AuthzError("用户不存在")
        if emp["role"] == "admin":
            return
        if emp["role"] != "operator":
            raise AuthzError("仅运营员可执行该操作")
        ok = self.conn.execute(
            "SELECT 1 FROM operator_routes WHERE operator_id=? AND route_id=?",
            (operator_id, route_id),
        ).fetchone()
        if not ok:
            raise AuthzError(f"未被授权操作线路 {route_id}")

    def change_vehicle(self, trip_id: int, operator_id: int,
                       new_vehicle_id: str) -> dict:
        """临时换车。缩容时按“候补队尾倒序”降级为候补，再尝试 FIFO 递补。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            self._require_route_operator(operator_id, trip["route_id"])
            new_seats = self._vehicle_seats(new_vehicle_id)
            old_vid = trip["vehicle_id"]

            self.conn.execute(
                "UPDATE trips SET vehicle_id=?, version=version+1 WHERE id=?",
                (new_vehicle_id, trip_id),
            )
            self._log(operator_id, "change_vehicle",
                      f"trip={trip_id} {old_vid}->{new_vehicle_id} "
                      f"seats={new_seats}")

            demoted: list[int] = []
            if new_seats < self._vehicle_seats(old_vid):
                # 反复找出当前“使超载成立”的、最靠候补语义的已确认乘客进行降级。
                # 策略：按 created_seq 倒序（最近订的先让），降级进候补队尾，
                # 直到所有边不超载。随后再 FIFO 递补，保证最终状态与
                # “以新车容量重新排队”一致且稳定。
                while True:
                    load = self.edge_load(trip_id)
                    if all(n <= new_seats for n in load):
                        break
                    # 选覆盖最紧边、且最晚预约的确认者降级
                    worst_edges = [e for e, n in enumerate(load) if n > new_seats]
                    cand = self.conn.execute(
                        "SELECT * FROM bookings WHERE trip_id=? AND status='confirmed' "
                        "ORDER BY created_seq DESC", (trip_id,),
                    ).fetchall()
                    stops, idx = self._stop_index_map(trip_id)
                    pick = None
                    for c in cand:
                        bi, ai = idx[c["boarding_stop_id"]], idx[c["alighting_stop_id"]]
                        if any(bi <= e < ai for e in worst_edges):
                            pick = c
                            break
                    if pick is None:
                        break
                    qseq = self._next_queue_seq(self.conn, trip_id)
                    self.conn.execute(
                        "UPDATE bookings SET status='waitlisted', queue_seq=? "
                        "WHERE id=?", (qseq, pick["id"]),
                    )
                    demoted.append(pick["id"])
                    self.notifier.send(
                        pick["employee_id"], "车辆临时调整，您已转入候补",
                        f"班次 {trip_id} 换为 {new_seats} 座车辆，座位不足，"
                        "您已进入候补队列，有座将自动通知。",
                    )
                    self._log(operator_id, "demote_on_shrink",
                              f"booking={pick['id']} seq={qseq}")
            promoted = self._promote_waitlist(trip_id)
            return {"vehicle": new_vehicle_id, "seats": new_seats,
                    "demoted": demoted, "promoted": promoted}

    def skip_stop(self, trip_id: int, operator_id: int, stop_id: int,
                  note: str = "") -> None:
        """绕行飞站：某站不停。已在此站上车或仅在此区间受影响的预约按规则处理。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            self._require_route_operator(operator_id, trip["route_id"])
            ordered, _, _ = self._ordered_all_stops(trip_id)
            if not any(s["id"] == stop_id for s in ordered):
                raise ValidationError("站点不属于该班次")
            stop = self.conn.execute(
                "SELECT * FROM stops WHERE id=?", (stop_id,)
            ).fetchone()
            self.conn.execute(
                "INSERT INTO trip_stop_overrides(trip_id,stop_id,kind,note) "
                "VALUES(?,?, 'skipped',?) ON CONFLICT(trip_id,stop_id) DO UPDATE SET "
                "kind='skipped', note=excluded.note",
                (trip_id, stop_id, note),
            )
            self.conn.execute(
                "UPDATE trips SET detour=1, version=version+1 WHERE id=?", (trip_id,)
            )
            self._log(operator_id, "skip_stop",
                      f"trip={trip_id} stop={stop_id} ({note})")
            # 上车站被飞的确认乘客 → 转入候补（需改站后按 FIFO 递补）；
            # 下车站被飞的乘客实际坐到下一有效站，占用由边计算自动延伸，无需处理
            affected = self.conn.execute(
                "SELECT * FROM bookings WHERE trip_id=? AND status IN "
                "('confirmed','waitlisted') AND boarding_stop_id=?",
                (trip_id, stop_id),
            ).fetchall()
            for bk in affected:
                if bk["status"] == "confirmed":
                    qseq = self._next_queue_seq(self.conn, trip_id)
                    self.conn.execute(
                        "UPDATE bookings SET status='waitlisted', queue_seq=? "
                        "WHERE id=?", (qseq, bk["id"]),
                    )
                self.notifier.send(
                    bk["employee_id"], "线路绕行通知",
                    f"班次 {trip_id} 将不停靠 {stop['name']}，"
                    "请在预订页改选邻近上车站点，系统将按候补顺序安排座位。",
                )
            self._promote_waitlist(trip_id)

    def add_temporary_stop(self, trip_id: int, operator_id: int,
                           name: str, after_stop_id: int,
                           note: str = "") -> int:
        """绕行临停：在 after_stop_id 之后增加一个临时站（同步可上下客）。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            self._require_route_operator(operator_id, trip["route_id"])
            anchor = self.conn.execute(
                "SELECT * FROM stops WHERE id=? AND route_id=? AND "
                "(trip_scope IS NULL OR trip_scope=?)",
                (after_stop_id, trip["route_id"], trip_id),
            ).fetchone()
            if not anchor:
                raise ValidationError("锚点站点不存在")
            code = f"TMP{trip_id}_{after_stop_id}"
            cur = self.conn.execute(
                "INSERT INTO stops(route_id,code,name,seq,trip_scope) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(route_id,code,trip_scope) DO UPDATE SET name=excluded.name "
                "RETURNING id",
                (trip["route_id"], code, name, anchor["seq"] + 500, trip_id),
            )
            stop_id = cur.fetchone()["id"]
            self.conn.execute(
                "INSERT INTO trip_stop_overrides(trip_id,stop_id,kind,"
                "after_stop_id,note) VALUES(?,?, 'added',?,?) "
                "ON CONFLICT(trip_id,stop_id) DO UPDATE SET after_stop_id=?,note=?",
                (trip_id, stop_id, after_stop_id, note, after_stop_id, note),
            )
            self.conn.execute(
                "UPDATE trips SET detour=1, version=version+1 WHERE id=?", (trip_id,)
            )
            self._log(operator_id, "add_temp_stop",
                      f"trip={trip_id} stop={stop_id} after={after_stop_id}")
            return stop_id

    def clear_detour(self, trip_id: int, operator_id: int) -> None:
        """解除绕行，恢复正常站点，随后重算递补。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            self._require_route_operator(operator_id, trip["route_id"])
            self.conn.execute(
                "DELETE FROM trip_stop_overrides WHERE trip_id=?", (trip_id,)
            )
            # 临停站行保留（可能被预约引用），仅不再停靠
            self.conn.execute(
                "UPDATE trips SET detour=0, version=version+1 WHERE id=?", (trip_id,)
            )
            self._log(operator_id, "clear_detour", f"trip={trip_id}")
            self._promote_waitlist(trip_id)

    # ------------------------------------------------------------------ #
    # 司机端：签到 / 扫码核验（幂等）
    # ------------------------------------------------------------------ #
    def driver_self_checkin(self, trip_id: int, driver_id: int) -> None:
        """司机自助签到。司机不能核销自己的出勤。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            if trip["driver_id"] != driver_id:
                raise AuthzError("您不是该班次司机")
            row = self.conn.execute(
                "SELECT * FROM driver_checkins WHERE trip_id=?", (trip_id,)
            ).fetchone()
            if row and row["checked_in_at"]:
                raise ConflictError("已签到，请勿重复")
            if row:
                self.conn.execute(
                    "UPDATE driver_checkins SET checked_in_at=? WHERE trip_id=?",
                    (_now(), trip_id),
                )
            else:
                self.conn.execute(
                    "INSERT INTO driver_checkins(trip_id,driver_id,checked_in_at) "
                    "VALUES(?,?,?)", (trip_id, driver_id, _now()),
                )
            self._log(driver_id, "driver_checkin", f"trip={trip_id}")

    def verify_checkin(self, trip_id: int, operator_id: int, note: str = "") -> None:
        """运营员核销司机出勤（仅授权线路；司机本人无法核销）。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            self._require_route_operator(operator_id, trip["route_id"])
            row = self.conn.execute(
                "SELECT * FROM driver_checkins WHERE trip_id=?", (trip_id,)
            ).fetchone()
            if not row or not row["checked_in_at"]:
                raise ConflictError("司机尚未签到，无法核销")
            if row["verified_at"]:
                raise ConflictError("已核销，请勿重复")
            self.conn.execute(
                "UPDATE driver_checkins SET verified_at=?, verified_by=?, "
                "verify_note=? WHERE trip_id=?",
                (_now(), operator_id, note, trip_id),
            )
            self._log(operator_id, "verify_checkin",
                      f"trip={trip_id} driver={row['driver_id']}")

    def boarding_scan(self, trip_id: int, driver_id: int,
                      booking_id: int) -> dict:
        """司机车载端扫码核验上车。重复扫码幂等：只确认第一次。

        返回 action=boarded（首次）/ already（重复扫码）/ waitlist（候补未递补）
        / wrong_trip / closed 等，前端用绿色/灰色标记区分。
        """
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            if trip["driver_id"] != driver_id:
                raise AuthzError("您不是该班次司机")
            bk = self.conn.execute(
                "SELECT * FROM bookings WHERE id=?", (booking_id,)
            ).fetchone()
            if not bk:
                return {"action": "not_found"}
            if bk["trip_id"] != trip_id:
                return {"action": "wrong_trip", "actual_trip": bk["trip_id"]}
            if bk["status"] in ("cancelled",):
                return {"action": "cancelled"}
            if bk["status"] == "noshow":
                return {"action": "closed", "reason": "已标记未上车"}
            if bk["status"] == "waitlisted":
                # 车端兜底：先按 FIFO 统一递补（避免越过更早的候补者），
                # 再判断本人是否在递补名单中；已驶离的站点不再递补
                stops = self.effective_stops(trip_id)
                nxt = self.locator.next_stop_order(trip_id, len(stops))
                if nxt is None:
                    return {"action": "closed", "reason": "班次已到终点站"}
                promoted = self._promote_waitlist(trip_id, min_board_order=nxt)
                if bk["id"] not in promoted:
                    return {"action": "waitlist", "queue_seq": bk["queue_seq"]}
                bk = self.conn.execute(
                    "SELECT * FROM bookings WHERE id=?", (booking_id,)
                ).fetchone()

            # status == confirmed
            if bk["boarded_at"]:
                self.conn.execute(
                    "UPDATE bookings SET scan_count=scan_count+1, last_scan_at=? "
                    "WHERE id=?", (_now(), booking_id),
                )
                self._log(driver_id, "scan_duplicate", f"booking={booking_id}")
                return {"action": "already", "boarded_at": bk["boarded_at"],
                        "scan_count": bk["scan_count"] + 1}
            self.conn.execute(
                "UPDATE bookings SET boarded_at=?, board_trip_id=?, "
                "scan_count=scan_count+1, last_scan_at=? WHERE id=?",
                (_now(), trip_id, _now(), booking_id),
            )
            self._log(driver_id, "boarding", f"booking={booking_id}")
            return {"action": "boarded", "booking_id": booking_id}

    def mark_noshow(self, trip_id: int, operator_id: int) -> list[int]:
        """班次结束后：已确认但未扫码上车者标记 noshow，并释放递补机会已无意义
        （班次已结束）；仅记录。运营员限定授权线路。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            self._require_route_operator(operator_id, trip["route_id"])
            rows = self.conn.execute(
                "SELECT id FROM bookings WHERE trip_id=? AND status='confirmed' "
                "AND boarded_at IS NULL", (trip_id,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            for i in ids:
                self.conn.execute(
                    "UPDATE bookings SET status='noshow' WHERE id=?", (i,)
                )
            if ids:
                self._log(operator_id, "noshow",
                          f"trip={trip_id} bookings={ids}")
            return ids

    def depart_stop(self, trip_id: int, driver_id: int, stop_id: int) -> dict:
        """司机报告驶离某站：推进定位，已驶离站点的候补不再递补，
        并尝试递补后续站点的候补。"""
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            if trip["driver_id"] != driver_id:
                raise AuthzError("您不是该班次司机")
            stops, idx = self._stop_index_map(trip_id)
            order = idx.get(stop_id)
            if order is None:
                raise ValidationError("站点不在本班次")
            self.locator.mark_departed(trip_id, order)
            # 迟到未上车：该上车站已驶离且未扫码 → noshow，释放其区间座位
            late = self.conn.execute(
                "SELECT * FROM bookings WHERE trip_id=? AND status='confirmed' "
                "AND boarded_at IS NULL AND boarding_stop_id=?",
                (trip_id, stop_id),
            ).fetchall()
            for bk in late:
                self.conn.execute(
                    "UPDATE bookings SET status='noshow' WHERE id=?", (bk["id"],)
                )
                self.notifier.send(
                    bk["employee_id"], "已标记未上车",
                    f"车辆已驶离，班次 {trip_id} 您未在 {stops[order]['name']} "
                    "扫码上车，已记录为未乘车。",
                )
                self._log(driver_id, "late_noshow", f"booking={bk['id']}")
            promoted = self._promote_waitlist(trip_id, min_board_order=order + 1)
            self._log(driver_id, "depart_stop",
                      f"trip={trip_id} stop={stop_id} order={order}")
            return {"departed_order": order, "promoted": promoted}

    def depart(self, trip_id: int, driver_id: int) -> None:
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            if trip["driver_id"] != driver_id:
                raise AuthzError("您不是该班次司机")
            self.conn.execute(
                "UPDATE trips SET status='departed' WHERE id=?", (trip_id,)
            )

    def complete(self, trip_id: int, driver_id: int) -> None:
        with transaction(self.conn):
            trip = self.get_trip(trip_id)
            if trip["driver_id"] != driver_id:
                raise AuthzError("您不是该班次司机")
            self.conn.execute(
                "UPDATE trips SET status='completed' WHERE id=?", (trip_id,)
            )

    # ------------------------------------------------------------------ #
    # 乘坐记录 / 名单
    # ------------------------------------------------------------------ #
    def boarding_list(self, trip_id: int) -> list[dict]:
        """待上车名单：confirmed 且未扫码，按实际上车站顺序排序（司机端大字）。"""
        stops, idx = self._stop_index_map(trip_id)
        rows = self.conn.execute(
            "SELECT b.*, e.name AS emp_name FROM bookings b "
            "JOIN employees e ON e.id=b.employee_id "
            "WHERE b.trip_id=? AND b.status='confirmed' AND b.boarded_at IS NULL "
            "ORDER BY b.boarding_stop_id, b.created_seq", (trip_id,),
        ).fetchall()
        out = []
        for r in rows:
            stop = next(s for s in stops if s["id"] == r["boarding_stop_id"])
            out.append({
                "booking_id": r["id"],
                "employee_id": r["employee_id"],
                "employee_name": r["emp_name"],
                "boarding_stop": stop["name"],
                "boarding_order": idx[stop["id"]],
                "alighting_stop": next(
                    s["name"] for s in stops if s["id"] == r["alighting_stop_id"]
                ),
            })
        return out

    def rider_records(self, trip_id: int) -> list[dict]:
        """实际乘坐记录（运营查看）：含状态、扫码次数、上下车、是否候补递补。"""
        stops, idx = self._stop_index_map(trip_id)
        rows = self.conn.execute(
            "SELECT b.*, e.name AS emp_name FROM bookings b "
            "JOIN employees e ON e.id=b.employee_id "
            "WHERE b.trip_id=? ORDER BY b.created_seq", (trip_id,),
        ).fetchall()
        out = []
        for r in rows:
            bs = next((s["name"] for s in stops if s["id"] == r["boarding_stop_id"]),
                      f"#{r['boarding_stop_id']}")
            as_ = next((s["name"] for s in stops if s["id"] == r["alighting_stop_id"]),
                       f"#{r['alighting_stop_id']}")
            out.append({
                "booking_id": r["id"],
                "employee": r["emp_name"],
                "board": bs,
                "alight": as_,
                "status": r["status"],
                "boarded_at": r["boarded_at"],
                "scans": r["scan_count"],
                "promoted": bool(r["promoted_from_waitlist"]),
            })
        return out

    # ------------------------------------------------------------------ #
    def _log(self, actor_id: int | None, action: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(at,actor_id,action,detail) VALUES(?,?,?,?)",
            (_now(), actor_id, action, detail),
        )
