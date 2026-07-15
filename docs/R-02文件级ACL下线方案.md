# R-02 文件级 ACL 下线方案

> 状态：已完成（2026-07-15）
>
> 基线分支：`code-optimization`
>
> 基线提交：`a13e3cb43dab824cf2e39345d6293ba780cd6df6`
>
> 编写日期：2026-07-13
>
> 本文性质：实施方案、执行计划与验收记录

## 1. 执行摘要

OpenRag 当前同时存在两套权限表达：

1. 工作区级 RBAC：通过工作区成员、角色和服务令牌工作区绑定控制访问；
2. 文件级 ACL：通过 `FilePermission` 记录用户或团队对单个文件的 `read`、`write`、`admin` 权限。

代码审查确认，文件级 ACL 只存在于独立的模型、服务和管理接口中，没有进入文件列表、文件读取、预览、下载、移动、删除、重处理、检索以及 L0/L1/L2 数据面。当前真实生效的访问边界是工作区权限。因此，继续保留文件 ACL 会让调用方误以为授权或撤权已经改变内容可见性，形成“接口写入成功、实际权限不变”的确定性冲突。

本方案决定在一个版本内彻底下线文件级 ACL：

- 删除三个文件 ACL 接口，不提供 `410 Gone` 兼容期；
- 删除 `PermissionManager`、`FilePermission` ORM 及关联关系；
- 基于 192.168.100.33 当前生产库零记录结论，迁移先断言 `file_permissions` 仍为空，再删除活动表和不再使用的枚举；若最终发布前发现非零记录，迁移必须中止并回到人工评审；
- 认证用户的文件权限统一由工作区 RBAC 决定；
- `File.owner_id` 只保留归属和审计含义，不产生额外访问权限；
- 同一工作区的用户共享同一套 L0/L1/L2，不创建用户级摘要或索引变体；
- Team 文件 ACL 不自动转换为工作区权限，也不在本次新增 Team 到 Workspace 的授权模型。

本次下线不改变 ShareLink、服务令牌、嵌入式预览令牌等独立能力令牌的既有语义。

## 2. 背景与已确认事实

### 2.1 文件 ACL 当前包含的能力

当前文件 ACL 由以下部分组成：

- `openrag/src/openrag/models/permission.py`
  - `EntityType`：`user`、`team`；
  - `Permission`：`read`、`write`、`admin`；
  - `FilePermission`：记录文件、主体、权限和创建时间。
- `openrag/src/openrag/services/permission_manager.py`
  - 授权、撤权；
  - 用户和团队权限检查；
  - 父目录递归继承；
  - 查询用户可访问文件。
- `openrag/src/openrag/api/permissions_api.py`
  - 查询文件权限；
  - 给用户或团队授权；
  - 撤销权限。

这套实现是 allow-only ACL，没有显式 deny，也没有中断继承的能力。接口只允许文件 `owner_id` 对 ACL 进行管理，文件 ACL 中的 `admin` 本身不能管理 ACL。

### 2.2 文件 ACL 没有进入实际数据面

实际生产路径采用工作区权限：

- 文件列表：查询用户具有读取权限的工作区，再返回这些工作区下的文件；
- 文件详情、内容和预览：通过 `WorkspaceService.check_user_permission(..., "read")` 校验；
- 上传、创建目录、移动、删除和重处理：通过工作区 `write` 权限校验；
- JWT 检索：检索前校验目标工作区的 `read` 权限；
- 服务令牌检索：依据服务令牌绑定的工作区范围；
- L0/L1/L2：按文件和工作区组织，没有用户 ACL 维度。

生产文件和检索路径没有调用 `PermissionManager.check_permission()` 或读取 `FilePermission`。因此，写入或删除文件 ACL 不会改变实际内容访问结果。

### 2.3 已确认的权限冲突

当前实现会稳定地产生以下结果：

| 场景 | 文件 ACL 表达 | 实际结果 |
|---|---|---|
| 用户有工作区 read，但没有文件 ACL | 文件 ACL 看似无权 | 用户仍可读取和检索工作区内全部文件 |
| 用户有文件 read，但没有工作区权限 | 文件 ACL 看似授权 | 用户仍被拒绝访问 |
| 用户有工作区 write，但文件 ACL 只有 read | 文件 ACL 看似只读 | 用户仍可执行工作区 write 允许的文件操作 |
| 撤销文件 ACL，但保留工作区 read | 文件 ACL 看似撤权 | 用户仍可读取和检索文件 |
| 用户属于获得文件 ACL 的 Team，但没有工作区权限 | Team ACL 看似授权 | 用户仍被工作区鉴权拒绝 |

这不是并发或边界条件问题，而是两套权限模型没有被组合导致的确定性设计冲突。

### 2.4 调用面现状

- Web 前端没有调用三个文件 ACL 接口；
- 前端的 `permissionsAPI` 只调用 `/users/{user_id}/permissions/details`，展示工作区和角色权限；
- 文件 ACL 接口可能存在未知的外部调用方，仓库代码无法证明外部调用量；
- 历史端到端测试仍包含“授予文件 ACL 后获得访问”的旧预期，但这些预期与当前生产调用链不一致，且多数不在默认 pytest 收集范围内。

## 3. 目标、非目标与最终权限契约

### 3.1 目标

1. 消除不会实际生效的文件 ACL 接口和权限模型；
2. 将认证用户的数据访问边界统一为工作区 RBAC；
3. 保证同一工作区内 L0/L1/L2 可复用，不产生用户级摘要分裂；
4. 在生产零记录前提下安全删除空 ACL 表，并支持旧版本代码和数据库 schema 回滚；
5. 清除代码、测试和文档中对文件 ACL 的误导性描述。

### 3.2 非目标

本次不实施以下能力：

- 不新增文件级 deny、密级、标签权限或行级安全策略；
- 不把文件 ACL 重定义为 move、delete、share 等“文件管理能力”；
- 不新增 `TeamWorkspacePermission`；
- 不自动把旧文件 ACL 扩大为工作区权限；
- 不调整 ShareLink、服务令牌或嵌入式预览令牌；
- 不在本次实施 Milvus workspace partition、namespace 或 L0/L1/L2 重构；
- 不顺带重构工作区 RBAC 的角色模型或接口。

### 3.3 最终权限来源

文件 ACL 下线后，权限来源固定为：

| 调用类型 | 权限来源 | 说明 |
|---|---|---|
| JWT 用户访问文件和检索 | 工作区成员、用户角色、系统管理员 | 统一调用现有工作区权限解析逻辑 |
| 服务 API | 服务令牌及其工作区绑定 | 不受本次下线影响 |
| ShareLink | 分享令牌、密码、过期时间、访问次数 | 属于独立能力令牌，不转换为工作区成员 |
| 嵌入式预览 | 既有预览令牌约束 | 不受本次下线影响 |
| `File.owner_id` | 无授权能力 | 仅用于归属展示、审计和业务追踪 |
| Team 成员关系 | 无文件或工作区授权能力 | 只有显式工作区成员或用户角色才能获得工作区权限 |

工作区 owner 在创建工作区时拥有的权限，来自对应的工作区成员记录，而不是文件或工作区的 `owner_id` 字段本身。

### 3.4 操作权限矩阵

| 操作 | 所需权限 | 下线后的行为 |
|---|---|---|
| 文件列表、详情、内容、预览 | workspace read | 返回该工作区内所有可见文件 |
| 文件切片、上下文、L0/L1/L2 | workspace read | 同工作区用户共享相同层级数据 |
| 语义检索、层级检索、全文检索 | workspace read | 只在已授权工作区范围内检索 |
| 上传文件、创建目录 | workspace write | 不再检查文件 ACL |
| 移动、重命名、删除、重处理 | workspace write | 不再检查 owner 或文件 ACL |
| 工作区成员和配置管理 | 既有工作区管理鉴权 | 本次不改变现有规则 |
| 系统管理员操作 | 既有系统管理员旁路 | 本次不改变现有规则 |

一个用户即使仍是某个文件的 `owner_id`，一旦失去该文件所属工作区的权限，也不能继续读取或管理该文件。

## 4. 公共接口变化

### 4.1 删除的接口

以下接口在同一个版本中直接删除：

| 方法 | 路径 | 当前功能 | 下线后 |
|---|---|---|---|
| GET | `/files/{file_id}/permissions` | 查询文件 ACL | 路由不存在，返回 404 |
| POST | `/files/{file_id}/permissions` | 给用户或 Team 授权 | 路由不存在，返回 404 |
| DELETE | `/files/{file_id}/permissions/{permission_id}` | 撤销文件 ACL | 路由不存在，返回 404 |

三个接口同时从 OpenAPI schema 中移除。不保留隐藏路由，不返回 `410 Gone`，也不提供兼容响应体。

### 4.2 明确保留的权限接口

以下接口不是文件 ACL，必须保留：

- `GET /users/{user_id}/permissions/details`；
- 工作区成员的新增、查询和移除接口；
- 角色的工作区权限查询、设置和移除接口；
- 服务令牌及其工作区绑定接口。

前端现有 `permissionsAPI.getPermissionDetails()` 继续工作，不因后端删除文件 ACL 路由而改名或下线。

### 4.3 外部调用方处理

由于采用单版本直接删除，外部调用方需要在发布前完成排查：

1. 查询 API 网关、Nginx、应用访问日志中三个路径的调用量；
2. 按调用主体、来源 IP、User-Agent 和最近调用时间形成清单；
3. 通知仍在调用的系统改用工作区成员或角色授权；
4. 发布后监控三个旧路径的 404 数量，用于发现遗漏调用方。

旧的文件 ACL 授权请求不能自动改写为工作区授权，因为这会把单文件意图扩大为整个工作区访问。

## 5. 受影响模块与内部调整

### 5.1 API 路由模块

涉及文件：

- `openrag/src/openrag/api/permissions_api.py`
- `openrag/src/openrag/api/main.py`
- `openrag/src/openrag/api/files_api.py`

调整内容：

1. 从 `permissions_api.py` 删除：
   - 文件 ACL 专用 `router`；
   - `PermissionResponse`；
   - `GrantPermissionRequest`；
   - 文件 ACL 专用 `MessageResponse`；
   - 查询、授权和撤权三个处理函数；
   - `FilePermission`、`EntityType`、`Permission`、`PermissionManager`、`File` 等只服务于上述接口的导入。
2. 保留 `user_permissions_router` 以及 `/users/{user_id}/permissions/details` 的全部行为。
3. 从 `main.py` 删除 `file_permissions_router` 的导入和 `include_router()`，继续注册 `user_permissions_router`。
4. 修正 `files_api.py` 中“删除文件需要 owner 或 admin”等与实际工作区 write 鉴权不一致的注释，不改变接口行为。

不建议为保留单个用户权限详情接口而进行额外模块拆分；保持 `permissions_api.py` 作为通用权限接口模块，是本次最小改动。

### 5.2 权限服务模块

涉及文件：

- `openrag/src/openrag/services/permission_manager.py`

调整内容：

- 删除整个 `PermissionManager`；
- 删除授权、撤权、文件权限检查、父目录继承和文件列表计算逻辑；
- 不将这些方法改造成工作区权限方法；
- 工作区权限继续由 `WorkspaceService` 负责，避免产生第二个工作区权限实现。

仓库内不得再存在生产代码对 `PermissionManager` 的导入。

### 5.3 ORM 与模型注册

涉及文件：

- `openrag/src/openrag/models/permission.py`
- `openrag/src/openrag/models/file.py`
- `openrag/src/openrag/models/__init__.py`

调整内容：

1. 删除 `permission.py` 及其中的三个类型；
2. 从 `File` 删除 `permissions` relationship，以及 `TYPE_CHECKING` 块中的 `FilePermission` 类型导入；`TYPE_CHECKING` 本身仍被其他关系类型使用，必须保留；
3. 从模型包删除 `FilePermission`、`EntityType`、`Permission` 的导入与 `__all__` 导出；
4. 保留 `File.owner_id`、`File.owner` 关系以及用户的文件归属关系；
5. 新数据库执行 `Base.metadata.create_all()` 时不再创建 `file_permissions`。

ORM 移除和数据库迁移必须进入同一个发布版本。生产发布时先停止旧实例，再执行数据库迁移，最后启动已移除 ORM 的新实例。历史 `file_permissions` 表没有数据库级 `ON DELETE CASCADE`；如果只删 ORM 关系但保留带数据的活动表，删除文件可能触发外键约束错误。

### 5.4 数据库与 Alembic

新增迁移建议：

- 文件：`openrag/alembic/versions/20260713_0006_retire_file_acl.py`
- revision：`20260713_0006`
- down_revision：当前最新 revision `20260626_0005`

迁移职责：

1. 识别 `file_permissions` 是否存在；
2. 在迁移事务内断言活动表记录数为 `0`；
3. 记录数非零时立即失败并回滚；
4. 删除空的活动表；
5. 删除相关索引和约束；
6. 仅在没有其他依赖时删除文件 ACL 专用枚举；
7. downgrade 时重建空活动表、约束、索引和主键序列。

`Base.metadata.create_all()` 不负责历史表删除，因此生产升级不能只依赖 API 启动；必须显式执行 Alembic 迁移。

### 5.5 检索与 L0/L1/L2

涉及模块：

- `openrag/src/openrag/retrieval/filters.py`
- `openrag/src/openrag/retrieval/retrieval_service.py`
- JWT 搜索和服务令牌搜索入口

生产逻辑原则上不需要因文件 ACL 下线而改变：

- `PermissionFilter` 已经按用户可读工作区收集文件，不读取 `FilePermission`；
- `RetrievalService` 使用工作区范围构造候选文件，不使用文件 ACL；
- 搜索入口继续在执行向量或全文检索前验证工作区权限；
- L0/L1/L2 继续按工作区和文件复用，不加入 `user_id`、Team 或 ACL digest 维度。

本模块的实际调整主要是删除测试中的文件 ACL 假数据和错误描述，不借本次下线实施检索架构重构。

### 5.6 前端

前端没有文件 ACL 的增删查入口，因此不需要功能改动。

需要保留：

- 管理员权限页面；
- 用户权限页面；
- `permissionsAPI.getPermissionDetails()`；
- 角色和工作区权限管理。

验收时只需确认前端构建结果中不存在三个文件 ACL 路径，并验证工作区权限页面没有回归。

### 5.7 Team、ShareLink 与服务令牌

- Team 模型、成员关系和管理接口继续保留，但 Team 成员身份不再表达内容访问权限；
- 不新增 Team 到 Workspace 的授权表；
- ShareLink 继续作为显式能力令牌提供受控外发；
- 服务令牌继续通过工作区绑定限制服务 API；
- 不把历史 Team 文件 ACL 自动转成任何工作区权限。

### 5.8 测试

涉及测试：

- `test_permissions_api.py`：删除文件 ACL 行为测试，新增旧路径不存在和用户工作区权限接口保留的契约测试；
- `test_permission_manager.py`：随服务删除；
- `test_models.py`：删除 `FilePermission` 模型、唯一约束和级联删除测试；
- `test_files_api.py`：移除 ACL fixture，改为只通过工作区 read/write 构造场景；
- `test_retrieval_service.py`、`test_integration_retrieval.py`：删除 ACL 假数据，断言工作区范围；
- `test_e2e.py`：删除授权、撤权、Team 文件权限、权限持久性和授权性能场景，替换为工作区权限矩阵；
- 新增 PostgreSQL 迁移测试，覆盖零记录 upgrade、非零记录中止、downgrade、枚举清理和序列恢复。

当前 `pytest.ini` 只默认收集部分测试。ACL 下线的核心契约测试必须进入默认收集范围，不能只修改默认不执行的历史测试。

### 5.9 文档

实施时同步更新：

- `openrag/README.md`：删除“细粒度文件权限”产品宣称和 `FilePermission` 模型说明；
- `docs/framework detail.md`：将权限架构改为系统管理员、工作区 RBAC、独立能力令牌；
- `docs/storage.md`：删除活动 `file_permissions` 表说明，补充其已下线且生产零记录删除的状态；
- `docs/04-外部系统接入与API.md`：删除文件权限接口说明；
- `docs/CURRENT_MODEL_SCHEMA_DDL.md`：删除活动表及文件 ACL 专用枚举、索引；
- `docs/2026-07-10-OpenRag架构风险与检索优化评审.md`：将 R-02 标记为已决策、待实施或已完成；
- 历史设计文档：保留原文，在顶部增加“文件 ACL 已由 R-02 下线”的状态提示和本文链接。

