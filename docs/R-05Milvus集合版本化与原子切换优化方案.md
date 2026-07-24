# R-05 Milvus 集合版本化与原子切换优化方案

> 状态：方案设计完成，尚未实施代码
>
> 适用分支：code-optimization
>
> 文档日期：2026-07-15
>
> 关联风险：R-04、R-05、R-11、R-14
>
> 方案范围：Milvus Chunk/L0/L1 索引版本化、旁路重建、验证、激活、回滚和清理；不包含代码实现

## 1. 执行摘要

R-05 的直接风险是：旧实现发现当前 Embedding 维度与存量 Milvus Collection 不一致时，会在普通运行时初始化路径中删除 Collection。R-04 已将该行为修改为抛出 VectorSchemaMismatchError，禁止自动删除，因此最危险的数据破坏路径已经止血。

但 R-05 尚未真正关闭。当前系统仍存在以下根本问题：

- Chunk 向量固定写入 openrag_chunks。
- L0/L1 向量固定写入 openrag_layers。
- Collection 没有 index generation、模型 revision 或完整 Embedding 指纹。
- API 和 Worker 根据各自进程中的当前环境变量决定模型和维度。
- 同维度但不同模型可以向同一 Collection 混写，Schema 校验无法发现。
- 模型或维度变化时只能停止使用旧 Collection，不能旁路重建。
- Chunk 与 L0/L1 没有一个共同的原子激活单元。
- 没有历史索引回滚窗口、重建进度、质量门禁和受控清理能力。

本方案的核心结论是：

1. 每次索引构建都创建不可变的 index generation。
2. 每个 generation 绑定一组物理 Collection、完整 Embedding 指纹和构建清单。
3. 新 generation 在旁路完成全量重建，不修改当前在线索引。
4. PostgreSQL 中的 active generation 路由记录是服务端权威指针。
5. 一次数据库事务原子绑定 Chunk Collection、Layer Collection、Embedding 配置和可选 ES 索引版本。
6. Milvus Alias 用作运维镜像和单 Collection 切换能力，但不承担两个 Collection 的跨资源事务。
7. API、普通 Worker 和业务请求永远不能创建、切换或删除 Collection。
8. 只有专用索引管理流程可以创建和切换；物理删除必须显式审批、满足冷静期并通过防误删校验。

目标结果不是“维度不一致时报错”，而是：

> Embedding 模型、维度或索引参数变化时，系统能够在旧索引继续服务的同时构建新索引，经完整性和检索质量验证后切换；切换失败可以回滚，任何普通业务流量都不能删除全量索引。

---

## 2. 当前实现与问题边界

### 2.1 当前存储结构

当前 Milvus 使用两个全局 Collection：

| 逻辑用途 | 当前物理名称 | 主要内容 |
|---|---|---|
| Chunk 检索 | openrag_chunks | chunk_id、file_id、text、embedding、page、level、block_type |
| 分层检索 | openrag_layers | layer_row_id、file_id、layer、text、embedding |

相关实现：

- openrag/src/openrag/vectorstore/milvus_store.py
- openrag/src/openrag/vectorstore/milvus_layer_store.py
- openrag/src/openrag/api/search_api.py
- openrag/src/openrag/worker/task_worker.py
- openrag/src/openrag/services/file_deletion.py

API 当前按进程缓存 EmbeddingEngine、MilvusStore 和 MilvusLayerStore。Worker 启动时初始化同类依赖。文件删除逻辑也会根据当前 Embedding 配置重新创建 Store。

### 2.2 R-04 已经完成的止血措施

code-optimization 工作区已经具备以下行为：

- 维度不一致时抛出 VectorSchemaMismatchError。
- Collection.drop 不再出现在初始化兼容性处理路径。
- Chunk 和 Layer 写入前执行维度、有限值、零向量、数量和 insert_count 校验。
- Worker 在 Embedding 或 Milvus 依赖不就绪时暂停领取新任务。
- 查询端对 Embedding、Milvus 和分层索引故障返回明确错误。
- 专项测试保证维度不一致不会调用 Collection.drop。

这些能力必须保留，是 R-05 的前置条件。

### 2.3 R-05 仍未解决的问题

#### 2.3.1 同维度模型不兼容

只比较向量维度不能识别向量空间是否兼容。例如模型 A 和模型 B 都输出 2560 维，但向量坐标语义不同：

~~~text
历史文档向量：模型 A
查询向量：模型 B
维度检查：通过
实际余弦相似度：没有可靠语义
表现：检索可运行，但准确率静默下降
~~~

#### 2.3.2 灰度期间跨模型混写

如果旧 Worker 使用模型 A、新 Worker 使用模型 B，且维度相同，两者会向同一个固定 Collection 写入。当前实体没有模型 revision 字段，污染发生后无法可靠识别和局部修复。

#### 2.3.3 Chunk 与 Layer 可能跨代

Chunk 和 Layer 是两个独立 Collection。即使分别使用 Alias，先切 Chunk、再切 Layer，中间仍存在短暂的跨代窗口。Milvus 的单个 alter_alias 可以原子重定向一个 Alias，但它不提供两个 Alias 的跨 Collection 事务。

#### 2.3.4 无法零停机升级

当前发生模型或维度变化时，安全行为只能是拒绝运行。系统没有候选 Collection、重建任务和激活路由，因此仍需要人工停机、清空或重建。

#### 2.3.5 删除和更新只面向“当前配置”

文件删除当前通过当前进程配置构造固定 Store。引入多 generation 后，隐私删除、软删除清理和文件更新必须覆盖 active、candidate 和 rollback-window generation，不能只处理一个 Collection。

---

## 3. 方案目标、非目标与基本假设

### 3.1 方案目标

R-05 实施后必须达到：

- 生产运行时不存在自动删除 Collection 的代码路径。
- 模型身份、revision、维度或预处理变化必然产生新 generation。
- active generation 不可原地改变 Schema 或 Embedding 指纹。
- 候选 generation 可以旁路构建、暂停、恢复、重试和取消。
- active 索引在候选构建期间持续提供检索。
- Chunk 与 L0/L1 以一个 generation 为单位被解析和激活。
- 每个请求使用单一 generation 的 EmbeddingEngine、Chunk Store 和 Layer Store。
- 激活前执行完整性、可用性、权限和质量门禁。
- 旧 generation 在回滚窗口内可用。
- 文件删除覆盖所有仍保留数据的 generation。
- Collection 删除只能由显式索引运维操作完成。
- 所有构建、激活、回滚和删除操作可审计。

### 3.2 第一阶段非目标

为控制改动范围，第一阶段不做：

- 不把每个 workspace 拆成独立 Milvus Collection。
- 不重构为一个统一的 Chunk/L0/L1 Collection。
- 不允许每个 workspace 独立选择 Embedding 模型。
- 不同时重构 Chunk 算法、Parser、目录摘要生成算法。
- 不把 Elasticsearch 全面纳入向量 generation，除非 Chunk 内容或 chunk_id 发生变化。
- 不以跨 PostgreSQL、Milvus、MinIO、Elasticsearch 的分布式事务为目标。
- 不使用向量相似度推断历史实体由哪个模型生成。

### 3.3 基本假设

- 当前 Embedding 模型是系统级配置，所有 workspace 共用一个 active generation。
- PostgreSQL 是索引生命周期、路由和构建进度的权威元数据源。
- Milvus Collection 是可重建派生数据，但重建成本高，不能随意销毁。
- MinIO 中的 L2 Chunk、L0 和 L1 文本是重建向量的主要内容来源。
- DocumentChunk 提供 Chunk 与文件、workspace、对象路径的映射。
- R-04 的真实 Embedding、强写入校验、任务重试和依赖预检已经存在。
- 生产 Milvus 为 2.4.x，支持 Collection Alias；项目当前部署使用 Milvus 2.4.17。

---

## 4. 设计原则

### 4.1 不可变 generation

generation 创建后，以下字段不可修改：

- Embedding provider。
- 模型名称和 revision。
- 模型身份指纹。
- 输出维度。
- 输入类型和归一化策略。
- 距离度量。
- Collection Schema 版本。
- Chunk 策略版本。
- L0/L1 内容策略版本。
- 物理 Collection 名称。

如果任何字段变化，必须创建新 generation。

### 4.2 控制面与数据面分离

控制面负责：

- 创建 generation。
- 创建 Collection。
- 安排重建。
- 验证候选索引。
- 激活和回滚。
- 退休和删除。

数据面负责：

- 使用已激活 generation 检索。
- 向任务绑定的 generation 写入。
- 删除指定 generation 中的文件实体。

普通 API 和 Worker 不拥有 DropCollection、CreateAlias 或 AlterAlias 权限。

### 4.3 fail-closed

遇到以下情况必须拒绝写入或检索，不能自动降级到未知索引：

- generation 不存在。
- active 路由缺失或指向非 active 状态。
- Collection 实际 Schema 与 generation 清单不一致。
- Embedding 指纹不一致。
- Chunk 与 Layer 指向不同 generation。
- 查询模型无法实例化。
- Alias 实际指向与控制面期望不一致且系统配置要求 Alias 一致。

### 4.4 旁路构建

候选 generation 只能写入自己的物理 Collection。构建失败不能修改 active generation，也不能删除 active 数据。

### 4.5 激活与删除分离

激活新 generation 只改变路由，不删除旧 generation。删除必须在回滚窗口结束后由独立流程执行。

---

## 5. 推荐目标架构

~~~text
                    PostgreSQL 控制面
        ┌────────────────────────────────────┐
        │ index_generations                  │
        │ index_generation_routes            │
        │ index_generation_files             │
        │ audit_logs                         │
        └────────────────────────────────────┘
                     │ 原子读取路由
                     ▼
          IndexRuntimeSnapshot(generation_id)
          ┌──────────────┼───────────────┐
          ▼              ▼               ▼
   EmbeddingEngine   Chunk Store      Layer Store
   指定模型/指纹      指定物理集合      指定物理集合
          │              │               │
          └──────────────┴───────────────┘
                         │
                         ▼
                       Milvus
       openrag_chunks_g_<generation>
       openrag_layers_g_<generation>

候选构建：
MinIO/DocumentChunk → Reindex Worker → Candidate Collections

激活：
短写入屏障 → 最终追平 → PostgreSQL 原子切换 route
           → 校验并同步 Milvus Alias 镜像
~~~

### 5.1 为什么 PostgreSQL 路由是权威指针

Milvus Alias 很适合将一个逻辑名称原子重定向到一个物理 Collection。官方文档也将它用于蓝绿更新。

但 OpenRag 当前有两个必须一致的 Collection：

- Chunk Collection。
- L0/L1 Layer Collection。

依次执行两个 alter_alias 不构成跨 Collection 原子事务。为了避免请求在切换窗口内读取“新 Chunk + 旧 Layer”，本方案使用 PostgreSQL 的单行路由记录一次绑定整组资源。

每个请求首先得到不可变的 IndexRuntimeSnapshot：

~~~text
generation_id
embedding_fingerprint
embedding_model_config_ref
chunk_collection_name
layer_collection_name
es_index_generation（可空）
route_version
~~~

请求后续不得重新解析 active 路由，保证一次请求内不会跨代。

### 5.2 Milvus Alias 的定位

建议保留以下 Alias：

- openrag_chunks_active
- openrag_layers_active

用途：

- 运维查看当前激活物理 Collection。
- 外部诊断工具使用稳定名称。
- 在单 Collection 操作中提供原子重定向。
- 作为 PostgreSQL 路由的可观测镜像。

但 OpenRag 服务端检索以 PostgreSQL 路由解析出的物理名称为准，不能依赖两个 Alias 恰好同时切换。

如果未来将 Chunk、L0、L1 合并成一个统一 Collection，则可以改为一个 Alias，并由 Milvus alter_alias 直接承担单资源原子切换。

---

## 6. Index Generation 数据模型

### 6.1 index_generations

建议新增索引版本主表：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID 或 String(64) | generation 唯一标识 |
| scope | String(32) | 第一阶段固定为 global |
| state | String(32) | 生命周期状态 |
| source_generation_id | FK，可空 | 基于哪个 generation 发起 |
| embedding_provider | String(64) | provider |
| embedding_model | String(256) | 模型名 |
| embedding_revision | String(256) | revision、镜像 digest 或发布版本 |
| embedding_dimension | Integer | 实际探针确认的维度 |
| embedding_fingerprint | String(64) | 规范化配置 SHA-256 |
| embedding_config_ref | String(256) | 密钥或模型注册表引用，不保存 API Key |
| vector_normalization | String(32) | none、l2 等 |
| distance_metric | String(16) | 当前为 COSINE |
| schema_version | Integer | Milvus Schema 版本 |
| chunk_policy_revision | String(128) | Chunk 策略身份 |
| hierarchy_policy_revision | String(128) | L0/L1 内容策略身份 |
| chunk_collection_name | String(255) | 物理 Chunk Collection |
| layer_collection_name | String(255)，可空 | 物理 Layer Collection |
| es_generation | String(128)，可空 | 需要同步切换 ES 时使用 |
| manifest | JSON | 完整不可变清单 |
| source_watermark_at | TIMESTAMP | 首次扫描水位 |
| expected_file_count | BigInteger | 预期文件数 |
| expected_chunk_count | BigInteger | 预期 Chunk 数 |
| expected_layer_count | BigInteger | 预期 Layer 数 |
| indexed_file_count | BigInteger | 已完成文件数 |
| indexed_chunk_count | BigInteger | 已写 Chunk 数 |
| indexed_layer_count | BigInteger | 已写 Layer 数 |
| failed_file_count | BigInteger | 最终失败文件数 |
| created_by | FK user | 创建者 |
| created_at | TIMESTAMP | 创建时间 |
| build_started_at | TIMESTAMP，可空 | 构建开始 |
| ready_at | TIMESTAMP，可空 | 通过门禁 |
| activated_at | TIMESTAMP，可空 | 激活时间 |
| retired_at | TIMESTAMP，可空 | 退休时间 |
| delete_after | TIMESTAMP，可空 | 最早允许删除时间 |
| last_error_code | String(64)，可空 | 稳定错误码 |
| last_error | Text，可空 | 脱敏错误 |
| lock_version | Integer | 乐观并发控制 |

