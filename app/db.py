"""SQLite 数据访问层。

并发策略：
- 每个写事务以 BEGIN IMMEDIATE 立即获取 RESERVED 锁，两个页面并发提交时
  第二个写者等待（busy_timeout），随后在同一事务内重新基于最新数据做冲突校验，
  因此不可能同时写入互相矛盾的判断；被拒绝的操作不会留下任何痕迹。
- 下载映射在读事务（deferred）中一次性读取记录与全部判断并计算分组，
  保证映射对应某一完整判断状态，而不是读到半截数据。
- record 表只提供 INSERT，不提供 UPDATE/DELETE，原记录永不改写。

增量供数（订阅 / 批次）策略：
- 采用事务性发件箱（transactional outbox）：每次业务写操作（导入 / 判断 /
  撤销）在同一个 BEGIN IMMEDIATE 事务内先完成业务写入，再计算归属（去重组）
  差异并写入 change_set / change_record。业务失败整体回滚，不会留下供数记录；
  业务成功则变化必然落库，不可能漏记。
- 订阅在同一个立即写事务内创建并生成首批全量快照，游标记录建订时刻已消费的
  最大 change_set id；创建期间任何并发业务写都被写锁挡在事务外，事务提交后
  才放行的变化其 id 必大于游标，后续批次一定能领到，不会漏记。
- 批次一经生成，其序号与内容即固化（feed_batch_item 只 INSERT，无 UPDATE/
  DELETE 入口）；未确认批次保持不变，新变化只能进入后续批次。
- 领取、确认均在立即写事务内完成，配合写锁串行化，并发领取不会产生重复或
  跳号；所有状态落库，服务重启后游标与批次原样保留，不会跳过任何变化。
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

-- 增量供数：变化集（一次业务操作一条，id 单调递增即全局发生顺序）
CREATE TABLE IF NOT EXISTS change_set (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    op_type     TEXT NOT NULL CHECK(op_type IN ('upload', 'judge', 'undo')),
    op_ref      INTEGER,              -- 关联业务实体 id（judgment id 等），导入为空
    record_count INTEGER NOT NULL,    -- 本变化集影响的记录数
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS change_record (
    change_set_id INTEGER NOT NULL REFERENCES change_set(id),
    record_id     INTEGER NOT NULL REFERENCES record(id),
    new_group_id  TEXT NOT NULL,
    seq           INTEGER NOT NULL,   -- 变化集内顺序（按 record_id 排序）
    PRIMARY KEY (change_set_id, record_id)
);

CREATE INDEX IF NOT EXISTS idx_change_record_set ON change_record(change_set_id);

CREATE TABLE IF NOT EXISTS subscription (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    last_change_id INTEGER NOT NULL DEFAULT 0,  -- 已入批的最大 change_set id
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feed_batch (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscription(id),
    batch_no        INTEGER NOT NULL,   -- 订阅内从 1 开始的稳定批次号
    kind            TEXT NOT NULL CHECK(kind IN ('snapshot', 'delta')),
    change_set_id   INTEGER REFERENCES change_set(id),  -- snapshot 批次为空
    item_count      INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    acked_at        TEXT,               -- 确认时间；NULL 表示待确认
    UNIQUE(subscription_id, batch_no)
);

CREATE TABLE IF NOT EXISTS feed_batch_item (
    batch_id   INTEGER NOT NULL REFERENCES feed_batch(id),
    seq        INTEGER NOT NULL,       -- 批次内顺序
    record_id  INTEGER NOT NULL REFERENCES record(id),
    source     TEXT NOT NULL,
    original_no TEXT NOT NULL,
    name       TEXT NOT NULL,
    new_group_id TEXT NOT NULL,
    PRIMARY KEY (batch_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_feed_batch_sub ON feed_batch(subscription_id);
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


# ---------- 增量供数：变化集写入（必须在调用方的写事务内执行） ----------

def _current_group_map(conn):
    """事务内基于最新记录与判断重算归属。"""
    records = _load_records(conn)
    judgments = _load_judgments(conn)
    groups = compute_groups(judgments, [r.id for r in records])
    return group_of_record(groups)


def _write_change_set(conn, op_type, op_ref, old_map, new_map):
    """计算归属差异并写入变化集；无差异则不产生任何供数记录。

    old_map / new_map: record_id -> group_id。
    返回 change_set id（无变化时返回 None）。
    """
    changed = sorted(
        rid for rid, gid in new_map.items()
        if old_map.get(rid) != gid
    )
    if not changed:
        return None
    cur = conn.execute(
        "INSERT INTO change_set(op_type, op_ref, record_count) VALUES (?, ?, ?)",
        (op_type, op_ref, len(changed)),
    )
    change_set_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO change_record(change_set_id, record_id, new_group_id, seq) "
        "VALUES (?, ?, ?, ?)",
        [
            (change_set_id, rid, new_map[rid], seq)
            for seq, rid in enumerate(changed, start=1)
        ],
    )
    return change_set_id


# ---------- 写入 ----------

def insert_records(conn, rows):
    """批量插入记录；遇 (source, original_no) 重复则跳过。

    rows: [(source, original_no, name), ...]，调用方已完成字段校验/去重。
    与变化集写入处于同一事务：插入失败整体回滚，不留供数记录；成功则新增
    记录的归属变化必然随变化集一起落库。
    返回 (inserted_count, skipped_count)。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        old_map = _current_group_map(conn)
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
        if inserted:
            new_map = _current_group_map(conn)
            _write_change_set(conn, "upload", None, old_map, new_map)
        conn.execute("COMMIT")
        return inserted, skipped
    except Exception:
        conn.execute("ROLLBACK")
        raise