历史文档不得被改写成“当时从未设计过文件 ACL”，以免丢失决策上下文。

## 6. 数据库迁移设计

### 6.1 生产数据盘点结论

2026-07-13 对 `192.168.100.33` 的外网部署做了只读核查：

| 检查项 | 结果 |
|---|---|
| PostgreSQL 容器 | `openrag-postgres-prod` |
| 数据库 | `openrag` |
| `file_permissions` 表 | 存在 |
| `file_permissions` 总记录数 | `0` |
| 可关联到现有文件的记录数 | `0` |
| 孤立文件记录数 | `0` |
| `workspace_id` 为空的记录数 | `0` |
| 涉及文件数 | `0` |
| 涉及主体数 | `0` |
| 创建时间范围 | `NULL` |
| 近 720 小时 API 日志中的旧 ACL 路径命中 | `0` |

因此当前生产库没有需要归档或人工迁移的活动文件 ACL 数据。

这里需要保留一个边界说明：`file_permissions` 的撤权是物理删除，API 日志也不一定覆盖完整历史，所以“当前记录数为 0”不能严格证明过去从未有人调用过文件 ACL 接口。它只能证明当前没有仍需迁移的有效记录。发布前数据库物理备份仍然必须保留，用于审计和灾难恢复。

### 6.2 迁移策略

默认实施路径不再创建 `file_permissions_archive` 归档表。

迁移的核心策略改为：

1. 发布前人工执行最终统计，确认 `file_permissions` 仍为 `0` 行；
2. `alembic upgrade` 在同一事务内再次读取 `file_permissions` 记录数；
3. 如果记录数为 `0`，删除空的 `file_permissions` 活动表、索引、约束和不再使用的专用枚举；
4. 如果记录数大于 `0`，迁移立即失败并回滚，不删除活动表，不创建工作区权限，不自动扩权；
5. 发现非零记录后，停止本次发布，重新评审是否恢复“归档后删除”的完整方案。

这个策略比归档表更简单，也更贴合当前生产事实：没有活动 ACL 数据时，创建长期只读归档表只会增加迁移、权限和回滚复杂度。

### 6.3 枚举兼容

当前 ORM 自动生成的 PostgreSQL 枚举名称是 `entitytype` 和 `permission`，而参考 DDL 文档使用 `entity_type` 和 `acl_permission`。迁移不能假设所有环境的类型名称完全一致。

迁移应从 PostgreSQL catalog 获取 `file_permissions.entity_type` 和 `file_permissions.permission` 实际绑定的类型名称，在删除活动表后仅删除：

1. 确认由这两个列使用的用户自定义枚举；
2. 已经没有其他列或对象依赖的枚举。

禁止使用不加判断的 `DROP TYPE ... CASCADE`，避免误删其他表的依赖对象。

### 6.4 Upgrade 行为

升级流程必须在同一事务内完成核心步骤：

1. 判断 `file_permissions` 是否存在；
2. 如果活动表不存在，视为已完成或全新环境，继续执行后续无害清理；
3. 如果活动表存在，获取记录数并要求记录数等于 `0`；
4. 记录数大于 `0` 时抛出异常并回滚；
5. 删除空的 `file_permissions` 表；
6. 删除无依赖的专用枚举；
7. 写入迁移日志，明确本次按“零记录删除空表”路径完成。

迁移不能在发现非零记录时自动创建 `file_permissions_archive`。非零记录意味着生产事实已经变化，需要先停下来重新评审，而不是在发布窗口内临时扩大方案。

### 6.5 Downgrade 行为

downgrade 必须支持旧版本代码重新启动，但由于 upgrade 只允许零记录表通过，因此 downgrade 只恢复空 schema：

1. 重建旧 ORM 兼容的枚举；
2. 重建空的 `file_permissions` 表；
3. 恢复文件和工作区外键、唯一约束及两个索引；
4. 重建主键序列，并设置为初始可写状态；
5. 验证旧模型可以查询空表并新增授权记录；
6. 不创建、不读取 `file_permissions_archive`。

应用回滚顺序固定为：先停止新版本流量，执行数据库 downgrade，确认活动表 schema 恢复，再部署旧版本代码。禁止先启动依赖 `file_permissions` 的旧代码再恢复数据库。

### 6.6 备份与审计

- 发布前仍必须完成数据库物理备份；
- 物理备份是本次零记录路径下的唯一历史审计兜底；
- 应用代码不新增归档 ORM、API 或服务路径；
- 后续如果在其他环境发现非零 ACL 数据，需要单独评审是否采用归档方案，不能直接套用本生产零记录路径。

## 7. 历史授权迁移规则

### 7.1 基本原则

当前生产库没有活动文件 ACL 记录，因此本次不需要迁移任何用户、Team 或文件级授权。

即便后续发现非零记录，也不能自动生成新权限。原因是：

- 单文件 read 自动转成 workspace read 会扩大可见范围；
- 单文件 write/admin 自动转成 workspace write 会扩大修改和删除能力；
- Team 文件 ACL 自动转成全 Team 工作区权限，需要一个当前不存在的新授权模型；
- 历史 ACL 本来没有进入数据面，不能把一条此前不生效的记录自动变成新的有效授权。

### 7.2 发布前最终确认

发布前只保留以下确认动作：

- 再次统计 `file_permissions` 记录总数，要求为 `0`；
- 再次检查近期 API、网关或 Nginx 日志中的三个旧路径调用；
- 将统计结果写入发布记录；
- 如果记录数仍为 `0`，不输出人工授权迁移清单；
- 如果记录数大于 `0`，中止发布，输出主体、文件、工作区和权限聚合清单，交由管理员评审。

管理员只有在确认业务主体需要访问整个工作区时，才通过既有方式进行迁移：

- 把用户加入工作区并授予 read/write；或
- 给用户分配具有该工作区权限的角色。

Team 需要迁移时，由管理员明确选择具体用户加入工作区；本次不提供批量自动扩权脚本。

## 8. 发布方案

### 8.1 发布前检查

1. 确认数据库物理备份可恢复；
2. 最终统计 `file_permissions`，确认记录数仍为 `0`；
3. 检查 API 网关和应用日志中的旧接口调用；
4. 若发现非零 ACL 记录，停止发布并输出人工评审清单；
5. 在预发布 PostgreSQL 上完成 upgrade、downgrade、再次 upgrade 演练；
6. 确认新版本代码不再导入 `FilePermission` 或 `PermissionManager`；
7. 确认 OpenAPI 不含旧路径。

### 8.2 生产发布顺序

由于旧版本代码仍会访问活动表，本次采用维护窗口发布：

1. 阻断写入并停止旧 API 实例流量；
2. 停止可能启动旧 API 代码的自动拉起或滚动更新；
3. 完成数据库备份和 ACL 最终统计；
4. 执行 `alembic upgrade head`，在零记录断言通过后删除空活动表；
5. 部署不含文件 ACL 的新 API 和 worker；
6. 执行数据库、OpenAPI、权限矩阵和检索 smoke test；
7. 恢复流量；
8. 监控旧路径 404、权限 403、文件操作失败和检索空结果。

worker 当前不使用文件 ACL，但仍应与 API 使用同一版本，避免运行环境中的模型注册不一致。

### 8.3 回滚顺序

1. 重新进入维护状态并停止新版本流量；
2. 执行 Alembic downgrade，恢复空活动表、约束、索引和序列；
3. 验证旧模型能查询空权限表并新增授权记录；
4. 部署旧版本 API 和 worker；
5. 验证三个文件 ACL 接口恢复；
6. 恢复流量。

如果 downgrade 失败，不得启动旧版本；应保持维护状态，从数据库备份人工恢复。

## 9. 测试与验收方案

### 9.1 API 契约测试

- 三个旧路径在有认证和无认证情况下均不再匹配业务路由；
- OpenAPI schema 不包含旧路径；
- `/users/{user_id}/permissions/details` 正常返回角色和工作区权限；
- 工作区成员和角色权限接口正常；
- 前端权限页面可正常加载。

### 9.2 工作区权限矩阵

至少覆盖：

| 身份 | 读文件/检索 | 上传/移动/删除/重处理 | 预期 |
|---|---|---|---|
| workspace read | 允许 | 拒绝 | 写操作 403 |
| workspace write | 允许 | 允许 | 全部按既有规则执行 |
| workspace admin/owner member | 允许 | 允许 | 权限来自成员记录 |
| 系统管理员 | 允许 | 允许 | 保持现有旁路 |
| 无工作区权限 | 拒绝 | 拒绝 | 不因 owner_id 或旧 ACL 放行 |
| 仅为文件 owner_id | 拒绝 | 拒绝 | 没有工作区权限时无特权 |
| 仅为 Team 成员 | 拒绝 | 拒绝 | Team 身份不授予工作区访问 |
| 仅有旧文件 ACL 测试数据 | 拒绝 | 拒绝 | 文件 ACL 不参与鉴权 |

### 9.3 L0/L1/L2 与检索测试

- 同一工作区两个 read 用户得到相同的 L0/L1/L2 候选和结果；
- 用户失去工作区权限后，不能通过文件搜索、层级搜索、chunk 查询或预览继续访问；
- 不同工作区中的唯一短语不能跨工作区召回；
- Team 成员身份不改变搜索结果；
- 服务令牌只能访问绑定工作区；
- ShareLink 仍按分享令牌规则工作。

### 9.4 数据库迁移测试

在 PostgreSQL 上覆盖：

1. 空 ACL 表升级；
2. 非空 ACL 表升级必须中止并保留活动表；
3. 活动表不存在时升级可通过；
4. ORM 枚举命名环境和参考 DDL 枚举命名环境；
5. 零记录断言失败时事务回滚；
6. 升级后活动表、索引和专用枚举不存在；
7. 删除曾经关联空 ACL 表的文件不再触发外键错误；
8. downgrade 后空表、约束、索引和主键序列恢复；
9. downgrade 后新增授权记录不会发生主键冲突；
10. 全新数据库通过 `init_db()` 不创建活动 ACL 表。

### 9.5 静态检查

实施完成后，以下搜索应没有生产代码命中：

```text
FilePermission
PermissionManager
file_permissions_router
/files/{file_id}/permissions
```

允许命中的位置只有：

- 本方案和历史文档中的废弃说明；
- Alembic 迁移中的旧表名称；
- 明确验证旧路径不存在的测试。

## 10. 风险与控制措施

| 风险 | 影响 | 控制措施 |
|---|---|---|
| 未知外部客户端仍调用文件 ACL | 发布后收到 404 | 发布前日志盘点，发布后监控旧路径 404 |
| 误把历史 ACL 自动扩大成工作区权限 | 数据越权 | 禁止自动转换，只允许管理员人工授权 |
| 最终统计发现非零 ACL 记录 | 原零记录方案不再适用 | 迁移中止，保留活动表，重新评审归档或人工迁移方案 |
| 删除活动表导致历史审计线索减少 | 无法直接从业务库查询旧 ACL | 物理备份、发布记录、迁移内零记录校验、可逆 migration |
| 只删 ORM 不删活动表 | 文件删除出现外键错误 | 同一发布完成零记录断言和空活动表删除 |
| 错误删除共享枚举 | 其他表损坏 | 从 catalog 获取实际类型，只删除无依赖枚举，禁止 CASCADE |
| 旧代码与新数据库同时运行 | ACL 接口 500 | 维护窗口停止旧实例后迁移 |
| 历史测试继续表达错误语义 | 后续开发重新引入冲突 | 清理测试并把工作区权限矩阵纳入默认测试集 |
| 文档仍宣称细粒度文件权限 | 产品和实现再次不一致 | 同步 README、架构、API、存储和 DDL 文档 |
| owner_id 被误当成授权 | 被移出工作区的所有者仍被错误放行 | 明确 owner_id 仅审计，并增加负向测试 |

## 11. 完成标准

以下条件全部满足后，R-02 才能标记完成。当前状态已于 2026-07-15 标记为完成：

- [x] 三个文件 ACL 接口返回 404，且不在 OpenAPI 中；
- [x] `/users/{user_id}/permissions/details` 和工作区 RBAC 接口正常；
- [x] 生产代码不存在 `FilePermission`、`PermissionManager` 和文件 ACL 路由；
- [x] `File.permissions` ORM 关系和模型导出已删除；
- [x] 生产发布前和迁移内均确认 `file_permissions` 为 `0` 行；
- [x] 空活动表和无依赖专用枚举已删除；
- [x] downgrade 可恢复空表、约束、索引和主键序列；
- [x] 文件读写和检索完全由工作区权限决定；
- [x] owner_id、Team 身份和旧文件 ACL 都不会单独授予访问；
- [x] 同工作区用户共享相同 L0/L1/L2；
- [x] ShareLink、服务令牌和前端工作区权限页面无回归；
- [x] ACL 下线核心测试进入默认测试集并通过；
- [x] README、架构、API、存储、DDL 和风险评审文档已同步；
- [x] 发布和回滚演练均通过。

## 12. 评审决策记录

本方案已锁定以下决策，实施阶段不得自行更改：

| 决策项 | 结论 |
|---|---|
| 下线节奏 | 单版本彻底删除 |
| 旧接口行为 | 普通 404，不保留 410 兼容路由 |
| 最终权限模型 | 认证用户仅使用工作区 RBAC |
| owner_id | 只用于归属和审计 |
| 历史数据 | 当前生产零记录，不创建归档表；若最终发现非零记录则中止发布 |
| 数据库回滚 | 支持恢复空 `file_permissions` schema |
| Team 替代方案 | 不新增 Team 工作区授权，不自动扩权 |
| L0/L1/L2 | 工作区内共享，不引入用户 ACL 变体 |
| ShareLink/服务令牌 | 保持既有语义 |
| 本文交付范围 | 只提供方案文档，审核后再实施代码 |

## 13. 具体执行计划

### 13.1 执行原则、顺序与中止条件

本节是审核通过后的实施顺序，不表示相关代码已经完成。每一步必须独立提交或至少形成可单独审查的变更集合；上一步校验不通过时，不进入下一步。

执行顺序固定为：

1. 冻结基线并完成生产前置盘点；
2. 先建立下线后的 API 和权限契约测试；
3. 删除文件 ACL API；
4. 删除文件 ACL 服务；
5. 删除文件 ACL ORM 与模型注册；
6. 实现并验证 Alembic 迁移；
7. 将文件操作和检索测试统一为工作区 RBAC；
8. 验证 L0/L1/L2、Team、ShareLink 和服务令牌边界；
9. 同步文档并检查前端；
10. 完成预发布集成验收；
11. 在维护窗口发布并持续监控；
12. 完成回滚演练，并在生产触发条件满足时按固定顺序回滚。

全流程中止条件：

- 任一环境的 `file_permissions` 在升级前出现非零记录；
- 无法确认 PostgreSQL 枚举的实际类型名、标签值或依赖对象；
- 三个旧接口仍存在已确认但未完成迁移的外部调用方；
- 工作区权限矩阵、跨工作区隔离或 L0/L1/L2 共享契约出现回归；
- 数据库 downgrade 不能恢复旧代码可用的空 schema；
- 当前工作区中的其他未提交修改与本计划修改同一代码段且无法安全合并。

### 13.2 步骤一：冻结实施基线并完成前置盘点

#### 目的

在修改代码前固定可追溯基线，确认“生产活动 ACL 为零”和“没有已知调用方”仍然成立，避免在已经变化的生产事实之上继续使用零记录删除方案。

#### 方法

1. 记录实施分支、提交 SHA、工作区未提交文件和 Alembic 当前 head；不执行 `git reset`、`git checkout --` 等会覆盖现有修改的操作。
2. 对 `file_permissions` 执行最终只读统计，并记录表结构、约束、索引、序列、两列枚举实际绑定的类型名和枚举标签值。
3. 检查 PostgreSQL catalog，记录枚举是否被其他表、列、函数或默认值依赖。
4. 检查 API、网关和 Nginx 日志中三个旧路由的近期调用；按来源形成发布记录。
5. 完成数据库物理备份，并在隔离环境验证备份可读取。

#### 解决的问题

- 防止生产表在方案审核后被写入而仍被当作空表删除；
- 防止仅依据参考 DDL 猜测枚举名称或标签；
- 防止误覆盖当前工作区中与 R-02 无关的修改；
- 为审计、回滚和外部调用方排查保留证据。

#### 涉及的函数、字段与对象

本步骤不修改仓库函数或字段，只读取以下数据库对象：