约束：

- embedding_fingerprint 与 manifest 创建后不可变。
- Collection 名必须全局唯一。
- 同一 scope 最多一个 active generation。
- active、ready、retired generation 不允许修改构建计数以外的不可变字段。
- deleted 状态的 generation 不允许重新激活。

### 6.2 index_generation_routes

建议使用一条全局路由记录：

| 字段 | 类型 | 说明 |
|---|---|---|
| scope | String(32)，主键 | 第一阶段为 global |
| active_generation_id | FK | 当前检索和新写入目标 |
| previous_generation_id | FK，可空 | 回滚窗口目标 |
| route_version | BigInteger | 每次切换递增 |
| activated_at | TIMESTAMP | 最近切换时间 |
| activated_by | FK user | 操作者 |
| rollback_deadline | TIMESTAMP，可空 | 回滚保留截止时间 |
| write_barrier | Boolean | 最终追平期间暂停新写入 |
| updated_at | TIMESTAMP | 路由更新时间 |

读取此行即可获得一组一致的 generation 资源。

### 6.3 index_generation_files

用于可恢复的文件级重建：

| 字段 | 类型 | 说明 |
|---|---|---|
| generation_id | FK | 目标 generation |
| file_id | FK | 文件 |
| workspace_id | FK | 工作区 |
| state | String(32) | pending、running、success、retry、failed、deleted |
| source_content_hash | String(64)，可空 | 构建时内容身份 |
| source_updated_at | TIMESTAMP | 构建所基于的文件版本 |
| expected_chunk_count | Integer | 预期 Chunk |
| written_chunk_count | Integer | 实写 Chunk |
| expected_layer_count | Integer | 预期 Layer |
| written_layer_count | Integer | 实写 Layer |
| retry_count | Integer | 重试次数 |
| next_retry_at | TIMESTAMP，可空 | 延迟重试 |
| worker_id | String(64)，可空 | 当前 Worker |
| heartbeat_at | TIMESTAMP，可空 | 心跳 |
| error_code | String(64)，可空 | 稳定错误码 |
| error | Text，可空 | 脱敏错误 |
| completed_at | TIMESTAMP，可空 | 完成时间 |

唯一约束：

- generation_id + file_id 唯一。

该表同时承担：

- 构建进度。
- 幂等键。
- 失败重试。
- 最终追平判断。
- 回滚前差异核对。

### 6.4 与现有表的关系

- files.processing_status 继续表示在线文档处理状态，不表示候选 generation 构建状态。
- 候选构建失败不能把在线文件改成 failed，因为 active generation 仍可能正常服务。
- DocumentChunk 不为每个 generation 复制一套元数据；同一个 chunk_id 可以存在于多个物理 Collection。
- EvalRun 已有 index_version 字段，后续必须填入真实 generation_id。
- TraceRun 的 search_config_snapshot 应记录 route_version、generation_id、Collection 名和 Embedding 指纹。

---

## 7. Embedding 指纹

### 7.1 指纹字段

建议对以下规范化 JSON 计算 SHA-256：

~~~text
provider
model
model_revision
model_identity
dimension
input_type
encoding_format
normalization
distance_metric
query_prefix_revision
document_prefix_revision
text_preprocess_revision
sdk_contract_revision
~~~

不纳入指纹：

- API Key。
- 临时超时。
- 批次大小。
- 重试次数。
- 日志级别。

base_url 是否纳入取决于其语义：

- 如果只是同一模型的网关地址，不纳入模型身份。
- 如果不同 base_url 可能提供同名但不同权重的模型，则必须通过 model_revision 或 model_identity 区分。

### 7.2 model_identity

优先级建议：

1. 模型服务返回的不可变 revision 或 digest。
2. 内部模型注册表版本。
3. 容器镜像 digest。
4. 本地模型文件清单的稳定哈希。
5. 最后才使用 provider + model 名称。

不得只依赖模型显示名称。

### 7.3 运行时校验

创建 generation 时和 Worker 使用前均执行：

- Embedding 探针成功。
- 实际维度等于 generation.embedding_dimension。
- 输出为有限非零向量。
- 运行时计算出的指纹等于 generation.embedding_fingerprint。

任何不一致都必须阻止写入该 generation。

### 7.4 对标结论

OpenViking 当前会记录 provider、model、dimension 和 model identity，这一思路可用于 OpenRag 的 Embedding 指纹。

但 OpenViking 在非空 Collection 的元数据不匹配时仍记录 warning 并继续运行。OpenRag 不应沿用这一行为；非空索引的指纹不匹配必须 fail-closed。

RAGFlow 提供了抽样重嵌入并比较新旧向量余弦相似度的兼容性检查。OpenRag 可以将其作为迁移评估信息，但规则必须是：

- 指纹不同仍然创建新 generation。
- 高相似度不能授权原地混写。
- 抽样兼容性不能替代全量索引重建和离线检索评测。

---

## 8. Collection 命名与 Schema

### 8.1 物理名称

建议：

~~~text
openrag_chunks_g_<generation_short_id>
openrag_layers_g_<generation_short_id>
~~~

要求：

- 只使用小写字母、数字和下划线。
- generation_short_id 至少保留足够碰撞空间。
- 完整名称保存在 index_generations 中，不能靠字符串反解析业务状态。
- Collection 名一旦写入 generation 不再修改。

### 8.2 Alias 名称

~~~text
openrag_chunks_active
openrag_layers_active
~~~

Alias 不包含模型名称或维度，避免应用依赖物理实现。

### 8.3 Collection 元数据

PostgreSQL 是权威源，但 Collection description 或 properties 中仍建议写入：

- generation_id。
- embedding_fingerprint。
- schema_version。
- created_at。
- source_generation_id。
- collection_role：chunks 或 layers。

Store 初始化时同时检查 PostgreSQL 清单和 Milvus 实际 Schema。

### 8.4 是否增加 generation_id 实体字段

第一阶段不强制在每条 Milvus 实体中增加 generation_id，因为一个物理 Collection 只属于一个 generation，重复字段没有过滤价值。

建议补充 workspace_id 到 Chunk 和 Layer Schema，用于：

- 更直接的多租户过滤。
- 审计和孤儿检测。
- 避免只依赖超长 file_id 表达式。

但这属于 Schema 升级，必须随首个版本化 generation 一起生效，不能原地修改 legacy Collection。

---

## 9. 运行时解析与写入契约

### 9.1 IndexRuntimeSnapshot

新增统一解析对象，至少包含：

- generation_id。
- route_version。
- Embedding 配置快照和指纹。
- chunk_collection_name。
- layer_collection_name。
- hierarchy_enabled。
- es_generation。

API 一次检索只能解析一次。RetrievalService、EmbeddingEngine、MilvusStore、MilvusLayerStore 全部由该快照创建或从 generation 缓存取得。

### 9.2 缓存策略

可以按 generation_id 缓存运行时对象，但必须满足：

- 缓存键包含 generation_id 和 embedding_fingerprint。
- route_version 变化后，新请求解析新 generation。
- 已开始请求继续使用旧快照，不中途切换。
- 旧 generation 在回滚窗口内不得立即释放或删除。
- 缓存失效只关闭客户端句柄，不删除 Collection。

### 9.3 API 启动行为

API 启动时必须做 active generation readiness：

- PostgreSQL 路由存在。
- active generation 状态正确。
- Embedding 指纹可实例化。
- Chunk Collection 存在、Schema 正确且已加载。
- 启用 L0/L1 时 Layer Collection 存在、Schema 正确且已加载。

可以保留请求级懒创建客户端句柄，但懒创建只能连接和验证已存在资源，不能创建、重建或删除 Collection。

### 9.4 Worker 任务绑定

每个文档处理或重建任务在领取时必须绑定 generation_id。

禁止以下行为：

- 任务开始时使用 generation A，执行中路由变化后写入 generation B。
- Worker 根据当前环境变量自行猜测目标 Collection。
- 未携带 generation_id 的新任务写入任意 versioned Collection。

普通在线文档任务默认绑定领取时的 active generation。候选重建任务显式绑定 candidate generation。

### 9.5 文件删除

文件删除必须查询 PostgreSQL generation registry，目标至少包括：

- active generation。
- building、validating、ready candidate。
- previous generation 和回滚窗口内 retired generation。
- 任何尚未物理删除且可能包含该文件的 generation。

删除流程应记录每个 generation 的结果。只删除 active generation 不满足数据删除要求。

---

## 10. Generation 生命周期状态机

建议状态：

~~~text
draft
  → provisioning
  → building
  → reconciling
  → validating
  → ready
  → activating
  → active
  → retired
  → deleting
  → deleted

任意构建状态
  → failed
  → building（人工或自动恢复）

ready / active
  → failed 仅表示验证或运行故障，不自动删除数据
~~~

### 10.1 draft

- 仅保存目标配置。
- 尚未创建物理 Collection。
- 可取消。

### 10.2 provisioning

- 创建 Chunk 和 Layer Collection。
- 创建向量索引。
- 写入 Collection 元数据。
- 验证实际 Schema。
- 任何一步失败都保留已创建资源并进入 failed，由显式清理流程处理。

### 10.3 building

- 记录 source_watermark_at。
- 枚举当前未删除、需要向量检索的文件。
- 创建 index_generation_files。
- 并行重嵌入并写入候选 Collection。

### 10.4 reconciling

- 处理首次水位之后发生的上传、更新、重新解析和删除。
- 对比 source_updated_at、内容 hash 和文件 deleted_at。
- 重复执行直到差异为零或达到激活屏障。

### 10.5 validating

- 停止修改 generation 清单。
- 执行完整性、Schema、检索质量和容量门禁。
- 失败返回 building 或 failed，不影响 active。

### 10.6 ready

- 候选索引已具备激活条件。
- 仍不接收在线查询。
- 如果源数据继续变化，需要在激活前再次追平。

### 10.7 activating

- 获取全局索引切换锁。
- 开启短写入屏障。
- 最终追平。
- 加载候选 Collection。
- 原子更新 PostgreSQL 路由。
- 同步 Alias 镜像。
- 解除写入屏障。

### 10.8 active

- 新检索和新文档任务默认绑定该 generation。
- 旧 active 进入 retired，并成为 previous_generation。

### 10.9 retired

- 不接收正常检索。
- 回滚窗口内保留 Collection 和模型可用性。
- 持续接收删除传播。
- 是否追平新变更取决于回滚策略。

### 10.10 deleting / deleted

- 只有运维控制面可以进入。
- 删除前必须满足防误删条件。
- deleted 记录永久保留用于审计，物理 Collection 已不存在。

---

## 11. 候选索引构建流程

### 11.1 创建 generation

输入：

- 目标 Embedding 模型配置引用。
- revision 或 model identity。
- 维度。
- Schema 版本。
- Chunk 和 hierarchy 策略 revision。
- 回滚窗口。

控制面执行：

1. 探测目标 Embedding 服务。
2. 计算指纹。
3. 与 active generation 对比。
4. 如果指纹完全一致且 Schema 不变，默认拒绝无意义重建；可由显式 force 允许灾备演练。
5. 生成物理 Collection 名。
6. 写入 draft generation。

### 11.2 Provision Collection

1. 使用索引管理凭据连接 Milvus。
2. 确认物理名称不存在。
3. 创建 Chunk Collection。
4. 创建 Layer Collection。
5. 创建 IVF_FLAT 或目标索引。
6. 验证字段、主键、维度、metric 和 index 参数。
7. 写入并读取 generation 元数据。
8. 将状态转为 building。

如果名称已存在但不属于相同 generation，立即失败，禁止接管未知 Collection。

### 11.3 内容来源

Embedding-only 重建不重新运行 Parser 和 ChunkEngine，直接使用现有规范内容：

- DocumentChunk 定位 L2 内容。
- MinIO 或本地层级存储读取完整 Chunk 文本。
- files.l0_path 读取 L0。
- files.l1_path 读取 L1。

text_preview 可能被截断，不能作为默认全量 Embedding 输入。

### 11.4 文件级幂等

每个 generation + file_id 只有一个状态行。

重试前：

- 删除候选 Collection 中该 file_id 的既有 Chunk 和 Layer。
- 重新生成全部向量。
- 整批校验后写入。
- 核对写入数量。
- 保存 source_content_hash 和 source_updated_at。

候选失败不能调用 active Collection 的 delete_by_file_id。

### 11.5 并发与限流

候选重建不得挤占在线任务全部资源：

- 使用独立 queue，例如 reindex。
- 设置独立并发上限。
- 设置 Embedding QPS 和 token 预算。
- Milvus 写入批次可配置但记录在 Trace。
- 在线任务优先级高于历史重建。
- Provider 或 Milvus 故障时暂停领取，不制造失败风暴。

### 11.6 变更追平

不建议第一阶段长期双写所有 generation。采用“水位扫描 + 最终短写入屏障”：

1. building 开始记录 source_watermark_at。
2. 完成首次全量扫描。
3. 找出 updated_at 晚于水位或内容 hash 不一致的文件。
4. 重建变化文件。
5. 传播删除 tombstone。
6. 重复追平。
7. 激活时短暂停止创建新的文档写任务。
8. 等待正在运行的 active 写任务结束。
9. 执行最后一次差异扫描。
10. 差异为零后切换路由。

