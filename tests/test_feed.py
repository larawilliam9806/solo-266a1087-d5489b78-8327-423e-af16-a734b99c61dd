"""增量供数测试：订阅、首批全量、按序批次、确认幂等、并发与重启不丢变化。"""

import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import create_app
from app import db as dbmod


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db_path=path)
    app.config.update(TESTING=True)
    yield app
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


@pytest.fixture()
def client(app):
    return app.test_client()


def _conn(app):
    return dbmod.connect(app.config["DB_PATH"])


def _seed(app, n):
    conn = _conn(app)
    rows = [("S%02d" % i, "NO-%03d" % i, "文物%d" % i) for i in range(1, n + 1)]
    inserted, skipped = dbmod.insert_records(conn, rows)
    conn.close()
    assert inserted == n and skipped == 0
    return rows


def _change_sets(app):
    conn = _conn(app)
    rows = conn.execute("SELECT id, op_type, op_ref, record_count FROM change_set ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------- 业务操作与变化集的事务性 ----------

def test_upload_records_change_set(app):
    _seed(app, 3)
    sets = _change_sets(app)
    assert len(sets) == 1 and sets[0]["op_type"] == "upload"
    assert sets[0]["record_count"] == 3
    conn = _conn(app)
    rows = conn.execute(
        "SELECT record_id, new_group_id FROM change_record ORDER BY record_id"
    ).fetchall()
    conn.close()
    # 新记录各自成单元素组 G00001/G00002/G00003
    assert [r["record_id"] for r in rows] == [1, 2, 3]
    assert [r["new_group_id"] for r in rows] == ["G00001", "G00002", "G00003"]


def test_redundant_judgment_leaves_no_change_set(app):
    _seed(app, 3)
    conn = _conn(app)
    dbmod.add_judgment(conn, 1, 2, "same")
    # 已同组的冗余判断：不写入 judgment，也不产生变化集
    result = dbmod.add_judgment(conn, 1, 2, "same")
    conn.close()
    assert result["status"] == "redundant"
    sets = _change_sets(app)
    assert [s["op_type"] for s in sets] == ["upload", "judge"]


def test_conflicted_judgment_leaves_no_change_set(app):
    _seed(app, 3)
    conn = _conn(app)
    dbmod.add_judgment(conn, 1, 2, "same")
    result = dbmod.add_judgment(conn, 1, 2, "different")  # 相反结论 -> 冲突
    conn.close()
    assert result["status"] == "conflict"
    # 失败的业务操作没有留下任何供数记录
    sets = _change_sets(app)
    assert [s["op_type"] for s in sets] == ["upload", "judge"]
    snap = dbmod.get_snapshot(_conn(app))
    assert len(snap["judgments"]) == 1


def test_judge_and_undo_change_sets_carry_new_group(app):
    _seed(app, 4)
    conn = _conn(app)
    dbmod.add_judgment(conn, 1, 2, "same")
    dbmod.add_judgment(conn, 2, 3, "same")
    # 撤销判断 #1（1-2）：2、3 仍同组，1 回到单元素组
    dbmod.delete_judgment(conn, 1)
    conn.close()
    sets = _change_sets(app)
    assert [s["op_type"] for s in sets] == ["upload", "judge", "judge", "undo"]
    conn = _conn(app)
    undo_set = sets[-1]
    rows = conn.execute(
        "SELECT record_id, new_group_id FROM change_record WHERE change_set_id = ? ORDER BY record_id",
        (undo_set["id"],),
    ).fetchall()
    conn.close()
    # 只有归属真正变化的记录进入变化集：
    # 撤销后 {2,3} 以最小成员 2 命名为 G00002；记录 1 留在 G00001（组 id 不变）
    assert [r["record_id"] for r in rows] == [2, 3]
    assert {r["new_group_id"] for r in rows} == {"G00002"}


def test_different_judgment_without_group_change_writes_nothing(app):
    _seed(app, 2)
    conn = _conn(app)
    result = dbmod.add_judgment(conn, 1, 2, "different")
    conn.close()
    assert result["status"] == "ok"
    # 异物判断不改变任何归属 -> 无变化集（除导入外）
    assert [s["op_type"] for s in _change_sets(app)] == ["upload"]


# ---------- 订阅与首批全量 ----------

def test_first_batch_is_full_snapshot(app):
    _seed(app, 4)
    conn = _conn(app)
    dbmod.add_judgment(conn, 1, 2, "same")
    sub = dbmod.create_subscription(conn, "检索库A")
    conn.close()
    assert sub["item_count"] == 4
    assert sub["last_change_id"] == 2  # upload + 同物 judge 两个变化集
    conn = _conn(app)
    batch = dbmod.get_batch_by_no(conn, sub["id"], 1)
    conn.close()
    assert batch["kind"] == "snapshot"
    assert len(batch["items"]) == 4
    payload_item = batch["items"][0]
    assert set(payload_item.keys()) >= {
        "record_id", "source", "original_no", "name", "new_group_id"
    }
    gids = {it["record_id"]: it["new_group_id"] for it in batch["items"]}
    assert gids[1] == gids[2]           # 同物同组
    assert gids[1] != gids[3]


def test_subscription_created_on_empty_db_has_empty_snapshot(app):
    conn = _conn(app)
    sub = dbmod.create_subscription(conn, "空库订阅")
    conn.close()
    assert sub["item_count"] == 0
    conn = _conn(app)
    batch = dbmod.get_batch_by_no(conn, sub["id"], 1)
    conn.close()
    assert batch["kind"] == "snapshot" and batch["items"] == []


# ---------- 领取：按序、整批、未确认不变 ----------

def test_claim_delivers_whole_change_sets_in_order(app):
    _seed(app, 4)
    conn = _conn(app)
    sub = dbmod.create_subscription(conn, "A")
    dbmod.add_judgment(conn, 1, 2, "same")     # 变化集 #2：记录 2 归属变化
    dbmod.add_judgment(conn, 3, 4, "same")     # 变化集 #3：记录 4 归属变化
    conn.close()

    conn = _conn(app)
    # 首批快照未确认前，领取始终返回首批
    r0 = dbmod.claim_next_batch(conn, sub["id"])
    assert r0["status"] == "pending" and r0["batch"]["batch_no"] == 1
    dbmod.acknowledge_batch(conn, sub["id"], 1)
    r1 = dbmod.claim_next_batch(conn, sub["id"])
    r2 = dbmod.claim_next_batch(conn, sub["id"])  # 未确认 -> 同一批
    conn.close()
    assert r1["status"] == "created"
    assert r2["status"] == "pending"
    b1, b1_again = r1["batch"], r2["batch"]
    assert b1["id"] == b1_again["id"] and b1["batch_no"] == b1_again["batch_no"] == 2
    # 1-2 同物：记录 1 留在 G00001，只有记录 2 的归属 G00002 -> G00001
    assert [it["record_id"] for it in b1["items"]] == [2]
    assert b1["items"][0]["new_group_id"] == "G00001"
    assert b1["change_set_id"] == 2

    # 确认批次 2 后才能领到下一个变化集（整批，不与 #4 合并）
    conn = _conn(app)
    assert dbmod.acknowledge_batch(conn, sub["id"], 2)["status"] == "ok"
    r3 = dbmod.claim_next_batch(conn, sub["id"])
    conn.close()
    assert r3["status"] == "created"
    b2 = r3["batch"]
    assert b2["batch_no"] == 3 and b2["change_set_id"] == 3
    # 同物并入 {3} 时只有记录 4 的组 id 改变（G00004 -> G00003）
    assert [it["record_id"] for it in b2["items"]] == [4]

    conn = _conn(app)
    # 未确认时再领仍是同一批；确认后才到 up_to_date
    r4 = dbmod.claim_next_batch(conn, sub["id"])
    assert r4["status"] == "pending" and r4["batch"]["id"] == b2["id"]
    assert dbmod.acknowledge_batch(conn, sub["id"], 3)["status"] == "ok"
    r5 = dbmod.claim_next_batch(conn, sub["id"])
    conn.close()
    assert r5["status"] == "up_to_date"


def test_unacked_batch_content_is_immutable(app):
    _seed(app, 3)
    conn = _conn(app)
    sub = dbmod.create_subscription(conn, "A")
    dbmod.acknowledge_batch(conn, sub["id"], 1)
    dbmod.add_judgment(conn, 1, 2, "same")
    r = dbmod.claim_next_batch(conn, sub["id"])
    batch_before = json.dumps(r["batch"], sort_keys=True, default=str)
    # 新变化发生：不能影响未确认批次，只能留到后续批次
    dbmod.add_judgment(conn, 2, 3, "same")
    conn.close()
    conn = _conn(app)
    r2 = dbmod.claim_next_batch(conn, sub["id"])
    conn.close()
    assert r2["status"] == "pending"
    assert json.dumps(r2["batch"], sort_keys=True, default=str) == batch_before


def test_batch_no_and_content_persist_after_reopen(app):
    _seed(app, 3)
    db_path = app.config["DB_PATH"]
    conn = dbmod.connect(db_path)
    sub = dbmod.create_subscription(conn, "A")
    dbmod.acknowledge_batch(conn, sub["id"], 1)
    dbmod.add_judgment(conn, 1, 2, "same")
    r = dbmod.claim_next_batch(conn, sub["id"])
    batch_id, items = r["batch"]["id"], [it["record_id"] for it in r["batch"]["items"]]
    conn.close()

    # 模拟服务重启：重新打开数据库
    conn = dbmod.connect(db_path)
    r2 = dbmod.claim_next_batch(conn, sub["id"])
    conn.close()
    assert r2["status"] == "pending"
    assert r2["batch"]["id"] == batch_id
    assert [it["record_id"] for it in r2["batch"]["items"]] == items


# ---------- 确认语义 ----------

def test_ack_rules(app):
    _seed(app, 4)
    conn = _conn(app)
    sub = dbmod.create_subscription(conn, "A")
    dbmod.add_judgment(conn, 1, 2, "same")
    dbmod.add_judgment(conn, 3, 4, "same")
    conn.close()

    conn = _conn(app)
    dbmod.acknowledge_batch(conn, sub["id"], 1)       # 确认首批快照
    dbmod.claim_next_batch(conn, sub["id"])           # 批次 2（待确认）
    dbmod.acknowledge_batch(conn, sub["id"], 2)
    dbmod.claim_next_batch(conn, sub["id"])           # 批次 3（待确认）
    # 重复确认无副作用
    assert dbmod.acknowledge_batch(conn, sub["id"], 2)["status"] == "already_acked"
    # 不存在的批次
    assert dbmod.acknowledge_batch(conn, sub["id"], 99)["status"] == "not_found"
    # 旧批次（#1）重复确认也无副作用，不能推进批次 3
    assert dbmod.acknowledge_batch(conn, sub["id"], 1)["status"] == "already_acked"
    assert dbmod.acknowledge_batch(conn, sub["id"], 3)["status"] == "ok"
    assert dbmod.acknowledge_batch(conn, sub["id"], 3)["status"] == "already_acked"
    conn.close()


def test_ack_blocked_when_earlier_batch_still_pending(app):
    """防御分支：存在更早未确认批次时，确认更晚批次被拒绝。"""
    _seed(app, 2)
    conn = _conn(app)
    sub = dbmod.create_subscription(conn, "A")  # 批次 1 待确认
    # 正常流程下更晚批次不会在更早批次未确认时生成；这里直接构造该状态
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO feed_batch(subscription_id, batch_no, kind, change_set_id, item_count) "
        "VALUES (?, 3, 'delta', NULL, 0)",
        (sub["id"],),
    )
    conn.execute("COMMIT")
    assert dbmod.acknowledge_batch(conn, sub["id"], 3)["status"] == "blocked"
    conn.close()


