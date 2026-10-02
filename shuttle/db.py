"""SQLite 存储层：建表、连接与全局写锁。

所有写操作都在 core.py 中持 _write_lock 后以 BEGIN IMMEDIATE 事务执行，
配合每请求独立连接，保证同一区间的并发预约不会超卖。
"""
import os
import sqlite3
import threading
from datetime import datetime

DB_PATH = os.environ.get(
    "SHUTTLE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "shuttle.db")
)

_write_lock = threading.RLock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    role  TEXT NOT NULL CHECK (role IN ('employee','driver','operator'))
);

CREATE TABLE IF NOT EXISTS vehicles (
    id     TEXT PRIMARY KEY,
    plate  TEXT NOT NULL,
    seats  INTEGER NOT NULL CHECK (seats > 0)
);

CREATE TABLE IF NOT EXISTS routes (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL
);

-- 线路模板站点（顺序即 seq）
CREATE TABLE IF NOT EXISTS stops (
    route_id  TEXT NOT NULL REFERENCES routes(id),
    seq       INTEGER NOT NULL,
    name      TEXT NOT NULL,
    PRIMARY KEY (route_id, seq)
);

CREATE TABLE IF NOT EXISTS trips (
    id          TEXT PRIMARY KEY,
    route_id    TEXT NOT NULL REFERENCES routes(id),
    name        TEXT NOT NULL,
    depart_at   TEXT NOT NULL,
    vehicle_id  TEXT NOT NULL REFERENCES vehicles(id),
    status      TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled','active','completed')),
    pos_seq     INTEGER NOT NULL DEFAULT 0,   -- 下一个/当前停靠站 seq
    pos_phase   TEXT NOT NULL DEFAULT 'before'
                CHECK (pos_phase IN ('before','at','after'))
);

-- 班次司机（可多名）
CREATE TABLE IF NOT EXISTS trip_drivers (
    trip_id    TEXT NOT NULL REFERENCES trips(id),
    driver_id  TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (trip_id, driver_id)
);

-- 司机出勤核销：只能由运营员登记（见 core.mark_attendance）
CREATE TABLE IF NOT EXISTS driver_attendance (
    trip_id     TEXT NOT NULL REFERENCES trips(id),
    driver_id   TEXT NOT NULL REFERENCES users(id),
    checked_at  TEXT NOT NULL,
    operator_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (trip_id, driver_id)
);

-- 运营员可操作的线路授权
CREATE TABLE IF NOT EXISTS operator_routes (
    operator_id TEXT NOT NULL REFERENCES users(id),
    route_id    TEXT NOT NULL REFERENCES routes(id),
    PRIMARY KEY (operator_id, route_id)
);

-- 班次站点快照：绕行时只改本班次，不影响线路模板与其他班次
CREATE TABLE IF NOT EXISTS trip_stops (
    trip_id  TEXT NOT NULL REFERENCES trips(id),
    seq      INTEGER NOT NULL,
    name     TEXT NOT NULL,
    skipped  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (trip_id, seq)
);

CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket      TEXT NOT NULL UNIQUE,          -- 乘车码
    trip_id     TEXT NOT NULL REFERENCES trips(id),
    employee_id TEXT NOT NULL REFERENCES users(id),
    board_seq   INTEGER NOT NULL,
    alight_seq  INTEGER NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN ('booked','waitlisted','boarded',
                                  'alighted','no_show','cancelled')),
    booked_at      TEXT NOT NULL,
    waitlisted_at  TEXT,
    waitlist_priority INTEGER NOT NULL DEFAULT 0,  -- 换车缩容被降补者优先
    remap_flag  TEXT CHECK (remap_flag IN ('board','alight')), -- 绕行跳站待改站
    boarded_at  TEXT,
    alighted_at TEXT,
    no_show_at  TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT,
    CHECK (board_seq < alight_seq)
);
CREATE INDEX IF NOT EXISTS idx_res_trip ON reservations(trip_id, status);

-- 扫码流水：(班次, 预约, 扫码类型) 唯一，重复扫码直接命中既有记录
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id         TEXT NOT NULL REFERENCES trips(id),
    reservation_id  INTEGER NOT NULL REFERENCES reservations(id),
    kind            TEXT NOT NULL CHECK (kind IN ('board','alight')),
    stop_seq        INTEGER NOT NULL,
    actor_id        TEXT NOT NULL REFERENCES users(id),
    scanned_at      TEXT NOT NULL,
    note            TEXT,
    UNIQUE (trip_id, reservation_id, kind)
);

-- 模拟通知适配器的落库收件箱
CREATE TABLE IF NOT EXISTS notifications (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title   TEXT NOT NULL,
    body    TEXT NOT NULL
);

-- 审计事件流
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    trip_id TEXT,
    type    TEXT NOT NULL,
    detail  TEXT NOT NULL
);
"""


def init_db(db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


class Tx:
    """持全局写锁的立即事务上下文。"""

    def __enter__(self):
        _write_lock.acquire()
        self.conn = connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
            _write_lock.release()
