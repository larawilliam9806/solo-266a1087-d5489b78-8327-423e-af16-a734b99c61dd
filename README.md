# 藏品同物异号核对工作台（Flask + SQLite）

核对旧目录中“同物异号”问题的小型 Web 工具：上传目录 CSV，数据员逐条确认两条记录是
**同物**还是**异物**，系统维护去重组并可下载每条原记录所属去重组的映射；同时提供
**增量供数**能力——检索库可创建订阅，先拿首批全量数据，再按发生顺序逐批领取导入、
判定、撤销造成的归属变化（JSON，供下游覆盖索引）。

## 功能与规则

| 需求 | 实现方式 |
| --- | --- |
| 上传 CSV（来源、原编号、名称） | `POST /upload`，表头兼容中英文；UTF-8、10MB 以内 |
| 来源 + 原编号组合唯一 | 数据库 `UNIQUE(source, original_no)` + 文件内重复整批拒绝；库中已存在的组合跳过 |
| 原记录不改写 | `record` 表只有 INSERT（`INSERT OR IGNORE`），应用不提供任何 UPDATE/DELETE 入口 |
| 同物关系传递 | 分组不持久化，由全部同物判断经并查集（DSU）实时派生传递闭包 |
| 异物记录不得同组 | 同物合并前检查跨组件异物边；异物判断前检查同物连通性 |
| 冲突展示判断链并拒绝 | 冲突时返回有序判断链（同物路径 + 异物边），事务回滚，原状不变 |
| 撤销单条判断 | `POST /judgment/<id>/undo` 删除该判断边，分组按剩余判断重算；DSU 重算天然保证仍有其他连接的记录不被误拆 |
| 并发不产生矛盾判断 | 所有写事务 `BEGIN IMMEDIATE` + `busy_timeout=30s` + `UNIQUE` 约束；事务内基于最新数据重新校验，两页面并发必有一方被拒 |
| 下载映射对应完整判断状态 | 映射在单个读事务快照中一次性读取记录与判断并计算分组，不会读到半截状态 |
| **创建订阅并下载首批全量** | `POST /api/subscriptions` 在一个立即写事务内创建订阅并固化批次 #1 全量快照（每条记录的记录ID、来源、原编号、名称、新组ID） |
| **按发生顺序领取归属变化** | `POST /api/subscriptions/<id>/claim`：每个变化集（一次导入/判定/撤销）整批固化为一个增量批次，不拆分、不合并，JSON 含记录ID、来源、原编号、名称、新组ID，供下游覆盖索引 |
| **未确认批次标识与内容不变** | 批次条目只 INSERT 无 UPDATE/DELETE；未确认时重复领取返回同一批（`pending`），新变化只能进入后续批次 |
| **确认指定批次，语义幂等** | `POST /api/subscriptions/<id>/ack/<batch_no>`：重复确认返回 `already_acked` 无副作用；旧批次的确认不能推进新批；存在更早未确认批次时确认被拒（`blocked`） |
| **创建订阅时并发修改不漏记** | 建订在 `BEGIN IMMEDIATE` 内取 `MAX(change_set.id)` 游标并写首批，事务期间业务写被锁挡在外面，提交后才放行的变化 id 必大于游标，后续批次必能领到 |
| **并发领取/确认、重启不跳变** | 领取与确认均在立即写事务内串行化；游标、批次、确认状态全部落库，服务重启后原样保留 |
| **业务失败不留供数记录、成功不漏记** | 事务性发件箱：业务写入与变化集在同一事务提交，冲突/冗余回滚时变化集一并消失，成功时归属差异必然随变化集落库 |

## 目录结构

```
app/
  __init__.py     Flask 应用工厂与路由（上传/判断/撤销/下载 + 订阅/批次 JSON API 与供数页）
  logic.py        纯逻辑：DSU、传递分组、冲突评估与判断链（无 DB 依赖，可单测）
  db.py           SQLite 访问层：建表、事务、并发控制、变化集/订阅/批次
  feed.py         批次 JSON 序列化（纯函数）
  csvutil.py      CSV 解析与校验
  templates/      单页工作台（base.html / index.html）与增量供数页（feed.html）
examples/
  sample_catalog.csv  示例目录（10 条）
  DEMO.md             分步演示脚本
tests/            44 个 pytest 用例（逻辑 / CSV / 接口 / 并发 / 快照 / 增量供数）
Dockerfile
docker-compose.yml
run.py            本地开发入口
requirements.txt
```

## 方式一：Docker Compose（推荐）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据保存在命名卷 `catalog-data`（容器内 `/data/catalog.db`），容器重建不丢数据。
生产环境请修改 `docker-compose.yml` 中的 `SECRET_KEY`。