| 对象 | 读取内容 |
|---|---|
| `file_permissions` | 总行数、列定义、默认值、外键、唯一约束和索引 |
| `file_permissions.id` | 主键与实际序列/identity 配置 |
| `file_permissions.file_id` | `files.id` 外键定义 |
| `file_permissions.workspace_id` | 可空性及 `workspaces.id` 外键定义 |
| `file_permissions.entity_type` | 实际枚举类型名与标签 |
| `file_permissions.entity_id` | 整数类型与非空约束 |
| `file_permissions.permission` | 实际枚举类型名与标签 |
| `file_permissions.created_at` | 类型、默认值和非空约束 |
| `uq_file_entity` | `(file_id, entity_type, entity_id)` 唯一约束 |
| `idx_permission_file_id`、`idx_permission_entity` | 索引定义 |

#### 本步骤校验

- `alembic heads` 只有一个 head，且其 revision 与迁移计划的 `down_revision` 一致；
- 发布记录中包含分支、SHA、未提交文件清单、数据库统计时间和日志查询时间范围；
- `SELECT COUNT(*) FROM file_permissions` 返回 `0`；若大于 `0`，立即中止 R-02；
- 枚举类型名、标签及依赖对象均已形成清单，不以文档猜测代替 catalog 结果；
- 备份文件存在且可在隔离环境读取基本 schema；
- 已知外部调用方为零，或全部具有明确迁移负责人和完成时间。

#### 步骤一执行记录（2026-07-13）

**执行状态：待 testing agent 与 review agent 复核。**

- 实施分支：`code-optimization`；冻结提交：`a13e3cb43dab824cf2e39345d6293ba780cd6df6`。
- 本地 Alembic 版本文件静态解析结果：唯一 head 为 `20260626_0005`（`20260626_0005_add_file_deleted_at.py`）；生产 `alembic_version` 同为 `20260626_0005`。
- 实施前已有未提交代码修改：`search_api.py`、`service_api.py`、`retrieval_service.py`、`test_retrieval_trace.py`、`test_scoped_retrieval.py`、`test_service_api.py`，均属于 R-01 工作；R-02 执行中必须保留。步骤八会接触相关检索测试，但当前未发现无法安全合并的同段修改。
- 生产核查时间：`2026-07-13T17:05:59+08:00` 至 `2026-07-13T17:08:31+08:00`；主机：`192.168.100.33`；当前发布目录：`openrag-1.1.7`。
- PostgreSQL：`16.14`；数据库：`openrag`；数据库大小约 `239 MB`；`file_permissions` 总记录数为 `0`。
- 表结构：`id` 使用 `public.file_permissions_id_seq`；`file_id`、`entity_id`、`entity_type`、`permission`、`created_at` 非空，`workspace_id` 可空；`created_at` 默认 `now()`。
- 约束与索引：主键 `file_permissions_pkey`；外键指向 `files.id`、`workspaces.id`；唯一约束 `uq_file_entity(file_id, entity_type, entity_id)`；索引 `idx_permission_file_id`、`idx_permission_entity` 均存在。
- 实际枚举：`public.entitytype` 标签为 `USER`、`TEAM`；`public.permission` 标签为 `READ`、`WRITE`、`ADMIN`。catalog 依赖仅包含 `file_permissions` 对应列和 PostgreSQL 自动生成的枚举数组类型，未发现其他表、列、函数或默认值依赖。
- 旧路由日志核查范围：近 `720h`；`openrag-api-prod` 与 `openrag-web-prod` 对 `/files/{id}/permissions` 的命中数均为 `0`，当前没有已知外部调用方证据。
- 已生成在线一致物理备份并通过 `pg_verifybackup`：`/home/guozhi/Documents/OpenRag/backups/openrag-r02-preflight-20260713-170801.tar.gz`，大小 `66,638,032` 字节，SHA-256 为 `41b1fe07b857475064253edd33e174de8153d9ba12e6a493bc12d9388c75cece`；归档可读取 `backup_manifest`、`PG_VERSION` 和 `global/pg_control`。
- 未执行 `git reset`、`git checkout --`、生产迁移、容器重启或流量切换。

##### Testing agent 校验结论（2026-07-13 17:17:54 +08:00）

**总体结论：PASS。未触发步骤一或全流程中止条件，可以提交 review agent 复核，但本结论不授权进入步骤二。**

| 校验类别 | 独立复核结果 | 结论 |
|---|---|---|
| Git 基线 | 当前分支为 `code-optimization`，HEAD 为 `a13e3cb43dab824cf2e39345d6293ba780cd6df6`。未提交代码文件为 `openrag/src/openrag/api/search_api.py`、`service_api.py`、`retrieval/retrieval_service.py`、`openrag/tests/test_retrieval_trace.py`、`test_scoped_retrieval.py`、`test_service_api.py`；未跟踪文档为 `docs/2026-07-10-OpenRag架构风险与检索优化评审.md`、`docs/R-01权限解析Fail-Open修复方案.md`、本方案文档。与冻结基线及 R-01 保留说明一致。 | PASS |
| Alembic 迁移图 | 静态读取 5 个版本文件并计算 revision/down_revision 图，唯一 head 为 `20260626_0005`；生产 `alembic_version` 也为 `20260626_0005`。 | PASS |
| SSH 与生产只读核查 | `guozhi@192.168.100.33` 可达；复核时间 `2026-07-13T17:10:01+08:00` 至 `17:11:39+08:00`。PostgreSQL 为 `16.14`，数据库为 `openrag`、约 `239 MB`，`SELECT COUNT(*) FROM public.file_permissions` 返回 `0`。 | PASS |
| 表结构与枚举 | 逐列、约束、索引及序列结果与执行记录一致：`id` 使用 `public.file_permissions_id_seq`，主键、两个外键、`uq_file_entity`、`idx_permission_file_id`、`idx_permission_entity` 均存在；`public.entitytype={USER,TEAM}`，`public.permission={READ,WRITE,ADMIN}`。catalog 依赖仅为两列和各枚举自动数组类型，未发现其他表、列、函数或默认值依赖。 | PASS |
| 720h 旧路由日志 | `openrag-api-prod` 和 `openrag-web-prod` 对 `/files/{id}/permissions`（含子路径）的计数均为 `0`。宽泛 `/permissions` 各有 1 条，独立查看后均为应保留的 `GET /users/1/permissions/details`，不是旧文件 ACL 路由。 | PASS |
| 备份存在性与摘要 | 备份存在，实测大小 `66,638,032` 字节；独立 `sha256sum` 为 `41b1fe07b857475064253edd33e174de8153d9ba12e6a493bc12d9388c75cece`，与记录完全一致；`gzip -t` 通过。 | PASS |
| tar、manifest 与验证工具证据 | tar 可列出且读取唯一 `backup_manifest`、`global/pg_control` 和 `PG_VERSION`；manifest 版本为 `1`，包含 WAL 范围及 `Manifest-Checksum=7cd5639f660e5f8c84a98d21ef5b26fff9ba0b0555d2922a9ee3fac80277fb41`。主机存在 `pg_verifybackup 16.14`。 | PASS（只读证据范围内） |

遗留限制与观察项：

- 本地 `alembic` 命令因当前 PATH 指向的 Python 环境缺少可执行模块而未成功启动；本次按步骤要求采用版本文件静态图解析，唯一 head 结论不受影响，后续迁移测试必须在项目正式测试环境中执行 Alembic CLI。
- 备份目录未保留解压目录或独立的 `pg_verifybackup` 输出日志；在“不写入、不重复生成、不解压”的边界下，testing agent 未重放完整 `pg_verifybackup`，只能独立确认压缩包摘要、gzip/tar 完整可读、manifest 内容及验证工具版本。步骤一执行记录中的“已通过 `pg_verifybackup`”仍依赖原执行会话证据。
- 720 小时统计覆盖两个当前容器仍保留的 stdout/stderr 日志，不能证明已轮转或上游未保留日志中的历史事实；当前可查询范围未发现旧文件 ACL 调用方。
- `openrag-web-prod` 在 `docker ps` 中显示 `unhealthy`，但容器日志可读且不影响本次旧路由计数；这不是 R-02 步骤一既定中止条件，仍应由发布负责人在预发布健康检查中单独处理。

##### Review agent 审查意见（2026-07-13 17:27:46 +08:00）

**结论：CHANGES_REQUIRED。当前不同意进入步骤二。**

审查认可以下事实：分支、HEAD、Alembic 唯一 head、生产 `file_permissions=0`、表/枚举/catalog 清单与备份文件摘要均有两轮可交叉复核的证据。当前只有 6 个 R-01 未提交代码/测试文件；R-02 步骤二预计新增的 `test_acl_retirement.py` 和 `pytest.ini` 修改均未出现，旧 ACL 路由、服务、ORM 和表仍在，未发现越过步骤一的代码删除、生产迁移、容器重启或流量切换。新建物理备份是步骤一显式要求，不属于越权的生产变更。

放行前必须补齐两项证据：

1. 将“近 `720h`”修正为真实可查窗口。API/Web 容器始于 2026-07-01，当前日志最早也是 2026-07-01，因此 `--since 720h` 只表示查询参数，不代表实际覆盖 720 小时。应记录实际最早/最晚日志时间，并继续查找已转储的 API、网关或 Nginx 日志；如确实不存在，需由发布负责人明确接受“仅覆盖当前容器约 12 天”的调用方排查剩余风险，不得继续表述为 720 小时零调用。
2. 保留可复核的完整备份验证证据，并实际满足“隔离环境可读取基本数据库 schema”。当前独立证据只能证明 SHA-256、gzip/tar、manifest 和基本文件可读；未保留完整 `pg_verifybackup` 输出，也没有隔离恢复后读取 `alembic_version`、`file_permissions` 定义等基本 schema 的记录。补齐后应由 testing agent 再次独立复核。

R-01 重叠方面，其未提交修改已触及 `assert_search_workspace_read()` 和多个 `RetrievalService` 范围解析函数，而 R-02 步骤八将验证这些函数。当前不属于“同段无法安全合并”，因为 R-02 计划明确不修改这些生产函数，也不与步骤二的新文件重叠；但必须保持 R-01 为独立可审查变更集。如后续 R-02 需要修改这些脏文件的同一代码段，或步骤八校验因 R-01 发生回归，必须按 13.1 中止并单独评审。

`openrag-web-prod` 的 `unhealthy` 为持续的本地健康探针拒绝连接，但 API、PostgreSQL 健康，且 Web/Nginx 日志可读。它不在步骤一或 13.1 的中止条件内，因此不单独阻断 R-02 代码工作；但应作为独立的现存运维风险记录，并在步骤十预发布健康验收之前闭环，不得把它归因于 R-02。

##### 步骤一整改记录（2026-07-13 17:25:47 +08:00 至 17:31:00 +08:00）

**整改状态：备份验证缺口已补齐；日志覆盖缺口等待发布负责人明确接受剩余风险，尚未申请进入步骤二。**

1. 基于既有物理备份完成完整 `pg_verifybackup` 重放，输出为 `backup successfully verified`。证据文件：`/home/guozhi/Documents/OpenRag/backups/r02-verify-20260713-170801/pg_verifybackup.log`，SHA-256 为 `9b05d10b5b9ef95da22f1093911c001e6b7c0bec96590501219b218697dc2f8e`。
2. 将备份解压到隔离临时目录，并使用 `postgres:16-alpine`、`--network none`、无宿主端口的临时容器启动恢复副本。隔离副本成功读取：数据库 `openrag`、PostgreSQL `16.14`、Alembic revision `20260626_0005`、`file_permissions=0`；同时读取了 7 个字段、4 个约束及主键默认序列。证据文件：`/home/guozhi/Documents/OpenRag/backups/r02-verify-20260713-170801/isolated-schema-check.log`，SHA-256 为 `17439d65bbd4c6425a50848b0f63ef7d9df25484ab1d69565b7ae4f9cffa0f5d`。
3. 隔离验证结束后，临时容器 `openrag-r02-restore-verify` 和解压目录均已删除；原物理备份和两份验证日志保留。生产 PostgreSQL、API、Web、Worker 未重启、未迁移、未切换流量。
4. 日志口径已纠正：当前 `openrag-api-prod` 创建于 `2026-07-01T08:12:43Z`、`openrag-web-prod` 创建于 `2026-07-01T08:21:00Z`，因此此前 `--since 720h` 的查询参数实际最多覆盖当前容器自 2026-07-01 起约 12 天，不等于完整 720 小时。
5. 已检查当前及已停止的 OpenRag 容器、容器日志配置、挂载目录、服务器部署目录内日志候选和历史 release。服务器只保留当前 API/Web 容器，日志驱动为 `json-file`，没有宿主目录日志挂载；部署目录中未找到可覆盖 2026-06-13 至 2026-07-01 的 API、网关或 Nginx 访问日志归档。因此可复核结论只能表述为：“2026-07-01 至核查时当前容器日志中旧文件 ACL 路由命中为 0；更早约 18 天无可用日志证据。”
6. 在缺少历史日志归档的情况下，步骤一无法技术性补足完整 720 小时。继续实施需要发布负责人明确接受“调用方排查仅覆盖当前容器约 12 天”的剩余风险；否则按 review agent 意见保持 `CHANGES_REQUIRED`。

##### Testing agent 整改复核结论（2026-07-13 17:32 +08:00）

**总体结论：PARTIAL。备份验证整改已通过；日志历史覆盖缺口属于无法由技术复核补齐的剩余风险，在发布负责人明确接受前，不同意进入步骤二。**

| 复核项 | 独立只读证据 | 结论 |
|---|---|---|
| 两份验证日志与摘要 | `pg_verifybackup.log` 和 `isolated-schema-check.log` 均存在；实测 SHA-256 分别为 `9b05d10b5b9ef95da22f1093911c001e6b7c0bec96590501219b218697dc2f8e`、`17439d65bbd4c6425a50848b0f63ef7d9df25484ab1d69565b7ae4f9cffa0f5d`，与整改记录一致。 | PASS |
| 物理备份完整性 | `pg_verifybackup.log` 内容为 `backup successfully verified`，已形成可独立复核的完整验证结论。 | PASS |
| 隔离 schema 读取 | `isolated-schema-check.log` 包含 `isolated_restore=PASS`，并记录数据库 `openrag`、PostgreSQL `16.14`、Alembic revision `20260626_0005`、`file_permissions=0`、7 个字段的类型/可空性/默认值以及主键、两个外键和 `uq_file_entity` 共 4 个约束。 | PASS |
| 临时资源清理 | `docker ps -a --filter name=openrag-r02-restore-verify` 无结果；备份目录下未发现名称含 `restore` 的解压目录。原备份和两份验证日志仍保留。 | PASS |
| 生产容器未重启 | PostgreSQL、API、Web 和 `openrag-task-worker` 的启动时间均为 2026-07-01，`RestartCount` 均为 `0`，早于本次 2026-07-13 隔离验证；未见因整改导致的生产重启。 | PASS |
| 当前日志实际覆盖范围 | API 创建于 `2026-07-01T08:12:43Z`，现存日志从 `2026-07-01T08:12:52Z` 开始；Web 创建于 `2026-07-01T08:21:00Z`，现存日志从 `2026-07-01T08:21:02Z` 开始。至复核时约覆盖 12 天，旧文件 ACL 路由命中均为 `0`。 | PASS（仅限现存窗口） |
| 更早日志可获得性 | API/Web 均使用 `json-file` 且未配置单独日志选项；各自容器目录只有当前 JSON 日志文件，没有轮转副本。API 仅挂载 uploads 和层级摘要卷，Web 仅挂载 `app-config.js`，均无宿主访问日志目录挂载；现存 OpenRag 容器中没有更早的 API/Web 容器，部署 release、shared、scripts 目录也未发现生产 API、网关或 Nginx 访问日志归档。 | PASS（“没有可用证据”的核查） |
| 720 小时调用方排查 | 从计划查询起点约 `2026-06-13` 到现存日志起点 `2026-07-01` 的约 18 天没有可用访问日志，无法证明该窗口内旧路由调用为零。因此整改记录中的“约 12 天覆盖、更早约 18 天无证据”表述准确，但不能等同于完整 720 小时无调用方。 | PARTIAL |

步骤一的备份存在性、完整性和隔离可恢复读取要求已经补齐，不再构成阻断项。唯一剩余阻断是历史调用日志覆盖不足：若发布负责人明确接受该剩余风险，可由 review agent 再判断是否放行；在此之前维持 `CHANGES_REQUIRED`，不得自动推进步骤二。

##### 发布负责人剩余风险接受记录（2026-07-13 17:41:54 +08:00）