def test_old_ack_cannot_advance_new_batch(app):
    _seed(app, 3)
    conn = _conn(app)
    sub = dbmod.create_subscription(conn, "A")
    dbmod.add_judgment(conn, 1, 2, "same")
    dbmod.acknowledge_batch(conn, sub["id"], 1)   # 首批先确认
    dbmod.claim_next_batch(conn, sub["id"])       # 批次 2 待确认
    # 重复确认旧批次 #1 不能推进批次 2
    again = dbmod.acknowledge_batch(conn, sub["id"], 1)
    conn.close()
    assert again["status"] == "already_acked"
    conn = _conn(app)
    pending = conn.execute(
        "SELECT batch_no FROM feed_batch WHERE subscription_id = ? AND acked_at IS NULL ORDER BY batch_no",
        (sub["id"],),
    ).fetchall()
    conn.close()
    assert [r["batch_no"] for r in pending] == [2]


# ---------- 并发：创建订阅不丢并发修改 ----------

def test_concurrent_create_subscription_never_loses_changes(app):
    _seed(app, 6)
    db_path = app.config["DB_PATH"]
    barrier = threading.Barrier(3)
    errors = []

    def make_subscription(idx):
        conn = dbmod.connect(db_path)
        try:
            barrier.wait()
            dbmod.create_subscription(conn, "并发订阅%d" % idx)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            conn.close()

    def make_judgments():
        conn = dbmod.connect(db_path)
        try:
            barrier.wait()
            for i in range(1, 6):
                try:
                    dbmod.add_judgment(conn, i, i + 1, "same")
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(make_subscription, i) for i in (1, 2)]
        futs.append(pool.submit(make_judgments))
        [f.result() for f in futs]
    assert not errors

    conn = dbmod.connect(db_path)
    total_sets = conn.execute("SELECT COUNT(*) AS c FROM change_set").fetchone()["c"]
    subs = dbmod.list_subscriptions(conn)
    assert len(subs) == 2
    for s in subs:
        # 不变量：从游标位置逐批领取并确认，最终覆盖全部变化集
        cur = s["last_change_id"]
        while True:
            r = dbmod.claim_next_batch(conn, s["id"])
            if r["status"] == "up_to_date":
                break
            assert r["status"] in ("created", "pending")
            if r["status"] == "created":
                cur = r["batch"]["change_set_id"]
            dbmod.acknowledge_batch(conn, s["id"], r["batch"]["batch_no"])
        assert cur == total_sets
    conn.close()


