# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性、公司行动调整、批次确认计划和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（批次、回执、权益版本、账务幂等键、日终断点、待对账）。
- `src/service.py`：用例编排、权限检查、乐观并发、日终处理和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和日终批次测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 结算指令接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`、`limit`、`batch_id`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...},"batch_ref":"B-..."}`，`batch_ref`可选。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 日终批次（交收一体化）

批次生命周期：`open`（未关账）→ `confirmed`（已确认、权益版本已冻结）→ `closed`（已关账）。
权益版本更新后，已确认但未关账的批次转为`stale`，必须按新版本重新确认后才能关账。

- `POST /api/batches`：创建日终批次，`{"reference":"B-...","settlement_day":2}`（settlement_officer）。
- `GET /api/batches` / `GET /api/batches/{id}`：批次列表与详情（含指令、回执、事件、账务分录）。
- `POST /api/batches/{id}/confirm`：确认交收。确认瞬间冻结当时的权益版本；批次在单事务内
  完成指令置为已交收、回执置为已应用和账务入账。两位交收员同时确认时只有一位成功，
  后到者返回409冲突；重跑靠幂等记账键`settle:batch-{b}:record-{r}`保证不重复记账。
  可选`{"expected_version":N}`做强版本校验。
- `POST /api/batches/{id}/close`：关账（仅`confirmed`可关，`stale`必须先重新确认）。
- `POST /api/eod/run`：`{"settlement_day":2}`。从上次完整批次之后继续；缺回执或金额不符的
  批次会阻断并返回`blocked_batch`，断点不前移；写库失败后重试从断点继续，已入账批次不重复记账。

## 交收回执与公司行动权益

- `POST /api/entitlements`：发布权益新版本，`{"instrument":"ACME","terms":{"cash_per_share":0.5,"quantity_ratio":1}}`
  （corporate_actions）。发布时同一事务内把引用该券号、已确认未关账且版本落后的批次置为`stale`。
- `GET /api/entitlements?instrument=ACME`：权益版本列表。
- `POST /api/receipts`：登记交收回执，`{"batch_ref":"B-...","data":{"instrument":"ACME","delivered_quantity":1000,"cash_paid":12518.0}}`
  （custodian/settlement_officer）。
  - 同一批次同一券号的有效回执只有一张，重复到达返回`{"duplicate":true}`并标记留痕，绝不参与交收。
  - 已确认未关账批次收到新的（非重复）回执时批次转为`stale`，需重新确认。
  - **关账后到达**的回执不进入交收，而是进入待对账，原批次的权益快照和账务分录不可倒改。
- `GET /api/reconciliation?status=pending`：待对账列表。
- `POST /api/reconciliation/{id}/complete`：补全关账后回执，生成独立的新调整批次与调整分录
  （键`recon:item-{id}`，幂等），原关账批次保持不变；重复补全返回409。
- `GET /api/ledger?batch_id={id}`：账务分录查询。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及日终确认的权益冻结、
回执去重、两人并发确认、写库失败断点重试不重复记账、权益版本更新重确认和关账后回执对账。
