"""SQLite 数据访问层。

并发策略：
- 每个写事务以 BEGIN IMMEDIATE 立即获取 RESERVED 锁，两个页面并发提交时
  第二个写者等待（busy_timeout），随后在同一事务内重新基于最新数据做冲突校验，
  因此不可能同时写入互相矛盾的判断；被拒绝的操作不会留下任何痕迹。
- 下载映射在读事务（deferred）中一次性读取记录与全部判断并计算分组，
  保证映射对应某一完整判断状态，而不是读到半截数据。
- record 表只提供 INSERT，不提供 UPDATE/DELETE，原记录永不改写。
"""

import sqlite3

from .logic import Judgment, Record, compute_groups, evaluate_proposal, group_of_record

SCHEMA = """
CREATE TABLE IF NOT EXISTS record (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    original_no TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source, original_no)
);

CREATE TABLE IF NOT EXISTS judgment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    record_a   INTEGER NOT NULL REFERENCES record(id),
    record_b   INTEGER NOT NULL REFERENCES record(id),
    verdict    TEXT NOT NULL CHECK(verdict IN ('same', 'different')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(record_a, record_b),
    CHECK(record_a < record_b)
);

CREATE INDEX IF NOT EXISTS idx_judgment_a ON judgment(record_a);
CREATE INDEX IF NOT EXISTS idx_judgment_b ON judgment(record_b);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    init_db(conn)
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)


# ---------- 读取 ----------

def _load_records(conn):
    rows = conn.execute(
        "SELECT id, source, original_no, name FROM record ORDER BY id"
    ).fetchall()
    return [Record(r["id"], r["source"], r["original_no"], r["name"]) for r in rows]


def _load_judgments(conn):
    rows = conn.execute(
        "SELECT id, record_a, record_b, verdict FROM judgment ORDER BY id"
    ).fetchall()
    return [Judgment(r["id"], r["record_a"], r["record_b"], r["verdict"]) for r in rows]


def get_snapshot(conn):
    """在单个读事务中取一致快照（记录 + 判断 + 派生分组）。"""
    conn.execute("BEGIN")
    try:
        records = _load_records(conn)
        judgments = _load_judgments(conn)
        record_ids = [r.id for r in records]
        groups = compute_groups(judgments, record_ids)
        mapping = group_of_record(groups)
        return {
            "records": records,
            "judgments": judgments,
            "groups": groups,
            "group_map": mapping,
        }
    finally:
        conn.execute("COMMIT")


# ---------- 写入 ----------

def insert_records(conn, rows):
    """批量插入记录；遇 (source, original_no) 重复则跳过。

    rows: [(source, original_no, name), ...]，调用方已完成字段校验/去重。
    返回 (inserted_count, skipped_count)。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        inserted = skipped = 0
        for source, original_no, name in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO record(source, original_no, name) VALUES (?, ?, ?)",
                (source, original_no, name),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
        conn.execute("COMMIT")
        return inserted, skipped
    except Exception:
        conn.execute("ROLLBACK")
        raise


def add_judgment(conn, a, b, verdict):
    """在写事务内重新加载最新状态并校验后写入判断。

    返回 evaluate_proposal 的结果 dict，并附带 judgment_id（仅 ok 时）。
    冲突/冗余时回滚，数据库保持原状。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        records = _load_records(conn)
        judgments = _load_judgments(conn)
        result = evaluate_proposal(judgments, [r.id for r in records], a, b, verdict)
        if result["status"] != "ok":
            conn.execute("ROLLBACK")
            return result
        lo, hi = sorted((a, b))
        cur = conn.execute(
            "INSERT INTO judgment(record_a, record_b, verdict) VALUES (?, ?, ?)",
            (lo, hi, verdict),
        )
        conn.execute("COMMIT")
        result["judgment_id"] = cur.lastrowid
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


def delete_judgment(conn, judgment_id):
    """撤销单条判断；分组由剩余同物判断重新派生，仍有其他连接的记录不会被拆开。

    返回 True 表示撤销成功，False 表示该判断不存在（同样保持原状）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("DELETE FROM judgment WHERE id = ?", (judgment_id,))
        found = cur.rowcount > 0
        conn.execute("COMMIT")
        return found
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_judgment(conn, judgment_id):
    row = conn.execute(
        "SELECT id, record_a, record_b, verdict FROM judgment WHERE id = ?",
        (judgment_id,),
    ).fetchone()
    if row is None:
        return None
    return Judgment(row["id"], row["record_a"], row["record_b"], row["verdict"])