def test_concurrent_claims_create_each_batch_once(app):
    _seed(app, 8)
    db_path = app.config["DB_PATH"]
    conn = dbmod.connect(db_path)
    sub = dbmod.create_subscription(conn, "A")
    for i in range(1, 8):
        dbmod.add_judgment(conn, i, i + 1, "same")
    conn.close()

    barrier = threading.Barrier(4)

    def claimer():
        c = dbmod.connect(db_path)
        try:
            barrier.wait()
            for _ in range(20):
                r = dbmod.claim_next_batch(c, sub["id"])
                if r["status"] == "up_to_date":
                    break
                # 固化后立即确认（含首批快照），后续领取才能继续推进
                dbmod.acknowledge_batch(c, sub["id"], r["batch"]["batch_no"])
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        [pool.submit(claimer) for _ in range(4)]

    conn = dbmod.connect(db_path)
    batches = conn.execute(
        "SELECT batch_no, change_set_id FROM feed_batch WHERE subscription_id = ? ORDER BY batch_no",
        (sub["id"],),
    ).fetchall()
    conn.close()
    # 7 个同物 judge 变化集（#2..#8）-> 批次 2..8，每个变化集恰好一个批次，无重复无跳号
    assert [r["batch_no"] for r in batches] == list(range(1, 9))
    assert [r["change_set_id"] for r in batches[1:]] == list(range(2, 9))