这样检索不中断，只在最终激活时短暂暂停索引写入。

---

## 12. 激活门禁

候选 generation 只有全部通过才可进入 ready。

### 12.1 配置门禁

- Embedding provider 可访问。
- 模型 revision 与清单一致。
- 实际维度一致。
- 指纹一致。
- Chunk 与 Layer 使用同一指纹。

### 12.2 Milvus 门禁

- 两个物理 Collection 均存在。
- Collection 元数据中的 generation_id 正确。
- Schema、主键、向量维度和 metric 正确。
- Index 构建完成。
- Collection load 成功。
- 最小探针搜索成功。
- 随机读取实体字段成功。

### 12.3 数据完整性门禁

默认强条件：

- 所有未删除且应检索文件都有 success 状态。
- failed_file_count 为 0。
- 每个文件 written_chunk_count 等于 expected_chunk_count。
- 启用分层检索时 written_layer_count 等于实际存在的 L0/L1 数。
- Collection 实体数量与 generation 汇总一致。
- 不存在未知 file_id。
- 不存在已删除文件实体。
- 抽样或全量向量均为有限非零值。
- source_updated_at 和内容 hash 与最终水位一致。

Milvus实体数可能受删除标记和 compaction 可见性影响，不能只依赖 num_entities。应结合 generation 文件清单、按 file_id 查询和写入回执验证。

### 12.4 检索质量门禁

使用现有 EvalService：

- 在相同 gold query 集上运行 active 和 candidate。
- EvalRun.index_version 写入 generation_id。
- 比较 Recall@K、nDCG@K、MRR、HitRate、no-answer precision。
- 目录类、专有名词类、数字类、长尾类分别统计。
- L0/L1 导航单独统计。

阈值应由业务基线配置，不建议把任意公共数值写死。推荐默认发布原则：

- 核心指标不能出现未批准的显著回退。
- 权限泄漏命中必须为 0。
- 空结果率不能异常上升。
- p95 延迟和错误率必须在容量预算内。

### 12.5 Shadow 门禁

可在正式激活前对少量真实查询执行 candidate shadow search：

- 用户响应仍来自 active。
- candidate 结果只写 Trace。
- 比较 top-k overlap、分数分布、空结果率和延迟。
- 不保存超出当前 Trace 策略允许的原始敏感 query。

Shadow 通过后仍不能跳过离线 gold 评测。

---

## 13. 激活协议

### 13.1 前置条件

- candidate.state = ready。
- active generation 未在候选构建期间被其他流程替换。
- candidate Collection 已加载。
- 运行环境可以按 candidate 指纹创建 EmbeddingEngine。
- 无其他 activating、rollback 或 deleting 操作。
- 最终追平预计可在允许的写入屏障内完成。

### 13.2 切换步骤

1. 获取 PostgreSQL advisory lock 或索引路由专用互斥锁。
2. 对 index_generation_routes 执行 SELECT FOR UPDATE。
3. 再次检查 active_generation_id 与 source_generation_id。
4. 设置 write_barrier = true。
5. Broker 暂停领取新的索引写任务，但检索继续。
6. 等待已绑定旧 active 的文档写任务完成或安全取消。
7. 最终处理新增、更新和删除差异。
8. 再次执行快速完整性门禁。
9. 确认 candidate 两个 Collection load 成功。
10. 在一个 PostgreSQL 事务中：
    - candidate 变为 active。
    - old active 变为 retired。
    - route.previous_generation_id = old active。
    - route.active_generation_id = candidate。
    - route.route_version + 1。
    - 写入 rollback_deadline。
11. 提交事务。
12. 同步两个 Milvus active Alias。
13. 读取 Alias 并校验实际指向。
14. 设置 write_barrier = false。
15. 发布路由变更事件或等待 API/Worker 根据 route_version 刷新。
16. 运行激活后 smoke test。

### 13.3 Alias 同步失败

因为服务端以 PostgreSQL 路由为准，Alias 同步失败不能导致路由回到未知状态。

处理规则：

- 记录 alias_mirror_out_of_sync 告警。
- 重试同步。
- 运维工具显示 PostgreSQL active 与 Alias 实际指向。
- 外部直接使用 Alias 的客户端在修复前应被阻止或明确告警。

如果未来正式承诺外部客户端以 Alias 检索，则 Alias 同步必须提升为激活硬门禁，并重新设计两个 Alias 的一致性协议。

---

## 14. 回滚设计

### 14.1 回滚前提

旧 generation 必须：

- 仍为 retired。
- Collection 存在并可加载。
- Embedding 模型仍可调用。
- 指纹校验通过。
- 不包含应已删除的文件。
- 已追平激活后的必要增量，或业务接受明确的回滚 RPO。

### 14.2 为什么不能无条件瞬时回滚

新 generation 激活后，如果新上传文件只写入新 generation，旧 generation 会缺少这些文件。此时直接切回旧 generation 会造成新内容消失。

因此回滚必须定义 RPO，而不能只保留旧 Collection。

### 14.3 推荐回滚窗口策略

在 rollback_deadline 之前：

- 所有文件删除同步传播到 active 和 previous。
- 新增或更新文件为 previous 创建异步 mirror reindex 任务。
- 单独监控 previous_generation_lag_files。
- 在线文件 completed 只以 active 写入成功为准，previous 的镜像状态独立记录。

回滚前：

1. 开启写入屏障。
2. 等待 previous 差异追平到 0。
3. 验证旧模型和 Collection。
4. 原子切换 PostgreSQL 路由。
5. 同步 Alias。
6. 解除屏障。

如果 previous lag 不为 0 且无法追平，回滚操作必须拒绝或要求操作者显式接受数据缺口。

### 14.4 回滚后处理

- 被回滚的新 generation 进入 retired，不自动删除。
- 保存失败原因和相关 Trace。
- 暂停继续向失败 generation 镜像。
- 评估是修复后重新激活，还是创建新的 generation。

---

## 15. 删除、退休与备份

### 15.1 防误删条件

物理删除前必须同时满足：

- generation.state = retired。
- 当前不是 active_generation_id。
- 当前不是 previous_generation_id。
- 不被任何 Alias 指向。
- 不存在 running、retry 或 pending 构建任务。
- rollback_deadline 已过。
- delete_after 已过。
- 删除传播任务已完成。
- 操作者提供完整 generation_id 二次确认。
- dry-run 输出将删除的 Collection、实体数、创建时间和最后使用时间。

任一条件不满足都拒绝删除。

### 15.2 权限隔离

建议至少拆分两个 Milvus 身份：

运行时身份：

- DescribeCollection。
- GetStatistics。
- Load、GetLoadState。
- Search、Query。
- Insert、Delete、Flush。
- 不授予 DropCollection、CreateAlias、DropAlias、AlterAlias。

索引管理身份：

- CreateCollection、CreateIndex。
- Load、Release。
- CreateAlias、AlterAlias、DropAlias。
- DropCollection 仅供独立清理命令使用。

如果 Milvus 2.4.x 的内置权限组过宽，应使用自定义角色逐项授权。

### 15.3 备份定位

Milvus Backup 可以在不停止在线服务的情况下备份 Collection，也支持恢复成带后缀的新 Collection。

建议：

- active generation 定期备份。
- 高成本或不可快速重建的 generation 在删除前备份。
- 备份不是激活门禁的唯一保障，也不能替代版本化。
- 恢复后的 Collection 必须重新注册为新 generation 并通过全部验证，不能直接接管 active Alias。

### 15.4 默认保留策略

建议初始策略：

- previous generation 至少保留一个明确回滚窗口。
- 至少保留最近一个已验证 generation。
- 保留时长按数据规模和重建耗时配置，不写死在代码。
- 仅释放 Milvus 内存不等于删除 Collection；过回滚热期后可以先 release，再延迟物理删除。

---

## 16. Elasticsearch 与跨存储一致性

### 16.1 Embedding-only 迁移

如果以下内容均不变化：

- Chunk 文本。
- chunk_id。
- Parser 结果。
- Chunk 策略。
- ES analyzer 和 mapping。

则新向量 generation 可以复用当前 ES 索引。ES 不需要随 Embedding 模型切换。

### 16.2 Chunk 策略变化

如果 chunk_policy_revision 变化，chunk_id 或内容可能变化。此时：

- 不能复用旧 DocumentChunk 和 ES 索引。
- 必须创建内容 generation。
- PostgreSQL DocumentChunk、MinIO L2、Milvus 和 ES 需要共同迁移。

第一阶段 R-05 应拒绝在同一任务中改变 Chunk 策略，并返回需要内容索引迁移的明确错误。不要把更大的跨存储重建偷偷塞入 Embedding 迁移。

### 16.3 L0/L1 内容变化

R-05 默认只重新 Embedding 已有 L0/L1 文本。如果 hierarchy_policy_revision 变化并要求重新生成摘要，应创建新的内容 generation，再基于新内容生成向量。

---

## 17. 需要调整的项目组件

以下是实施时的建议职责，不代表本方案已经修改代码。

### 17.1 新增 IndexGenerationService

职责：

- 创建和读取 generation。
- 校验状态转换。
- 计算并校验指纹。
- 管理 active route。
- 执行并发锁。
- 生成审计事件。

### 17.2 新增 IndexRuntimeResolver

职责：

- 从 route 原子读取 active generation。
- 构建 IndexRuntimeSnapshot。
- 按 generation 缓存 EmbeddingEngine 和 Store。
- 校验 route_version。
- 为查询和普通写任务提供统一依赖。

### 17.3 收紧 MilvusStore / MilvusLayerStore

建议拆分：

- Runtime Store：只连接、验证、检索、插入、删除指定物理 Collection。
- Collection Provisioner：只由控制面调用，负责创建 Schema 和 Index。
- Collection Cleaner：独立运维命令，负责受控删除。

MilvusStore 构造函数不得根据“不存在”自动创建 active Collection。运行时缺失应报错。

### 17.4 修改 Search API

- 不再使用唯一全局 _vector_store 指向固定 Collection。
- 每次请求获得 generation 快照。
- query embedding 与 Store 必须来自同一快照。
- Trace 记录 generation_id、route_version、模型 revision、Collection。
- 路由或指纹异常返回结构化 503。

### 17.5 修改 Worker

- 普通任务领取时绑定 active generation_id。
- 重建任务显式绑定 candidate generation_id。
- Worker 不使用全局环境变量决定任务目标。
- Worker 在执行前校验目标 generation 状态和指纹。
- 激活写入屏障期间不领取新的索引写任务。

### 17.6 修改文件删除服务

- 从 registry 获取所有需要清理的 generation。
- 每个 generation 独立记录删除结果。
- 失败进入重试和告警。
- 不因某个 retired Collection 暂时不可用而跳过 active 删除。

### 17.7 修改 Trace 和 Eval

所有相关 Trace 至少记录：

- generation_id。
- route_version。
- embedding_fingerprint。
- embedding_model 和 revision。
- chunk_collection_name。
- layer_collection_name。
- candidate 或 active 角色。

EvalRun.index_version 必须使用 generation_id，禁止手工填写与真实索引无关的标签。

---

## 18. 管理接口与任务设计

### 18.1 建议管理操作

仅管理员或内部运维身份可用：

- 创建 generation。
- 开始 provisioning。
- 开始或恢复 build。
- 暂停 build。
- 取消尚未激活的 generation。
- 运行 validate。
- 激活 ready generation。
- 回滚到 previous generation。
- retire generation。
- 查看 delete dry-run。
- 显式删除 retired generation。

### 18.2 幂等要求

- 创建操作支持 client_request_id。
- build 重复调用只恢复未完成文件。
- validate 可重复执行并保存每次报告。
- activate 使用 source_generation_id 和 route_version 进行 compare-and-swap。
- rollback 同样使用 route_version。
- delete 重复调用对已删除 generation 返回成功状态，不误删同名未知资源。

### 18.3 任务类型

可以在现有 Broker 基础上增加：

- reindex_generation_file。
- reconcile_generation_file。
- mirror_previous_generation_file。
- purge_file_from_generations。

全局协调状态保存在 index_generations，不应只存在某个 Task.payload 中。

---

## 19. 可观测性

### 19.1 指标

建议增加：

- openrag_index_generation_state。
- openrag_index_generation_files_total。
- openrag_index_generation_files_completed_total。
- openrag_index_generation_files_failed_total。
- openrag_index_generation_chunks_written_total。
- openrag_index_generation_layers_written_total。
- openrag_index_generation_build_lag_files。
- openrag_previous_generation_lag_files。
- openrag_index_activation_total。
- openrag_index_rollback_total。
- openrag_index_activation_duration_seconds。
- openrag_index_route_version。
- openrag_index_alias_mismatch。
- openrag_index_fingerprint_mismatch。
- openrag_index_orphan_entities。
- openrag_index_delete_failures_total。

### 19.2 日志字段

统一结构化字段：

- generation_id。
- source_generation_id。
- route_version。
- collection_role。
- collection_name。
- file_id。
- workspace_id。
- embedding_fingerprint。
- task_id。
- worker_id。
- transition_from。
- transition_to。
- error_code。

### 19.3 告警

高优先级告警：

- active route 不存在。
- active Collection 不存在。
- active 指纹不一致。
- Chunk/Layer generation 不一致。
- Alias 与 PostgreSQL route 不一致。
- active 写入失败率超过阈值。
- 删除传播持续失败。
- candidate 构建长期无进展。
- rollback window 即将结束但 previous lag 不为 0。

---

## 20. 故障场景与预期行为

