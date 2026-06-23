# 方案二执行方案：上传时创建实体目录行（folder upload 目录不显示修复）

> 分支：`fix/folder-upload-dir-rows`（从 `main` 切出）
> 本文只描述**执行方案**，不含实现代码。实现按下文「分步（TDD）」落地。

## 1. 背景与根因（一句话）

`ingest_new_file(require_parent_dir=False)` 在上传时只为文件落一行带完整深层 `uri` 的记录（`is_directory=false`），**从不创建中间目录行**。而「目录」在本系统里是 `files` 表里一行 `is_directory=true` 的记录：

- Web 左侧目录树懒加载用 `list_files(parent_path=P)` 取 **P 的直接子项**，依赖实体目录行；
- 服务令牌 `GET /workspaces/{ws}/tree`、`/children` 用 `build_nested_tree` / `list_direct_children`，**强依赖实体目录行**（查不到目录行直接 404）。

因此：文件夹上传后文件解析正常、右侧列表（走 `under_path` 取整棵子树）能看到全部文件，但左侧树与服务端目录接口都看不到这些目录。

详细取证见 `fix-folder-upload` 分支的 `docs/2026-06-22-folder-upload-diagnosis.md` 与本次定位结论。

## 2. 改动点（唯一 chokepoint）

- **位置**：`openrag/src/openrag/services/file_ingest.py` 的 `ingest_new_file`，在 `require_parent_dir=False` 分支、**构造文件行之前**，调用 `ensure_directory_path(db, workspace, <父目录逻辑路径>)`（即 `validate_path(parent_logical_path)`）。
- **为什么放这里**：`ingest_new_file` 是**唯二**上传入口的公共必经点——
  - `openrag/src/openrag/api/files_api.py:399`（Web `/files/upload`）
  - `openrag/src/openrag/api/service_api.py:314`（服务令牌上传）

  放在 `ingest_new_file` 内，一处即可覆盖两条路径，避免只改 Web 端点导致服务端不一致。
- **复用现成能力**：`ensure_directory_path`（`file_ingest.py:191`）已是生产级 `mkdir -p`：幂等、并发安全（`IntegrityError` 重试 3 次、各自独立事务、唯一约束兜底），并会顺带 seed 根目录 `/`。无需新写路径/并发逻辑。

## 3. 与现有逻辑的兼容性

- **`service_api` 在 `create_dirs=True` 时已先调 `ensure_directory_path`**（`service_api.py:312`），随后 `ingest_new_file(require_parent_dir=False)`。改动后会再幂等调用一次——**无害**（命中已存在直接返回）。
- **`service_api` 在 `create_dirs=False` 时**用 `require_parent_dir=True`，**不进入**新分支，严格模式行为完全不变（父目录不存在仍报错）。
- **重复文件 / 超限 / MIME 不支持** 等既有分支位置不变。

## 4. 分步落地（TDD）

1. **先写失败测试（后端）**：
   - *建目录*：新空间，`ingest_new_file(parent_logical_path="/a/b", require_parent_dir=False, ...)`；断言落库后存在 `/a`、`/a/b` 两行且 `is_directory=true`，文件行 `uri="/a/b/<name>"`。
   - *幂等*：同目录再 ingest 另一文件；断言不重复建目录、无异常。
   - *严格模式不回归*：`require_parent_dir=True` 且父目录缺失 → 仍 400（`_assert_parent_directory_exists` 现有行为）。
   - *根目录上传*：`parent_logical_path="/"` → 不应新增除根外的目录行。
2. **实现**：在 `require_parent_dir=False` 分支加入 `ensure_directory_path` 调用（传父目录逻辑路径）。
3. **回归**：跑既有 upload / service upload / `test_workspace_file_tree` 等测试。

## 5. 行为与影响

- 单文件上传到深层路径也会幂等建祖先目录（更一致，符合预期）。
- 每次上传多若干次幂等 `SELECT/INSERT`，绝大多数命中已存在、开销极小。
- **并发**：`ensure_directory_path` 自带重试与独立事务；持续高争用的极端下抛 `503`。因为它在**落文件行之前**执行，503 时该文件整体上传失败并计入前端失败汇总，**不会留下「无目录的孤儿文件」**。

## 6. 风险与回滚

- 风险低：复用既有生产级函数，改动集中一处。
- 回滚：移除该调用即可；已建的目录行（`size=0`、`is_directory=true`）无害，可保留。

## 7. 验收标准

- 新建空间上传文件夹后：Web 左侧「项目文件目录」出现对应目录并可逐级展开；服务令牌 `/tree`、`/children` 对这些目录返回正常（非 404）。
- 既有「单文件上传 / 严格模式 / 重复文件」行为不回归。

## 8. 上线与存量数据

- 代码上线后**只影响新上传**。
- **存量文档**（上线前已上传、缺目录行）用回填脚本追溯补齐：见
  - 脚本：`openrag/scripts/backfill_directory_rows.py`
  - 执行说明（内网/外网）：`docs/2026-06-23-backfill-runbook.md`

## 9. 关于「方案一（前端推断兜底）」是否仍需要

做了方案二（放在 `ingest_new_file`）+ 回填后，实体目录行总会存在，Web 树根节点 `parent_path='/'` 能直接查到目录行并渲染——**所以方案二足以修复本问题，方案一并非必需**。

方案一（把 `Files.tsx` 根节点构建树时传给推断函数的清单从 `rootRaw` 改为全量快照）的价值是**防御性的、可选的**：

- 它修掉 `Files.tsx` 一个**既有不一致**：根节点喂 `rootRaw`（仅直接子项），深层展开却喂全量 `snap`。统一后根节点与各层行为一致。
- 它把「UI 正确性」与「是否已跑回填 / 是否所有写入路径都建了目录行」**解耦**：某环境忘跑回填、或将来出现绕过 `ingest_new_file` 直插 `files` 行的路径（裸 SQL 导入、旧库恢复、新连接器）时，UI 仍能从文件 `uri` 推断目录、优雅降级，而不是把列表里存在的文件对应目录藏起来。

结论：**以方案二为主即可闭环**；方案一作为 2 行级别的低成本兜底，建议后续顺手做，但不阻塞本次修复。
