# 社区回收点服务日历 HTTP API

为社区回收点维护**每周开放时段**、**指定日期全天停开**与**当日补开时段**，
按当地日期范围查询，逐日返回**实际开放区间的 UTC 起止、当地日期和来源**。

技术栈：Python 3.11 · FastAPI · PostgreSQL 16 · psycopg 3 · Uvicorn。

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

- 启动两个容器：`db`（PostgreSQL 16 Alpine）与 `api`（本服务）。
- 数据库由 Compose 健康检查把关；API 启动时会自动等待数据库并幂等执行
  [`app/schema.sql`](app/schema.sql) 完成建表初始化，无需手工迁移。
- 数据持久化在命名卷 `pgdata` 中。加 `-d` 可后台运行；`docker compose down -v`
  可连数据卷一起清除。

### 地址与端口

| 服务 | 容器内地址 | 宿主机默认地址 |
| --- | --- | --- |
| API | `http://0.0.0.0:8000` | <http://localhost:8000> |
| Swagger UI（交互式文档） | — | <http://localhost:8000/docs> |
| ReDoc | — | <http://localhost:8000/redoc> |
| OpenAPI 描述 | — | <http://localhost:8000/openapi.json> |
| PostgreSQL | `db:5432` | `localhost:5432` |
| 健康检查 | — | `GET http://localhost:8000/health` |

---

## 2. 配置（环境变量）

均为可选；`docker compose up` 直接用默认值即可。也可复制
[`.env.example`](.env.example) 为 `.env` 后修改，Compose 会自动加载。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_DB` | `recycle` | 数据库名 |
| `POSTGRES_USER` | `recycle` | 数据库用户 |
| `POSTGRES_PASSWORD` | `recycle` | 数据库密码 |
| `POSTGRES_PORT_PUBLISH` | `5432` | 映射到宿主机的数据库端口 |
| `APP_PORT_PUBLISH` | `8000` | 映射到宿主机的 API 端口 |
| `MAX_QUERY_DAYS` | `370` | 日历查询允许的最大跨天数量（两端包含） |
| `DATABASE_URL` | （无，由上面拼装） | 完整 libpq 连接串，**非容器/本地运行时**可直接设置，例如 `postgresql://recycle:recycle@localhost:5432/recycle` |

容器内 API 固定监听 `0.0.0.0:8000`；容器内数据库主机固定为 `db:5432`。

### 不使用 Docker 的本地运行方式

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=postgresql://recycle:recycle@localhost:5432/recycle
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 3. 领域规则与语义

- **时区**：每个回收点持有一个 IANA 时区名（如 `Asia/Shanghai`、
  `America/New_York`），所有时刻以该时区的挂钟时间解释，输出统一为 UTC。
- **每周开放时段 `weekly`**：
  - `weekday` 为 ISO 星期：`1`=周一 … `7`=周日；
  - 同一规则内 `end_time <= start_time` 即**跨午夜**，最长延伸到次日；
  - **跨午夜时段按开始日匹配周规则**（周五 20:00–次日 02:00 属于周五场次）。
- **全天停开 `closed_dates`**：
  - 扣除该当地日上的**全部常规片段**，包括前一晚（`D-1`）延续到当日的片段；
  - 同时取消**由当日开场、延伸到次日凌晨的尾巴**，使跨午夜场次整体消失，
    不会在次日留下悬空片段；
  - 然后在当日叠加补开时段。
- **当日补开 `makeup`**：仅限当日（`end_time` 必须严格晚于 `start_time`）。
- **逐日裁切**：查询范围两端包含；结果按当地日逐天给出，区间被裁到当天的
  当地午夜之间（因此跨午夜场次会在午夜处切成两段，但来源相同）。
- **区间合并与来源保留**：同一日内相互重叠（或首尾相接）的开放区间合并为一个，
  `sources` 保留构成该并集的**全部来源**。
- **夏令时**：
  - 挂钟时间在春令时“缺失”（gap）时，边界**顺延到第一个有效时刻**；
  - 在令时“重复”（fall-back）时，**起点取较早实例、终点取较晚实例**；
  - 所有区间运算在 UTC 下进行，正确计入回退重复的一小时。
- **版本化与原子发布**：
  - 规则整套提交，请求体带 `expected_version`（乐观并发控制）；
  - 服务端对回收点行加 `FOR UPDATE` 行锁，校验当前版本号后在**单个事务**里
    插入新版本并推进版本号，原子提交或整体回滚；
  - 历史版本永不就地修改，全部保留可查；
  - 每次日历查询在 `REPEATABLE READ` 事务里读取**同一个已发布版本**的完整规则；
    也可用 `?version=N` 显式固定到某个历史已发布版本。
  - 新建回收点版本号为 `0`，表示“尚无已发布规则”。

---

## 4. HTTP API

所有请求/响应均为 JSON；错误统一为：

```json
{ "error": { "code": "...", "message": "...", "details": { } } }
```

### 4.1 回收点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/points` | 创建回收点（校验 IANA 时区） |
| `GET` | `/points/{id}` | 查看回收点（含当前版本号） |
| `PATCH` | `/points/{id}` | 修改名称/时区 |

