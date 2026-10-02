# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/eod.py`：日终处理纯逻辑（权益版本冻结、按券号汇总交收、记账明细、对账差额）。
- `src/repository.py`：SQLite建表、事务和查询（含日终批次、回执、账务、对账、run检查点）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景、并发确认与日终处理测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 日终处理（结算指令 × 交收回执 × 公司行动权益）

结算指令在`data`中可带`batch_key`归入日终批次。处理语义：

- **确认交收冻结权益版本**：`POST /api/batches/{key}/confirm`（`expected_version`为批次版本）。
  确认时逐券号冻结当时生效的权益版本（`entitlement_snapshot`）并据此记账；快照之后不受新版本影响。
  新权益版本发布时，所有**未关账**且含该券号的批次自动回到开放态、清除临时账务，必须按新版本重新确认。
- **回执去重**：`POST /api/receipts`按`(batch_key, instrument)`唯一。重复到达且内容一致幂等忽略（仅`ingest_count`+1）；
  内容冲突返回409，不覆盖首条回执。
- **并发确认**：两个交收员同时确认同一批次，只放行一位（条件更新`state='open' AND version=?`），后到者409冲突。
- **失败重试**：`POST /api/eod/run`按批次顺序提交，每批次一个原子事务，run记录`last_completed_batch`检查点；
  写库失败后用同一`run_key`重试，从上次完整批次之后继续，账务行按幂等键去重，不重复记账。
- **关账后回执**：批次`close`后到达的回执不记账、不倒改，直接进入待对账（`GET /api/reconciliation?status=pending`）；
  补全通过`POST /api/reconciliation/complete`生成`{原批次}#RCn`新结果批次（已关账），沿用原冻结快照、只记差额调整账。

日终相关接口：

- `POST /api/entitlements`：发布权益新版本（角色`corporate_actions`），`data`为`{"instrument","event_type","ratio","cash_rate"}`。
- `GET /api/entitlements`：当前生效权益版本。
- `POST /api/batches`：显式开批次（也可在回执首达时按`settlement_day`自动开）。
- `GET /api/batches` / `GET /api/batches/{key}`：批次列表/详情（含冻结快照、账务、回执）。
- `POST /api/receipts`：登记交收回执（角色`settlement_officer`）。
- `GET /api/batches/{key}/receipts`、`GET /api/batches/{key}/ledger`。
- `POST /api/batches/{key}/confirm` / `POST /api/batches/{key}/close`。
- `POST /api/eod/run`：执行/续跑日终，可带`run_key`。
- `GET /api/reconciliation`、`POST /api/reconciliation/complete`（角色`settlement_officer`或`reconciliation_clerk`）。
- `GET /api/events?kind=batch&key=...`：日终事件时间线。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、日终冻结/去重/并发/重试/对账。