发布负责人已明确接受“调用方排查仅覆盖当前容器约 12 天；从计划查询起点约 2026-06-13 到现存日志起点 2026-07-01 的约 18 天没有可用访问日志，无法技术性证明旧文件 ACL 路由零调用”的剩余风险，并授权在 review agent 完成最终复核后进入步骤二。

##### Review agent 最终复核结论（2026-07-13 17:42:57 +08:00）

**结论：PASS。同意进入步骤二。本结论取代前述 `CHANGES_REQUIRED`。**

备份完整性已由保留的 `pg_verifybackup` 成功日志闭环，隔离恢复也已读取 PostgreSQL 版本、Alembic revision、`file_permissions=0`、字段、约束和序列；testing agent 已独立核对证据文件摘要、内容和临时资源清理状态，因此前次审查的备份验证阻断已解除。

日志核查已纠正为真实可复核的约 12 天窗口，更早约 18 天无可用日志的边界已经明确记录。当前可用日志中旧文件 ACL 路由命中为零，也没有“已确认但未完成迁移的外部调用方”；无法补证的历史窗口属于已披露的剩余风险，发布负责人已明确接受，因此不再构成步骤一的阻断。

最终复核未发现 13.1 的其他中止条件：生产 `file_permissions` 仍以最终核查结果 `0` 为实施前提；枚举类型、标签和依赖已确认；尚未出现权限矩阵、跨工作区隔离或 L0/L1/L2 契约回归；R-01 脏文件与步骤二变更集没有同段冲突，且未发现迁移、重启或流量切换被提前执行。

后续仍需跟踪以下非阻断风险：R-01 必须保持独立可审查，如后续与 R-02 出现同段无法安全合并或导致步骤八回归，立即按 13.1 中止；步骤六必须在正式测试环境使用 Alembic CLI 和 PostgreSQL 完成迁移验收；生产迁移前必须再次核对 `file_permissions=0`，一旦非零立即中止；`openrag-web-prod` 的既有 `unhealthy` 状态必须在步骤十预发布健康验收前单独闭环。

### 13.3 步骤二：建立下线后的契约测试

#### 目的

先把“旧路由消失、工作区权限保留、owner_id 和 Team 不产生特权”转化为可重复执行的测试，作为后续删除动作的验收边界。

#### 方法

1. 新增专用契约测试文件，测试只使用 `WorkspaceMember`、角色权限和现有认证依赖，不创建 `FilePermission`。
2. 将专用测试文件加入 `pytest.ini` 的默认收集范围。
3. 在删除 API 前先运行测试，确认旧路由 404 和 OpenAPI 移除用例会按预期失败，其余既有工作区权限用例不应失败。
4. 不通过修改 FastAPI 全局 404 处理器来“伪造”旧路由下线；测试必须验证路由确实未注册。

#### 解决的问题

- 防止只删除实现但遗漏路由注册或 OpenAPI schema；
- 防止删除文件 ACL 时误删 `/users/{user_id}/permissions/details`；
- 防止未来再次把 `owner_id` 或 Team 成员关系解释为内容授权；
- 保证核心契约进入默认测试集，而不是只存在于历史测试中。

#### 涉及修改/新增的函数、字段

| 文件 | 类型 | 函数或字段 | 调整 |
|---|---|---|---|
| `openrag/tests/test_acl_retirement.py` | 新增测试函数 | `test_file_acl_routes_are_not_registered` | 参数化校验三个旧路由均为普通 404 |
| 同上 | 新增测试函数 | `test_openapi_excludes_file_acl_paths` | 校验 OpenAPI `paths` 不包含旧路由 |
| 同上 | 新增测试函数 | `test_user_permission_details_route_is_preserved` | 校验用户工作区权限详情接口继续工作 |
| 同上 | 新增测试函数 | `test_owner_id_without_workspace_permission_is_denied` | 校验仅为文件 owner 不获得读取或管理权限 |
| 同上 | 新增测试函数 | `test_team_membership_without_workspace_permission_is_denied` | 校验 Team 身份不授予文件访问 |
| 同上 | 新增测试函数 | `test_workspace_read_can_read_but_cannot_write` | 校验 read 用户读成功、写操作 403 |
| 同上 | 新增测试函数 | `test_workspace_write_can_manage_files` | 校验 write 用户可执行既有文件管理操作 |
| `openrag/pytest.ini` | 修改配置字段 | `python_files` | 增加 `test_acl_retirement.py` 和后续迁移测试文件 |

测试函数名是建议的实施名称；实现时允许按仓库命名风格微调，但每个契约不得被合并后遗漏。

#### 本步骤校验

- `pytest --collect-only` 能收集所有新增契约测试；
- 删除 API 之前，只有“旧路由不存在”和“OpenAPI 不暴露旧路由”相关断言预期失败；
- `/users/{user_id}/permissions/details`、workspace read/write、owner_id 负向和 Team 负向用例在基线上能够表达明确结果；
- 新测试不导入 `FilePermission`、`EntityType`、`Permission` 或 `PermissionManager`。

##### Coding agent 执行记录（2026-07-13 17:59:31 +08:00）

**执行状态：主线程接管完成。** 原 `/root/coding_agent` 与 `/root/coding_agent_r2` 均在两轮明确任务后长时间无文件产出，已被主线程中断；为避免步骤二空转，主线程按同一边界完成最小实现，后续仍由 testing agent 和 review agent 独立复核。

本步骤只新增/修改以下文件：`openrag/tests/test_acl_retirement.py`、`openrag/pytest.ini`、本方案文档。未修改 R-01 既有脏文件，未删除旧 ACL API、服务、ORM 或迁移对象。

实现内容：

1. 新增 `test_acl_retirement.py`，使用独立 SQLite in-memory、`TestClient(app)` 和 `app.dependency_overrides` 构造契约测试。
2. 新增 7 类契约：三个旧文件 ACL 路由必须变为普通 404、OpenAPI 不暴露旧路由、用户权限详情接口保留、仅 `owner_id` 不授予访问、Team 成员关系不授予访问、workspace read 可读不可写、workspace write 可执行文件管理操作。
3. `pytest.ini` 的 `python_files` 白名单加入 `test_acl_retirement.py`，保证默认收集包含本契约测试。
4. 新测试文件未直接导入或创建 `FilePermission`、`EntityType`、`Permission`、`PermissionManager`。

校验结果：

| 命令 | 结果 |
|---|---|
| `python -m pytest --collect-only openrag/tests/test_acl_retirement.py` | 当前 shell 的 `python` 环境无 `pytest`，未进入测试收集；改用项目依赖环境复核。 |
| `uv run --python 3.11 --with-editable . --with "pytest>=7.4.0" --with-requirements requirements.txt python -m pytest --collect-only tests/test_acl_retirement.py` | PASS，收集 9 个 item：旧路由参数化 3 个用例 + 6 个独立契约。 |
| `uv run --python 3.11 --with-editable . --with "pytest>=7.4.0" --with-requirements requirements.txt python -m pytest tests/test_acl_retirement.py` | 预期失败：4 failed、5 passed。失败仅来自三条旧文件 ACL 路由仍被注册，以及 OpenAPI 仍暴露旧路由；workspace、owner_id、Team、用户权限详情契约均通过。 |
| `uv run --python 3.11 --with-editable . --with "pytest>=7.4.0" --with-requirements requirements.txt python -m pytest --collect-only -q` | PASS，默认收集 196 个测试，包含 `tests/test_acl_retirement.py` 的 9 个 item。 |
| `rg -n "FilePermission|EntityType|PermissionManager|Permission" openrag\tests\test_acl_retirement.py` | PASS，无命中。 |

##### Testing agent 校验结论（2026-07-13 18:10:07 +08:00）

**总体结论：PASS（含预期失败）。步骤二新增契约测试已被默认收集，当前失败全部符合“旧 ACL 尚未删除”的基线预期，可以提交 review agent 审查。**

独立复核结果：

| 校验项 | 结果 |
|---|---|
| 文件与收集配置 | `openrag/tests/test_acl_retirement.py` 存在；`openrag/pytest.ini` 的 `python_files` 已包含 `test_acl_retirement.py`。 |
| 禁止依赖旧 ACL 类型 | `rg -n "FilePermission|EntityType|PermissionManager|Permission" openrag\tests\test_acl_retirement.py` 无命中，新测试未直接导入或创建旧 ACL model/service/enum。 |
| 目标测试收集 | `uv run --python 3.11 --with-editable . --with "pytest>=7.4.0" --with-requirements requirements.txt python -m pytest --collect-only tests/test_acl_retirement.py` 通过，收集 9 个 item。 |
| 目标测试运行 | `uv run --python 3.11 --with-editable . --with "pytest>=7.4.0" --with-requirements requirements.txt python -m pytest tests/test_acl_retirement.py` 返回预期失败：4 failed、5 passed。3 个失败来自旧 `/files/{file_id}/permissions` 相关路由仍被注册并返回业务响应；1 个失败来自 OpenAPI 仍暴露 `/files/{file_id}/permissions`。用户权限详情、owner_id 负向、Team 负向、workspace read/write 契约均通过。 |
| 默认 pytest 收集 | `uv run --python 3.11 --with-editable . --with "pytest>=7.4.0" --with-requirements requirements.txt python -m pytest --collect-only -q` 通过，默认收集 196 个测试，包含 `tests/test_acl_retirement.py` 的 9 个 item。 |
| 步骤越界检查 | 旧 `permissions_api.py` 中 `list_permissions()`、`grant_permission()`、`revoke_permission()` 仍存在；`PermissionManager`、`FilePermission`、`EntityType`、`Permission` 仍存在；未观察到提前删除旧 ACL API、服务、ORM 或迁移对象。当前 R-01 脏文件仍为既有独立变更集，步骤二未要求修改它们。 |

##### 主线程接管与步骤二放行记录（2026-07-14 10:10:51 +08:00）

用户已明确授权“主线程接管编码和验证，推进步骤三”。因此，本记录以主线程复核取代原定的独立 coding/testing/review 三方一致门禁，不追认未形成的 Agent 结论。

主线程复核确认：步骤二只新增 `test_acl_retirement.py` 并修改 `pytest.ini` 的收集白名单；新增测试未导入或创建 `FilePermission`、`EntityType`、`Permission`、`PermissionManager`；删除 API 前的基线结果为 4 个预期失败、5 个通过，失败范围仅为旧路由和 OpenAPI 尚未下线。现有 testing agent 记录的测试事实与主线程此前执行结果一致，且未提前修改 ACL API、服务、ORM 或数据库对象。

**主线程结论：PASS。步骤二完成，允许进入步骤三。**

### 13.4 步骤三：删除文件 ACL API 表面

#### 目的

停止对外提供不会改变实际内容可见性的文件 ACL 管理接口，同时完整保留用户工作区权限详情接口。

#### 方法

1. 从 `permissions_api.py` 删除文件 ACL 路由、Pydantic 模型、处理函数和专用导入。
2. 保留 `user_permissions_router` 和 `get_user_permission_details()` 的行为、路径和返回结构。
3. 从 `main.py` 删除文件 ACL router 导入和注册。
4. 修正 `files_api.py` 中 `delete_file()` 的 owner/admin 误导性说明，只改注释和 docstring，不改现有 workspace write 鉴权代码。
5. 删除 `test_permissions_api.py` 中三个文件 ACL 接口的历史测试及由此产生的孤立 fixture/import，保留用户权限详情测试。

#### 解决的问题

- 消除“请求成功但权限不生效”的公开入口；
- 消除 OpenAPI 对文件级授权能力的错误承诺；
- 避免把通用用户权限详情接口连带删除；
- 统一文件删除接口的文档与实际工作区 write 行为。

#### 涉及修改/删除的函数、字段

| 文件 | 对象 | 调整 |
|---|---|---|
| `openrag/src/openrag/api/permissions_api.py` | `router` | 删除文件 ACL 专用 `APIRouter(prefix="/files")` |
| 同上 | `PermissionResponse` | 删除类及字段 `id`、`file_id`、`entity_type`、`entity_id`、`permission`、`created_at` |
| 同上 | `GrantPermissionRequest` | 删除类及字段 `entity_type`、`entity_id`、`permission` |
| 同上 | `MessageResponse` | 删除文件 ACL 专用响应类及 `message` 字段 |
| 同上 | `list_permissions()` | 删除 |
| 同上 | `grant_permission()` | 删除 |
| 同上 | `revoke_permission()` | 删除 |
| 同上 | `user_permissions_router` | 保留，不改路径 |
| 同上 | `get_user_permission_details()` | 保留，不改返回字段 |
| `openrag/src/openrag/api/main.py` | `file_permissions_router` 导入 | 删除 |
| 同上 | `app.include_router(file_permissions_router)` | 删除 |
| 同上 | `app.include_router(user_permissions_router)` | 保留 |
| `openrag/src/openrag/api/files_api.py` | `delete_file()` | 仅将“owner/admin”说明改为 workspace write，函数行为不变 |
| `openrag/tests/test_permissions_api.py` | `TestListPermissions`、`TestGrantPermission`、`TestRevokePermission` | 删除旧接口测试 |
| 同上 | `test_get_user_permission_details()` | 保留 |

删除专用模型后，应同步清理 `BaseModel`、`Field`、`status`、`File`、ACL 模型和 `PermissionManager` 等只为旧路由服务的导入；仍被用户权限详情接口使用的依赖不得删除。

#### 本步骤校验

- 三个旧路由在有认证和无认证情况下均为普通 404；
- `/openapi.json` 不包含三个旧路径，也不包含 `PermissionResponse`、`GrantPermissionRequest`；
- `/users/{user_id}/permissions/details` 返回结构中的 `user_id`、`roles`、`workspace_permissions` 保持不变；
- `main.py` 仍只注册一次 `user_permissions_router`；
- 步骤二的契约测试中与 API 下线相关的预期失败全部转为通过；
- `files_api.py` 仅有说明文字变化，文件删除权限测试结果不因本步骤改变。

##### 主线程编码记录（2026-07-14 10:10:51 +08:00）

**执行状态：完成。** 本步骤只删除文件 ACL 的公开 API 表面，没有提前执行步骤四的服务层删除、步骤五的 ORM 清理或步骤六的数据库迁移。

实际修改如下：

1. `permissions_api.py` 删除文件 ACL 专用 `router`、`PermissionResponse`、`GrantPermissionRequest`、`MessageResponse`、`list_permissions()`、`grant_permission()`、`revoke_permission()` 及其专用导入；保留 `user_permissions_router` 和 `get_user_permission_details()`，函数体和返回结构未改。
2. `main.py` 删除 `file_permissions_router` 的导入和注册；`user_permissions_router` 仍各导入、注册一次。
3. `files_api.py` 只将 `delete_file()` 的说明从 owner/admin 改为 workspace write，鉴权代码未改；该文件的差异为 1 行删除、1 行新增。
4. `test_permissions_api.py` 删除三组旧文件 ACL 接口测试和由此孤立的 fixture/import，保留 `test_get_user_permission_details()`，并补充 `user_id` 返回字段断言。
5. `test_acl_retirement.py` 将三条旧路由扩展为有认证/无认证两种情形，并断言 OpenAPI 不再包含 `PermissionResponse`、`GrantPermissionRequest`。workspace write 用例对 `MinioStorage.ensure_bucket()` 使用局部 stub，避免契约测试依赖外部 MinIO；生产代码未因此修改。

##### 主线程测试结论（2026-07-14 10:10:51 +08:00）

**总体结论：PASS。步骤三的全部声明校验已经满足。**

| 校验项 | 结果 |
|---|---|
| R-02 契约测试 | `python -m pytest tests/test_acl_retirement.py -q`：12 passed。三条旧路由在有认证和无认证下共 6 个用例均返回普通 404；OpenAPI 路径/schema、用户权限详情、owner_id/Team 负向和 workspace read/write 契约全部通过。 |
| 用户权限详情历史测试 | `python -m pytest tests/test_permissions_api.py -vv -s`：1 passed；`user_id`、`roles`、`workspace_permissions` 均保持。该历史文件不在默认白名单中，但相同保留接口契约已由默认收集的 `test_acl_retirement.py` 覆盖。 |
| 文件删除权限回归 | 历史 `TestFileDelete` 会执行真实存储清理；测试进程内临时替换 `delete_file_with_storage()` 为仅删除测试数据库记录的 stub 后，`TestFileDelete` 4 passed。权限判断路径未替换，仓库文件未为该 stub 产生修改。 |
| 默认测试收集 | `python -m pytest --collect-only -q`：PASS，收集 199 项，包含 `test_acl_retirement.py` 的 12 项。 |
| 编译检查 | `python -m compileall -q src/openrag`：PASS。 |
| 静态检查 | `permissions_api.py` 和 `main.py` 中不再存在 `file_permissions_router`、ACL 专用模型或三个旧处理函数；`main.py` 中 `user_permissions_router` 恰好导入、注册各一次；`git diff --check` 通过。其他 API 中各自独立的 `MessageResponse` 有意保留。 |

