# 企业集团合并申报

本项目维护企业集团合并申报的领域约定、角色边界与样例数据，并提供一套零第三方依赖的
Python 服务端：维护企业关系生效区间、申报批次、内部交易标记和合并抵销规则，
草案生成时固定组织快照，达到签署条件后封存；子公司退出、追溯更正、跨集团重组与
并发提交均形成可审计差异，报告接口可展开集团总额、抵销项与各法人独立责任。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/group_filing/`：合并申报服务端。
  - `store.py`：SQLite 建表与连接（时态区间、修订、审计日志）。
  - `service.py`：领域服务（时态股权、期间加权快照、抵销计算、封存、差异）。
  - `server.py`：`http.server` JSON 接口。
  - `errors.py`：领域错误（400/404/409）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归与服务端场景测试。

## 领域规则

- **时态控股关系**：每条关系为半开区间 `[effective_from, effective_to)`，
  同一父-子链区间不得重叠；新增开放区间自动截断旧的开放区间，全部追加留痕。
- **期间加权并入**：成员资格按申报年度内被集团控制的日期段判定，
  集团总额按受控天数（及期间加权持股）归因。年中并购只并入受控段，
  年中退出不影响退出前数据，旧数据不会整段并入新集团。
- **内部交易标记**：按交易发生日判定双方是否同受控制；
  不受控交易进入 `excluded_intercompany_txns` 并给出原因，绝不参与抵销；
  同一期间/双方/业务编号唯一，防止重复登记、重复计算。
- **组织快照**：草案生成时把成员、控制区间、路径、阈值固化；
  在途批次不受之后股权变动影响；每次重新生成写入不可变修订版本，
  可通过 diff 接口查看成员增删、覆盖率与抵销额变化。
- **合并抵销**：内置内部销售、内部采购、未实现损益抵销规则；
  少数股东权益仅列示（memo），不抵减合并积分。申报内部交易数与标记数
  自动核对，不一致生成 `reconciliation_flags` 并阻止封存。
- **批次状态**：草稿 → 待核算 → 已确认 →（执行中）→ 已封存；
  封存要求全体成员已申报且核对一致；封存后报告整体冻结，
  相关期间内部交易标记禁止追加。
- **追溯更正**：仅草稿/待核算可更正，原值、新值、原因与版本全部入审计日志；
  所有写接口支持 `expected_version` 乐观锁，并发提交失败方收到 409。
- **跨集团检查**：`/periods/{YYYY}/membership` 汇总同一年度各集团快照，
  同一法人覆盖率合计超过 100% 时标记 `potential_double_count`。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/entities` | 登记法人 |
| POST/GET | `/ownership-links` | 时态控股关系（自动截断）/ 查询 |
| GET | `/groups/{root}/structure?date=` | 任一时点集团结构 |
| POST/GET | `/intercompany-txns?period=` | 内部交易标记 / 查询 |
| GET | `/rules` | 抵销规则 |
| POST/GET | `/batches` | 申报批次 |
| POST | `/batches/{id}/draft` | 生成/重新生成草案（固定快照、写修订） |
| PUT | `/batches/{id}/filings/{entity}` | 成员填报 |
| POST | `/batches/{id}/filings/{entity}/submit` | 成员提交（乐观锁） |
| POST | `/batches/{id}/filings/{entity}/correct` | 追溯更正 |
| POST | `/batches/{id}/submit`、`/review`、`/execute`、`/seal` | 批次流转 |
| GET | `/batches/{id}/report` | 总额/抵销项/法人独立责任展开 |
| GET | `/batches/{id}/revisions`、`/diff?from=N&to=M` | 修订与可审计差异 |
| GET | `/batches/{id}/audit`、`/audit` | 审计轨迹 |
| GET | `/periods/{YYYY}/membership` | 跨集团重复并入检查 |

## 运行

```bash
# 启动（默认 data/group_filing.db，可用 DB_PATH/PORT 覆盖）
python3 -m group_filing.server --host 127.0.0.1 --port 8080 --db data/demo.db
# 或安装后：group-filing-server
```

快速走查（年中并购场景）：

```bash
curl -s localhost:8080/entities -d '{"entity_id":"G","name":"母公司"}'
curl -s localhost:8080/entities -d '{"entity_id":"S2","name":"年中并入"}'
curl -s localhost:8080/ownership-links -d '{"parent_id":"G","child_id":"S2","share_pct":100,"effective_from":"2026-07-01"}'
curl -s localhost:8080/batches -d '{"period_label":"2026","root_entity_id":"G","batch_id":"B1"}'
curl -s -X POST localhost:8080/batches/B1/draft
curl -s localhost:8080/batches/B1/report | python3 -m json.tool
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
