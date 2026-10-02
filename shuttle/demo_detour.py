"""一键演示脚本：绕行 → 改站 → 换车缩容 → 扫码核验（含重复扫码幂等）。

用法：python3 -m shuttle.demo_detour
直接操作核心 API（不经 HTTP），每步打印结果。
"""
import os

from . import core, db, seed as seed_mod


def line(t=""):
    print(t)


def show_trip(conn, tid):
    d = core.trip_detail(conn, tid)
    line(f"  车辆 {d['plate']}（{d['seats']}座）状态 {d['status']}")
    for s in d["segments"]:
        if s["from_skipped"] or s["to_skipped"]:
            continue
        line(f"    {s['from']}→{s['to']}: 占用 {s['load']}/{s['seats']}，余 {s['free']}")
    line("    预约：")
    for r in d["reservations"]:
        line(f"      - {r['employee_name']} {r['board_seq']}→{r['alight_seq']} "
             f"{r['status']}"
             + (f" [待确认:{r['remap_flag']}]" if r["remap_flag"] else ""))
    line("    候补：" + ", ".join(
        f"{r['employee_name']}{'(优先)' if r['waitlist_priority'] else ''}"
        for r in d["waitlist"]) or "    候补：无")


def main() -> None:
    seed_mod.seed()  # 从干净数据开始
    op1 = {"id": "op1"}

    line("\n========== 1) T3 绕行样例：跳过「滨河路口」(seq=1) ==========")
    r = core.detour_skip_stops(None, op1, "T3", [1])
    line(f"  绕行结果：改站 {r['affected']} 人，取消 {r['cancelled']} 人，"
         f"递补 {r['promoted']} 人")
    with db.connect() as conn:
        show_trip(conn, "T3")

    line("\n  员工 e2 在预订页确认绕行后的站点调整：")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM reservations WHERE trip_id='T3' AND employee_id='e2'").fetchone()
        rid = row["id"]
    out = core.ack_remap(None, {"id": "e2"}, rid)
    line(f"  确认后：{out['board_seq']}→{out['alight_seq']}，remap_flag={out['remap_flag']}")

    line("\n========== 2) 恢复站点，回到正常线路 ==========")
    core.restore_stops(None, op1, "T3", [1])

    line("\n========== 3) T2 换车缩容：12 座 → 8 座 ==========")
    line("  缩容前：")
    with db.connect() as conn:
        show_trip(conn, "T2")
    r = core.replace_vehicle(None, op1, "T2", "V2")  # V2=20? 用 8 座需先加车
    line(f"  V2 为 20 座（扩容），降补 {r['demoted']}")

    # 真正的缩容：临时插入一辆 8 座车
    with db.Tx() as conn:
        conn.execute("INSERT INTO vehicles(id,plate,seats) VALUES('V8','粤B·8008',8)")
    r = core.replace_vehicle(None, op1, "T2", "V8")
    line(f"  换到 8 座：降补 {len(r['demoted'])} 人 → {r['demoted']}；"
         f"递补 {len(r['promoted'])} 人")
    with db.connect() as conn:
        show_trip(conn, "T2")

    line("\n========== 4) T1 发车，扫码核验与重复扫码幂等 ==========")
    with db.connect() as conn:
        t = conn.execute("SELECT ticket FROM reservations WHERE trip_id='T1' AND employee_id='e1'").fetchone()
        ticket = t["ticket"]
    driver = {"id": "d1"}
    line("  到站 seq=0（城南家园）：")
    core.advance(None, driver, "T1")
    line(f"  第 1 次扫 {ticket}：")
    out = core.scan_board(None, driver, "T1", ticket)
    line(f"    -> {out['message']}，状态 {out['reservation']['status']}")
    line("  第 2 次扫同一张票（重复扫码）：")
    out = core.scan_board(None, driver, "T1", ticket)
    line(f"    -> idempotent={out['idempotent']}，{out['message']}；状态仍为 "
         f"{out['reservation']['status']}，不重复计数")

    line("\n========== 5) 迟到未上车：关门离站自动判爽约 ==========")
    ev = core.advance(None, driver, "T1")["event"]
    line(f"  离开 seq=0：爽约 {ev['no_show']}，候补错过 {ev['waitlist_missed']}")

    line("\n演示完成。启动 ./run.sh 可在页面上操作完整流程。")
    line(f"数据库：{os.environ.get('SHUTTLE_DB', db.DB_PATH)}")


if __name__ == "__main__":
    main()