测试过程中的一次环境问题已闭环：最初组合执行在 124 秒超时；拆分后确认 `test_workspace_write_can_manage_files` 的唯一失败是尝试连接未启动的 `localhost:9000` MinIO（其余 11 项已通过）。为该单一用例增加局部 `ensure_bucket()` stub 后，R-02 契约稳定为 12/12 通过。该修复只隔离外部副作用，不改变权限断言。

##### 主线程 Review 结论（2026-07-14 10:10:51 +08:00）

**结论：PASS。步骤三实现切合目标、无冗余生产改动，同意进入步骤四。**

- 目的匹配：旧路由通过取消 router 注册自然变为 Starlette 普通 404，没有增加全局 404 伪装；OpenAPI 同时移除旧路径和专用 schema。
- 保留面正确：用户权限详情 router、处理函数和返回字段均保留；文件删除鉴权仍由现有 workspace write 检查执行。
- 步骤边界正确：`PermissionManager`、`FilePermission` 和 ACL 枚举仍存在，符合步骤四、步骤五后续顺序；没有 schema、迁移、生产环境或前端变更。
- 风险评估：本步骤的预期破坏性变化仅为三条旧路由 404；历史调用方覆盖不足的剩余风险已在步骤一由发布负责人接受。测试中的 MinIO 外部依赖已在契约测试局部隔离，不会掩盖生产权限逻辑。
- 变更隔离：未修改 R-01 已有的 `search_api.py`、`service_api.py`、`retrieval_service.py` 及其测试，不存在同段冲突。

### 13.5 步骤四：删除文件 ACL 服务层

#### 目的

移除只为已删除接口和未接入数据面的 ACL 逻辑，避免后续代码继续误调用或将其包装为新的工作区授权实现。

#### 方法

1. 删除 `openrag/src/openrag/services/permission_manager.py`。
2. 删除 `test_permission_manager.py`；这些测试验证的是已明确废弃的 ACL 语义，不迁移为 `WorkspaceService` 测试。
3. 全仓搜索生产代码导入，确认没有其他调用者。
4. 不新增 `PermissionManager` 兼容壳、空实现、别名或转发到 `WorkspaceService` 的适配器。

#### 解决的问题

- 消除第二套权限判断与递归继承语义；
- 防止旧调用者在接口删除后仍通过内部服务写入 ACL；
- 防止将单文件授权静默扩大成工作区授权。

#### 涉及删除的函数、字段

| 对象 | 调整 |
|---|---|
| `PermissionManager.PERMISSION_HIERARCHY` | 删除 |
| `PermissionManager.__init__()` | 删除 |
| `PermissionManager.grant_permission()` | 删除 |
| `PermissionManager.revoke_permission()` | 删除 |
| `PermissionManager.check_permission()` | 删除 |
| `PermissionManager.get_file_permissions()` | 删除 |
| `PermissionManager.get_user_permissions()` | 删除 |
| `PermissionManager.list_accessible_files()` | 删除 |
| `PermissionManager._has_permission()` | 删除 |

本步骤不修改 `WorkspaceService.check_user_permission()`、`WorkspaceService.get_user_workspaces_with_permission()` 或其权限合并规则。

#### 本步骤校验

- 生产代码中 `rg "PermissionManager|permission_manager" openrag/src` 无命中；
- 仓库中不存在对 `openrag.services.permission_manager` 的可执行导入；
- 工作区服务测试保持通过，证明没有把旧 ACL 逻辑迁入 `WorkspaceService`；
- 默认测试收集不再包含 `test_permission_manager.py`；
- API 契约测试继续通过。

##### 主线程编码记录（2026-07-14 10:19:24 +08:00）

**执行状态：完成。** 本步骤采用直接删除，没有增加兼容壳、空实现、别名或到 `WorkspaceService` 的转发。

实际变更仅包含：

1. 删除 `openrag/src/openrag/services/permission_manager.py`，移除 `PermissionManager` 及其权限层级、ACL 写入、撤销、检查、继承和可访问文件枚举逻辑，共删除 332 行。
2. 删除 `openrag/tests/test_permission_manager.py`，移除只验证废弃文件 ACL 语义的专用测试，共删除 447 行。
3. 未修改 `WorkspaceService`、ACL ORM/枚举、模型关系、Alembic、API、前端或生产环境；ORM 和数据库对象继续留待步骤五、步骤六处理。

##### 主线程测试结论（2026-07-14 10:19:24 +08:00）

**总体结论：PASS。步骤四全部声明校验满足。**

| 校验项 | 结果 |
|---|---|
| 生产及测试代码静态搜索 | `rg -n "PermissionManager|permission_manager" openrag/src openrag/tests`：无匹配。除废弃文档外，仓库不存在旧服务的可执行引用。 |
| 工作区服务与 API 契约 | `python -m pytest tests/test_workspace_service.py tests/test_acl_retirement.py -q`：18 passed；工作区服务 6 项和 R-02 契约 12 项全部通过。 |
| 默认测试收集 | `python -m pytest --collect-only -q`：PASS，仍收集 199 项，输出中不包含 `test_permission_manager.py`。 |
| 模块不可导入 | `importlib.util.find_spec("openrag.services.permission_manager") is None`：PASS，未留下可导入兼容模块。 |
| 编译与差异检查 | `python -m compileall -q src/openrag`、`git diff --check` 均通过。 |

##### 主线程 Review 结论（2026-07-14 10:19:24 +08:00）

**结论：PASS。步骤四实现与计划一致、无冗余修改，同意进入步骤五。**

- 目的匹配：旧接口删除后，内部 ACL 写入和第二套权限判断入口也已消失；不存在继续误调用 `PermissionManager` 的生产路径。
- 变更最小：实际代码改动只有两个文件删除，没有为了“兼容”保留同名模块，也没有创建新的权限抽象。
- 权限边界正确：`WorkspaceService` 未发生变化，工作区权限合并规则未吸收文件 ACL、Team ACL 或递归继承语义。
- 顺序正确：`FilePermission`、`EntityType`、`Permission`、`File.permissions` 和数据库表仍然存在，符合步骤五、步骤六分层删除和独立验证的执行顺序。
- 风险评估：全仓搜索和步骤三 API 删除记录共同证明没有剩余生产调用者；主要兼容性风险仅限仓库外部代码直接导入内部模块，但该能力本就未进入实际数据面，且调用方剩余风险已在步骤一披露并接受。

### 13.6 步骤五：删除 ORM、关系和模型注册

#### 目的

让应用模型层不再声明 `file_permissions`，并明确保留 `File.owner_id` 的审计含义以及 L0/L1/L2 字段的工作区共享结构。

#### 方法

1. 删除 `openrag/src/openrag/models/permission.py`。
2. 从 `File` 删除 `permissions` relationship 和 `FilePermission` 类型导入；保留 `TYPE_CHECKING` 及其他类型导入。
3. 从模型包导出中删除 ACL 类型。
4. 清理模型测试中的 ACL 模型、唯一约束和 ORM 级联删除测试。
5. 新增 metadata 断言，验证全新数据库不会由 `Base.metadata.create_all()` 创建 ACL 表。

#### 解决的问题

- 防止全新环境重新创建已经下线的表；
- 防止 ORM 在删除文件时继续尝试维护 `File.permissions`；
- 防止其他模块继续从模型包导入废弃类型；
- 避免误删仍用于展示、审计和层级摘要定位的字段。

#### 涉及修改/删除的函数、字段

| 文件 | 对象 | 调整 |
|---|---|---|
| `openrag/src/openrag/models/permission.py` | `EntityType` | 删除 `USER`、`TEAM` 枚举 |
| 同上 | `Permission` | 删除 `READ`、`WRITE`、`ADMIN` 枚举 |
| 同上 | `FilePermission` | 删除模型 |
| 同上 | 模型字段 | 删除 `id`、`file_id`、`workspace_id`、`entity_type`、`entity_id`、`permission`、`created_at` |
| 同上 | ORM 关系 | 删除 `file`、`workspace`、`user`、`team` |
| 同上 | 方法 | 删除 `_strip_nul_enums()`、`__repr__()` |
| 同上 | 约束/索引声明 | 删除 `uq_file_entity`、`idx_permission_file_id`、`idx_permission_entity` |
| `openrag/src/openrag/models/file.py` | `File.permissions` | 删除 relationship |
| 同上 | `FilePermission` TYPE_CHECKING 导入 | 删除；`TYPE_CHECKING` 保留 |
| 同上 | `File.owner_id`、`File.owner` | 保留，不增加权限判断 |
| 同上 | `l0_path`、`l1_path`、`l2_path`、`l0_vector_id` | 保留，不增加 `user_id` 或 ACL digest 字段 |
| `openrag/src/openrag/models/__init__.py` | ACL 导入和 `__all__` | 删除 `FilePermission`、`EntityType`、`Permission` |
| `openrag/tests/test_models.py` | `TestFilePermissionModel`、`test_cascade_delete_file_permissions()` | 删除 |
| 同上或专用测试文件 | `test_metadata_excludes_file_permissions` | 新增，断言 metadata 不含旧表 |

#### 本步骤校验

- `"file_permissions" not in Base.metadata.tables`；
- 新建空测试数据库后不存在 `file_permissions` 表；
- `File.__mapper__.relationships` 不包含 `permissions`，但仍包含 `owner`、`workspace`、`children`、`share_links` 和 `document_chunks`；
- `File` 仍包含 `owner_id`、`l0_path`、`l1_path`、`l2_path`、`l0_vector_id`；
- `from openrag.models import FilePermission` 不再可用，仓库生产代码也无此导入；
- 非 ACL 模型测试、文件模型测试和应用启动导入检查通过。

##### 主线程编码记录（2026-07-14 10:38:52 +08:00）

**执行状态：完成。** 本步骤只移除应用 ORM 声明及其模型测试，不执行数据库迁移，也不提前改写步骤七、步骤八负责的文件与检索历史测试。

实际变更：

1. 删除 `openrag/src/openrag/models/permission.py`，移除 `EntityType`、`Permission`、`FilePermission`、字段、关系、校验器、约束和索引声明，共删除 93 行。
2. 从 `File` 删除 `FilePermission` 的 TYPE_CHECKING 导入和 `permissions` relationship；保留 `TYPE_CHECKING`、`owner_id`、`owner`、`workspace`、`children`、`share_links`、`document_chunks` 以及 L0/L1/L2 字段。
3. 从 `openrag.models` 包删除三个 ACL 类型的导入和 `__all__` 导出。
4. 从 `test_models.py` 删除 `TestFilePermissionModel` 和 `test_cascade_delete_file_permissions()`。历史非 ACL 文件测试原本未提供已必填的 `workspace_id`，新增复用的 `test_workspace` fixture，并只为保留的 File 测试数据补齐工作区字段。
5. 在默认收集的 `test_acl_retirement.py` 新增 `test_metadata_excludes_file_permissions()`，同时断言 metadata/空 SQLite 数据库无旧表、`File.permissions` 消失、必要关系和审计/层级字段保留。

##### 主线程测试结论（2026-07-14 10:38:52 +08:00）

**步骤五声明校验：PASS。默认测试门禁：计划内暂时 FAIL，必须由步骤七闭环。**

| 校验项 | 结果 |
|---|---|
| R-02 模型/API 契约 | `python -m pytest tests/test_acl_retirement.py -q`：13 passed；新增 metadata、空库表、关系和保留字段断言通过。 |
| 非 ACL 模型测试 | 首次执行为 6 passed、6 failed，失败统一为历史 File 构造缺少必填 `workspace_id`；补齐工作区 fixture 后重跑为 12 passed。未修改生产模型。 |
| 应用与模型综合检查 | `openrag.api.main.app` 可导入；`file_permissions` 不在 metadata 和全新 SQLite 表清单中；`File.permissions` 不在 mapper；必要关系与 `owner_id`、`l0_path`、`l1_path`、`l2_path`、`l0_vector_id` 均存在。 |
| 旧导出/模块 | `from openrag.models import FilePermission` 触发 ImportError；`openrag.models.permission` 已不存在。 |
| 生产代码静态搜索 | `openrag/src` 中搜索 `openrag.models.permission`、`FilePermission`、ACL `EntityType` 无匹配；未误删 GraphRAG 的普通 `entity_type` 数据字段。 |
| 编译与差异 | `python -m compileall src/openrag`、`git diff --check` 均通过。 |
| 默认 pytest 收集 | 167 项成功收集，随后仅 `test_files_api.py:20` 因仍导入 `openrag.models.permission` 产生 1 个收集错误。该文件已在步骤七中明确安排改写，因此本步骤不提前修改。 |

##### 主线程 Review 结论（2026-07-14 10:38:52 +08:00）

**结论：PASS（带计划内过渡项）。步骤五符合目标，同意进入步骤六；当前版本不可发布。**

- 目的匹配：应用 metadata 不再声明 `file_permissions`，新环境不会由 `Base.metadata.create_all()` 重建旧表，模型包也不再公开 ACL 类型。
- 保留面正确：`File.owner_id` 仍是非空外键和审计归属字段，工作区、父子、分享、文档块及 L0/L1/L2 关系/字段未受影响。
- 变更边界正确：没有修改生产文件权限判断、WorkspaceService、检索、GraphRAG 字段或数据库实体；活动数据库表和枚举仍等待步骤六迁移删除。
- 测试修正必要且最小：补 `workspace_id` 只让历史非 ACL 模型测试符合当前既有 schema，没有改变测试预期或生产行为。
- 过渡风险：默认测试门禁当前因 `test_files_api.py` 的旧 ACL 测试导入而失败；`test_retrieval_service.py`、`test_integration_retrieval.py`、`test_e2e.py` 也仍含计划由步骤八清理的旧测试数据。这些均不是生产调用者，但在步骤七、步骤八完成前不得声称全量测试通过。
- 发布约束：ORM 移除与步骤六数据库迁移必须进入同一发布版本，且必须先停旧实例、执行零记录保护迁移，再启动新实例；禁止部署当前中间状态。

### 13.7 步骤六：实现零记录保护的 Alembic 迁移

#### 目的

从已有 PostgreSQL 环境安全删除空的活动表和无依赖专用枚举，并提供旧版本代码可使用的空 schema 回滚能力。

#### 方法

1. 新增 `20260713_0006_retire_file_acl.py`，`down_revision` 指向实施时确认的唯一 head。
2. `upgrade()` 在事务内检查表是否存在；存在时锁定或以能防止并发写入的方式读取行数，并要求为零。
3. 行数非零时抛出带明确操作提示的异常，使整个迁移回滚；不归档、不删表、不创建工作区权限。
4. 从 catalog 读取 `entity_type`、`permission` 列实际绑定的枚举类型名及依赖关系，再删除空表。
5. 仅删除确认无其他依赖的专用枚举，禁止 `DROP TYPE ... CASCADE`。
6. `downgrade()` 按旧 ORM 可用的规范重建枚举、空表、外键、唯一约束和索引，并通过实际插入验证主键自动生成。
7. 迁移测试直接构造旧 schema，不依赖已经删除的 `FilePermission` ORM。

#### 解决的问题

- 防止检查为零后又被旧实例并发写入；
- 防止非零 ACL 数据被静默删除或自动扩权；
- 兼容 ORM 自动命名与参考 DDL 命名不一致的环境；
- 防止误删共享枚举；
- 确保旧代码回滚时不会因缺表或主键序列缺失而启动失败。

#### 涉及修改/新增的函数、字段

