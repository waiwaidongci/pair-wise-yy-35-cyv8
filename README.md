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
python3 app.py --db ./data.db --port 8312 --lease-seconds 1800
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 调查接办任务

事件进入调查（`investigation`）后自动建立**接办任务**，同一事件只有一个有效任务，重复接办沿用首次任务。接办时记录证据摘要与事件版本；事件改动后旧快照失效（`stale=true`），接手人刷新快照但从处理断点继续。

- 接办入口仅对 `radiation_officer` 开放，其他角色返回 `403`。
- 同一时间只有一人接办：他人在租约期内接办返回 `409`；同一人重复接办续租且不重复计数。
- 租约超时（值班员调班或服务中断）后任务**自动回到待接办**，断点保留；服务另有后台线程定期回收超时租约。
- 处理失败/中断后重新接办即可从断点恢复。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/tasks`，接办任务列表（可按`?status=pending|active|done`过滤）
- `GET /api/items/{id}/task`，事件的接办任务
- `POST /api/items/{id}/task/take`，接办/续租（仅`radiation_officer`）
- `POST /api/items/{id}/task/checkpoint`，记录处理断点（仅接办人，续租约）
- `POST /api/items/{id}/task/complete`，办结任务（仅`radiation_officer`）
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