def test_concurrent_ack_is_idempotent_and_ordered(app):
    _seed(app, 4)
    db_path = app.config["DB_PATH"]
    conn = dbmod.connect(db_path)
    sub = dbmod.create_subscription(conn, "A")
    dbmod.add_judgment(conn, 1, 2, "same")
    dbmod.add_judgment(conn, 3, 4, "same")
    dbmod.acknowledge_batch(conn, sub["id"], 1)
    dbmod.claim_next_batch(conn, sub["id"])
    dbmod.acknowledge_batch(conn, sub["id"], 2)
    dbmod.claim_next_batch(conn, sub["id"])  # 批次 3
    conn.close()

    barrier = threading.Barrier(2)

    def ack(batch_no):
        c = dbmod.connect(db_path)
        try:
            barrier.wait()
            return [
                dbmod.acknowledge_batch(c, sub["id"], batch_no)["status"]
                for _ in range(3)
            ]
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(ack, 3)
        f2 = pool.submit(ack, 3)
        r1, r2 = f1.result(), f2.result()
    # 恰好一次 ok，其余 already_acked
    statuses = r1 + r2
    assert statuses.count("ok") == 1
    assert set(statuses) <= {"ok", "already_acked"}


# ---------- HTTP / JSON 接口 ----------

def test_api_full_flow(client, app):
    _seed(app, 3)
    # 创建订阅
    resp = client.post("/api/subscriptions", json={"name": "接口订阅"})
    assert resp.status_code == 201
    sub = resp.get_json()
    assert sub["first_batch_no"] == 1
    # 名称校验
    assert client.post("/api/subscriptions", json={"name": "  "}).status_code == 400

    # 首批 JSON 字段完整
    resp = client.get(
        "/api/subscriptions/%d/batches/1" % sub["id"]
    )
    payload = resp.get_json()
    assert payload["kind"] == "snapshot" and payload["item_count"] == 3
    assert set(payload["records"][0].keys()) == {
        "record_id", "source", "original_no", "name", "new_group_id"
    }

    # 业务变化后领取（先确认首批快照）
    client.post("/api/subscriptions/%d/ack/1" % sub["id"])
    client.post("/judge", data={"record_a": 1, "record_b": 2, "verdict": "same"})
    resp = client.post("/api/subscriptions/%d/claim" % sub["id"])
    claimed = resp.get_json()
    assert claimed["status"] == "created"
    assert claimed["batch"]["batch_no"] == 2
    # 1-2 同物：记录 1 组 id 不变，仅记录 2 由 G00002 变为 G00001
    assert {r["record_id"] for r in claimed["batch"]["records"]} == {2}
    assert claimed["batch"]["records"][0]["new_group_id"] == "G00001"
    # 再领一次：pending 同批
    again = client.post("/api/subscriptions/%d/claim" % sub["id"]).get_json()
    assert again["status"] == "pending"
    assert again["batch"]["batch_id"] == claimed["batch"]["batch_id"]

    # 确认流程
    ack_resp = client.post(
        "/api/subscriptions/%d/ack/2" % sub["id"]
    ).get_json()
    assert ack_resp["status"] == "ok"
    dup = client.post("/api/subscriptions/%d/ack/2" % sub["id"]).get_json()
    assert dup["status"] == "already_acked"
    # 无新变化
    assert client.post(
        "/api/subscriptions/%d/claim" % sub["id"]
    ).get_json()["status"] == "up_to_date"
    # 不存在的订阅 / 批次
    assert client.post("/api/subscriptions/999/claim").status_code == 404
    assert client.get("/api/subscriptions/%d/batches/99" % sub["id"]).status_code == 404


def test_batch_json_download(client, app):
    _seed(app, 2)
    sub = client.post("/api/subscriptions", json={"name": "下载订阅"}).get_json()
    resp = client.get(
        "/api/subscriptions/%d/batches/1/download" % sub["id"]
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    assert "attachment" in resp.headers["Content-Disposition"]
    data = json.loads(resp.data.decode("utf-8"))
    assert data["batch_no"] == 1 and len(data["records"]) == 2


def test_feed_page_renders(client, app):
    _seed(app, 2)
    resp = client.get("/feed")
    assert resp.status_code == 200 and "增量供数".encode() in resp.data
    sub = client.post("/api/subscriptions", json={"name": "页面订阅"}).get_json()
    resp = client.get("/feed?subscription_id=%d" % sub["id"])
    assert resp.status_code == 200
    assert "页面订阅".encode() in resp.data and "首批全量".encode() in resp.data


def test_original_mapping_download_still_works(client, app):
    _seed(app, 3)
    client.post("/judge", data={"record_a": 1, "record_b": 2, "verdict": "same"})
    resp = client.get("/download/mapping.csv")
    assert resp.status_code == 200 and b"dereplication_mapping.csv" in resp.headers["Content-Disposition"].encode()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