| 文件 | 对象 | 调整 |
|---|---|---|
| `openrag/alembic/versions/20260713_0006_retire_file_acl.py` | `revision` | 新增 `20260713_0006` |
| 同上 | `down_revision` | 指向步骤一确认的当前唯一 head |
| 同上 | `upgrade()` | 新增表存在检查、零记录断言、表删除及无依赖枚举清理 |
| 同上 | `downgrade()` | 新增旧 schema 重建逻辑 |
| downgrade 表字段 | `id` | 主键、自增/序列可写 |
| 同上 | `file_id` | 非空，外键到 `files.id` |
| 同上 | `workspace_id` | 可空，外键到 `workspaces.id` |
| 同上 | `entity_type` | 非空，旧 ORM 兼容枚举 |
| 同上 | `entity_id` | 非空整数 |
| 同上 | `permission` | 非空，旧 ORM 兼容枚举 |
| 同上 | `created_at` | 非空，服务端默认当前时间 |
| downgrade 约束/索引 | `uq_file_entity`、`idx_permission_file_id`、`idx_permission_entity` | 重建 |
| `openrag/tests/test_retire_file_acl_migration.py` | `test_upgrade_drops_empty_acl_schema` | 新增 |
| 同上 | `test_upgrade_aborts_and_preserves_nonempty_acl_schema` | 新增 |
| 同上 | `test_upgrade_handles_missing_acl_table` | 新增 |
| 同上 | `test_upgrade_preserves_shared_enum_types` | 新增 |
| 同上 | `test_downgrade_restores_empty_writable_schema` | 新增 |
| 同上 | `test_upgrade_downgrade_upgrade_round_trip` | 新增 |

除 `upgrade()`、`downgrade()` 外，只有在 catalog 查询确实复用时才增加私有辅助函数；不得为单次 SQL 建立额外抽象层。

#### 本步骤校验

- 在 PostgreSQL 上分别完成“空表 upgrade”“非空表中止”“表不存在 upgrade”三条路径；
- 非空表场景中迁移 revision 不前进，原表和原记录均保留；
- 空表升级后 `file_permissions`、专用索引和无依赖专用枚举不存在；共享枚举仍存在；
- downgrade 后旧表各字段的类型、可空性、默认值、外键、唯一约束和索引与旧 ORM 兼容；
- downgrade 后连续插入两条合法记录，主键不同且无序列冲突；
- 完成 upgrade → downgrade → upgrade 往返，最终 schema 与首次 upgrade 一致；
- SQLite 不能替代本步骤的 PostgreSQL 迁移验收。

##### 主线程编码记录（2026-07-14 10:49:16 +08:00）

**执行状态：完成。** 本步骤只新增文件 ACL 退役迁移及其 PostgreSQL 验收测试，没有修改生产数据库，也没有提前处理步骤七、步骤八负责的旧测试表达。

1. 新增 `20260713_0006_retire_file_acl.py`，唯一前序 revision 为 `20260626_0005`。`upgrade()` 仅允许 PostgreSQL：表存在时先取得 `ACCESS EXCLUSIVE` 锁，再执行零记录断言；发现记录即抛出明确异常，由 Alembic 事务回滚，不归档、不删表、不转换权限。
2. 删除空表前从 PostgreSQL catalog 读取 `entity_type`、`permission` 两列实际绑定的枚举类型。表删除后逐个检查外部依赖，仅删除无其他依赖的枚举；全程没有使用 `DROP TYPE ... CASCADE`。
3. `downgrade()` 重建旧代码可使用的空 schema：`id` 自增主键、文件和可选工作区外键、旧枚举标签、`created_at` 服务端默认值、唯一约束及两个索引。遇到同名共享枚举时复用兼容类型，标签不兼容则中止，避免静默生成错误 schema。
4. 新增 `test_retire_file_acl_migration.py`，测试直接构造旧 schema，不导入已删除的 ACL ORM；覆盖空表升级、非空中止、缺表升级、共享枚举保留、降级可写和 upgrade → downgrade → upgrade 六条路径。

##### 主线程测试结论（2026-07-14 10:49:16 +08:00）

**步骤六声明校验：PASS。**

| 校验项 | 结果 |
|---|---|
| PostgreSQL 迁移专项测试 | 在本机临时 `postgres:16-alpine` 容器、专用数据库 `openrag_r02_test` 中执行，6/6 通过。 |
| 非空数据保护 | 迁移按预期失败；revision 保持 `20260626_0005`，原表和 1 条原记录均保留。 |
| 空表与枚举清理 | 空表升级后 revision 为 `20260713_0006`，`file_permissions` 及无依赖 `entitytype`、`permission` 均不存在。 |
| 共享枚举保护 | 被其他表引用的 `entitytype` 保留；无其他依赖的 `permission` 删除。 |
| downgrade 可用性 | 字段、可空性、默认值、外键、唯一约束和索引检查通过；连续插入两条记录得到不同自增主键。 |
| 往返迁移 | upgrade → downgrade → upgrade 通过，最终 revision 和 schema 与首次升级一致。 |
| 关联回归 | `test_retire_file_acl_migration.py`、`test_acl_retirement.py`、`test_models.py`、`test_workspace_service.py` 合计 37 项通过。 |
| Alembic 拓扑 | `alembic heads` 仅返回 `20260713_0006 (head)`。 |

测试只使用回环地址上的临时隔离数据库，没有连接或修改生产数据库；测试结束后临时容器已停止并删除。步骤五记录的默认 pytest 收集错误仍属于步骤七计划内过渡项：`test_files_api.py` 仍导入已删除 ACL 模型，因此当前版本仍不可发布，不能将本步骤专项通过表述为全量门禁通过。

##### 主线程 Review 结论（2026-07-14 10:49:16 +08:00）

**结论：PASS。同意进入步骤七；当前版本不可发布。**

- 安全性符合目标：锁与零记录检查位于同一事务，消除了检查后并发写入窗口；非零数据采用 fail-closed 策略，失败时 revision、表和数据均不变。
- 兼容性符合目标：没有假定固定枚举绑定；共享枚举会保留，专用枚举仅在无外部依赖时删除，且无 `CASCADE` 扩大删除范围。
- 回滚能力符合目标：downgrade 恢复的是可实际写入的旧 schema，不只是形式上的表结构；主键序列与往返迁移均经过 PostgreSQL 验证。
- 变更保持最小：新增内容仅为一个迁移和一个迁移测试文件，未引入通用抽象、运行时兼容层或生产数据修复逻辑。
- 后续约束：步骤七、步骤八必须清理残留旧 ACL 测试数据和导入；步骤十发布前仍需再次确认活动环境 `file_permissions=0`，并按停旧实例、执行迁移、启动新实例的顺序发布。

### 13.8 步骤七：把文件操作测试统一为工作区 RBAC

#### 目的

清除测试中“工作区权限 + 文件 ACL”混合构造和“owner 自动有权”的旧表达，使文件列表、读取和写操作只由工作区 read/write 决定。

#### 方法

1. 从 `test_files_api.py` 删除 ACL 模型导入和 ACL fixture/记录。
2. 使用 `WorkspaceMember(workspace_id, user_id, role)` 或现有角色权限构造 read/write 用户。
3. 重命名仍带有 `shared`、`with_permission`、`as_owner` 等模糊含义的用例，使名称直接表达工作区权限来源。
4. 对 write 操作分别覆盖 read 用户 403、write 用户成功、仅 owner_id 但无 workspace 权限 403。
5. 不修改文件 API 的生产鉴权实现；若测试暴露实现不符合已锁定权限矩阵，先停止并单独评审，而不是在本步骤顺带重构。

#### 解决的问题

- 防止测试在删除 ORM 后导入失败；
- 防止无效 ACL 假数据掩盖真正生效的 workspace 权限；
- 固化 owner_id 仅审计、read 不包含写能力、write 可管理工作区文件的契约。

#### 涉及修改/新增的函数、字段

| 文件 | 原测试函数 | 调整后的职责 |
|---|---|---|
| `openrag/tests/test_files_api.py` | `test_list_files_with_shared()` | 改为 `test_list_files_with_workspace_read()`，只创建 `WorkspaceMember.role="read"` |
| 同上 | `test_get_file_with_permission()` | 改为 `test_get_file_with_workspace_read()` |
| 同上 | `test_delete_file_with_write_permission()` | 改为 `test_delete_file_with_workspace_write()`，预期成功，不再写入文件 ACL |
| 同上 | `test_move_file_with_write_permission()` | 改为 `test_move_file_with_workspace_write()` |
| 同上 | `test_delete_file_no_permission()` | 保留并明确无 workspace 权限时为 403 |
| 同上 | 新增 `test_file_owner_without_workspace_membership_cannot_read_or_write()` | 验证 `File.owner_id` 不产生特权 |
| 同上 | 新增 `test_workspace_read_cannot_delete_move_or_reprocess()` | 验证 read/write 边界 |
| 测试 fixture | `WorkspaceMember.workspace_id`、`user_id`、`role` | 作为权限场景唯一直接成员字段 |

生产函数 `_get_readable_file_or_404()`、`list_files()`、`delete_file()`、`delete_path_prefix()`、`move_file()`、`create_directory()`、`reprocess_file()` 只参与验证，原则上不修改。

#### 本步骤校验

- `test_files_api.py` 不再导入或创建 ACL 模型；
- workspace read 用户可列表、读取、预览，但删除、移动、重处理返回 403；
- workspace write 用户可执行既有写操作；
- 仅 `File.owner_id` 相同但无工作区权限的用户读写均被拒绝；
- 工作区 A 的权限不能读取工作区 B 的同名或同路径文件；
- `test_files_api.py` 作为默认测试文件完整通过。

##### 主线程编码记录（2026-07-14 10:59:11 +08:00）

**执行状态：完成。** 本步骤只修改 `test_files_api.py`，没有修改文件 API、WorkspaceService、模型、迁移或步骤八负责的检索测试。

1. 删除 `FilePermission`、`EntityType`、`Permission` 导入及全部文件 ACL 假记录，权限场景只通过 `WorkspaceMember(workspace_id, user_id, role)` 构造。
2. 将 `shared`、`with_permission`、`as_owner` 等旧命名改为直接表达 `workspace_read` 或 `workspace_write`；原来“文件 WRITE ACL 仍不能删除”的旧断言改为 workspace write 成员删除成功。
3. workspace read 场景覆盖列表、详情和预览成功，同时覆盖删除、移动、重处理均返回 403；workspace write 场景覆盖删除、移动以及既有重处理成功路径。
4. 新增 `test_file_owner_without_workspace_membership_cannot_read_or_write()`，将文件 `owner_id` 指向无工作区成员关系的用户，验证详情、删除、移动和重处理均返回 403。
5. 新增同路径跨工作区隔离场景：用户只拥有工作区 A 的 read 权限时，列表只返回 A 中的文件，直接读取工作区 B 的同路径文件返回 403。
6. 删除成功测试使用数据库内替身隔离底层删除动作，避免权限测试访问 MinIO、Milvus 等外部系统；生产删除实现没有变化。

##### 主线程测试结论（2026-07-14 10:59:11 +08:00）

**步骤七声明校验：PASS。**

| 校验项 | 结果 |
|---|---|
| 旧 ACL 引用扫描 | `test_files_api.py` 中不存在 ACL 模型导入、对象创建及 `shared`、`with_permission`、`as_owner` 旧表达。 |
| 文件 API 专项 | `tests/test_files_api.py` 35 项全部通过。 |
| workspace read | 列表、详情、预览为 200；删除、移动、重处理为 403。 |
| workspace write | 删除、移动、既有重处理路径成功。 |
| owner_id 边界 | 仅文件 `owner_id` 相同、无 workspace 成员关系时，读写请求均为 403。 |
| 跨工作区隔离 | A、B 存在同 URI 文件时，A 的 read 成员只看到 A 文件，读取 B 文件为 403。 |
| 关联回归 | `test_files_api.py`、`test_acl_retirement.py`、`test_models.py`、`test_workspace_service.py` 合计 66 项通过。 |
| 默认测试收集 | 202 项成功收集，步骤五遗留的 ACL 模型导入收集错误已消除。 |

首次专项运行有 1 个新增测试断言失败：测试读取了 `FileResponse` 未暴露的 `workspace_id`。确认接口已只返回授权工作区的单条记录后，改为比较授权文件 ID、排除未授权文件 ID，并增加对未授权文件直接读取 403 的断言；最终 35/35 通过。该问题只涉及测试对响应模型的错误假设，没有修改生产接口。

##### 主线程 Review 结论（2026-07-14 10:59:11 +08:00）

**结论：PASS。同意进入步骤八；当前版本仍不可发布。**

- 权限来源已单一化：测试不再借助已下线的文件 ACL，所有允许与拒绝结果都可追溯到 workspace read/write 成员关系。
- 核心契约已固化：read 不包含写能力；write 可以管理工作区文件；`File.owner_id` 只保留审计含义，不产生读取或写入特权。
- 工作区隔离验证有效：同 URI 文件用于排除路径偶合，列表和直接读取两条数据面均验证了 B 工作区不可见。
- 变更边界正确：实际代码变更仅位于测试文件，未因测试结果顺带改动生产鉴权；未引入通用 fixture 或额外抽象。
- 后续约束：步骤八仍需清理检索、L0/L1/L2 及独立能力测试中的旧 ACL 假数据；在步骤八及后续发布门禁完成前，不得将当前中间版本发布。

### 13.9 步骤八：统一检索、L0/L1/L2 和独立能力边界

#### 目的

证明文件 ACL 下线不会改变现有数据面实现，并固化“同工作区共享层级摘要、跨工作区隔离、Team 不授权、独立令牌不回归”的契约。

#### 方法

1. 从 `test_retrieval_service.py`、`test_integration_retrieval.py` 和 `test_e2e.py` 删除 ACL fixture、导入和旧授权场景。
2. 为测试数据补齐 `Workspace`、`WorkspaceMember` 和 `File.workspace_id`，按工作区构造候选集合。
3. 将直接用户 ACL、Team ACL 和 owner 可见性的旧预期替换为 workspace read/write 预期。
4. 比较同一工作区两个 read 用户的授权文件集合、L0/L1 候选文件集合和最终 L2 chunk 结果，结果应一致。
5. 验证用户失去 workspace read 后，文件检索、层级检索、chunk context 和预览均不可见。
6. 保留并运行 ShareLink 与服务令牌测试，不把它们改写为工作区成员授权。
7. 本步骤原则上不修改检索生产函数；若发现与工作区隔离契约不一致，单独建立缺陷计划，不把 R-02 扩大为检索重构。

#### 解决的问题

- 清除历史测试对文件 ACL 的错误依赖；
- 防止 L0/L1/L2 因用户或 Team 生成重复变体；
- 防止 Team 成员关系被误接为检索授权；
- 防止下线 ACL 时误伤 ShareLink 和服务令牌的独立鉴权。

#### 涉及修改/新增的函数、字段

| 文件 | 对象 | 调整 |
|---|---|---|
| `openrag/tests/test_retrieval_service.py` | `test_permissions` fixture | 删除，改为 workspace/member fixture |
| 同上 | `test_get_accessible_uris_with_direct_permission()` | 改为 workspace read 场景 |
| 同上 | `test_get_accessible_uris_with_team_permission()` | 改为 Team-only 不能访问的负向场景 |
| 同上 | `test_search_filters_by_permission()` | 改为 `test_search_filters_by_workspace_permission()` |
| `openrag/tests/test_integration_retrieval.py` | `test_permissions` fixture | 删除，文件全部绑定明确 `workspace_id` |
| 同上 | owner/direct/team 相关测试 | 重写为 workspace read、无权限和跨 workspace 场景 |
| `openrag/tests/test_e2e.py` | `TestPermissionManagement` | 删除文件 ACL 工作流，替换为工作区权限矩阵 |
| 同上 | `test_permission_persistence()`、`test_concurrent_permission_grants()`、`test_permission_grant_performance()` | 删除 |
| `openrag/tests/test_acl_retirement.py` | `test_same_workspace_readers_share_hierarchy_scope()` | 新增 |
| 同上 | `test_workspace_revocation_hides_search_preview_and_context()` | 新增 |
| 同上 | `test_team_membership_does_not_change_retrieval_scope()` | 新增 |
| `File` 层级字段 | `l0_path`、`l1_path`、`l2_path`、`l0_vector_id` | 保持文件级字段，不新增用户维度 |

以下生产函数只验证、不计划修改：`PermissionFilter.get_accessible_uris()`、`RetrievalService._accessible_file_ids()`、`RetrievalService._effective_file_ids()`、`RetrievalService.resolve_file_scope()`、`assert_search_workspace_read()` 和 `get_chunk_context()`。

#### 本步骤校验

- 同一工作区两个 read 用户解析到相同的 file IDs、L0/L1 候选和 L2 chunk 结果；
- 无 workspace read 的用户、仅 owner_id 用户和仅 Team 成员均得到拒绝或空的授权范围；
- workspace read 被撤销后，搜索、预览、文件内容和 chunk context 都不能继续访问；
- 不同工作区的唯一短语、文件路径和层级摘要不会跨工作区召回；
- 服务令牌只能检索绑定工作区，ShareLink 仍按令牌、密码、有效期和次数工作；
- 生产检索代码中没有新增 `FilePermission`、Team ACL、`user_id` 摘要键或 ACL digest 过滤。