def add_judgment(conn, a, b, verdict):
    """在写事务内重新加载最新状态并校验后写入判断。

    返回 evaluate_proposal 的结果 dict，并附带 judgment_id（仅 ok 时）。
    冲突/冗余时回滚，数据库保持原状，且不会产生变化集。
    判断写入与归属变化集在同一事务提交。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        old_map = _current_group_map(conn)
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
        judgment_id = cur.lastrowid
        new_map = _current_group_map(conn)
        _write_change_set(conn, "judge", judgment_id, old_map, new_map)
        conn.execute("COMMIT")
        result["judgment_id"] = judgment_id
        return result
    except Exception:
        conn.execute("ROLLBACK")
        raise


def delete_judgment(conn, judgment_id):
    """撤销单条判断；分组由剩余同物判断重新派生，仍有其他连接的记录不会被拆开。

    删除与归属变化集在同一事务提交。
    返回 True 表示撤销成功，False 表示该判断不存在（同样保持原状）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        old_map = _current_group_map(conn)
        cur = conn.execute("DELETE FROM judgment WHERE id = ?", (judgment_id,))
        found = cur.rowcount > 0
        if found:
            new_map = _current_group_map(conn)
            _write_change_set(conn, "undo", judgment_id, old_map, new_map)
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


# ---------- 增量供数：订阅与批次 ----------

def _insert_batch(conn, subscription_id, batch_no, kind, change_set_id, items):
    """在当前事务内固化一个批次及其全部条目（items 已按 record_id 排序）。

    items: [(record_id, source, original_no, name, new_group_id), ...]
    返回 batch_id。
    """
    cur = conn.execute(
        "INSERT INTO feed_batch(subscription_id, batch_no, kind, change_set_id, item_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (subscription_id, batch_no, kind, change_set_id, len(items)),
    )
    batch_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO feed_batch_item(batch_id, seq, record_id, source, original_no, "
        "name, new_group_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (batch_id, seq, rid, source, original_no, name, group_id)
            for seq, (rid, source, original_no, name, group_id) in enumerate(items, start=1)
        ],
    )
    return batch_id


