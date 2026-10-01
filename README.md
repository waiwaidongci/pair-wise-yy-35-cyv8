# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/investigation/claim`，接办调查任务（仅`radiation_officer`）
- `POST /api/items/{id}/investigation/checkpoint`，写入处理断点并续租（仅当前接办人）
- `GET /api/items/{id}/investigation`，查看接办任务状态
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 调查接办任务

事件进入`investigation`后以"接办任务"方式处理，同一事件至多有一个有效任务（数据库部分唯一索引保证）：

- 接办时固化**证据摘要**（标题、严重程度、剂量/阈值、未关闭记录数、最近记录）与**事件版本**`item_version`。
- 任务带租约（`lease_seconds`，默认900秒，范围30–86400）；租约超时任务自动回到`pending`，下一人接办沿用**同一任务**并从断点继续，`attempts`递增。
- 同一处理人重复接办返回首次任务（`outcome=reused`）并续租；他人在租约有效期内接办返回409。
- 处理人通过`checkpoint`（`step`/`note`）记录处理断点并续租；租约超时或事件版本变化后旧租约的迟到写入返回409。
- 事件改动（版本递增）后旧任务置为`stale`，新接办生成后继任务（`supersedes_task_id`指向旧任务、`outcome=succeeded`）并继承断点；事件转出调查时任务置为`completed`。
- 接办与断点接口仅`radiation_officer`（辐射防护员）可用，其他角色返回403。
- 调查中事件的`GET /api/items/{id}`响应附带`investigation_task`摘要（当前处理人、断点、租约是否有效）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