| 故障场景 | 预期行为 |
|---|---|
| 候选 Embedding Provider 不可用 | 暂停候选任务，active 继续服务 |
| 候选 Collection 创建一半失败 | generation 进入 failed，active 不变，不自动删除已创建资源 |
| 单文件重建失败 | 只标记 candidate 文件失败，可重试，不修改 files.processing_status |
| 候选 Chunk 写完但 Layer 失败 | 该文件不算 success，候选不能通过门禁 |
| 构建期间上传新文件 | 由水位追平捕获 |
| 构建期间删除文件 | 从候选及所有保留 generation 传播删除 |
| 激活前 active 已被其他操作替换 | compare-and-swap 失败，拒绝激活 |
| 激活时 API 正在查询旧 generation | 请求完成旧快照；新请求读取新路由 |
| 第一个 Alias 切换成功、第二个失败 | 服务仍按 PostgreSQL物理名称工作，告警并重试 Alias 镜像 |
| 新 generation 激活后质量异常 | 先追平 previous 差异，再原子回滚 |
| previous 模型已不可用 | 回滚门禁失败，不宣称可回滚 |
| API 配置模型与 active 指纹不同 | readiness 失败，拒绝使用未知模型查询 |
| 普通 API 尝试 DropCollection | Milvus RBAC 拒绝 |
| 清理命令误指 active generation | 控制面防误删校验拒绝 |
| PostgreSQL 暂时不可用 | 不能解析新请求路由；按服务策略返回 503，不能猜测 Collection |

---

## 21. 测试计划

### 21.1 单元测试

- 指纹对字段顺序稳定。
- API Key、超时和批次大小不改变指纹。
- 模型 revision、维度、归一化变化会改变指纹。
- Collection 名生成合法且不碰撞。
- generation 状态机拒绝非法转换。
- active generation 不允许修改不可变清单。
- RuntimeSnapshot 一次绑定全部资源。
- 删除目标覆盖 active、candidate、previous 和保留 retired。

### 21.2 数据库测试

- 同一 scope 只能有一个 active。
- route 切换事务同时更新 active 和 previous。
- 两个操作者并发激活只有一个成功。
- route_version compare-and-swap 正确。
- generation + file_id 幂等唯一。
- 任务恢复不会重复累计成功计数。

### 21.3 Milvus 集成测试

使用与生产一致的 Milvus 2.4.17：

- 创建两个不同维度的 generation。
- 两个 Collection 可同时存在。
- Runtime Store 不会自动创建或删除 Collection。
- 指纹或 Schema 不一致时失败。
- alter_alias 后单 Alias 指向正确。
- 模拟第二个 Alias 切换失败，服务仍使用同一 PostgreSQL generation。
- active、candidate 同时 load 和搜索。
- release retired 不影响 active。

### 21.4 构建测试

- 从现有 MinIO L2 内容重建 Chunk 向量。
- 从 L0/L1 内容重建 Layer 向量。
- text_preview 截断不影响完整文本来源。
- 中断后从 index_generation_files 恢复。
- 文件更新被 reconcile 捕获。
- 文件删除不会在 candidate 留下实体。
- 写入数量不一致时文件不能 success。

### 21.5 激活并发测试

- 激活过程中持续发起检索。
- 每个请求只看到完整旧 generation 或完整新 generation。
- 不出现新 Chunk + 旧 Layer。
- 灰度 API Pod 能根据 route_version 刷新。
- 旧请求完成后旧句柄可以安全保留到过期。
- 激活屏障期间新写任务不被领取。

### 21.6 回滚测试

- 无增量时回滚。
- 有新增文件且 previous 已追平时回滚。
- previous lag 非零时拒绝无损回滚。
- 删除文件不会在回滚后重新出现。
- previous 模型不可用时回滚失败但 active 保持不变。

### 21.7 安全测试

- API Milvus 账号不能 DropCollection。
- Worker 账号不能 AlterAlias。
- 非管理员不能创建、激活、回滚或删除 generation。
- 删除 dry-run 不产生写操作。
- active、previous、Alias 指向对象均无法被清理命令删除。
- 审计日志记录操作者和完整 generation_id。

### 21.8 质量测试

- 同维度不同模型必须创建新 generation。
- 新旧模型抽样相似度仅作为报告，不允许原地复用。
- EvalRun 准确记录 index_version。
- RAG 质量指标门禁生效。
- 权限不可见文件在 active 和 candidate 的测试召回结果均为 0。

---

## 22. 分阶段实施顺序

### 阶段 0：保留 R-04 安全基线

- 禁止自动 drop。
- 保留 VectorSchemaMismatchError。
- 保留强 Embedding 和写入校验。
- 增加 Milvus RBAC，先移除运行时 DropCollection 权限。

退出条件：

- API、Worker 和文件删除请求均无法删除 Collection。

### 阶段 1：Generation 元数据与 legacy 注册

- 新增 index_generations。
- 新增 index_generation_routes。
- 新增 index_generation_files。
- 实现指纹。
- 把现有固定 Collection 注册为 legacy generation。

退出条件：

- 系统可以从 PostgreSQL 描述当前实际索引和模型身份。

### 阶段 2：运行时路由

- 引入 IndexRuntimeResolver。
- Search API 使用 RuntimeSnapshot。
- Worker 任务绑定 generation_id。
- 文件删除按 registry 解析目标。
- Trace 写入 generation 信息。

退出条件：

- 运行时不再由固定常量和当前环境变量单独决定存储目标。

### 阶段 3：版本化 Collection Provisioning

- 创建独立 Provisioner。
- 创建 versioned Chunk/Layer Collection。
- 写入并校验元数据。
- 建立 Alias 镜像。

退出条件：

- 可以同时存在 legacy active 和空 candidate，互不影响。

### 阶段 4：可恢复旁路重建

- 增加重建任务队列。
- 从现有 L2/L0/L1 内容生成候选向量。
- 增加文件级状态、重试、限流和水位追平。

退出条件：

- 重启 Worker 后候选构建可以继续。
- 候选失败不影响 active。

### 阶段 5：验证门禁

- 完整性验证。
- Milvus load/search 探针。
- EvalService 新旧 generation 对比。
- Shadow 查询。

退出条件：

- 不完整或质量不达标的 generation 无法进入 ready。

### 阶段 6：激活与回滚

- 写入屏障。
- PostgreSQL 原子路由切换。
- Alias 同步和 reconciliation。
- previous 增量镜像。
- 回滚门禁。

退出条件：

- 并发检索下只看到完整旧代或新代。
- 回滚不会丢失已删除或已追平的新文件。

### 阶段 7：退休、备份与显式清理

- 保留窗口。
- release 策略。
- delete dry-run。
- 二次确认和审计。
- 可选 Milvus Backup。

退出条件：

- active 或 previous generation 在任何情况下都无法被误删。

---

## 23. Legacy Collection 迁移方案

现有 openrag_chunks 和 openrag_layers 不能假设具有完整指纹，也不建议先原地重命名。

推荐迁移：

1. 部署 R-04 fail-closed 版本。
2. 对现有 Collection 做备份或至少记录实体统计和 Schema。
3. 根据当前生产配置计算 legacy 指纹。
4. 将现有物理名称直接注册为 legacy generation。
5. PostgreSQL active route 指向 legacy generation。
6. 部署 RuntimeResolver，验证检索结果不变。
7. 创建第一个 versioned candidate。
8. 使用真实模型从规范内容全量重建。
9. 通过完整性和质量门禁。
10. 激活 versioned candidate。
11. 保留 legacy generation 作为 previous。
12. 回滚窗口结束后再显式清理 legacy Collection。

需要注意：

- legacy Collection 缺少可靠模型身份时，不应仅根据当前环境变量断言其绝对可信。
- 如果历史上可能出现 R-04 伪向量污染，首个 candidate 必须使用真实模型全量重建。
- 不使用向量分布猜测哪些 legacy 实体是伪向量。

---

## 24. 发布与回滚 Runbook

### 24.1 发布前

- 备份 PostgreSQL。
- 确认 Milvus 版本和 PyMilvus 兼容。
- 确认运行时账号无 DropCollection 权限。
- 确认 active legacy Collection 统计。
- 部署 additive 数据库迁移。
- 注册 legacy generation。
- 验证 active route。

### 24.2 构建候选

- 创建 generation。
- 检查生成的指纹和 Collection 名。
- Provision Collection。
- 启动低并发重建。
- 观察 Provider、Milvus 和任务失败率。
- 完成首次构建和追平。

### 24.3 激活前

- 运行完整验证报告。
- 运行 Eval 对比。
- 检查 candidate load 状态和资源容量。
- 检查最终差异。
- 确认 previous 回滚模型仍可用。
- 记录变更审批。

### 24.4 激活后

- 校验 PostgreSQL route。
- 校验两个 Alias。
- 执行语义搜索和分层搜索 smoke test。
- 观察错误率、空结果率、p95、质量采样。
- 启动 previous 增量镜像。
- 不删除旧 generation。

### 24.5 需要回滚时

- 检查 previous lag。
- 开启写入屏障并追平。
- 验证 previous Collection 和模型。
- 执行路由回滚。
- 校验 Alias。
- 运行 smoke test。
- 保存故障 generation 和诊断信息。

---

## 25. 最终验收标准

R-05 只有全部满足才算关闭：

- 代码搜索确认生产运行时不存在自动 Collection.drop。
- API 和普通 Worker 的 Milvus 账号没有 DropCollection 权限。
- 每个 active 索引都有 generation_id 和完整 Embedding 指纹。
- 同维度不同模型不能写入同一 generation。
- API 查询向量与被检索 Collection 来自同一 generation。
- Chunk 和 L0/L1 在一次请求中不会跨 generation。
- 新 generation 可以旁路构建，active 检索不中断。
- 构建可暂停、恢复、重试和取消。
- 文件更新和删除能在激活前完成最终追平。
- 不完整 generation 无法激活。
- 质量不达标 generation 无法激活。
- PostgreSQL 一次事务完成 active/previous 路由切换。
- Alias 失配可观测并可自动 reconciliation。
- 切换期间并发请求只看到完整旧代或完整新代。
- previous 增量 lag 可测量，回滚前有强门禁。
- active、previous 或 Alias 指向的 Collection 无法被清理。
- 删除只能由显式运维命令执行，并有 dry-run、二次确认和审计。
- Trace 和 Eval 能准确回答一次请求使用了哪个 generation、模型和 Collection。
- 历史可能受 R-04 污染的索引已通过真实模型全量重建并退出在线路由。

---

## 26. 对标与参考

### 26.1 RAGFlow

本地参考：

- D:\code\origin\ragflow\api\db\db_models.py
- D:\code\origin\ragflow\api\apps\services\dataset_api_service.py

可借鉴：

- 知识库显式保存 embd_id。
- 模型切换前执行可用性检查。
- 抽样重嵌入，比较维度和余弦相似度。

不直接照搬：

- 抽样相似不能证明整个索引可原地复用。
- OpenRag 当前是全局模型，不在 R-05 中顺带改为 workspace 级模型。

### 26.2 OpenViking

本地参考：

- D:\code\origin\OpenViking\openviking\storage\collection_schemas.py
- D:\code\origin\OpenViking\openviking\storage\vikingdb_manager.py

可借鉴：

- 在 Collection 元数据中记录 provider、model、dimension 和 model identity。
- 统一描述 L0/L1/L2 上下文 Schema 的思路。
- Embedding 队列和后台处理能力。

不直接照搬：

- 非空 Collection 元数据不匹配后继续运行属于 fail-open，不符合 R-05 要求。
- OpenViking 的后端抽象和统一 Collection 不是当前 OpenRag 的最小改造路径。

### 26.3 Milvus 官方资料

- Alias 管理与蓝绿切换：https://milvus.io/docs/manage-aliases.md
- Collection、Load 与 Release：https://milvus.io/docs/manage-collections.md
- PyMilvus 2.4 ORM alter_alias：https://milvus.io/api-reference/pymilvus/v2.4.x/ORM/utility/alter_alias.md
- Milvus RBAC 权限：https://milvus.io/docs/grant_privileges.md
- Milvus Backup：https://milvus.io/docs/milvus_backup_cli.md

Milvus 官方说明 Alias 可以在不修改应用代码的情况下把一个逻辑名称重定向到另一个物理 Collection，并明确将其用于无停机数据更新和蓝绿部署。本方案采用该能力，但同时补充 PostgreSQL generation 路由，以解决 OpenRag 两个 Collection 无法通过一次 Milvus Alias 操作共同切换的问题。

---

## 27. 最终建议

推荐按照以下优先级推进：

1. 先落实运行时 RBAC，彻底剥离 DropCollection 权限。
2. 建立 generation registry 和 Embedding 指纹。
3. 让 API、Worker、删除服务统一通过 generation 路由工作。
4. 再实现 versioned Collection 和旁路重建。
5. 最后开放激活、回滚和清理操作。

不要先做一个只有 Collection 名后缀、没有 PostgreSQL 状态机和任务绑定的“轻量版本化”。那种实现虽然能保留多个 Collection，但仍会留下以下问题：

- Worker 可能写错 generation。
- Chunk 与 Layer 可能跨代。
- 删除可能漏清理旧 generation。
- Alias 切换失败无法 reconciliation。
- 无法判断候选是否完整。
- 无法保证回滚后的新增文件不丢失。

R-05 的最小完整闭环应当是：

> 不可变 Embedding 指纹 + versioned 物理 Collection + 可恢复旁路重建 + PostgreSQL 原子 generation 路由 + 激活质量门禁 + previous 增量追平 + 显式受控清理。

---

## 28. 具体执行方案

本节将前述设计拆分为可以逐步开发、逐步验证、逐步发布的实施任务。每一步都必须独立完成测试和退出门禁，不能只把所有修改一次性合并后做最终验证。

### 28.1 执行规则

执行过程中遵循：

