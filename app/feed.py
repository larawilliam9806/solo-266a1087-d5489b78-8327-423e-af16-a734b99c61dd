"""增量供数的批次序列化（纯函数，不依赖数据库连接）。

下游检索库按 JSON 中的记录标识覆盖索引：每条变化含
记录ID、来源、原编号、名称、新组ID。
"""

OP_LABELS = {
    "upload": "目录导入",
    "judge": "提交判断",
    "undo": "撤销判断",
    "snapshot": "首批全量",
}


def item_payload(item):
    """单条变化 -> 下游覆盖索引所需的 JSON 对象。"""
    return {
        "record_id": item["record_id"],
        "source": item["source"],
        "original_no": item["original_no"],
        "name": item["name"],
        "new_group_id": item["new_group_id"],
    }


def batch_payload(batch):
    """整批 -> 可直接返回给下游的 JSON 结构。

    一次操作影响的记录整批交付：records 即该变化集（或首批快照）的全部条目，
    批次标识（subscription_id + batch_no）与内容在生成后保持不变。
    """
    return {
        "subscription_id": batch["subscription_id"],
        "batch_id": batch["id"],
        "batch_no": batch["batch_no"],
        "kind": batch["kind"],
        "change_set_id": batch["change_set_id"],
        "op_type": batch.get("op_type"),
        "op_type_label": OP_LABELS.get(
            batch.get("op_type") or batch["kind"],
            batch["kind"],
        ),
        "op_ref": batch.get("op_ref"),
        "item_count": batch["item_count"],
        "acked": batch["acked_at"] is not None,
        "created_at": batch["created_at"],
        "records": [item_payload(it) for it in batch["items"]],
    }
