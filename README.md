# 藏品同物异号核对工作台（Flask + SQLite）

核对旧目录中“同物异号”问题的小型 Web 工具：上传目录 CSV，数据员逐条确认两条记录是
**同物**还是**异物**，系统维护去重组并可下载每条原记录所属去重组的映射。

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

## 目录结构

```
app/
  __init__.py     Flask 应用工厂与路由（上传/判断/撤销/下载）
  logic.py        纯逻辑：DSU、传递分组、冲突评估与判断链（无 DB 依赖，可单测）
  db.py           SQLite 访问层：建表、事务、并发控制
  csvutil.py      CSV 解析与校验
  templates/      单页工作台（base.html / index.html）
examples/
  sample_catalog.csv  示例目录（10 条）
  DEMO.md             分步演示脚本
tests/            24 个 pytest 用例（逻辑 / CSV / 接口 / 并发 / 快照）
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
.venv/bin/pytest          # 24 passed
```

覆盖点：同物传递、异物冲突与判断链内容、跨组件异物边拦截、冗余判断、
撤销后仍连接的记录不被误拆、CSV 各类非法输入、记录不可改写、
线程级并发矛盾提交只有一方生效、并发写入下读快照的一致性不变量。

## 设计取舍说明

- **分组不入库**：去重组完全是同物判断的派生视图，避免“判断与分组”双写不一致；
  撤销判断无需任何补偿逻辑，重算即可。
- **判断边存储规范化**：`record_a < record_b` 且 `UNIQUE`，一对记录至多一条判断，
  改判须先撤销，防止相反结论并存。
- **SQLite 并发模型**：单库多连接，写事务立即取写锁串行化；配合 WAL，
  读（含下载）不阻塞写。多 worker/gunicorn 进程下同样成立。