- 每一步只解决当前明确问题，不提前实现后续阶段的业务操作。
- 所有数据库迁移采用 additive 方式；旧字段和旧 Collection 在完成切换前继续可用。
- 每一步先增加失败测试，再修改实现。
- 任何步骤都不能恢复自动创建、自动重建或自动删除 active Collection。
- 新能力默认关闭，只有完成对应门禁后才允许打开。
- 管理接口先实现只读和 dry-run，再开放写操作。
- 激活、回滚和物理删除必须最后实施。
- 每个步骤完成后更新本文档中的实施状态和实际偏差。

推荐执行顺序：

| 步骤 | 任务 | 依赖 | 主要交付物 |
|---|---|---|---|
| 0 | 固化安全基线 | R-04 已完成 | 防回归测试、权限基线 |
| 1 | 建立 generation 数据模型 | 步骤 0 | 三张核心表、状态枚举、迁移 |
| 2 | 建立 Embedding 指纹 | 步骤 1 | 稳定指纹、显式模型身份 |
| 3 | 注册 legacy generation | 步骤 1、2 | 当前索引进入 registry |
| 4 | 拆分 Milvus 运行时与管理职责 | 步骤 2 | Runtime Store、Provisioner、Cleaner |
| 5 | 建立运行时 generation 路由 | 步骤 3、4 | RuntimeSnapshot、Search 切换 |
| 6 | 任务绑定 generation | 步骤 5 | Task/Worker 写入隔离 |
| 7 | 创建候选 generation 和 Collection | 步骤 4、6 | 管理服务、候选空集合 |
| 8 | 实现文件级旁路重建 | 步骤 7 | Reindex Processor、恢复队列 |
| 9 | 实现水位追平与删除传播 | 步骤 8 | Reconciliation、全 generation 删除 |
| 10 | 实现完整性验证 | 步骤 8、9 | Validation Report、ready 门禁 |
| 11 | 实现质量评测和 Shadow | 步骤 5、10 | active/candidate 对比报告 |
| 12 | 实现原子激活 | 步骤 10、11 | 写屏障、路由切换、Alias 同步 |
| 13 | 实现 previous 镜像与回滚 | 步骤 12 | 回滚 RPO、追平和回滚门禁 |
| 14 | 实现退休、备份和受控清理 | 步骤 13 | dry-run、二次确认、RBAC |
| 15 | 完善可观测性和运维接口 | 贯穿所有步骤 | 指标、Trace、健康检查、告警 |
| 16 | 执行 legacy 到 versioned 的生产迁移 | 全部步骤 | 首个正式 generation 激活 |

---

### 28.2 步骤 0：固化 R-04 安全基线

#### 要解决的问题

R-05 会大幅调整 Milvus 初始化和路由。如果没有先固定 R-04 的 fail-closed 行为，后续重构可能重新引入以下风险：

- 维度不一致时删除 Collection。
- Collection 不存在时由普通 API 自动创建。
- Embedding 失败后继续写入非法向量。
- Chunk 或 Layer 部分写入仍被视为成功。

#### 采用的方法

1. 保留现有 VectorSchemaMismatchError 和 VectorWriteIncompleteError。
2. 将“运行时永不 drop”提升为所有 Store 和管理服务的共同契约。
3. 在引入新类前增加代码扫描和行为测试。
4. 记录当前生产 Collection 的 Schema、实体统计、Index 参数和部署版本，作为迁移前基线。
5. 调查当前 Milvus 是否开启认证、API/Worker 使用什么身份，以及该身份是否具有 DropCollection 权限。

#### 涉及修改或新增的函数

现有函数需要保持行为：

- MilvusStore._ensure_collection
- MilvusStore.probe
- MilvusStore.insert_chunks
- MilvusLayerStore._ensure_collection
- MilvusLayerStore.probe
- MilvusLayerStore.upsert_file_layers
- EmbeddingEngine._validate_embedding
- DocumentProcessor._assert_processing_complete

建议新增测试辅助函数：

- assert_collection_not_dropped
- snapshot_collection_schema
- assert_runtime_identity_has_no_drop_privilege

#### 涉及修改或新增的字段

本步骤不新增业务字段。

可在测试配置中增加：

- MILVUS_TEST_ADMIN_TOKEN
- MILVUS_TEST_RUNTIME_TOKEN

生产密钥字段在步骤 14 正式落地。

#### 校验有效性和准确性

必须通过：

- 现有 test_vector_write_contract.py 全部通过。
- 新增测试确认 Chunk/Layer 维度不一致时 Collection 仍存在。
- 新增测试确认非法向量不会产生 insert 调用。
- 代码搜索确认 API、Worker、DocumentProcessor、文件删除服务没有 drop 调用。
- 使用运行时 Milvus 身份执行 drop 测试时被服务端拒绝；如果当前尚未开启 Milvus 认证，则记录为步骤 14 的发布阻断项。
- 记录的 Chunk/Layer Schema、实体统计和线上实际一致。

#### 本步骤退出门禁

- R-04 专项测试稳定通过。
- 后续所有修改均以“普通运行时无 Collection 生命周期权限”为不可破坏前提。

---

### 28.3 步骤 1：建立 Index Generation 数据模型和状态机

#### 要解决的问题

当前数据库无法回答：

- 哪个 Collection 是 active。
- 当前索引使用哪个模型和维度。
- 哪个候选正在构建。
- 某个文件是否已经写入候选 generation。
- 哪个旧 generation 可以回滚。

如果没有权威元数据，Collection 后缀和 Alias 只能形成不可审计的隐式状态。

#### 采用的方法

1. 新增 IndexGeneration、IndexGenerationRoute、IndexGenerationFile 三个模型。
2. 使用 IndexGenerationState 枚举限制生命周期。
3. 用 PostgreSQL 约束保证同一 scope 只有一个 active。
4. 使用 route_version 和 lock_version 处理并发激活。
5. 将 generation 清单保存为不可变 manifest。
6. 状态转换集中在 IndexGenerationService，禁止 API 直接修改 state。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/models/index_generation.py
- openrag/src/openrag/services/index_generation_service.py
- openrag/alembic/versions/20260715_0008_add_index_generations.py
- openrag/tests/test_index_generation_models.py
- openrag/tests/test_index_generation_service.py

需要修改：

- openrag/src/openrag/models/__init__.py
- docs/CURRENT_MODEL_SCHEMA_DDL.md

#### 涉及修改或新增的函数

建议新增：

- IndexGenerationService.create_generation
- IndexGenerationService.get_generation
- IndexGenerationService.list_generations
- IndexGenerationService.transition_state
- IndexGenerationService.get_route
- IndexGenerationService.compare_and_swap_route
- IndexGenerationService.mark_failed
- IndexGenerationService.recalculate_counts
- validate_generation_transition
- utcnow

create_generation 只创建 draft 记录，不能创建 Milvus Collection。

#### 涉及修改或新增的字段

IndexGeneration 核心字段：

- id
- scope
- state
- source_generation_id
- embedding_provider
- embedding_model
- embedding_revision
- embedding_dimension
- embedding_fingerprint
- embedding_config_ref
- vector_normalization
- distance_metric
- schema_version
- chunk_policy_revision
- hierarchy_policy_revision
- chunk_collection_name
- layer_collection_name
- es_generation
- manifest
- source_watermark_at
- expected_file_count
- expected_chunk_count
- expected_layer_count
- indexed_file_count
- indexed_chunk_count
- indexed_layer_count
- failed_file_count
- validation_report
- quality_report
- created_by
- build_started_at
- ready_at
- activated_at
- retired_at
- delete_after
- last_error_code
- last_error
- lock_version

IndexGenerationRoute 核心字段：

- scope
- active_generation_id
- previous_generation_id
- route_version
- activated_at
- activated_by
- rollback_deadline
- write_barrier
- updated_at

IndexGenerationFile 核心字段：

- generation_id
- file_id
- workspace_id
- state
- source_content_hash
- source_updated_at
- expected_chunk_count
- written_chunk_count
- expected_layer_count
- written_layer_count
- retry_count
- next_retry_at
- worker_id
- heartbeat_at
- error_code
- error
- completed_at

数据库约束：

- generation_id + file_id 唯一。
- Collection 名唯一。
- 同一 scope 最多一个 active。
- state 值受约束。
- 计数字段非负。
- route_version 和 lock_version 非负。

#### 校验有效性和准确性

单元测试：

- 合法状态转换成功。
- draft 不能直接进入 active。
- failed 不能直接进入 active。
- active 不能回到 building。
- deleted 不能恢复。
- 不可变字段在 draft 之后修改会失败。

数据库测试：

- 两个事务并发创建 active 时只有一个成功。
- generation + file_id 重复插入失败。
- route compare-and-swap 使用旧 route_version 时失败。
- 数据库升级和降级在空测试库可执行。
- 迁移不会修改现有 files、tasks、DocumentChunk 数据。

#### 本步骤退出门禁

- PostgreSQL 可以完整表达 generation、route 和文件级构建状态。
- 尚未改变现有 API、Worker 和 Milvus 行为。

---

### 28.4 步骤 2：建立 Embedding 模型身份和稳定指纹

#### 要解决的问题

当前配置主要依赖 model 和 dimension，无法可靠区分：

- 同名模型的不同 revision。
- 同维度但不同向量空间。
- 本地模型权重变化。
- query/document 前缀或归一化策略变化。

只用维度作为兼容条件会造成静默混写。

#### 采用的方法

1. 扩展 EmbeddingConfig，使模型身份可显式描述。
2. 新增规范化 EmbeddingManifest。
3. 对规范化 JSON 计算 SHA-256 指纹。
4. EmbeddingEngine 支持传入显式 EmbeddingConfig，而不是只能读取全局配置。
5. probe 返回实际模型身份、维度和指纹校验结果。
6. API Key 和瞬时运行参数不得进入指纹。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/__init__.py
- openrag/src/openrag/indexing/fingerprint.py
- openrag/tests/test_embedding_fingerprint.py

需要修改：

- openrag/src/openrag/config.py
- openrag/src/openrag/embedding/embedding_engine.py
- openrag/src/openrag/embedding/__init__.py
- docker/.env.example
- k8s/13-configmap-openrag-llm.yaml
- k8s/01-secret.example.yaml

#### 涉及修改或新增的函数

建议新增：

- build_embedding_manifest
- normalize_embedding_manifest
- compute_embedding_fingerprint
- resolve_model_identity
- EmbeddingEngine.get_manifest
- EmbeddingEngine.get_fingerprint
- EmbeddingEngine.assert_fingerprint

需要修改：

- validate_embedding_config
- EmbeddingEngine.__init__
- EmbeddingEngine.probe
- EmbeddingEngine.health_snapshot
- initialize_embedding_dependency

EmbeddingEngine.__init__ 建议增加 config 参数，允许 candidate Worker 使用目标 generation 的显式配置。

#### 涉及修改或新增的字段

EmbeddingConfig 建议新增：

- revision
- model_identity
- normalization
- input_type
- query_prefix_revision
- document_prefix_revision
- text_preprocess_revision
- sdk_contract_revision
- config_ref

环境变量建议对应：

- EMBEDDING_REVISION
- EMBEDDING_MODEL_IDENTITY
- EMBEDDING_NORMALIZATION
- EMBEDDING_INPUT_TYPE
- EMBEDDING_QUERY_PREFIX_REVISION
- EMBEDDING_DOCUMENT_PREFIX_REVISION
- EMBEDDING_TEXT_PREPROCESS_REVISION
- EMBEDDING_SDK_CONTRACT_REVISION

#### 校验有效性和准确性

必须验证：

- 相同配置不同字段顺序生成相同指纹。
- API Key、batch_size、timeout、max_attempts 变化不改变指纹。
- provider、model、revision、dimension、normalization 任一变化都会改变指纹。
- 同维度不同 model_identity 的指纹不同。
- 本地模型身份使用稳定 digest，而不是文件修改时间。
- Engine.probe 的实际维度不一致时失败。
- Engine.assert_fingerprint 不一致时在请求 Embedding 前失败。
- 日志和 API 响应不输出 API Key 或完整敏感 base_url。

#### 本步骤退出门禁

- 任何 generation 都能被一个不可变指纹准确标识。
- 同维度模型不再被视为天然兼容。

---

### 28.5 步骤 3：注册现有 Legacy Collection

#### 要解决的问题

引入 registry 后，线上现有 openrag_chunks 和 openrag_layers 仍在提供服务。如果直接要求 versioned 名称，部署会立即中断检索。

#### 采用的方法

1. 增加一次性 bootstrap 命令，读取当前生产配置和现有 Collection Schema。
2. 创建 state=active 的 legacy generation。
3. 创建 scope=global 的 route，指向 legacy generation。
4. 不重命名、不复制、不删除现有 Collection。
5. legacy 指纹标记来源和可信度；如果无法证明历史模型身份，在 manifest 中记录 identity_confidence=declared。
6. bootstrap 必须显式执行，不能在 API 每次启动时自动写数据库。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/legacy_bootstrap.py
- openrag/src/openrag/cli/index_generation.py
- openrag/tests/test_legacy_generation_bootstrap.py

需要修改：

- openrag/src/openrag/api/main.py
- docs/02-Kubernetes部署.md
- docs/外网DockerCompose部署指南.md

#### 涉及修改或新增的函数

建议新增：

- inspect_legacy_collections
- build_legacy_manifest
- bootstrap_legacy_generation
- verify_legacy_route
- print_legacy_bootstrap_dry_run

startup_event 只调用只读校验函数：

- validate_active_route_on_startup

startup_event 不得自动执行 bootstrap_legacy_generation。

#### 涉及修改或新增的字段

legacy manifest 建议增加：

- legacy
- identity_confidence
- observed_chunk_schema
- observed_layer_schema
- observed_chunk_count
- observed_layer_count
- bootstrap_at
- bootstrap_operator

