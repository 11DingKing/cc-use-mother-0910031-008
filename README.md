# 企业集团合并申报

维护企业集团年度积分（申报分项）统一合并申报的领域约定与 Python 服务端。覆盖企业申报员、核算专员、交易运营员、监管审计员四类角色，并落实四条关键不变量：**组织关系时态、合并抵销规则、申报组织快照、法人责任展开**。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/group_filing/`：合并申报服务端（零第三方依赖，仅标准库 + SQLite）。
  - `models.py`：五态状态机（草稿/待核算/已确认/执行中/已封存）、时态区间、金额值对象。
  - `storage.py`：SQLite 建表与进程内事务锁。
  - `service.py`：核心领域服务。
  - `server.py` / `app.py`：HTTP 接口与启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、核心场景回归、HTTP 端到端测试。

## 审计关切的对应设计

| 审计风险 | 机制 |
| --- | --- |
| 年中控股变化后旧数据被错误并入新集团 | 控股关系以**左闭右开生效区间**存储；同一子公司任一时点至多一个控股股东（开放区间唯一 + 历史区间重叠拒绝）；草案生成时**固化组织快照**并计算 SHA-256 基哈希；非快照法人的数据登记被拒绝并留 `exclusion` 差异 |
| 子公司退出 | 关闭开放区间，在所有在途批次写入 `exit` 差异；成员窗口截断至退出日；封存前必须重新固化快照 |
| 内部转移重复计算 | 交易以 `txn_id` 去重（批次内唯一）；内部/外部由系统按**交易日双方实际控股股东**裁定；进入「待核算」时每笔内部交易只生成一条抵销分录（`UNIQUE(batch_code, txn_id)`） |
| 追溯更正 / 跨集团重组 | `correction` 与新集团登记均产生 before/after 差异（`retroactive`/`restructure`），逐笔重裁交易、重建抵销；已封存批次永不改变；重新固化后强制退回「待核算」重走确认链 |
| 并发提交 | 批次级审计序号乐观锁，`expected_seq` 不符返回 409 并记录 `concurrent` 差异；存储层写操作经全局锁串行化 |
| 签署封存 | 封存时复核组织基哈希，不一致即拒绝；封存为终态；法人独立责任表固化，可逐法人追责 |

## HTTP 接口

```
POST /entities                         登记法人
POST /ownership                        登记控股区间（自动截断旧开放区间）
POST /ownership/{id}/correction        追溯更正
POST /subsidiaries/{code}/exit         子公司退出
POST /batches                          生成草案（固化组织快照）
POST /batches/{code}/lines             法人独立分项
POST /batches/{code}/transactions      内部交易登记与标记裁定
POST /batches/{code}/transition        状态迁移（target 取五态中文名）
POST /batches/{code}/resnapshot        重新固化快照
GET  /batches/{code}                   批次详情（含快照与 current_seq）
GET  /batches/{code}/expansion         集团总额 / 抵销项 / 各法人独立责任
GET  /batches/{code}/diffs             可审计差异（exit/retroactive/restructure/concurrent/exclusion）
GET  /batches/{code}/audit             完整审计链（按序号有序）
GET  /health
```

写请求可携带 `expected_seq`（取自 `GET /batches/{code}` 的 `current_seq`）实现乐观并发控制；业务冲突返回 400，并发冲突返回 409，资源不存在返回 404。

`expansion` 同时返回三层口径：`group_total_yuan`（= 分项合计 − 抵销合计）、逐项 `eliminations`、以及每个法人的 `standalone_yuan`（独立责任）、`share_pct`、`eliminated_yuan`、`attributed_yuan`（按持股窗口归因）与 `member_window`。

## 运行与验证

```bash
# 启动服务（默认内存库；--db 指定持久化 SQLite 路径）
PYTHONPATH=src python3 -m group_filing.app --db data.db --port 8080

# 全部测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约检查
python3 tools/check_contract.py domain/contract.json
```
