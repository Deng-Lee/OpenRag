# OpenRag 待完善功能

## 切块引擎

### 1. 策略硬编码，无法运行时切换
- **位置**: `processors/document_processor.py` → `ChunkEngine(strategy=ChunkStrategy.SEMANTIC)`
- **问题**: 策略在代码中固定为语义分块，前端和 API 均未提供切换入口，用户无法根据文件类型选择段落分块或固定大小分块
- **建议**: 提供环境变量 `OPENRAG_CHUNK_STRATEGY` 或在 API 参数中支持 `chunk_strategy` 字段

### 2. chunk_method 参数形同虚设
- **位置**: `chunking/chunk_params.py` → `resolve_chunk_method()` → `chunking/ragflow_core/semantic.py` → `_ = chunk_method`
- **问题**: `resolve_chunk_method()` 根据文件类型返回 `pdf_manual` / `presentation` / `manual`，但 `chunk_semantic_ragflow` 将其直接丢弃，三种类型走相同逻辑
- **建议**: 让 `chunk_method` 实际影响分块行为，例如 PDF 启用位置感知切分、PPT 按幻灯片边界切分

### 3. 巨型 section 无法被拆分
- **位置**: `chunking/ragflow_core/semantic.py` → `ragflow_naive_merge()` → `add_chunk()`
- **问题**: 算法只在 section 之间决定断开，不会拆分单个 section 内部。若解析器产出一个远超 chunk_token_num 的大段落，它会原样保留为独立切片
- **建议**: 在 `add_chunk` 中检测单 section token 数超过上限时，递归调用 `_chunk_fixed_size` 对其二次拆分

### 4. fixed_size 兜底范围太窄
- **位置**: `chunking/ragflow_core/semantic.py` → `chunk_semantic_ragflow()` 末尾
- **问题**: 兜底触发条件为语义合并结果 ≤ 1 个切片。若文档有多个正常块加一个巨型块，最终切片数 > 1，兜底不触发，超大切片会漏过
- **建议**: 改为遍历所有合并结果，对单个切片 token 数超过阈值的也触发二次拆分

### 5. strict_limit 默认关闭
- **位置**: `chunking/ragflow_core/semantic.py` → `OPENRAG_STRICT_TOKEN_LIMIT`
- **问题**: 默认不开启，语义合并不会预判合并后大小，可能导致正常块被后续大块吞并
- **建议**: 评估后将默认值改为开启，或在文档中说明此配置项

---

## 前端页面

### 6. 缺少团队管理页面
- **位置**: 后端 `api/teams_api.py` + 前端 `services/api.ts` → `teamsAPI`
- **问题**: 后端提供了团队的完整 CRUD 和成员管理接口，但前端没有 `/teams` 页面，普通用户无法使用团队功能
- **建议**: 新建 `pages/Teams.tsx`，提供团队列表、创建、成员管理等功能，并在 `App.tsx` 中注册路由

### 7. 缺少面向普通用户的工作空间成员管理
- **位置**: 后端 `api/workspaces_api.py` → 成员管理端点 + 前端 `services/api.ts` → `workspacesAPI.listMembers/addMember/removeMember`
- **问题**: 成员管理仅在管理员界面（`AdminPermissions.tsx`）中暴露，普通用户无法查看和管理自己参与的工作空间成员
- **建议**: 在 `pages/Files.tsx` 或独立页面中添加成员列表入口，允许 write 权限用户管理成员

---

## 层级传播与目录聚合

### 8. 文件夹创建和文件上传未设置 parent_id，导致目录 L0/L1 聚合失效
- **位置**: `services/file_ingest.py:215-224` (`ingest_new_file`) + `api/files_api.py:1061-1069` (`create_directory`)
- **问题**: 创建文件和目录记录时均未设置 `parent_id` 字段，导致 `propagate_parent_directory_hierarchies()` 中 `leaf.parent_id` 始终为 `None`，while 循环一次都不执行，多级目录的 L0/L1 聚合完全失效。MinIO 中只有单文件级别的 `.abstract.md` / `.overview.md`，缺少目录级别的聚合总结
- **建议**: 
  - `create_directory` 中根据 `parent_path` 查询父目录记录并赋值 `parent_id`
  - `ingest_new_file` 中根据 `parent_logical_path` 查询父目录记录并赋值 `parent_id`
  - 修复后已存在数据需通过迁移脚本补全 `parent_id`（根据 `uri` 反推父目录）

---

## 其他

### 9. RAGFlow parser 部分实现为占位符
- **位置**: `parsers/ragflow_parser.py` → `RAGFlowPDFParser` / `RAGFlowMarkdownParser`
- **问题**: 类中方法直接 `raise NotImplementedError`，实际解析能力依赖于 `parsers/adapters/` 中另起炉灶的适配器实现，两套 parser 并存造成混淆
- **建议**: 清理废弃的占位符代码，或将适配器逻辑回填到这些类中统一入口