#### 校验有效性和准确性

必须验证：

- dry-run 不写 PostgreSQL 和 Milvus。
- 重复执行 bootstrap 幂等，不创建第二个 active。
- Collection 不存在或 Schema 不一致时 bootstrap 失败。
- bootstrap 前后 Collection 名、实体数和 Alias 均不变化。
- active route 指向 legacy generation 后，查询结果与改造前一致。
- declared 指纹不会被误标为 verified。

#### 本步骤退出门禁

- 现有线上索引已进入 registry。
- 服务仍使用原有物理 Collection，检索结果无变化。

---

### 28.6 步骤 4：拆分 Milvus 运行时 Store、Provisioner 和 Cleaner

#### 要解决的问题

当前 MilvusStore 和 MilvusLayerStore 构造时会连接并调用 _ensure_collection。即使已经禁止 drop，它仍把“加载已有 Collection”和“创建新 Collection”混在一个普通运行时入口里。

R-05 需要明确隔离：

- 运行时只能使用已存在 Collection。
- 控制面可以创建候选 Collection。
- 清理工具在严格门禁下删除退休 Collection。

#### 采用的方法

1. Runtime Store 构造必须传入物理 collection_name 和 expected manifest。
2. Collection 不存在时 Runtime Store 直接失败，不自动创建。
3. 将 Schema 构造和 create_index 移入 MilvusCollectionProvisioner。
4. 将 drop 移入独立 MilvusCollectionCleaner。
5. Provisioner 和 Cleaner 使用 admin 连接；Runtime Store 使用 runtime 连接。
6. 提供只读 describe、probe 和 count 能力供 Validator 使用。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/milvus_provisioner.py
- openrag/src/openrag/indexing/milvus_cleaner.py
- openrag/src/openrag/indexing/milvus_schema.py
- openrag/tests/test_milvus_runtime_store.py
- openrag/tests/test_milvus_provisioner.py
- openrag/tests/test_milvus_cleaner.py

需要修改：

- openrag/src/openrag/vectorstore/milvus_store.py
- openrag/src/openrag/vectorstore/milvus_layer_store.py
- openrag/src/openrag/vectorstore/__init__.py
- openrag/src/openrag/config.py

#### 涉及修改或新增的函数

MilvusStore：

- 将 _ensure_collection 改为 _load_and_validate_collection。
- 新增 describe_schema。
- 新增 validate_manifest。
- 保留 insert_chunks、search、delete_by_file_id、count、probe。

MilvusLayerStore：

- 将 _ensure_collection 改为 _load_and_validate_collection。
- 新增 describe_schema。
- 新增 validate_manifest。
- 保留 upsert_file_layers、search_layers、delete_by_file_id、probe。

MilvusCollectionProvisioner 建议新增：

- create_chunk_collection
- create_layer_collection
- create_vector_index
- write_collection_metadata
- verify_provisioned_collection
- provision_generation

MilvusCollectionCleaner 建议新增：

- build_delete_plan
- assert_deletable
- drop_generation_collections

#### 涉及修改或新增的字段

VectorDBConfig 建议新增：

- runtime_user
- runtime_password
- admin_user
- admin_password
- secure
- connection_timeout_seconds

Store 构造参数新增：

- collection_name
- expected_dimension
- expected_schema_version
- expected_embedding_fingerprint
- connection_role

移除或限制：

- Runtime Store 的默认 collection_name。
- Runtime Store 中的 Collection 创建分支。

#### 校验有效性和准确性

必须验证：

- Runtime Store 对不存在 Collection 抛出稳定错误，不创建任何资源。
- Runtime Store 对错误维度、字段、metric 或指纹 fail-closed。
- Provisioner 可以创建两个不同 generation 的 Collection。
- Provisioner 重复调用对完全相同资源幂等，对同名未知资源拒绝接管。
- Cleaner 在 active、previous、Alias 指向或保留期未结束时拒绝删除。
- 运行时测试使用 mock drop 时确认永远不可到达。
- Provisioner/Cleaner 测试使用与生产相同的 Milvus 2.4.17。

#### 本步骤退出门禁

- 普通 Store 不再具有隐式 Collection 生命周期副作用。
- 创建和删除职责已经物理隔离。

---

### 28.7 步骤 5：建立 IndexRuntimeResolver 并改造 Search API

#### 要解决的问题

当前 Search API 使用进程级 _embedding_engine、_vector_store、_layer_store_instance，目标由当前环境变量和固定 Collection 决定。无法保证：

- query Embedding 与 Collection 属于同一 generation。
- Chunk 和 Layer 来自同一个 generation。
- route 切换后新请求及时使用新 generation。

#### 采用的方法

1. 新增不可变 IndexRuntimeSnapshot。
2. IndexRuntimeResolver 一次数据库读取解析 generation 和资源。
3. 按 generation_id + fingerprint 缓存 Runtime 对象。
4. 每个请求只解析一次快照。
5. 已开始请求继续使用旧 Runtime；新请求读取新 route_version。
6. PostgreSQL 不可用、route 缺失或指纹不一致时返回 503，不回退固定 Collection。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/runtime.py
- openrag/tests/test_index_runtime_resolver.py
- openrag/tests/test_search_generation_routing.py

需要修改：

- openrag/src/openrag/api/search_api.py
- openrag/src/openrag/retrieval/retrieval_service.py
- openrag/src/openrag/api/main.py
- openrag/src/openrag/services/trace_service.py

#### 涉及修改或新增的函数

建议新增：

- IndexRuntimeResolver.get_active_snapshot
- IndexRuntimeResolver.get_generation_snapshot
- IndexRuntimeResolver.get_runtime
- IndexRuntimeResolver.invalidate_route
- IndexRuntimeResolver.evict_retired_runtime
- IndexRuntimeResolver.probe_active_runtime
- build_runtime_from_snapshot

Search API 需要替换：

- _get_embedding_engine
- _get_vector_store
- _get_layer_store

建议新增：

- _get_index_runtime_resolver
- _resolve_search_runtime

需要修改：

- _execute_search
- semantic_search
- hierarchical_search
- _prepare_retrieval_trace
- _record_response_trace
- health_check
- startup_event

RetrievalService 构造建议增加：

- generation_id
- route_version
- embedding_fingerprint

#### 涉及修改或新增的字段

IndexRuntimeSnapshot：

- generation_id
- route_version
- embedding_fingerprint
- embedding_config_ref
- chunk_collection_name
- layer_collection_name
- hierarchy_enabled
- es_generation

Trace search_config_snapshot 增加：

- index_generation_id
- route_version
- embedding_fingerprint
- embedding_revision
- chunk_collection_name
- layer_collection_name

#### 校验有效性和准确性

必须验证：

- 一次请求只调用一次 route 解析。
- 查询 EmbeddingEngine、Chunk Store、Layer Store 的 generation_id 完全一致。
- route 切换后新请求使用新 generation，已开始请求仍完成旧 generation。
- 同维度不同指纹时不复用旧 Engine 缓存。
- PostgreSQL 路由不可用时不查询固定 legacy Collection。
- hierarchical search 在 Layer Runtime 缺失时明确返回 503。
- Trace 中记录的 generation 与实际 Store Collection 一致。
- legacy generation 接入后现有搜索回归测试结果不变。

#### 本步骤退出门禁

- 所有查询已经通过 generation 路由。
- 尚未改变普通文档 Worker 的写入目标。

---

### 28.8 步骤 6：让 Task、Broker、Worker 和 DocumentProcessor 绑定 Generation

#### 要解决的问题

查询完成路由后，如果 Worker 仍根据当前环境变量写固定 Collection，会出现：

- 新旧 Worker 跨模型混写。
- 路由切换过程中任务写错 generation。
- 任务开始和结束使用不同 active generation。
- 无法判断某次写入属于哪个索引。

#### 采用的方法

1. Task 增加显式 index_generation_id。
2. 普通文档任务创建或领取时绑定 active generation。
3. 重建任务创建时显式绑定 candidate generation。
4. Worker 执行期间不重新解析 active route。
5. Worker 使用 IndexRuntimeResolver.get_generation_snapshot 创建任务 Runtime。
6. DocumentProcessor 接收 generation 上下文并在写前校验 Engine/Store 一致。
7. write_barrier=true 时 Broker 不分配新的索引写任务。

#### 涉及修改或新增的文件

需要修改：

- openrag/src/openrag/models/task.py
- openrag/src/openrag/services/task_service.py
- openrag/src/openrag/broker/task_broker.py
- openrag/src/openrag/api/broker_api.py
- openrag/src/openrag/api/tasks_api.py
- openrag/src/openrag/worker/task_worker.py
- openrag/src/openrag/processors/document_processor.py
- openrag/alembic/versions/新增任务 generation 字段迁移

建议新增测试：

- openrag/tests/test_task_generation_binding.py
- openrag/tests/test_worker_generation_runtime.py
- openrag/tests/test_broker_write_barrier.py

#### 涉及修改或新增的函数

TaskService：

- add_task
- create_task
- retry_task
- schedule_task_retry

TaskBroker：

- get_tasks
- _ready_task_filter
- _assign_tasks
- 新增 _bind_active_generation
- 新增 _write_barrier_filter

TaskWorker：

- _initialize_processing_dependencies
- _preflight_processing_dependencies
- _execute_task
- _process_document
- _delete_file_task
- _delete_path_prefix_task
- _handle_task_failure
- 新增 _resolve_task_runtime
- 新增 _assert_task_generation_writable

DocumentProcessor：

- __init__ 增加 generation_context。
- process_document 在写入前校验 generation。
- _assert_processing_complete 增加 generation 一致性校验输入。

#### 涉及修改或新增的字段

Task 新增：

- index_generation_id，可空 FK。

TaskResponse 和 Task.to_dict 增加：

- index_generation_id。

任务 payload 对候选重建增加：

- source_content_hash。
- source_updated_at。

不允许只把 generation_id 放在 payload 而没有数据库字段，因为 Broker 和数据库索引需要直接过滤。

#### 校验有效性和准确性

必须验证：

- 普通文档任务在领取后具有 active generation_id。
- 路由切换后，已领取任务仍写入原 generation。
- 新领取任务写入新 active。
- candidate 任务永远不能写 active Collection。
- active 任务永远不能写 candidate Collection。
- Worker 配置指纹与任务 generation 不一致时在 Embedding 前失败。
- write_barrier 开启后不领取 process_document、embed_document、reindex_generation_file。
- 删除任务仍可在屏障期间执行，避免安全删除被阻塞。
- Task API 和 Broker Response 正确返回 generation_id。

#### 本步骤退出门禁

- 查询和在线写入均由 generation 显式绑定。
- 灰度 Worker 无法向错误模型空间混写。

---

### 28.9 步骤 7：实现候选 Generation 管理接口和 Provisioning

#### 要解决的问题

具备路由后仍缺少创建候选索引的受控入口。直接由脚本创建 Collection 会绕过状态机、审计和幂等。

#### 采用的方法

1. 新增仅管理员可访问的 Index Generation API。
2. create 只创建 draft。
3. provision 根据 draft manifest 创建物理 Collection。
4. 每个操作使用 client_request_id 保证幂等。
5. Provisioning 失败进入 failed，保留资源供诊断，不自动 drop。
6. 先提供 dry-run 和只读接口，再开放写接口。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/api/index_generations_api.py
- openrag/src/openrag/indexing/provision_service.py
- openrag/src/openrag/indexing/schemas.py
- openrag/tests/test_index_generations_api.py
- openrag/tests/test_generation_provision_service.py

需要修改：

- openrag/src/openrag/api/main.py
- openrag/src/openrag/api/roles_api.py 或复用 require_admin
- openrag/src/openrag/models/audit.py 的调用入口

#### 涉及修改或新增的函数

建议新增：

- require_index_admin
- create_index_generation
- list_index_generations
- get_index_generation
- preview_generation_manifest
- provision_index_generation
- pause_index_generation
- resume_index_generation
- cancel_index_generation
- ProvisionService.provision
- ProvisionService.verify_result

IndexGenerationService 增加：

- reserve_collection_names
- assert_manifest_immutable
- record_provision_result

#### 涉及修改或新增的字段

API Request：

- client_request_id
- embedding_config_ref
- embedding_model
- embedding_revision
- embedding_dimension
- schema_version
- chunk_policy_revision
- hierarchy_policy_revision
- rollback_window_seconds
- force_rebuild

API Response：

- generation_id
- state
- manifest
- physical_collections
- validation_summary
- created_at

AuditLog metadata 增加：

- generation_id
- operation
- previous_state
- next_state
- client_request_id

#### 校验有效性和准确性

必须验证：

- 非管理员所有写接口返回 403。
- create 不产生 Milvus 写操作。
- preview/dry-run 不写数据库。
- 同 client_request_id 重试返回相同 generation。
- provision 创建的两个 Collection 与 manifest 完全一致。
- 任何一个 Collection 创建失败时 active route 不变。
- 同名未知 Collection 存在时拒绝 provision。
- provision 重试不会创建额外 Collection。
- API 错误不暴露 Milvus 凭据或 Provider 原始异常。

#### 本步骤退出门禁

- 可以受控创建一个空 candidate generation。
- candidate 与 legacy active 可同时存在。

---

### 28.10 步骤 8：实现只读源内容的文件级旁路重建

#### 要解决的问题

候选 Collection 创建后，需要全量生成向量，但不能直接复用 DocumentProcessor.process_document，因为它还会：

- 重新运行 Parser 和 ChunkEngine。
- 修改 files.processing_status。
- 更新 DocumentChunk。
- 写 ES。
- 保存或覆盖 L0/L1/L2 层级内容。