def create_subscription(conn, name):
    """创建订阅并生成首批全量快照，全部在一个立即写事务内完成。

    写锁保证：事务进行期间没有任何业务写可以提交，因此“首批快照 + 游标”
    与后续变化严格衔接，创建订阅时的并发修改不会漏记。
    返回 dict（含 id / name / first_batch 信息）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        records = _load_records(conn)
        judgments = _load_judgments(conn)
        groups = compute_groups(judgments, [r.id for r in records])
        group_map = group_of_record(groups)
        last_change = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM change_set"
        ).fetchone()["m"]

        cur = conn.execute(
            "INSERT INTO subscription(name, last_change_id) VALUES (?, ?)",
            (name, last_change),
        )
        subscription_id = cur.lastrowid

        items = [
            (r.id, r.source, r.original_no, r.name, group_map[r.id])
            for r in records
        ]
        batch_id = _insert_batch(conn, subscription_id, 1, "snapshot", None, items)
        conn.execute("COMMIT")
        return {
            "id": subscription_id,
            "name": name,
            "last_change_id": last_change,
            "first_batch_id": batch_id,
            "first_batch_no": 1,
            "item_count": len(items),
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise


def list_subscriptions(conn):
    """返回全部订阅及其批次 / 确认进度。"""
    rows = conn.execute(
        "SELECT s.id, s.name, s.last_change_id, s.created_at, "
        "       COALESCE(MAX(b.batch_no), 0) AS batch_count, "
        "       COALESCE(MAX(CASE WHEN b.acked_at IS NOT NULL THEN b.batch_no END), 0) AS acked_batch_no "
        "FROM subscription s LEFT JOIN feed_batch b ON b.subscription_id = s.id "
        "GROUP BY s.id ORDER BY s.id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_subscription(conn, subscription_id):
    row = conn.execute(
        "SELECT id, name, last_change_id, created_at FROM subscription WHERE id = ?",
        (subscription_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def claim_next_batch(conn, subscription_id):
    """领取下一批。

    - 存在未确认批次：原样返回该批（标识与内容不变），不生成新批。
    - 全部已确认且有新变化：按 change_set 发生顺序，整批取出下一个变化集
      固化为新批次（一个变化集恰好一个批次，绝不拆分 / 合并 / 跨操作）。
    - 没有新变化：返回 None。
    写锁串行化并发领取，游标只在批次固化后推进，重启后状态不变、不跳变。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        sub = conn.execute(
            "SELECT id, last_change_id FROM subscription WHERE id = ?",
            (subscription_id,),
        ).fetchone()
        if sub is None:
            conn.execute("ROLLBACK")
            return {"status": "not_found"}

        pending = conn.execute(
            "SELECT id, batch_no FROM feed_batch "
            "WHERE subscription_id = ? AND acked_at IS NULL ORDER BY batch_no LIMIT 1",
            (subscription_id,),
        ).fetchone()
        if pending is not None:
            conn.execute("COMMIT")
            return {"status": "pending", "batch": _load_batch(conn, pending["id"])}

        nxt = conn.execute(
            "SELECT id, op_type, op_ref FROM change_set WHERE id > ? ORDER BY id LIMIT 1",
            (sub["last_change_id"],),
        ).fetchone()
        if nxt is None:
            conn.execute("COMMIT")
            return {"status": "up_to_date"}

        change_rows = conn.execute(
            "SELECT cr.record_id, r.source, r.original_no, r.name, cr.new_group_id "
            "FROM change_record cr JOIN record r ON r.id = cr.record_id "
            "WHERE cr.change_set_id = ? ORDER BY cr.seq",
            (nxt["id"],),
        ).fetchall()
        items = [
            (r["record_id"], r["source"], r["original_no"], r["name"], r["new_group_id"])
            for r in change_rows
        ]
        next_no = conn.execute(
            "SELECT COALESCE(MAX(batch_no), 0) + 1 AS n FROM feed_batch WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()["n"]
        batch_id = _insert_batch(
            conn, subscription_id, next_no, "delta", nxt["id"], items
        )
        conn.execute(
            "UPDATE subscription SET last_change_id = ? WHERE id = ?",
            (nxt["id"], subscription_id),
        )
        conn.execute("COMMIT")
        return {"status": "created", "batch": _load_batch(conn, batch_id)}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def acknowledge_batch(conn, subscription_id, batch_no):
    """确认指定批次。

    - 批次不存在：not_found。
    - 批次已确认：重复确认无副作用，返回 already_acked。
    - 仍有更早的未确认批次：拒绝（old ack 不能推进新批），返回 blocked。
    - 否则确认该批，返回 ok。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        sub = conn.execute(
            "SELECT id FROM subscription WHERE id = ?", (subscription_id,)
        ).fetchone()
        if sub is None:
            conn.execute("ROLLBACK")
            return {"status": "not_found"}
        batch = conn.execute(
            "SELECT id, acked_at FROM feed_batch "
            "WHERE subscription_id = ? AND batch_no = ?",
            (subscription_id, batch_no),
        ).fetchone()
        if batch is None:
            conn.execute("ROLLBACK")
            return {"status": "not_found"}
        if batch["acked_at"] is not None:
            conn.execute("COMMIT")
            return {"status": "already_acked"}
        earlier_pending = conn.execute(
            "SELECT 1 FROM feed_batch WHERE subscription_id = ? AND batch_no < ? "
            "AND acked_at IS NULL LIMIT 1",
            (subscription_id, batch_no),
        ).fetchone()
        if earlier_pending is not None:
            conn.execute("COMMIT")
            return {"status": "blocked"}
        conn.execute(
            "UPDATE feed_batch SET acked_at = datetime('now') WHERE id = ?",
            (batch["id"],),
        )
        conn.execute("COMMIT")
        return {"status": "ok"}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def list_batches(conn, subscription_id):
    rows = conn.execute(
        "SELECT id, batch_no, kind, change_set_id, item_count, acked_at, created_at "
        "FROM feed_batch WHERE subscription_id = ? ORDER BY batch_no",
        (subscription_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_batch(conn, batch_id):
    head = conn.execute(
        "SELECT b.id, b.subscription_id, b.batch_no, b.kind, b.change_set_id, "
        "       b.item_count, b.acked_at, b.created_at, cs.op_type, cs.op_ref "
        "FROM feed_batch b LEFT JOIN change_set cs ON cs.id = b.change_set_id "
        "WHERE b.id = ?",
        (batch_id,),
    ).fetchone()
    if head is None:
        return None
    item_rows = conn.execute(
        "SELECT seq, record_id, source, original_no, name, new_group_id "
        "FROM feed_batch_item WHERE batch_id = ? ORDER BY seq",
        (batch_id,),
    ).fetchall()
    batch = dict(head)
    batch["items"] = [dict(r) for r in item_rows]
    return batch


def get_batch_by_no(conn, subscription_id, batch_no):
    row = conn.execute(
        "SELECT id FROM feed_batch WHERE subscription_id = ? AND batch_no = ?",
        (subscription_id, batch_no),
    ).fetchone()
    if row is None:
        return None
    return _load_batch(conn, row["id"])
