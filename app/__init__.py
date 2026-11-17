"""Flask 应用工厂与路由。"""

import csv
import io
import json
import os

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db as dbmod
from . import feed as feedmod
from .csvutil import CsvImportError, parse_catalog
from .logic import DIFFERENT, SAME, group_sizes

VERDICT_LABEL = {SAME: "同物", DIFFERENT: "异物"}


def create_app(db_path=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.getcwd(), "data", "catalog.db")
    )
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)

    def get_db():
        return dbmod.connect(app.config["DB_PATH"])

    def record_index(snapshot):
        return {r.id: r for r in snapshot["records"]}

    def enrich_chain(chain, records_by_id):
        """把逻辑层返回的 record id 翻译成页面可展示的完整信息。"""
        out = []
        for step in chain:
            fr = records_by_id[step["from_record"]]
            tr = records_by_id[step["to_record"]]
            out.append({
                "judgment_id": step["judgment_id"],
                "from_record": fr,
                "to_record": tr,
                "verdict": step["verdict"],
                "verdict_label": VERDICT_LABEL[step["verdict"]],
            })
        return out

    def enriched_judgments(snapshot):
        by_id = record_index(snapshot)
        items = []
        for j in snapshot["judgments"]:
            items.append({
                "id": j.id,
                "a": by_id[j.a],
                "b": by_id[j.b],
                "verdict": j.verdict,
                "verdict_label": VERDICT_LABEL[j.verdict],
            })
        items.sort(key=lambda x: x["id"])
        return items

    # ---------- 页面 ----------

    @app.route("/")
    def index():
        conn = get_db()
        try:
            snapshot = dbmod.get_snapshot(conn)
        finally:
            conn.close()
        return render_template(
            "index.html",
            records=snapshot["records"],
            judgments=enriched_judgments(snapshot),
            groups=snapshot["groups"],
            group_map=snapshot["group_map"],
            group_sizes=group_sizes(snapshot["groups"]),
            multi_group_count=sum(1 for g in snapshot["groups"] if len(g["members"]) > 1),
            verdict_label=VERDICT_LABEL,
        )

    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("csv_file")
        if file is None or not file.filename:
            flash(("error", "请先选择要上传的 CSV 文件"), "error")
            return redirect(url_for("index"))
        raw = file.read()
        if len(raw) > 10 * 1024 * 1024:
            flash(("error", "文件超过 10MB 限制"), "error")
            return redirect(url_for("index"))
        try:
            rows = parse_catalog(raw)
        except CsvImportError as exc:
            flash(("error", "导入失败：%s" % exc), "error")
            return redirect(url_for("index"))
        conn = get_db()
        try:
            inserted, skipped = dbmod.insert_records(conn, rows)
        finally:
            conn.close()
        msg = "导入完成：新增 %d 条，跳过 %d 条（来源+原编号已存在的记录不重复写入，原记录不改写）" % (
            inserted, skipped)
        flash(("ok", msg), "ok")
        return redirect(url_for("index"))

    @app.route("/judge", methods=["POST"])
    def judge():
        try:
            a = int(request.form["record_a"])
            b = int(request.form["record_b"])
            verdict = request.form["verdict"]
        except (KeyError, TypeError, ValueError):
            flash(("error", "提交参数不完整或不合法"), "error")
            return redirect(url_for("index"))
        if verdict not in (SAME, DIFFERENT):
            flash(("error", "判断类型必须是 same 或 different"), "error")
            return redirect(url_for("index"))

        conn = get_db()
        try:
            snapshot = dbmod.get_snapshot(conn)
            valid_ids = {r.id for r in snapshot["records"]}
            if a not in valid_ids or b not in valid_ids:
                flash(("error", "所选记录不存在，请刷新页面后重试"), "error")
                return redirect(url_for("index"))
            result = dbmod.add_judgment(conn, a, b, verdict)
            by_id = record_index(snapshot)
            if result["status"] == "ok":
                flash((
                    "ok",
                    "已记录判断 #%d：%s 与 %s 为【%s】" % (
                        result["judgment_id"],
                        _label(by_id[a]), _label(by_id[b]),
                        VERDICT_LABEL[verdict],
                    ),
                ), "ok")
            elif result["status"] == "redundant":
                flash(("warn", result["reason"]), "warn")
            else:
                chain = enrich_chain(result.get("chain", []), by_id)
                flash((
                    "conflict",
                    "操作被拒绝，原状未改动：%s" % result["reason"],
                    chain,
                ), "conflict")
        finally:
            conn.close()
        return redirect(url_for("index"))

    @app.route("/judgment/<int:judgment_id>/undo", methods=["POST"])
    def undo(judgment_id):
        conn = get_db()
        try:
            target = dbmod.get_judgment(conn, judgment_id)
            if target is None:
                flash(("error", "判断 #%d 不存在或已被撤销" % judgment_id), "error")
            else:
                ok = dbmod.delete_judgment(conn, judgment_id)
                if ok:
                    flash(("ok", "已撤销判断 #%s，分组已按剩余判断重新形成" % judgment_id), "ok")
        finally:
            conn.close()
        return redirect(url_for("index"))

    @app.route("/download/mapping.csv")
    def download_mapping():
        conn = get_db()
        try:
            # 快照在单个读事务内生成，保证对应某一完整判断状态
            snapshot = dbmod.get_snapshot(conn)
        finally:
            conn.close()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["去重组ID", "记录ID", "来源", "原编号", "名称", "组内记录数"])
        sizes = {g["group_id"]: len(g["members"]) for g in snapshot["groups"]}
        by_id = record_index(snapshot)
        for g in snapshot["groups"]:
            for rid in g["members"]:
                r = by_id[rid]
                writer.writerow([
                    g["group_id"], r.id, r.source, r.original_no, r.name,
                    sizes[g["group_id"]],
                ])
        data = buf.getvalue().encode("utf-8-sig")
        return Response(
            data,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=dereplication_mapping.csv"
            },
        )

    # ---------- 增量供数：页面 ----------

    @app.route("/feed")
    def feed_page():
        conn = get_db()
        try:
            subscriptions = dbmod.list_subscriptions(conn)
            detail = None
            sub_id = request.args.get("subscription_id", type=int)
            if sub_id is not None:
                sub = dbmod.get_subscription(conn, sub_id)
                if sub is None:
                    abort(404)
                detail = {
                    "subscription": sub,
                    "batches": dbmod.list_batches(conn, sub_id),
                }
        finally:
            conn.close()
        return render_template(
            "feed.html", subscriptions=subscriptions, detail=detail
        )

    # ---------- 增量供数：JSON API ----------

    @app.route("/api/subscriptions", methods=["POST"])
    def api_create_subscription():
        payload = request.get_json(silent=True) or request.form
        name = (payload.get("name") or "").strip() if payload else ""
        if not name:
            return jsonify({"error": "订阅名称不能为空"}), 400
        if len(name) > 100:
            return jsonify({"error": "订阅名称不能超过 100 个字符"}), 400
        conn = get_db()
        try:
            result = dbmod.create_subscription(conn, name)
        finally:
            conn.close()
        return jsonify(result), 201

    @app.route("/api/subscriptions/<int:subscription_id>/claim", methods=["POST"])
    def api_claim(subscription_id):
        conn = get_db()
        try:
            result = dbmod.claim_next_batch(conn, subscription_id)
        finally:
            conn.close()
        if result["status"] == "not_found":
            return jsonify({"error": "订阅不存在"}), 404
        if result["status"] == "up_to_date":
            return jsonify({"status": "up_to_date", "message": "暂无新变化"})
        return jsonify({
            "status": result["status"],
            "batch": feedmod.batch_payload(result["batch"]),
        })

    @app.route("/api/subscriptions/<int:subscription_id>/batches/<int:batch_no>")
    def api_get_batch(subscription_id, batch_no):
        conn = get_db()
        try:
            batch = dbmod.get_batch_by_no(conn, subscription_id, batch_no)
            if batch is None:
                return jsonify({"error": "批次不存在"}), 404
        finally:
            conn.close()
        return jsonify(feedmod.batch_payload(batch))

    @app.route("/api/subscriptions/<int:subscription_id>/ack/<int:batch_no>",
               methods=["POST"])
    def api_ack(subscription_id, batch_no):
        conn = get_db()
        try:
            result = dbmod.acknowledge_batch(conn, subscription_id, batch_no)
        finally:
            conn.close()
        status_code = 200
        if result["status"] == "not_found":
            status_code = 404
        return jsonify(result), status_code

    @app.route("/api/subscriptions/<int:subscription_id>/batches/<int:batch_no>/download")
    def api_download_batch(subscription_id, batch_no):
        conn = get_db()
        try:
            batch = dbmod.get_batch_by_no(conn, subscription_id, batch_no)
            if batch is None:
                abort(404)
            payload = feedmod.batch_payload(batch)
        finally:
            conn.close()
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        filename = "subscription_%d_batch_%03d.json" % (subscription_id, batch_no)
        return Response(
            data,
            mimetype="application/json; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=%s" % filename},
        )

    return app


def _label(record):
    return "%s/%s（%s）" % (record.source, record.original_no, record.name)