候选向量重建只应读取当前规范内容，不能改变在线内容状态。

#### 采用的方法

1. 新增 GenerationReindexProcessor，专门读取现有 L2/L0/L1。
2. 候选重建不调用 Parser、ChunkEngine 和 hierarchy builder。
3. 从 DocumentChunk 和 HierarchyStorage/MinIO 读取完整 Chunk 内容。
4. 使用 candidate EmbeddingEngine 批量生成 Chunk 和 Layer 向量。
5. 只写 candidate Collection 和 IndexGenerationFile。
6. 每个文件按 generation_id + file_id 幂等。
7. 重试前只删除 candidate 中该 file_id 的数据。
8. 使用独立 reindex queue 和并发限流。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/reindex_processor.py
- openrag/src/openrag/indexing/reindex_service.py
- openrag/src/openrag/indexing/source_reader.py
- openrag/tests/test_generation_reindex_processor.py
- openrag/tests/test_generation_reindex_recovery.py

需要修改：

- openrag/src/openrag/models/task.py
- openrag/src/openrag/services/task_service.py
- openrag/src/openrag/worker/task_worker.py
- openrag/src/openrag/hierarchy/hierarchy_storage.py

#### 涉及修改或新增的函数

GenerationSourceReader：

- list_active_source_files
- load_chunk_sources
- load_layer_sources
- compute_source_content_hash
- get_source_revision

GenerationReindexProcessor：

- reindex_file
- _embed_chunks
- _embed_layers
- _replace_candidate_vectors
- _verify_file_counts

GenerationReindexService：

- initialize_generation_files
- enqueue_pending_files
- claim_file
- mark_file_success
- mark_file_retry
- mark_file_failed
- recover_stale_files
- recalculate_generation_progress

TaskWorker 增加：

- _reindex_generation_file_task

TaskType 增加：

- REINDEX_GENERATION_FILE

#### 涉及修改或新增的字段

IndexGenerationFile 使用：

- source_content_hash
- source_updated_at
- expected_chunk_count
- written_chunk_count
- expected_layer_count
- written_layer_count
- retry_count
- next_retry_at
- worker_id
- heartbeat_at
- error_code
- error

Task.payload 增加：

- generation_id
- source_content_hash
- source_updated_at

#### 校验有效性和准确性

必须验证：

- 重建过程中 files.processing_status 不变化。
- DocumentChunk 数量和内容不变化。
- MinIO L0/L1/L2 对象不被覆盖。
- ES 不产生写操作。
- text_preview 被截断时仍使用完整 L2 内容。
- candidate Chunk 数等于预期 Chunk 数。
- candidate Layer 数等于实际非空 L0/L1 数。
- Worker 中断后 stale 文件可以重新领取。
- 重复执行同一个文件不会产生重复实体。
- 某文件失败不会标记在线 File 为 failed。
- candidate Provider 故障时 active 查询和在线任务不受影响。

#### 本步骤退出门禁

- 可以从现有规范内容完整构建 candidate。
- 构建可中断、重试和恢复。

---

### 28.11 步骤 9：实现水位追平、变更捕获和跨 Generation 删除

#### 要解决的问题

全量重建期间文件会继续上传、更新、重新解析或删除。只做一次初始扫描会导致 candidate 在激活时落后于 active。

文件删除还必须覆盖所有仍保留数据的 generation，不能只删除当前 active。

#### 采用的方法

1. building 开始时记录 source_watermark_at。
2. 首次扫描后按 source_updated_at 和内容 hash 查找变化文件。
3. 对新增/更新文件重新创建 candidate 任务。
4. 对 deleted_at 不为空的文件向所有保留 generation 传播删除。
5. 增加 reconciliation 循环，直到 lag 为 0。
6. 删除失败记录稳定错误并进入延迟重试。
7. 文件删除服务从 registry 解析目标，而不是根据当前环境变量创建固定 Store。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/reconciliation_service.py
- openrag/tests/test_generation_reconciliation.py
- openrag/tests/test_generation_delete_propagation.py

需要修改：

- openrag/src/openrag/services/file_deletion.py
- openrag/src/openrag/worker/task_worker.py
- openrag/src/openrag/services/task_service.py
- openrag/src/openrag/models/task.py

#### 涉及修改或新增的函数

ReconciliationService：

- capture_source_watermark
- scan_changed_files
- scan_deleted_files
- enqueue_reconciliation_tasks
- calculate_build_lag
- reconcile_until_stable
- assert_zero_lag

文件删除服务：

- 将 delete_milvus_vectors_for_file 改造为 delete_vectors_for_file_across_generations。
- 新增 resolve_generations_containing_file。
- 新增 delete_file_from_generation。
- 新增 record_generation_delete_result。

TaskWorker：

- _delete_file_task
- _delete_path_prefix_task
- 新增 _purge_file_from_generations_task

TaskType 增加：

- RECONCILE_GENERATION_FILE
- PURGE_FILE_FROM_GENERATIONS

#### 涉及修改或新增的字段

IndexGeneration：

- source_watermark_at
- last_reconciled_at
- build_lag_files

IndexGenerationFile.state 增加或明确：

- deleted
- stale

删除任务结果增加：

- target_generation_ids
- succeeded_generation_ids
- failed_generation_ids

#### 校验有效性和准确性

必须执行并发测试：

- 全量构建期间上传新文件，最终 candidate 包含该文件。
- 构建期间更新文件，candidate 使用最新 source hash。
- 构建期间删除文件，candidate 和 previous 不包含该文件。
- 删除一个文件不会影响其他 file_id。
- 某个 retired Collection 不可用时，active 删除仍执行，并记录 retired 失败。
- 重复删除幂等。
- lag 计算不会把已删除文件误判为缺失。
- reconciliation 达到 0 后再发生变更，lag 能重新增加。

#### 本步骤退出门禁

- candidate 能持续追平在线变化。
- 文件删除覆盖所有仍保留数据的 generation。

---

### 28.12 步骤 10：实现完整性和 Milvus 可用性验证

#### 要解决的问题

任务全部结束不等于候选完整。可能存在：

- 某些文件未创建状态行。
- Chunk 写入成功但 Layer 失败。
- Collection 实体数与清单不一致。
- Index 未构建完成或 Collection 未加载。
- 已删除文件仍存在。
- Schema、metric 或指纹被意外改变。

#### 采用的方法

1. 新增 IndexGenerationValidator。
2. 验证结果保存为结构化 validation_report。
3. Validator 只读 PostgreSQL、MinIO 和 Milvus。
4. 完整性硬门禁全部通过才能从 validating 进入 ready。
5. 验证失败保留详细稳定错误码，不自动修复或删除。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/validator.py
- openrag/src/openrag/indexing/validation_report.py
- openrag/tests/test_index_generation_validator.py
- openrag/tests/integration/test_generation_milvus_validation.py

需要修改：

- openrag/src/openrag/services/index_generation_service.py
- openrag/src/openrag/api/index_generations_api.py

#### 涉及修改或新增的函数

IndexGenerationValidator：

- validate_manifest
- validate_embedding_probe
- validate_chunk_collection_schema
- validate_layer_collection_schema
- validate_index_build_state
- validate_collection_load_state
- validate_file_coverage
- validate_file_counts
- validate_deleted_file_absence
- validate_orphan_entities
- validate_vector_samples
- run_smoke_search
- build_report

IndexGenerationService：

- start_validation
- record_validation_report
- mark_ready_if_valid

#### 涉及修改或新增的字段

IndexGeneration：

- validation_report
- validation_started_at
- validation_completed_at
- validation_error_code

ValidationReport 至少包含：

- checks
- passed
- failed_checks
- warnings
- expected_counts
- observed_counts
- sampled_vector_count
- orphan_file_ids
- deleted_file_ids_found
- schema_snapshot
- index_state
- load_state
- started_at
- completed_at

#### 校验有效性和准确性

必须为每个失败场景提供测试：

- 少一个文件状态行时失败。
- written_chunk_count 不一致时失败。
- Layer 数量不一致时失败。
- 存在 failed 文件时失败。
- Collection Schema 不一致时失败。
- 指纹元数据不一致时失败。
- 已删除文件仍存在时失败。
- 存在未知 file_id 时失败。
- Collection load 或 smoke search 失败时失败。
- warning 不会被错误计为 passed 硬门禁。
- validation_report 与实际故障原因一致。

#### 本步骤退出门禁

- 只有数据、Schema、Index 和服务状态完整的 candidate 才能进入 ready。

---

### 28.13 步骤 11：实现 Candidate 质量评测和 Shadow Search

#### 要解决的问题

技术完整不代表检索准确。新模型可能：

- Recall、nDCG 或 MRR 明显下降。
- 目录类查询退化。
- 空结果率上升。
- 延迟或错误率不可接受。

同时，现有 EvalRun.index_version 虽然已有字段，但必须绑定真实 generation。

#### 采用的方法

1. EvalService 支持显式指定 generation_id。
2. 同一评测集分别运行 active 和 candidate。
3. 保存指标差异和质量门禁结论。
4. 增加内部 Shadow Search，真实用户响应仍来自 active。
5. candidate 质量报告写入 IndexGeneration.quality_report。
6. 指标阈值配置化，但权限泄漏必须固定为 0。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/quality_gate.py
- openrag/src/openrag/indexing/shadow_search.py
- openrag/tests/test_generation_quality_gate.py
- openrag/tests/test_shadow_generation_search.py

需要修改：

- openrag/src/openrag/services/eval_service.py
- openrag/src/openrag/api/eval_api.py
- openrag/src/openrag/api/search_api.py
- openrag/src/openrag/services/trace_service.py

#### 涉及修改或新增的函数

EvalService：

- create_eval_run
- execute_eval_run
- _execute_query
- summarize_eval_run
- 新增 compare_eval_runs

Search API：

- _execute_search 增加仅内部可用的 runtime 参数，不把任意 generation 选择权开放给普通用户。

QualityGateService：

- evaluate_candidate
- compare_metrics
- validate_security_results
- build_quality_report
- assert_quality_gate

ShadowSearchService：

- should_shadow
- run_candidate_shadow
- record_shadow_comparison

#### 涉及修改或新增的字段

EvalRun：

- index_version 使用真实 generation_id。
- search_config_snapshot 增加 route_version、fingerprint 和物理 Collection。

IndexGeneration：

- quality_report
- quality_gate_passed
- quality_validated_at

配置字段建议新增：

- INDEX_QUALITY_RECALL_MAX_REGRESSION
- INDEX_QUALITY_NDCG_MAX_REGRESSION
- INDEX_QUALITY_MRR_MAX_REGRESSION
- INDEX_QUALITY_P95_MAX_REGRESSION
- INDEX_SHADOW_SAMPLE_RATE

#### 校验有效性和准确性

必须验证：

- active 和 candidate 使用相同 EvalDataset 和 query 集。
- EvalRun.index_version 与实际 Runtime generation 一致。
- Candidate 查询不会影响用户响应。
- Candidate 查询失败不会拖慢主请求超过 Shadow 预算。
- 权限不可见文件在 candidate 结果中为 0。
- 指标超过允许回退时 generation 无法进入可激活状态。
- Shadow Trace 不泄露超出既有策略的 query 内容。
- 同维度不同模型也必须完成质量评测。

#### 本步骤退出门禁

- ready candidate 已有完整性报告和质量报告。
- 未通过质量门禁的 candidate 无法激活。

---

### 28.14 步骤 12：实现写入屏障、原子激活和 Alias 同步

#### 要解决的问题

Candidate ready 后，需要在不跨代、不漏增量的情况下切换：

- Chunk 和 Layer 必须作为一组。
- 激活期间不能有旧任务继续写入即将退休的 generation。
- 两个并发管理员不能同时激活。
- 两个 Milvus Alias 不能被误认为一个跨 Collection 事务。

#### 采用的方法

1. IndexActivationService 获取 PostgreSQL advisory lock。
2. SELECT FOR UPDATE 锁定 route。
3. compare-and-swap 校验 source_generation_id 和 route_version。
4. 开启 write_barrier，Broker 停止分配新的索引写任务。
5. 等待旧 generation 正在运行的写任务完成。
6. 执行最后一次 reconcile 和快速验证。
7. 一个 PostgreSQL 事务切换 active/previous。
8. 事务提交后同步 Milvus Alias。
9. Alias 失败进入 reconciliation 告警，不把服务端路由切成未知状态。
10. 解除 write_barrier 并执行 smoke test。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/activation_service.py
- openrag/src/openrag/indexing/alias_reconciler.py
- openrag/tests/test_index_activation_service.py
- openrag/tests/integration/test_generation_activation_concurrency.py

需要修改：

- openrag/src/openrag/services/index_generation_service.py
- openrag/src/openrag/broker/task_broker.py
- openrag/src/openrag/api/index_generations_api.py
- openrag/src/openrag/api/main.py

#### 涉及修改或新增的函数

IndexActivationService：

- activate_generation
- _acquire_activation_lock
- _assert_activation_preconditions
- _enable_write_barrier
- _drain_generation_writes
- _run_final_reconciliation
- _switch_route_transaction
- _disable_write_barrier
- _run_post_activation_smoke_test

AliasReconciler：

- get_expected_aliases
- inspect_actual_aliases
- sync_active_aliases
- reconcile_aliases
- report_alias_drift

TaskBroker：

- _ready_task_filter
- _write_barrier_filter
- get_tasks

#### 涉及修改或新增的字段

IndexGenerationRoute：

- write_barrier
- route_version
- previous_generation_id
- rollback_deadline
- activated_at
- activated_by

IndexGeneration：

- activated_at
- retired_at

AuditLog：

- source_generation_id
- target_generation_id
- old_route_version
- new_route_version
- alias_sync_result

#### 校验有效性和准确性

