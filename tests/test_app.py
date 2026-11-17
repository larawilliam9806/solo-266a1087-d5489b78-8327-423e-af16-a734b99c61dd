"""数据库与 Flask 接口测试：不可变记录、冲突拒绝、撤销、并发一致性、映射快照。"""

import csv
import io
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import create_app
from app import db as dbmod
from app.logic import DIFFERENT, SAME


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


# ---------- 记录不可变 / 唯一性 ----------

def test_duplicate_combo_skipped_not_overwritten(app):
    _seed(app, 2)
    conn = _conn(app)
    inserted, skipped = dbmod.insert_records(
        conn, [("S01", "NO-001", "被篡改的名称"), ("S03", "NO-003", "文物3")]
    )
    conn.close()
    assert (inserted, skipped) == (1, 1)
    conn = _conn(app)
    snap = dbmod.get_snapshot(conn)
    conn.close()
    by_key = {(r.source, r.original_no): r.name for r in snap["records"]}
    assert by_key[("S01", "NO-001")] == "文物1"  # 原记录未被改写


def test_upload_endpoint_rejects_in_file_duplicates(client):
    data = {
        "csv_file": (io.BytesIO("来源,原编号,名称\nA,1,x\nA,1,y\n".encode()), "c.csv")
    }
    resp = client.post("/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=True)
    assert resp.status_code == 200
    assert "重复".encode() in resp.data
    # 整批拒绝：库里没有记录
    snap = dbmod.get_snapshot(_conn(client.application))
    assert snap["records"] == []


def test_full_upload_then_mapping_download(client):
    raw = open(os.path.join(os.path.dirname(__file__), "..", "examples",
                            "sample_catalog.csv"), "rb").read()
    resp = client.post(
        "/upload",
        data={"csv_file": (io.BytesIO(raw), "sample.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    resp = client.get("/download/mapping.csv")
    assert resp.status_code == 200
    text = resp.data.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 10
    assert set(rows[0].keys()) == {"去重组ID", "记录ID", "来源", "原编号", "名称", "组内记录数"}
    # 初始每个组只有自己
    assert all(r["组内记录数"] == "1" for r in rows)


# ---------- 判断 / 冲突链 / 撤销 ----------

def test_judge_conflict_chain_and_undo(app, client):
    _seed(app, 5)
    # 1 same 2, 2 same 3
    for a, b in ((1, 2), (2, 3)):
        resp = client.post("/judge", data={"record_a": a, "record_b": b,
                                           "verdict": SAME})
        assert resp.status_code == 302
    # 1 different 3 -> 冲突，带判断链
    resp = client.post("/judge", data={"record_a": 1, "record_b": 3,
                                       "verdict": DIFFERENT},
                       follow_redirects=True)
    assert "操作被拒绝".encode() in resp.data
    assert "判断链".encode() in resp.data
    conn = _conn(app)
    assert len(dbmod.get_snapshot(conn)["judgments"]) == 2  # 原状未变
    # 撤销 #1 后：2-3 仍同组，1 单独
    client.post("/judgment/1/undo")
    snap = dbmod.get_snapshot(conn)
    gmap = snap["group_map"]
    assert gmap[2] == gmap[3]
    assert gmap[1] != gmap[2]
    assert len(snap["judgments"]) == 1
    conn.close()


def test_merge_conflict_with_crossing_different_edge(app):
    _seed(app, 6)
    conn = _conn(app)
    dbmod.add_judgment(conn, 1, 2, SAME)
    dbmod.add_judgment(conn, 3, 4, SAME)
    result = dbmod.add_judgment(conn, 2, 3, DIFFERENT)
    assert result["status"] == "ok"
    # 试图合并两大组
    result = dbmod.add_judgment(conn, 1, 4, SAME)
    assert result["status"] == "conflict"
    assert {s["verdict"] for s in result["chain"]} == {SAME, DIFFERENT}
    assert len(dbmod.get_snapshot(conn)["judgments"]) == 3  # 没有写入第四条
    conn.close()


def test_undo_nonexistent_keeps_state(app, client):
    _seed(app, 2)
    conn = _conn(app)
    assert dbmod.delete_judgment(conn, 999) is False
    conn.close()


# ---------- 并发：两个页面同时提交矛盾判断 ----------

def test_concurrent_conflicting_judgments_only_one_wins(app):
    _seed(app, 4)
    barrier = threading.Barrier(2)
    db_path = app.config["DB_PATH"]

    def worker(verdict):
        conn = dbmod.connect(db_path)
        try:
            barrier.wait()  # 尽量同时开始
            return dbmod.add_judgment(conn, 1, 2, verdict)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(worker, SAME), pool.submit(worker, DIFFERENT)]
        results = [f.result() for f in futs]

    statuses = sorted(r["status"] for r in results)
    assert statuses == ["conflict", "ok"]  # 一方成功，一方被拒
    conn = _conn(app)
    judgments = dbmod.get_snapshot(conn)["judgments"]
    conn.close()
    assert len(judgments) == 1  # 只留下一条，绝不矛盾


def test_concurrent_same_merges_never_violate_different_constraint(app):
    """两个线程分别建立 1-2、3-4 同物与 1-3 异物，再并发尝试 2-4 同物合并等
    矛盾组合，最终状态必须始终满足：异物记录不同组。"""
    _seed(app, 4)
    db_path = app.config["DB_PATH"]
    conn = dbmod.connect(db_path)
    dbmod.add_judgment(conn, 1, 2, SAME)
    dbmod.add_judgment(conn, 3, 4, SAME)
    dbmod.add_judgment(conn, 1, 3, DIFFERENT)
    conn.close()

    barrier = threading.Barrier(2)

    def worker(payload):
        c = dbmod.connect(db_path)
        try:
            barrier.wait()
            return dbmod.add_judgment(c, *payload)
        finally:
            c.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(worker, (2, 4, SAME))       # 想合并两个异物组
        f2 = pool.submit(worker, (1, 4, DIFFERENT))  # 与已有关系不矛盾的异物
        r1, r2 = f1.result(), f2.result()

    # 合并必须失败；另一条可能成功也可能因锁顺序失败，但库里状态必须自洽
    assert r1["status"] == "conflict"
    conn = dbmod.connect(db_path)
    snap = dbmod.get_snapshot(conn)
    conn.close()
    gmap = snap["group_map"]
    assert gmap[1] != gmap[3]  # 异物始终不同组
    for j in snap["judgments"]:
        if j.verdict == DIFFERENT:
            assert gmap[j.a] != gmap[j.b]


def test_mapping_is_consistent_snapshot_under_concurrent_writes(app):
    _seed(app, 6)
    db_path = app.config["DB_PATH"]
    stop = threading.Event()
    errors = []

    def writer():
        c = dbmod.connect(db_path)
        try:
            for i in range(1, 6):
                try:
                    dbmod.add_judgment(c, i, i + 1, SAME)
                except Exception as exc:  # pragma: no cover - 并发下不应发生
                    errors.append(exc)
        finally:
            c.close()
            stop.set()

    def reader():
        c = dbmod.connect(db_path)
        try:
            while not stop.is_set():
                snap = dbmod.get_snapshot(c)
                # 快照内不变量：每个 record 恰好出现一次；同物必同组
                seen = set()
                for g in snap["groups"]:
                    for rid in g["members"]:
                        assert rid not in seen
                        seen.add(rid)
                for j in snap["judgments"]:
                    if j.verdict == SAME:
                        assert snap["group_map"][j.a] == snap["group_map"][j.b]
        finally:
            c.close()

    t = threading.Thread(target=reader)
    t.start()
    writer()
    t.join(timeout=10)
    assert not t.is_alive() and not errors


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