## 方式二：本地 Python 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py          # 开发服务器，:8000
# 或：.venv/bin/gunicorn --bind 0.0.0.0:8000 'app:create_app()'
```

## 使用流程

1. 打开首页，上传 `examples/sample_catalog.csv`（可重复上传验证去重与不可改写）。
2. 在记录表选择两条记录（点“选为 A/选为 B”或手填记录 ID），选择同物/异物后提交。
3. 冲突时页面红框展示导致冲突的完整判断链，操作被拒绝、数据原状不变。
4. 在“已提交判断”区可撤销任意判断，分组立即按剩余判断重新形成。
5. 点击“下载 dereplication_mapping.csv”，每条原记录一行：
   `去重组ID, 记录ID, 来源, 原编号, 名称, 组内记录数`（UTF-8 BOM，Excel 可直接打开）。

## 测试

```bash
.venv/bin/pip install pytest
.venv/bin/pytest          # 44 passed
```

覆盖点：同物传递、异物冲突与判断链内容、跨组件异物边拦截、冗余判断、
撤销后仍连接的记录不被误拆、CSV 各类非法输入、记录不可改写、
线程级并发矛盾提交只有一方生效、并发写入下读快照的一致性不变量；
增量供数：首批全量内容、变化集与业务事务同成败（失败不留痕、成功不漏记）、
按变化集整批按序交付、未确认批次内容不可变、确认幂等与顺序约束、
创建订阅并发修改不漏记、并发领取每批恰好一次、并发确认幂等、重启后批次不跳变、
JSON 接口与文件下载、原有映射下载不受影响。

## 增量供数 API

| 方法与路径 | 作用 |
| --- | --- |
| `GET /feed` | 增量供数演示网页（创建订阅、领取/查看/下载/确认批次） |
| `POST /api/subscriptions`，body `{"name": "..."}` | 创建订阅，返回订阅 id 与首批全量批次（batch_no=1, kind=snapshot） |
| `POST /api/subscriptions/<id>/claim` | 领取下一批：未确认批次在时返回 `pending` 原批次；新变化固化为 `created` 批次；无变化返回 `up_to_date` |
| `GET /api/subscriptions/<id>/batches/<n>` | 查看指定批次 JSON（标识与内容恒定） |
| `GET /api/subscriptions/<id>/batches/<n>/download` | 以附件方式下载该批 JSON 文件 |
| `POST /api/subscriptions/<id>/ack/<n>` | 确认指定批次：`ok` / `already_acked` / `blocked` / `not_found` |

批次 JSON 结构（每条记录含下游覆盖索引所需全部字段）：

```json
{
  "subscription_id": 1,
  "batch_id": 2,
  "batch_no": 2,
  "kind": "delta",
  "change_set_id": 2,
  "op_type": "judge",
  "op_type_label": "提交判断",
  "op_ref": 1,
  "item_count": 1,
  "acked": false,
  "records": [
    {"record_id": 2, "source": "故宫旧藏", "original_no": "JD-002",
     "name": "青花缠枝莲纹梅瓶（残）", "new_group_id": "G00001"}
  ]
}
```

## 设计取舍说明

- **分组不入库**：去重组完全是同物判断的派生视图，避免“判断与分组”双写不一致；
  撤销判断无需任何补偿逻辑，重算即可。
- **判断边存储规范化**：`record_a < record_b` 且 `UNIQUE`，一对记录至多一条判断，
  改判须先撤销，防止相反结论并存。
- **SQLite 并发模型**：单库多连接，写事务立即取写锁串行化；配合 WAL，
  读（含下载）不阻塞写。多 worker/gunicorn 进程下同样成立。
- **增量供数采用事务性发件箱（outbox）**：`change_set / change_record` 与业务写入
  在同一 `BEGIN IMMEDIATE` 事务内提交，天然满足“失败不留供数记录、成功不漏变化”，
  无需额外后台任务或消息中间件。变化集只收录归属（新组ID）实际改变的记录，
  不改变归属的异物判断不产生变化集。
- **批次是变化集的不可变快照**：一个变化集恰好对应一个增量批次（整批交付，不拆分
  不合并）；批次内容在固化时复制记录字段，之后业务再变化也不影响历史批次。
- **订阅游标语义**：`subscription.last_change_id` 是“已固化进批次”的最大变化集 id，
  建订时取当前 `MAX(change_set.id)` 并在同事务写首批，保证首批与增量无缝衔接；
  游标推进与批次写入同事务，崩溃/重启后可精确续领，不会跳过变化。