`POST /points` 请求体：

```json
{ "name": "阳光社区回收点", "timezone": "Asia/Shanghai" }
```

### 4.2 规则发布与版本

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/points/{id}/rules` | 条件式、原子地发布整套规则（201） |
| `GET` | `/points/{id}/rules` | 读取当前已发布版本 |
| `GET` | `/points/{id}/rules/versions` | 已发布版本列表（新到旧） |
| `GET` | `/points/{id}/rules/versions/{version}` | 读取指定历史版本 |

发布请求体：

```json
{
  "expected_version": 0,
  "rules": {
    "weekly": [
      { "weekday": 1, "start_time": "09:00", "end_time": "17:00" },
      { "weekday": 5, "start_time": "20:00", "end_time": "02:00" }
    ],
    "closed_dates": ["2026-10-01"],
    "makeup": [
      { "day": "2026-10-01", "start_time": "13:00", "end_time": "16:00" }
    ]
  }
}
```

时间均为 `HH:MM`（当地挂钟时间，秒可选）。成功返回新版本号与发布时间。

### 4.3 日历查询

```
GET /points/{id}/calendar?from=YYYY-MM-DD&to=YYYY-MM-DD[&version=N]
```

- `from`、`to` 为**当地日期**，**两端包含**；`to` 不得早于 `from`。
- `version` 省略时使用最新已发布版本。

响应示例：

```json
{
  "point_id": 1,
  "version": 1,
  "timezone": "Asia/Shanghai",
  "date_from": "2026-09-28",
  "date_to": "2026-10-04",
  "days": [
    {
      "local_date": "2026-09-28",
      "intervals": [
        {
          "start": "2026-09-28T01:00:00Z",
          "end": "2026-09-28T09:00:00Z",
          "sources": [
            { "kind": "weekly", "weekday": 1,
              "start_time": "09:00:00", "end_time": "17:00:00" }
          ]
        }
      ]
    }
  ]
}
```

无开放时段的日期仍然出现，`intervals` 为空数组。`sources[].kind` 为
`weekly`（带 `weekday`）或 `makeup`（带 `day`）。

### 4.4 可区分错误码

| HTTP | `error.code` | 触发场景 |
| --- | --- | --- |
| 404 | `point_not_found` | 回收点不存在 |
| 404 | `version_not_found` | 指定的已发布版本不存在 |
| 409 | `no_published_version` | 尚无已发布规则（当前版本为 0）就查询日历 |
| 409 | `version_conflict` | `expected_version` 与服务端当前版本不符（`details` 给出双方版本号） |
| 422 | `invalid_period` | 无效时段：星期越界、时间格式错、常规起终点相等、补开跨日/零长度、重复规则、日期范围倒置/超限 |
| 422 | `unknown_timezone` | 无法识别的 IANA 时区 |
| 422 | `invalid_request` | 请求体结构未通过校验（字段缺失、类型错误等） |

---

## 5. 调用示例

服务启动后任选其一：

```bash
# 方式一：curl 脚本（创建点 → 发布 → 冲突 → 查询 → 错误演示）
./examples/curl_examples.sh

# 方式二：仅依赖 Python 标准库的客户端
python3 examples/python_client.py
```

手动快速体验：

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/points \
  -H 'Content-Type: application/json' \
  -d '{"name":"阳光社区回收点","timezone":"Asia/Shanghai"}'
```

---

## 6. 本地开发与测试

```bash
python3 -m venv .venv --without-pip && . .venv/bin/activate
# 若 venv 中没有 pip：python /tmp/get-pip.py  （或用系统包安装 python3-venv）
pip install -r requirements.txt
pip install pytest pgserver
pytest -q
```

测试无需本机安装 Docker/PostgreSQL：通过 [`pgserver`](https://pypi.org/project/pgserver/)
拉起一次性的真实 PostgreSQL 16 实例，对完整 HTTP 链路做集成测试，覆盖：

- 跨午夜规则按开始日匹配、逐日裁切；
- 停开扣除当日全部常规片段（含前夜延续）并取消当日场次次日尾巴，再叠加补开；
- 重叠区间合并且保留全部来源；
- 夏令时春缺失顺延、秋重复起点取早/终点取晚（纽约、哈瓦那午夜跳变）；
- 版本条件提交、冲突 409、历史版本固定查询、并发发布恰好一方成功；
- 全部可区分错误码。

---

## 7. 项目结构

```
app/
  config.py      环境变量配置
  schema.sql     PostgreSQL 建表脚本（启动时幂等执行）
  db.py          连接池、版本化规则的原子发布与读取（行锁 + 事务）
  scheduler.py   纯 Python 日历展开引擎（跨午夜/停开/补开/合并/DST）
  schemas.py     Pydantic 请求响应模型与时段语义校验
  errors.py      可区分错误类型
  main.py        FastAPI 路由与错误处理
examples/        curl 与 Python 调用示例
tests/           引擎单测 + 真实 PostgreSQL 的 API 集成测试
Dockerfile, docker-compose.yml
```