必须执行真实并发测试：

- 激活过程中持续发起 flat 和 hierarchical search。
- 每个请求只使用完整旧 generation 或完整新 generation。
- 不出现新 Chunk + 旧 Layer。
- 两个管理员并发激活只有一个成功。
- route_version 过期的激活请求被拒绝。
- write_barrier 开启后不再领取新写任务。
- 已领取旧任务处理完后才切换。
- 最终 reconcile lag 不为 0 时激活失败。
- 第一个 Alias 成功、第二个 Alias 失败时 PostgreSQL route 仍然一致，告警产生且可重试。
- 激活失败时 active route 不变化。

#### 本步骤退出门禁

- Candidate 可以作为一个完整 generation 原子成为 active。
- 在线检索没有跨代窗口。

---

### 28.15 步骤 13：实现 Previous 增量镜像和安全回滚

#### 要解决的问题

只保留旧 Collection 不代表可回滚。新 generation 激活后新增、更新或删除的文件如果没有同步到 previous，直接回滚会：

- 丢失新文件。
- 恢复旧版本内容。
- 让已删除文件重新出现。

#### 采用的方法

1. rollback_deadline 之前为新增和更新文件创建 previous mirror 任务。
2. 删除始终同步传播到 previous。
3. 计算 previous_generation_lag_files。
4. 回滚前开启写入屏障并把 lag 追平到 0。
5. 校验 previous 模型仍可用、指纹一致、Collection 可加载。
6. 使用与激活相同的 PostgreSQL route 事务执行反向切换。
7. 如果 lag 非零且操作者未明确接受 RPO，拒绝回滚。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/mirror_service.py
- openrag/src/openrag/indexing/rollback_service.py
- openrag/tests/test_previous_generation_mirror.py
- openrag/tests/test_index_generation_rollback.py

需要修改：

- openrag/src/openrag/services/task_service.py
- openrag/src/openrag/worker/task_worker.py
- openrag/src/openrag/models/task.py
- openrag/src/openrag/api/index_generations_api.py

#### 涉及修改或新增的函数

PreviousGenerationMirrorService：

- enqueue_file_mirror
- mirror_file
- propagate_delete
- calculate_previous_lag
- reconcile_previous
- stop_mirroring

IndexRollbackService：

- rollback_to_previous
- _assert_previous_available
- _assert_rollback_rpo
- _drain_active_writes
- _reconcile_previous_before_rollback
- _switch_route_transaction
- _run_post_rollback_smoke_test

TaskWorker：

- _mirror_previous_generation_file_task

TaskType 增加：

- MIRROR_PREVIOUS_GENERATION_FILE

#### 涉及修改或新增的字段

IndexGenerationRoute：

- rollback_deadline

IndexGeneration 可增加汇总：

- mirror_lag_files
- last_mirrored_at

IndexGenerationFile 可增加：

- mirror_state
- mirrored_source_hash

如果不增加 mirror 专用字段，也必须能从 generation 文件状态准确推导 lag，不能只使用日志。

#### 校验有效性和准确性

必须验证：

- 激活后新增文件被 previous mirror 捕获。
- 更新文件在 previous 中使用最新内容 hash。
- 删除文件不会在回滚后重新出现。
- previous lag 为 0 时回滚后文件集合与回滚前 active 一致。
- lag 非零时无损回滚被拒绝。
- previous Embedding Provider 不可用时回滚失败但当前 active 不变化。
- 回滚后原新 generation 进入 retired，不被自动删除。
- 连续激活、回滚不会使 route_version 倒退。

#### 本步骤退出门禁

- 回滚能力具有明确 RPO 和强校验。
- 不再把“旧 Collection 仍存在”等同于“可安全回滚”。

---

### 28.16 步骤 14：实现退休、RBAC、备份和显式清理

#### 要解决的问题

Versioned Collection 会持续占用存储和内存，需要释放和删除。但清理是 R-05 中风险最高的操作，必须防止：

- 删除 active。
- 删除 previous。
- 删除 Alias 指向对象。
- 删除仍有任务写入的 candidate。
- 运行时账号绕过控制面直接 drop。

#### 采用的方法

1. 启用 Milvus 认证和最小权限角色。
2. API/Worker 只使用 runtime 身份。
3. Provisioner 使用 index-admin 身份。
4. DropCollection 凭据只注入独立清理命令或受控 Job。
5. 清理先生成 dry-run DeletePlan。
6. assert_deletable 验证全部防误删条件。
7. 操作者必须输入完整 generation_id 二次确认。
8. 可先 release 退休 Collection，过保留期后再物理删除。
9. 高成本 generation 按策略执行 Milvus Backup。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/retention_service.py
- openrag/src/openrag/indexing/backup_service.py
- openrag/src/openrag/cli/index_cleanup.py
- openrag/tests/test_generation_retention.py
- openrag/tests/test_generation_cleanup_guard.py
- openrag/tests/test_milvus_runtime_rbac.py

需要修改：

- openrag/src/openrag/config.py
- k8s/07-milvus-config.yaml
- k8s/09-api.yaml
- k8s/10-task-worker.yaml
- k8s/01-secret.example.yaml
- docker/docker-compose.prod.yml
- docker/docker-compose.worker.yml
- docker/.env.example

#### 涉及修改或新增的函数

RetentionService：

- calculate_delete_after
- list_retention_candidates
- release_retired_generation
- build_delete_plan
- assert_deletable
- mark_deleting
- mark_deleted

MilvusCollectionCleaner：

- drop_generation_collections
- verify_collections_absent

BackupService：

- should_backup_before_delete
- create_generation_backup
- verify_backup_manifest

CLI：

- cleanup_generation_dry_run
- cleanup_generation_execute

#### 涉及修改或新增的字段

VectorDBConfig：

- runtime_user
- runtime_password
- index_admin_user
- index_admin_password
- cleanup_user
- cleanup_password
- secure

IndexGeneration：

- delete_after
- backup_id
- backup_status
- deletion_plan
- deleted_at
- deleted_by

部署 Secret：

- MILVUS_RUNTIME_USER
- MILVUS_RUNTIME_PASSWORD
- MILVUS_INDEX_ADMIN_USER
- MILVUS_INDEX_ADMIN_PASSWORD
- MILVUS_CLEANUP_USER
- MILVUS_CLEANUP_PASSWORD

#### 校验有效性和准确性

安全测试必须使用真实 Milvus 权限：

- runtime 身份可以 Search、Query、Insert、Delete，但不能 CreateCollection、AlterAlias、DropCollection。
- index-admin 可以 Provision 和 Alias，但默认不能 DropCollection。
- cleanup 身份只在受控命令中使用。
- active、previous、Alias 指向、保留期未结束、存在运行任务时 delete 均被拒绝。
- dry-run 不产生任何 Milvus 写操作。
- 输入错误 generation_id 时拒绝。
- 删除后 PostgreSQL 保留 deleted 审计记录。
- 备份失败且策略要求备份时禁止删除。
- release 不改变 Collection 数据，重新 load 后仍可搜索。

#### 本步骤退出门禁

- 运行时无法执行破坏性 Collection 操作。
- 退休数据可以在明确门禁下安全清理。

---

### 28.17 步骤 15：完善健康检查、Trace、指标、告警和管理可视性

#### 要解决的问题

如果 generation 只存在于数据库和日志中，运维无法快速判断：

- 当前 active/previous 是谁。
- 候选构建到什么进度。
- Alias 是否漂移。
- Candidate 为什么不能激活。
- Previous 是否仍可回滚。

#### 采用的方法

1. 扩展健康检查，区分 liveness、readiness 和 generation 状态。
2. 所有 Trace 写入 generation 上下文。
3. 暴露构建、追平、Alias、回滚 lag 和删除失败指标。
4. 管理 API 返回结构化 validation/quality/delete plan。
5. 告警使用稳定错误码，不依赖文本匹配。

#### 涉及修改或新增的文件

建议新增：

- openrag/src/openrag/indexing/metrics.py
- openrag/src/openrag/indexing/health.py
- openrag/tests/test_index_generation_health.py
- openrag/tests/test_index_generation_metrics.py

需要修改：

- openrag/src/openrag/api/main.py
- openrag/src/openrag/services/trace_service.py
- openrag/src/openrag/api/traces_api.py
- openrag/src/openrag/api/index_generations_api.py
- openrag/src/openrag/processors/document_processor.py
- openrag/src/openrag/retrieval/retrieval_service.py

#### 涉及修改或新增的函数

建议新增：

- build_index_health_snapshot
- check_active_generation_readiness
- check_alias_consistency
- collect_generation_progress_metrics
- collect_previous_lag_metrics
- collect_cleanup_failure_metrics

需要修改：

- health_check
- TraceService.start_run
- TraceService.start_span
- _prepare_retrieval_trace
- _record_response_trace
- DocumentProcessor 的 embedding/vector Trace 输出

#### 涉及修改或新增的字段

健康响应增加：

- active_generation_id
- previous_generation_id
- route_version
- active_fingerprint
- chunk_collection_ready
- layer_collection_ready
- alias_consistent
- write_barrier
- candidate_states
- previous_lag_files

Trace 字段增加：

- index_generation_id
- route_version
- embedding_fingerprint
- embedding_revision
- collection_role
- collection_name

#### 校验有效性和准确性

必须验证：

- 健康接口不暴露密码、API Key 或完整内部异常。
- active Collection 缺失时 readiness 失败。
- Candidate 构建失败不导致 API liveness 失败。
- Alias 漂移产生告警但不会伪造 active route。
- Trace 中 generation_id 与实际查询 Collection 一致。
- 指标计数在任务重试时不重复累计。
- previous lag 和实际差异文件数一致。
- 删除失败可以定位到具体 generation 和 Collection。

#### 本步骤退出门禁

- 运维可以从 API、指标、Trace 和日志一致地识别索引状态。

---

### 28.18 步骤 16：执行首个 Versioned Generation 的生产迁移

#### 要解决的问题

前述代码能力完成后，还需要把 legacy active 安全迁移到第一个正式 versioned generation，才能真正关闭：

- 历史伪向量污染。
- 未知模型身份。
- 固定 Collection 路由。

#### 采用的方法

1. 先部署所有 additive 数据库和运行时兼容修改。
2. 注册并验证 legacy generation。
3. 确认运行时已经通过 route 使用 legacy。
4. 创建首个 versioned candidate。
5. 使用真实 Embedding 模型全量重建。
6. 完成水位追平、完整性验证、Eval 和 Shadow。
7. 在变更窗口执行激活。
8. 启动 previous 镜像并观察。
9. 回滚窗口结束前不删除 legacy。
10. 稳定后 release legacy，最后按审批清理。

#### 涉及修改或新增的函数

本步骤原则上不再新增核心业务函数，主要执行：

- bootstrap_legacy_generation
- create_generation
- provision_generation
- initialize_generation_files
- reconcile_until_stable
- validate_generation
- evaluate_candidate
- activate_generation
- reconcile_previous
- build_delete_plan

如果迁移中发现必须临时编写脚本，该脚本也必须：

- 只调用正式 Service。
- 支持 dry-run。
- 支持幂等。
- 不直接调用 Collection.drop。

#### 涉及修改或新增的字段

生产数据将写入：

- legacy IndexGeneration。
- active IndexGenerationRoute。
- candidate IndexGeneration。
- IndexGenerationFile 全量进度。
- validation_report。
- quality_report。
- AuditLog。

不得手工直接修改 state、active_generation_id 或 Alias。

#### 校验有效性和准确性

迁移前：

- PostgreSQL、Milvus 元数据和必要 Collection 已备份。
- API/Worker runtime 身份无 DropCollection。
- legacy 查询基线和 Eval 已保存。

构建中：

- active 错误率、空结果率和延迟无异常。
- candidate 不接收用户查询。
- failed_file_count 最终为 0。

激活前：

- build lag 为 0。
- validation_report 全部硬门禁通过。
- quality_report 获得批准。
- Candidate Chunk/Layer 均已加载。
- Previous 模型仍可调用。

激活后：

- active route、Alias 和健康检查一致。
- flat、hierarchical、权限过滤、文件删除 smoke test 通过。
- 新上传文件写入新 active。
- previous mirror lag 在预算内。
- 观察期内无质量和错误率异常。

清理前：

- rollback_deadline 已过。
- previous 不再被 route 引用。
- Alias 不指向 legacy。
- delete dry-run 已审批。

#### 本步骤退出门禁

- 线上 active 已是 versioned generation。
- legacy 不再承载在线查询和写入。
- 历史向量已使用真实模型全量重建。
- R-05 最终验收标准全部通过。

---

### 28.19 步骤级交付记录模板

每完成一个步骤，应在实施 PR 或进度文档中记录：

| 项目 | 内容 |
|---|---|
| 步骤编号 | 例如 R05-S06 |
| 解决的问题 | 本步骤关闭的具体风险 |
| 实际采用方法 | 与本文方案一致或偏差说明 |
| 修改文件 | 实际文件清单 |
| 新增函数/类 | 实际符号清单 |
| 修改函数 | 实际符号清单 |
| 数据库字段 | 实际新增/修改字段 |
| 配置字段 | 实际新增/修改环境变量 |
| 单元测试 | 测试名称和结果 |
| 集成测试 | Milvus/PostgreSQL/Worker 测试结果 |
| 故障注入 | 执行场景和结果 |
| 数据准确性 | 预期数与实际数 |
| 性能影响 | 构建 QPS、查询 p95、资源使用 |
| 未解决问题 | 明确留给后续步骤的事项 |
| 回滚方式 | 本步骤代码和数据迁移回滚方法 |
| 退出门禁 | 通过或未通过 |

任何步骤退出门禁未通过时，不进入依赖它的下一步骤。
