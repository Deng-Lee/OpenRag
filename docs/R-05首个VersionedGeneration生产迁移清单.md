# R-05 首个 Versioned Generation 生产迁移清单

本清单只编排已经实现的正式 Service/API/CLI，不允许 SQL 手改 state/route，不允许直接调用 `Collection.drop`。当前状态为 **BLOCKED**，阻断项见“生产前硬门禁”。

## 生产前硬门禁

- [ ] 在目标 Milvus 同版本环境完成 `milvus-rbac-cutover.md` 的三身份允许/拒绝矩阵，runtime 与 index-admin 的 DropCollection 必须被拒绝。
- [ ] 部署并验证 Milvus Backup backend；对达到阈值的 generation 能创建并验证 manifest。
- [ ] 评审并应用 Alembic `20260715_0008` 至 `20260715_0017`，完成数据库备份与恢复演练。
- [ ] 保存 PostgreSQL、legacy Chunk/Layer Collection 元数据、实体数、Schema、索引、Alias 和 Eval 基线。
- [ ] 配置真实且可解析的 Embedding config-ref；不得把 API Key 写入 generation manifest、日志或文档。
- [ ] API `/health` liveness 正常，`/ready` 对 legacy active 返回 ready；Worker 使用 route 绑定写入。

任一项未通过，停止迁移，不创建/激活 candidate。

## 阶段 A：Legacy 注册与兼容验证

1. 先执行 dry-run：`python -m openrag.cli.index_generation bootstrap-legacy --operator <name>`。
2. 审批输出中的 Collection、dimension、metric、Schema、实体数和 embedding 身份；未知身份不得伪造，先补证据。
3. 使用 `--execute --operator-id <id>` 幂等注册 legacy。
4. 验证 route.active_generation_id 指向 legacy；flat/hierarchical/权限过滤和删除 smoke 均从同一 Runtime snapshot 读取 legacy。

退出条件：legacy 仍承载在线流量，但固定环境变量路由已退出数据面。

## 阶段 B：首个 Candidate 构建

1. 管理 API preview，审批 credential-free manifest、真实模型 revision/fingerprint、物理 Collection 名和 rollback window。
2. 使用唯一 client_request_id 创建 draft，再 provision；失败不得改 active route，也不得自动 drop。
3. start reindex，持续观察文件状态、failed_file_count、build_lag_files；暂停/恢复仅使用管理 API。
4. 全量结束后反复 reconcile，直到 lag=0；期间新建、更新、删除各做一次在线验证。

退出条件：failed_file_count=0、build_lag_files=0，candidate 不承担用户响应。

## 阶段 C：完整性与质量批准

1. 调用 validate；要求 validation_report 的所有硬检查 passed，Chunk/Layer 已加载且 smoke search 成功。
2. 对同一 EvalDataset 分别创建绑定 legacy active 与 candidate generation_id 的 EvalRun。
3. 开启小比例 Shadow，观察空结果、错误、p95 和权限泄漏；原始 query 不新增出既有 Trace 策略。
4. 生成 quality_report；Recall/nDCG/MRR/p95 均在批准阈值内，permission_leak_count 必须为 0。

退出条件：candidate state=ready，validation/quality 均通过且人工审批留痕。

## 阶段 D：变更窗口原子激活

1. 记录当前 route_version，确认 previous 模型仍可调用。
2. 调用 activate 并传 expected_route_version；服务自动执行 advisory lock、write barrier、旧写任务 drain、最终 lag/门禁、单事务 route 切换、Alias reconcile 和 smoke。
3. 若返回 Alias drift 或 post-smoke 失败，不手改 Alias/route；保持 PostgreSQL route 为真相，立即告警并用正式 reconciler/rollback。
4. 验证 active/previous/route_version、`/ready`、flat/hierarchical、ACL、删除和新上传写入。

退出条件：线上 active 为 versioned generation，legacy 为 previous/retired，所有在线请求只出现完整旧对或新对。

## 阶段 E：回滚窗口观察

1. 持续 previous mirror，监控 mirror_lag_files、删除失败 generation IDs、Shadow/在线错误率和 p95。
2. 无损回滚必须 lag=0 且 previous Provider/指纹/Collection 可用；否则默认拒绝。只有经外部审批才可 `accept_rpo=true`，并记录具体 lag。
3. rollback_deadline 前禁止 release/drop legacy。

## 阶段 F：Legacy 退出与清理

1. 观察期结束且 route.previous 不再引用 legacy 后，先执行 `python -m openrag.cli.index_cleanup <generation_id>` dry-run。
2. 审批 route/Alias/任务/保留期/备份计划；高成本 generation 必须验证 backup manifest。
3. 可先 release 并验证重新 load/search；物理删除只能在受控 cleanup Job 注入独立凭据后执行：`--execute --confirm <完整generation_id> --operator-id <id>`。
4. 确认 Collection 均 absent、registry state=deleted，PostgreSQL 审计行保留。

## 最终验收

- [ ] 线上 active 是 versioned generation，legacy 不再读写。
- [ ] 历史内容已使用真实模型重建，无伪向量/未知指纹。
- [ ] route、Alias、健康、Trace、指标一致。
- [ ] 回滚窗口与 RPO 有记录，清理具备备份和审批证据。
- [ ] 生产 RBAC 的破坏性权限拒绝证据归档。
