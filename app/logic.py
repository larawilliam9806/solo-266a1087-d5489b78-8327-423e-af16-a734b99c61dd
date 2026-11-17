"""同物异号核对核心逻辑（纯函数，不依赖数据库，便于单元测试）。

术语：
- record   原始目录记录，不可变
- judgment 两条记录之间的人工判断，verdict 为 same（同物）或 different（异物）
- group    仅由 same 判断经传递闭包派生出的去重组，不持久化
"""

from collections import deque
from dataclasses import dataclass

SAME = "same"
DIFFERENT = "different"


@dataclass(frozen=True)
class Record:
    id: int
    source: str
    original_no: str
    name: str


@dataclass(frozen=True)
class Judgment:
    id: int
    a: int          # 约定 a < b（记录 id）
    b: int
    verdict: str    # SAME / DIFFERENT


class DSU:
    """并查集，用于由同物边计算传递分组。"""

    def __init__(self, nodes):
        self.parent = {n: n for n in nodes}
        self.rank = {n: 0 for n in nodes}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1


def build_adjacency(judgments):
    """判断边 -> 邻接表：record_id -> [(judgment, 对端记录id), ...]"""
    adj = {}
    for j in judgments:
        adj.setdefault(j.a, []).append((j, j.b))
        adj.setdefault(j.b, []).append((j, j.a))
    return adj


def _same_path(adj, start, end):
    """返回仅由同物边构成的 start->end 路径（有序 Judgment 列表），不存在返回 None。"""
    if start == end:
        return []
    prev = {start: None}          # node -> (judgment, prev_node)
    q = deque([start])
    while q:
        node = q.popleft()
        for j, nbr in adj.get(node, []):
            if j.verdict != SAME or nbr in prev:
                continue
            prev[nbr] = (j, node)
            if nbr == end:
                path = []
                cur = end
                while prev[cur] is not None:
                    j2, p = prev[cur]
                    path.append(j2)
                    cur = p
                path.reverse()
                return path
            q.append(nbr)
    return None


def _chain_steps(path_judgments, start):
    """把有序判断边翻译成面向展示的判断链。"""
    steps = []
    cur = start
    for j in path_judgments:
        nxt = j.b if cur == j.a else j.a
        steps.append({
            "judgment_id": j.id,
            "from_record": cur,
            "to_record": nxt,
            "verdict": j.verdict,
        })
        cur = nxt
    return steps


def evaluate_proposal(judgments, record_ids, a, b, verdict):
    """评估一条新判断是否可接受。

    返回 dict：
      status:  ok        可写入
               conflict  与现有判断矛盾（含 chain 判断链），必须拒绝
               redundant 该判断已存在，无需重复写入
    """
    if a not in record_ids or b not in record_ids:
        return {"status": "conflict", "reason": "记录不存在", "chain": []}
    if a == b:
        return {"status": "conflict", "reason": "不能对同一条记录建立判断", "chain": []}
    if verdict not in (SAME, DIFFERENT):
        return {"status": "conflict", "reason": "未知判断类型", "chain": []}

    lo, hi = sorted((a, b))
    existing = next((j for j in judgments if j.a == lo and j.b == hi), None)
    if existing is not None:
        if existing.verdict == verdict:
            return {"status": "redundant",
                    "reason": "这两条记录之间已经存在相同判断，无需重复提交",
                    "chain": []}
        return {"status": "conflict",
                "reason": "这两条记录之间已有相反判断；如需改判，请先撤销原判断",
                "chain": _chain_steps([existing], a)}

    dsu = DSU(record_ids)
    for j in judgments:
        if j.verdict == SAME:
            dsu.union(j.a, j.b)
    adj = build_adjacency(judgments)

    if verdict == SAME:
        ra, rb = dsu.find(a), dsu.find(b)
        if ra == rb:
            return {"status": "redundant",
                    "reason": "两条记录已通过同物传递关系属于同一去重组，无需重复判断",
                    "chain": []}
        # 若存在一条异物边跨越待合并的两个组件，合并即违反“异物不得同组”
        for j in judgments:
            if j.verdict != DIFFERENT:
                continue
            x, y = j.a, j.b
            rx, ry = dsu.find(x), dsu.find(y)
            if {rx, ry} != {ra, rb}:
                continue
            # 定向：left 与 a 同组件，right 与 b 同组件
            if rx == ra:
                left, right = x, y
            else:
                left, right = y, x
            p1 = _same_path(adj, a, left) or []
            p2 = _same_path(adj, right, b) or []
            chain = _chain_steps(p1 + [j] + p2, a)
            return {"status": "conflict",
                    "reason": "同物合并会把已判定为异物的记录放进同一组，判断链如下",
                    "chain": chain}
        return {"status": "ok", "reason": "", "chain": []}

    # verdict == DIFFERENT
    if dsu.find(a) == dsu.find(b):
        path = _same_path(adj, a, b) or []
        return {"status": "conflict",
                "reason": "两条记录已由同物关系连通，不能再判定为异物，同物判断链如下",
                "chain": _chain_steps(path, a)}
    return {"status": "ok", "reason": "", "chain": []}


def compute_groups(judgments, record_ids):
    """由当前全部同物判断派生去重组。

    返回 list[dict]，每个元素：
      {group_id, members: [record_id, ...]}，按组内最小 id 稳定编号并排序。
    无任何判断的记录各自成为单元素组。
    """
    dsu = DSU(record_ids)
    for j in judgments:
        if j.verdict == SAME:
            dsu.union(j.a, j.b)
    buckets = {}
    for rid in record_ids:
        buckets.setdefault(dsu.find(rid), []).append(rid)
    groups = []
    for members in buckets.values():
        members.sort()
        groups.append({"group_id": "G%05d" % members[0], "members": members})
    groups.sort(key=lambda g: g["members"][0])
    return groups


def group_of_record(groups):
    """record_id -> group_id 映射。"""
    mapping = {}
    for g in groups:
        for rid in g["members"]:
            mapping[rid] = g["group_id"]
    return mapping


def group_sizes(groups):
    return {g["group_id"]: len(g["members"]) for g in groups}
