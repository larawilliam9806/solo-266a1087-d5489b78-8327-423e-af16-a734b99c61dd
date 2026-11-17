"""逻辑层：传递分组、冲突检测、判断链、撤销重算。"""

import pytest

from app.logic import (
    DIFFERENT,
    SAME,
    Judgment,
    compute_groups,
    evaluate_proposal,
    group_of_record,
)

IDS = list(range(1, 9))


def j(jid, a, b, verdict):
    lo, hi = sorted((a, b))
    return Judgment(jid, lo, hi, verdict)


def test_groups_are_single_elements_without_judgments():
    groups = compute_groups([], IDS)
    assert len(groups) == len(IDS)
    assert all(len(g["members"]) == 1 for g in groups)
    assert groups[0]["group_id"] == "G00001"


def test_same_is_transitive():
    judgments = [j(1, 1, 2, SAME), j(2, 2, 3, SAME), j(3, 4, 5, SAME)]
    groups = compute_groups(judgments, IDS)
    mapping = group_of_record(groups)
    assert mapping[1] == mapping[2] == mapping[3]
    assert mapping[4] == mapping[5]
    assert mapping[1] != mapping[4]
    assert mapping[6] not in (mapping[1], mapping[4])


def test_different_rejected_when_same_path_exists():
    judgments = [j(1, 1, 2, SAME), j(2, 2, 3, SAME)]
    result = evaluate_proposal(judgments, IDS, 1, 3, DIFFERENT)
    assert result["status"] == "conflict"
    chain_ids = [step["judgment_id"] for step in result["chain"]]
    assert chain_ids == [1, 2]


def test_same_merge_rejected_when_crossing_different_edge():
    # 组 {1,2} 与 {3,4} 之间存在异物边 2-3；合并 1 与 4 必须拒绝
    judgments = [
        j(1, 1, 2, SAME),
        j(2, 3, 4, SAME),
        j(3, 2, 3, DIFFERENT),
    ]
    result = evaluate_proposal(judgments, IDS, 1, 4, SAME)
    assert result["status"] == "conflict"
    chain = result["chain"]
    verdicts = [step["verdict"] for step in chain]
    assert DIFFERENT in verdicts
    # 链条从 1 走到 4，且包含全部三条判断
    assert chain[0]["from_record"] == 1
    assert chain[-1]["to_record"] == 4
    assert {step["judgment_id"] for step in chain} == {1, 2, 3}


def test_different_edge_between_other_groups_is_fine():
    judgments = [
        j(1, 1, 2, SAME),
        j(2, 3, 4, SAME),
        j(3, 5, 6, SAME),
        j(4, 2, 5, DIFFERENT),  # 组{1,2} 与 组{5,6} 异物
    ]
    # 合并 3 与 4 已经是同组 -> redundant；合并 3 与 6 不触碰该异物边 -> ok
    assert evaluate_proposal(judgments, IDS, 3, 6, SAME)["status"] == "ok"
    assert evaluate_proposal(judgments, IDS, 1, 2, SAME)["status"] == "redundant"


def test_duplicate_opposite_verdict_is_conflict():
    judgments = [j(1, 1, 2, SAME)]
    result = evaluate_proposal(judgments, IDS, 2, 1, DIFFERENT)
    assert result["status"] == "conflict"
    assert result["chain"][0]["judgment_id"] == 1


def test_same_judgment_between_already_connected_is_redundant():
    judgments = [j(1, 1, 2, SAME), j(2, 2, 3, SAME)]
    assert evaluate_proposal(judgments, IDS, 1, 3, SAME)["status"] == "redundant"


def test_undo_recomputes_groups_without_splitting_still_connected():
    # 1-2-3 三角同物；撤销 1-2 后，1 与 2 仍经 1-3-2 连通
    judgments = [j(1, 1, 2, SAME), j(2, 2, 3, SAME), j(3, 1, 3, SAME)]
    remaining = [x for x in judgments if x.id != 1]
    mapping = group_of_record(compute_groups(remaining, IDS))
    assert mapping[1] == mapping[2] == mapping[3]
    # 再撤销到只剩一条边：未连接的记录拆开，仍连接的不拆
    remaining = [x for x in remaining if x.id != 2]
    mapping = group_of_record(compute_groups(remaining, IDS))
    assert mapping[1] == mapping[3]
    assert mapping[2] != mapping[1]


def test_self_and_unknown_record_rejected():
    assert evaluate_proposal([], IDS, 1, 1, SAME)["status"] == "conflict"
    assert evaluate_proposal([], IDS, 1, 99, SAME)["status"] == "conflict"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