##### 主线程编码记录（2026-07-14 11:24:19 +08:00）

**执行状态：完成。** 本步骤只修改检索、E2E 和 ACL 退役测试，没有修改检索、文件预览、chunk context、ShareLink 或服务令牌的生产实现。

1. 将 `test_retrieval_service.py` 从已失效的文件 owner/direct/Team ACL 场景改写为当前 `PermissionFilter` 和 `RetrievalService` 的 workspace 测试：workspace read/write 得到同一工作区文件，owner_id-only 和 Team-only 不扩大范围，平面检索与 L0/L1/L2 均按 workspace file IDs 过滤。
2. 从 `test_integration_retrieval.py` 删除 ACL 模型与 `test_permissions` fixture；所有文件绑定明确 workspace，成员关系改为 workspace read/write，并将 owner/direct/team 相关名称和预期改成 workspace 语义。该历史文件仍保留原有非授权测试正文。
3. 从 `test_e2e.py` 删除文件 ACL 模型导入、文件授权工作流、权限持久性、并发授权和授权性能用例；新增 workspace read → write → revoke 矩阵以及 Team-only 不能读取文件的负向场景。多用户协作改用 workspace read，ShareLink 仍通过独立令牌访问。
4. 在默认收集的 `test_acl_retirement.py` 新增三条当前数据面契约：同工作区 reader 共享层级检索范围、撤权后搜索/预览/内容/chunk context 均不可见、Team 成员关系不改变检索范围；同时扩展 owner_id-only 的检索空范围断言。
5. 修复 `test_acl_retirement.py` 自动 fixture 的测试隔离：进入时保存已有 FastAPI dependency overrides，退出时恢复，避免默认套件按文件顺序运行时破坏 `test_files_api.py` 的隔离数据库覆盖。该调整只影响测试生命周期。

##### 主线程测试结论（2026-07-14 11:24:19 +08:00）

**步骤八核心契约：PASS。默认全集：203 通过、2 个非 R-02 环境依赖失败。**

| 校验项 | 结果 |
|---|---|
| ACL 退役与当前 RetrievalService | `test_acl_retirement.py` + `test_retrieval_service.py`：23 项通过。 |
| 默认契约与文件测试顺序隔离 | `test_acl_retirement.py` + `test_files_api.py`：51 项通过，确认 dependency overrides 不再跨文件泄漏。 |
| E2E workspace 权限矩阵 | `TestWorkspacePermissionMatrix`：2 项通过。 |
| ShareLink 与服务令牌基础鉴权 | `test_share_manager.py`、`test_share_api.py`、`test_service_token_workspace.py`、`test_service_token_deps.py`：58 项通过。 |
| 服务令牌搜索与预览绑定 | `test_service_api.py -k "search or preview"`：21 项通过、20 项未选择。 |
| 历史测试文件收集 | `test_integration_retrieval.py` 与 `test_e2e.py`：57 项成功收集；目标文件语法编译通过。 |
| ACL 残留扫描 | 三个目标历史测试中不存在 `FilePermission`、`EntityType`、`PermissionManager`、`test_permissions` 或旧文件权限路由调用；检索生产代码中不存在 ACL digest、Team ACL 或用户维度 L0/L1/L2 键。 |
| 默认 pytest 全集 | 首轮 180 通过、25 失败，其中 23 项为契约 fixture 清空全局 overrides 导致的顺序污染；修复后复跑为 203 通过、2 失败。剩余两项均为 parser parity 加载 JSONL 解析器时缺少环境依赖 `roman_numbers`，与 R-02 变更无关。 |

`test_integration_retrieval.py` 仍属于默认白名单之外的历史套件：它已完成 ACL fixture 和语义清理并可收集，但其大量原测试仍调用早已移除的 `RetrievalService(..., agfs_client=...)` 接口，因此没有将该文件全量执行结果作为步骤八通过证据。为避免扩大 R-02，本步骤没有给生产 RetrievalService 增加旧接口兼容层，也没有重写该文件中与权限无关的 reranker/API 历史用例。

##### 主线程 Review 结论（2026-07-14 11:24:19 +08:00）

**结论：PASS（带独立门禁风险）。同意进入步骤九；当前版本不可发布。**

- 数据面契约符合目标：同 workspace readers 的 file IDs、L0/L1 调用范围和 L2 结果一致；跨 workspace 文件不会进入任一候选阶段。
- 撤权为 fail-closed：成员记录删除后搜索在 embedding/vector 调用前返回空，预览、原始内容和 chunk context 均返回 403。
- 独立能力未被混入 workspace membership：Team-only 仍无检索权；ShareLink 和服务令牌继续按各自令牌、绑定及约束工作。
- 变更边界正确：生产代码零修改，没有引入用户级摘要变体、ACL digest、Team 过滤或旧 RetrievalService 兼容层。
- 非阻断历史风险：`test_integration_retrieval.py` 的旧 `agfs_client` 调用仍需独立测试现代化计划，不属于文件 ACL 下线；默认全集剩余 `roman_numbers` 缺依赖必须在步骤十发布门禁前闭环或明确调整测试环境。

##### JSONL parser 依赖修复记录（2026-07-14 11:34:13 +08:00）

**状态：已闭环。** 前述默认全集 `203` 项通过、`2` 项失败是修复前的历史结果；修复后默认全集为 `205` 项全部通过。

- 根因不是 JSONL parser 的解析逻辑，而是核心 editable/API 安装清单未声明 `rag.nlp` 包初始化时直接导入的运行时依赖；worker 安装清单虽已有相关包，但默认测试环境不使用该清单。
- 在 `setup.py` 和 `requirements.txt` 补齐最小直接依赖集合：`roman-numbers==1.0.2`、`word2number==1.1`、`cn2an==0.5.22`、`chardet>=5.2.0,<6.0.0`、`Pillow>=10.0.0`；同时将 `requirements-worker.txt` 中对应声明对齐，避免不同运行环境继续漂移。
- 仅补 `roman-numbers` 后，导入链继续暴露 `word2number` 缺失，证明缺口属于 `rag.nlp` 顶层直接依赖链，而非单一包偶发缺失。因此补齐上述五项直接依赖，没有修改 parser 生产代码，也没有用延迟导入或异常吞噬掩盖环境问题。
- 版本与导入校验通过：`roman-numbers=1.0.2`、`word2number=1.1`、`cn2an=0.5.22`、`chardet=5.2.0`、`Pillow=12.3.0`，五个模块均可在核心 editable 环境直接导入。
- 两个原失败的 parser parity 用例复跑结果为 `2 passed`；默认 pytest 全集复跑结果为 `205 passed, 12 warnings`。告警均为既有框架弃用告警，本次 `roman_numbers` 发布门禁风险已关闭。

### 13.10 步骤九：同步文档并确认前端无调用

#### 目的

让产品说明、架构、API、存储和 DDL 与最终权限模型一致，同时保留历史设计上下文。

#### 方法

1. 更新产品和当前架构文档，删除仍在提供文件级 ACL 的表述。
2. 从当前 API 和 DDL 文档移除三个旧接口及活动表定义。
3. 对历史设计文档只增加废弃提示和 R-02 链接，不改写历史正文。
4. 将风险评审中的 R-02 状态更新为“已实施”只能发生在生产验收完成后；代码完成但未发布时标记为“已决策/待发布”。
5. 前端不做功能改动，只检查不存在旧路由调用，并构建验证现有权限页面。

#### 解决的问题

- 防止文档继续承诺不存在的细粒度权限；
- 防止 DBA 或外部调用方按旧 DDL/API 新增 ACL；
- 防止误删历史决策依据；
- 防止误将前端 `permissionsAPI` 当作文件 ACL 客户端删除。

#### 涉及修改/保留的函数、字段

| 文件 | 对象 | 调整 |
|---|---|---|
| `openrag/README.md` | 文件权限产品描述 | 删除或改为工作区 RBAC |
| `docs/framework detail.md` | 权限架构章节 | 删除 `FilePermission` 数据面描述 |
| `docs/storage.md` | `file_permissions` | 标记已下线并从当前活动表清单移除 |
| `docs/04-外部系统接入与API.md` | 三个旧接口 | 删除 |
| `docs/CURRENT_MODEL_SCHEMA_DDL.md` | ACL 枚举、表、约束和索引 | 从当前 schema 删除 |
| `docs/2026-07-10-OpenRag架构风险与检索优化评审.md` | R-02 状态 | 按实际发布阶段更新 |
| `docs/superpowers/specs/2026-06-24-file-preview-action-and-single-file-filter-design.md` 等历史文档 | 顶部状态提示 | 新增“文件 ACL 已由 R-02 下线”提示，不改历史结论 |
| `web/src/services/api.ts` | `permissionsAPI.getPermissionDetails()` | 保留，不改路径 |
| `web/src/pages/AdminPermissions.tsx`、`Permissions.tsx` | 权限详情调用 | 保留 |

本步骤没有新增后端函数或数据库字段。

#### 本步骤校验

- 当前态文档中不再把 `FilePermission` 宣称为可用功能；
- 当前 API 文档和 DDL 不再包含旧路由、表、索引或 ACL 专用枚举；
- 历史文档仍保留原设计正文，并能链接到本方案；
- 前端源码中不存在 `/files/{file_id}/permissions` 的调用；
- `npm run build` 通过，管理员权限页和用户权限页仍能读取 `/users/{user_id}/permissions/details`。

##### 主线程编码记录（2026-07-14 11:57:38 +08:00）

**执行状态：完成。** 本步骤仅同步文档并验证前端，没有修改前端或后端生产代码，也没有执行生产迁移、发布或流量切换。

1. `openrag/README.md` 删除细粒度文件 ACL 产品承诺和 `FilePermission` 模型说明，改为工作空间成员/角色 `read`、`write` 授权，并明确 Team 不直接授予文件访问权限。
2. `docs/framework detail.md` 将权限架构统一为工作空间 RBAC，明确 `File.owner_id` 仅用于审计、Team 不参与文件授权；同步删除已下线模型/服务的目录树说明，同时保留 `permissions_api.py` 的用户工作空间权限详情职责。
3. `docs/storage.md` 从活动 PostgreSQL 数据清单移除 `file_permissions`，记录实施前生产核查为 `0` 行、待发布迁移会再次做零记录保护，以及生产发布尚未执行。
4. `docs/04-外部系统接入与API.md` 的 `/files` 能力表不再承诺文件级权限。该文档原本没有展开三个旧 ACL 路由，因此没有删除无关 API 正文。
5. `docs/CURRENT_MODEL_SCHEMA_DDL.md` 删除 `file_permissions`、`entity_type`、`acl_permission`、唯一约束及两个专用索引。该文件原称覆盖全部业务表，但当前 ORM metadata 实际有 25 张表、原脚本只覆盖其中一部分；为避免扩大 R-02，只将其范围如实标为“本文件收录的 14 张核心表”，没有补写 11 张无关模型。
6. 风险评审中的 R-02 更新为“已决策/待发布”，并把旧代码证据标为原始评审基线；没有在生产验收前写成“已实施”。
7. 历史设计 `2026-06-24-file-preview-action-and-single-file-filter-design.md` 只在顶部新增 R-02 废弃提示和链接，原设计正文零修改。R-01 文档中“该方案不处理 R-02”的历史边界仍准确，因此未修改。
8. 前端 `permissionsAPI.getPermissionDetails()`、`AdminPermissions.tsx` 和 `Permissions.tsx` 保持原样，继续调用 `/users/{user_id}/permissions/details`。

##### 主线程测试结论（2026-07-14 11:57:38 +08:00）

| 校验项 | 结果 |
|---|---|
| 当前态文档扫描 | README、架构和存储文档不存在仍将 `FilePermission`、`PermissionManager` 或文件 ACL 宣称为可用能力的表述；存储文档仅保留下线状态说明。 |
| API 与 DDL 扫描 | 外部 API 文档不存在三个旧文件 ACL 路由或“文件级权限”能力；DDL 不存在旧表、两类 ACL 枚举、唯一约束或专用索引，脚本内 `CREATE TABLE` 数为 14。 |
| 历史文档 | 指定设计文档 diff 仅新增顶部两行提示，R-02 链接目标存在；R-01 历史正文没有改动。 |
| 前端静态检查 | `web/src` 中搜索旧文件 ACL 路由、`FilePermission`、`file_permissions` 无命中；用户权限详情方法及两个页面共 4 个预期命中，路径仍为 `/users/${userId}/permissions/details`。 |
| 前端依赖与构建 | 工作区最初缺少 `node_modules`，按既有 `package-lock.json` 执行 `npm ci` 后，`npm run build` 成功，Vite 完成 3400 个模块转换并生成 `dist`。只有既有 `app-config.js` 非 module 和大 chunk 提示。 |
| 后端权限契约 | `test_acl_retirement.py` + `test_permissions_api.py`：17 项通过、6 个既有弃用告警，确认旧路由继续消失且用户权限详情接口保留。 |
| 差异与工作区 | `git diff --check` 通过，仅有既有 LF/CRLF 提示；`web/` 没有 tracked 修改，`node_modules` 和构建产物未进入变更集。 |

`npm ci` 同时报告锁定依赖树存在 14 个审计漏洞（1 low、7 moderate、4 high、2 critical）。本步骤没有执行会改变版本或可能引入破坏性升级的 `npm audit fix`；该既有供应链风险必须在步骤十发布准入中单独评估。

##### 主线程 Review 结论（2026-07-14 11:57:38 +08:00）

**结论：PASS。同意进入步骤十。该结论为 2026-07-14 的历史发布前状态；R-02 当前状态已在 2026-07-15 标记为已完成。**

- 目的匹配：产品、架构、API、存储和 DDL 不再承诺不存在的文件级授权；真实数据面边界统一为工作空间 RBAC，分享链接和服务令牌仍作为独立能力保留。
- 变更保持最小：实际实现只改文档；没有为了证明前端无调用而删除或改名 `permissionsAPI`，也没有改动两个权限页面、后端路由或数据库对象。
- 历史信息保存正确：旧设计正文和风险形成时的证据仍可追溯，同时通过状态提示避免被误当作当前能力。
- 状态表达正确：风险评审没有提前标记“已实施”；生产零记录复核、迁移、健康验收和发布仍属于步骤十及后续发布动作。
- 独立剩余风险：DDL 文件仍不是 25 张 ORM 表的全量快照，已显式标注范围，完整 DDL 现代化不属于 R-02；前端锁定依赖的 2 个 critical 等审计漏洞必须进入步骤十准入评估，不能因本次构建成功而忽略。
- 测试充分性：步骤九没有生产代码变更，因此未重复执行全量后端测试；步骤八修复依赖后默认全集已为 205 项通过，本步骤补充的 17 项权限契约和前端生产构建均通过。

### 13.11 步骤十：预发布集成验收与发布准入

#### 目的

在生产维护窗口之前完成代码、数据库、API、权限矩阵和回滚的组合验证，形成明确的可发布/不可发布结论。

#### 方法

1. 在与生产 PostgreSQL 版本一致的预发布环境执行 upgrade → 应用启动 → smoke test → downgrade → 旧应用启动 → 再次 upgrade。
2. 运行默认测试集、R-02 专用测试、迁移测试和前端构建。
3. 生成 OpenAPI 并与基线比较，确认只移除目标接口和 schema。
4. 执行静态搜索；区分 ACL 的 `EntityType` 与 GraphRAG 中无关的 `entity_type`，禁止用全局字符串替换误伤图谱代码。
5. 记录所有验证命令、版本、结果和失败重试，不以人工点击代替可重复测试。

#### 解决的问题

- 防止代码删除和数据库迁移分别通过、组合部署却失败；
- 防止默认 pytest 未收集关键测试；
- 防止误伤 GraphRAG、ShareLink、服务令牌或前端权限页；
- 防止未验证 downgrade 就进入生产。

#### 涉及的函数、字段与产物

本步骤原则上不再修改生产函数或字段。若验收失败，只修复能够直接追溯到步骤三至步骤九的遗漏，并重新执行对应步骤校验。需要生成：

- 测试报告；
- Alembic 往返迁移报告；
- OpenAPI 差异；
- 静态搜索结果；
- 权限矩阵与 L0/L1/L2 验收记录；
- 前端构建结果；
- 发布准入结论。

#### 本步骤校验

- `python -m pytest` 的默认收集集通过；
- R-02 API、文件操作、检索和 PostgreSQL 迁移专用测试全部通过；
- `python -m compileall src/openrag` 通过；
- `npm run build` 通过；
- OpenAPI 只移除三个旧路由和专用 schema，保留用户、工作区、角色、ShareLink 和服务令牌接口；
- 生产代码搜索 `FilePermission`、`PermissionManager`、`file_permissions_router` 无命中；旧表名只允许出现在 migration、测试和废弃文档中；
- 预发布 downgrade 后旧版本能启动并写入空 ACL 表，再次 upgrade 能成功；
- 所有完成标准均有证据后才签署发布准入。

##### 主线程预发布演练记录（2026-07-14 15:01:34 +08:00）

**验收执行完成；R-02 代码与迁移组合验证通过。** 演练使用独立临时容器和 detached 基线工作树，没有复用、清空或修改本机既有 `openrag-postgres`，也没有对生产执行迁移、重启、部署或流量切换。

| 阶段 | 结果 |
|---|---|
| 隔离环境 | 镜像 `postgres:16.14-alpine`，镜像 digest `sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777`；数据库报告版本 `16.14`，与生产只读核查版本一致。 |
| 旧版本基线 | detached worktree 固定在 `93279e271f24b42e1009bb2d9b21f88a7c88ada9`（R-01 已提交、R-02 未落地）。旧 ORM 创建 26 张表，包含空 `file_permissions`，并 stamp 到 `20260626_0005`。 |
| 首次 upgrade | 当前迁移升级到 `20260713_0006`；`file_permissions`、`entitytype`、`permission` 均不存在。当前应用完整 startup/shutdown 生命周期通过，`GET /health` 返回 200、`status=healthy`、`database=connected`。 |
| downgrade | 回退到 `20260626_0005`；空 ACL 表恢复，枚举标签分别为 `{USER,TEAM}` 与 `{READ,WRITE,ADMIN}`。 |
| 旧应用与旧 ORM | 基线应用完整 startup/shutdown 生命周期通过，健康检查连接数据库；旧 `FilePermission` ORM 成功插入 1 条 READ ACL，再删除回 0 条，证明恢复 schema 可由旧版本实际读写。 |
| 再次 upgrade | 在 ACL 清回 0 条后再次升级到 `20260713_0006`；旧表和枚举再次删除，当前应用再次健康启动。 |
| 清理 | 临时 PostgreSQL 容器自动删除；detached 基线工作树内容 hash 与 HEAD 一致后移除。`code-optimization` 工作区未因演练新增代码修改。 |

失败与重试记录：

1. 旧 schema 初始化首轮命令因 PowerShell 嵌套 f-string 引号错误在 Python 解析阶段退出，数据库尚未执行；改用 `str.format()` 后成功。
2. 当前应用首次 smoke 已完成数据库升级，但 Windows GBK 控制台无法输出既有 `✓/✗` 字符，生命周期在日志打印处失败；生产目标为 Linux UTF-8，设置 `PYTHONUTF8=1` 后当前与旧应用均通过。该问题与 R-02 无关，本步骤未扩大范围修改日志代码。
3. 生产只读组合查询首轮因本地 PowerShell 与远端 Bash 嵌套引号失败，未形成有效 SQL；拆分为简单只读命令后完成，没有生产写操作。
4. 清理临时基线时 Git 因 Windows 换行统计将一个大文件报告为 modified；工作树文件与 HEAD 的 blob hash 均为 `f1f50811175d9446bcd26399db7108ef2969ad94`，确认内容一致后才移除临时工作树。

##### 主线程测试与契约报告（2026-07-14 15:01:34 +08:00）

| 校验项 | 结果 |
|---|---|
| 默认 pytest | `205 passed, 12 warnings`；默认收集集全部通过，告警为既有框架弃用和 SQLAlchemy 提示。 |
| R-02 PostgreSQL 迁移专项 | `6 passed`；覆盖空表升级、非空中止、缺表、共享枚举、可写 downgrade 和 upgrade → downgrade → upgrade。 |
| 权限与独立能力组合专项 | `119 passed, 8 warnings`；覆盖 ACL 退役、文件 API、工作区 read/write、owner/Team 无特权、检索与 L0/L1/L2、权限详情、ShareLink、服务令牌及 E2E 工作区矩阵。 |
| 编译 | `python -m compileall -q src/openrag` 通过。 |
| 前端构建 | `npm run build` 通过，Vite 转换 3400 个模块并生成 `dist`；只有既有 `app-config.js` 非 module 和大 chunk 提示，`web/` 无 tracked 变更。 |
| OpenAPI 基线 | 以 `93279e2` 为基线，operation 从 106 变为 103；只移除 `GET/POST /files/{file_id}/permissions` 和 `DELETE /files/{file_id}/permissions/{permission_id}`，无新增 operation。 |
| OpenAPI schema | 只移除 `GrantPermissionRequest`、`PermissionResponse`、`openrag__api__permissions_api__MessageResponse`，无新增 schema；`/users/{user_id}/permissions/details` 保留，用户/工作区/角色/分享/服务相关路径仍有 45 条。 |
| 静态扫描 | `openrag/src` 中 `FilePermission`、`PermissionManager`、`file_permissions_router` 无命中；`file_permissions` 仅存在于迁移、专项测试、R-02 方案和存储下线提示。GraphRAG/vendor 的 `entity_type`、`entity_types`、`entity_type_kwd` 命中均保留，未发生全局替换。 |
| 差异格式 | `git diff --check` 在最终记录后复核；步骤十没有新增生产代码修改。 |

##### 生产只读复核与发布风险（2026-07-14 15:01:34 +08:00）

- 生产主机 `192.168.100.33`：PostgreSQL `16.14`、Alembic `20260626_0005`、`file_permissions=0`；API `/health` 返回 healthy/database connected；近 24 小时 API 与 Web 日志中旧文件 ACL 路由命中均为 0。此前由发布负责人接受的“调用方排查仅覆盖当前容器日志窗口”剩余风险不变。
- 生产 Web 对外根路径返回 HTTP 200，但 `openrag-web-prod` 仍为 `unhealthy`，失败累计 37195 次。健康检查使用 `wget http://localhost/health`，容器内 `localhost` 解析到 `::1` 后连接被拒绝；同容器访问 `http://127.0.0.1/health` 返回 200。该风险不是 R-02 引入，但方案此前明确要求在步骤十准入前闭环。
- `npm audit` 全依赖树为 14 个漏洞（1 low、7 moderate、4 high、2 critical）；两个 critical 来自开发依赖 `vitest`、`@vitest/ui`，修复需要 major 升级。`npm audit --omit=dev` 的生产依赖为 6 个漏洞（4 moderate、2 high、0 critical）：直接依赖 `axios@1.14.0` 和传递依赖 `form-data@4.0.5` 为 high，均报告存在修复版本。
- 本步骤没有执行 `npm audit fix`、修改生产健康检查、变更镜像或重启容器。这些动作超出 R-02 步骤十的最小修复边界，需要独立授权、回归与发布决策。

##### 主线程 Review 与发布准入结论（2026-07-14 15:01:34 +08:00）

**结论：R-02 实现验收 PASS；整体发布准入 NO-GO。不得进入步骤十一。**

- R-02 本身满足组合验证：新旧应用、数据库 upgrade/downgrade、旧 ORM 写入、权限矩阵、检索层级、独立访问能力、OpenAPI、静态扫描和前端构建均通过，未发现由文件 ACL 下线引入的回归。
- 当前 NO-GO 的首要阻断是生产 Web 健康检查尚未闭环。虽然实际 IPv4 Web 服务可访问，但容器持续 `unhealthy`，违反本方案在步骤一和步骤九 Review 中已记录的步骤十前置约束。
- 第二项准入待决策是生产前端依赖仍含 2 个 high 漏洞，且直接依赖 `axios` 有可用修复。发布负责人需要选择“升级并回归”或形成明确、限时、可审计的风险接受；在没有该结论前不签署整体准入。
- 解除 NO-GO 的最小条件：修正并验证 `openrag-web-prod` 对应部署配置的健康检查，使新容器稳定为 healthy；完成生产依赖 high 漏洞的修复或书面风险接受；随后重跑前端构建、健康检查和本节受影响门禁，再更新本结论。
- 由于当时准入为 NO-GO，风险评审中的 R-02 状态在 2026-07-14 仍保持“已决策/待发布”，不更新为“已实施”，也不执行步骤十一的生产迁移与发布。该历史状态已由 2026-07-15 的完成状态更新取代。

##### NO-GO 本地修复记录（2026-07-14 15:28:18 +08:00）

**仓库修复与本地验证完成；生产尚未发布，因此整体准入仍保持 NO-GO。**

1. 健康检查根因位于 `docker/docker-compose.prod.yml` 的 Web healthcheck 覆盖：镜像 `Dockerfile.web` 已使用 `127.0.0.1`，但 Compose 又覆盖为 `http://localhost/health`。现仅将该覆盖改为 `http://127.0.0.1/health`，未修改 Nginx 监听、业务路由或 API。
2. 将直接生产依赖的安全下限提升为 `axios^1.18.1`、`dompurify^3.4.12`、`react-router-dom^6.30.4`；锁文件解析结果为 `axios 1.18.1`、`form-data 4.0.6`、`follow-redirects 1.16.0`、`dompurify 3.4.12`、`react-router-dom/react-router 6.30.4`。没有使用 `npm audit fix --force`、override 或把 `form-data` 伪装成直接依赖。
3. `npm ci` 可从更新后的锁文件完整复现；`npm audit --omit=dev` 从 6 个生产漏洞降为 0（0 low/moderate/high/critical）。全依赖树仍有 8 个开发工具链漏洞，其中 `vitest`、`@vitest/ui` 为 critical，`vite` 及其 `ws` 链为 high；修复需要同步升级到 Vitest 4/Vite 8 并提升 Docker builder Node 版本，作为独立工具链迁移处理，不与生产热修混合。
4. 前端测试使用稳定的 forks/串行文件模式执行：15 个 test files、112 项测试全部通过。首次将默认测试与构建并行运行时达到 120 秒工具超时并遗留 Vitest 子进程，清理该进程后拆分执行；这次超时不计为测试通过证据。
5. `npm run build` 通过，Vite 转换 3402 个模块。实际 `Dockerfile.web` 镜像构建通过；第一次单独运行 Web 容器因缺少 Compose 中名为 `api` 的上游而在 Nginx 启动前退出，补齐测试用 `api` 主机映射后容器状态为 `healthy`，healthcheck 明确使用 `127.0.0.1`，宿主机请求 `/health` 返回 200。临时容器和镜像已清理。
6. `docker compose -f docker/docker-compose.prod.yml config --quiet` 通过。生产 Compose 实际由当前 release、`shared/docker-compose.server.yml` 和 `shared/docker-compose.web-fix-preview.yml` 三层组成；当前生产仍运行 `openrag-web:1.1.7-fix-preview`，未重建、未重启、未修改服务器文件。

生产部署需要同时发布新的 Web 镜像和 Compose 修复，才能真正清除 `unhealthy` 与旧依赖漏洞。依据 `deploy-openrag-server` 流程，发布产物必须带明确版本号并由本地构建后 `docker save/load` 上传；当前尚未提供新版本号，因此未执行生产发布。发布完成后必须复核：Web 容器持续 healthy、`/health`/`/api/health`/直连 API 均通过、`app-config.js` 返回 200、生产镜像由更新后的 lock 构建，并再次执行生产依赖审计证据核对。

### 13.12 步骤十一：生产发布与发布后监控

#### 目的

在避免旧代码访问新 schema 的前提下完成单版本下线，并快速发现遗漏调用方或权限回归。

#### 方法

1. 进入维护窗口，停止旧 API/worker 流量和自动拉起。
2. 再次执行 ACL 行数、旧路由调用和数据库备份检查。
3. 执行 `alembic upgrade head`；只有零记录断言通过后才继续。
4. 部署同一版本的新 API 和 worker，执行数据库、OpenAPI、文件读写、检索和令牌 smoke test。
5. 恢复流量，按固定窗口监控旧路由 404、权限 403、数据库异常、文件操作失败和检索空结果。

#### 解决的问题

- 防止旧实例在迁移后访问已删除表；
- 防止发布期间产生新的 ACL 记录；
- 防止 API 成功启动但权限或检索数据面回归；
- 通过 404 监控发现遗漏的外部调用方。

#### 涉及的函数、字段与运行对象

本步骤不新增或修改函数、字段。运行对象包括：

- Alembic `upgrade()`；
- 新版本 API 与 worker；
- `file_permissions` 零记录断言；
- OpenAPI、工作区权限、L0/L1/L2、ShareLink 和服务令牌 smoke test；
- 三个旧路径的 404 监控指标。

#### 本步骤校验

- 迁移 revision 到达 `20260713_0006`，`file_permissions` 不存在；
- 三个旧路由为普通 404，OpenAPI 不暴露；
- workspace read/write 权限矩阵与预发布一致；
- 无工作区权限、仅 owner_id、仅 Team 身份均不能访问内容；
- 同工作区用户获得相同层级检索范围，跨工作区结果为零；
- ShareLink 和服务令牌 smoke test 通过；
- 监控窗口内无新增数据库缺表异常、异常 403 峰值或检索空结果峰值；
- 发布记录包含最终 ACL 行数、迁移 revision、镜像/提交 SHA、开始结束时间和验收人。

##### 完成状态更新（2026-07-15）

**R-02 文件级 ACL 下线优化已标记完成。** 当前文档状态、完成标准、存储说明和风险评审均已同步为“已完成”。历史章节中保留的 2026-07-14 待发布或 NO-GO 记录仅表示当时的阶段性准入结论，不再代表当前状态。

### 13.13 步骤十二：回滚演练与生产回滚

#### 目的

证明 R-02 可以恢复到旧版本可运行的空 ACL schema，并在生产出现明确故障时使用唯一、安全的回滚顺序。

#### 方法

1. 预发布必须执行回滚演练；生产只有触发条件满足时才执行实际回滚。
2. 先停止新版本流量，再执行 Alembic downgrade。
3. 验证空表、枚举、外键、唯一约束、索引和主键自动生成恢复后，再部署旧 API 和 worker。
4. 验证旧接口重新注册且可写入、查询、删除一条临时 ACL；演练数据验证后清理。
5. 若 downgrade 失败，保持维护状态，禁止启动旧代码，转入数据库备份恢复流程。

生产回滚触发条件包括：

- 新版本无法启动或持续出现 `file_permissions` 缺表异常；
- 工作区权限矩阵出现越权或大面积误拒绝；
- 检索发生跨工作区泄漏；
- ShareLink、服务令牌或核心文件操作出现无法在维护窗口内修复的回归。

单纯观察到旧 ACL 路由 404 不是回滚理由，因为这是本次发布的预期行为；应先定位调用方。

#### 解决的问题

- 防止在数据库 schema 尚未恢复时启动依赖旧表的旧代码；
- 防止因预期的旧接口 404 进行不必要回滚；
- 防止 downgrade 失败后继续放量，扩大数据库与应用版本不一致的影响；
- 证明恢复后的空 ACL 表、枚举和主键生成能够被旧版本真实使用。

#### 涉及的函数、字段与对象

| 对象 | 调整/恢复 |
|---|---|
| Alembic `downgrade()` | 执行旧 schema 重建 |
| `file_permissions` 字段 | 恢复 `id`、`file_id`、`workspace_id`、`entity_type`、`entity_id`、`permission`、`created_at` |
| 约束和索引 | 恢复外键、`uq_file_entity`、`idx_permission_file_id`、`idx_permission_entity` |
| 旧版本 API 函数 | 恢复 `list_permissions()`、`grant_permission()`、`revoke_permission()` |
| 旧版本服务和 ORM | 随旧镜像恢复，不在新版本代码中重新实现 |

#### 本步骤校验

- downgrade 后旧版本应用启动无缺表、枚举或 mapper 错误；
- 可通过旧 API 新增一条 ACL、查询该记录、撤销该记录，证明序列和约束可写；
- 工作区文件和检索功能恢复到回滚前旧版本基线；
- 回滚全过程遵循“停新流量 → downgrade → 验证 schema → 部署旧代码 → 恢复流量”；
- 若使用备份恢复，恢复后的 revision、schema 和业务数据均完成一致性校验；
- 回滚演练报告归档后，R-02 才具备生产发布资格。
