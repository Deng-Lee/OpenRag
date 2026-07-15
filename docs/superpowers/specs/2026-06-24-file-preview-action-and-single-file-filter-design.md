# 文件预览入口 + 单文件列表收窄 — 设计文档

> 历史设计提示：本文保留 2026-06-24 的设计上下文。文中 per-file `FilePermission` ACL 已由 [R-02 文件级 ACL 下线方案](../../R-02文件级ACL下线方案.md) 决策下线，不再是当前能力或未来扩展基础；当前文件访问边界为工作空间 `read` / `write` 权限。

- 日期：2026-06-24
- 状态：设计待评审（Design / pending review）
- 范围：前端 `web/` 文件管理页两处交互改动；后端零改动

## 1. 背景与目标

两个独立的前端改动：

1. **预览入口**：当前点击文件名时，已解析（`simple_status === 'done'`）的文件会跳转到切块视图，没有"看原文"的入口；只有未解析/失败的文件点名才弹出 `FilePreviewModal`。希望在文件列表的**操作列新增一个「预览」按钮**，点击即对**任意解析状态**的文件拉起预览弹窗，补上已解析文件无预览入口的缺口。
2. **单文件收窄**：左侧目录树里点击单个**文件**节点目前**无反应**；希望选中某个文件时，右侧列表**只显示该文件一行**。

## 2. 已确认的决策

| 决策点 | 选择 |
|---|---|
| 预览入口形态 | 操作列新增显式「预览」按钮（**非** hover 浮层，**非**复用名字点击） |
| 预览可见范围 | **所有用户可见**（含只读 `canWrite=false`）；预览是只读操作 |
| 操作列渲染条件 | 从"仅 `canWrite` 渲染"改为**始终渲染**（右侧列表恒为文件、不含目录，每行至少有「预览」） |
| 下载按钮权限（路线 B） | 预览弹窗内的**下载按钮对只读用户隐藏**——明确定位为**界面/策略对齐与威慑**，**非访问控制边界**（见 §6） |
| 选中文件时右侧显示 | **只显示被选中的那一个文件**；再选目录恢复按目录过滤 |
| 后端 | **不改动**（read 权限语义、`/content`、`/preview` 全部维持现状） |

## 3. 关键现状（代码事实）

- 操作列当前仅 `canWrite` 才渲染，内含「重新解析」+「删除」
  （[FileList.tsx:208-236](../../../web/src/components/FileList.tsx)）。
- `FilePreviewModal` 已作为独立组件挂在 FileList 内，由 `previewFile` 状态驱动
  （[FileList.tsx:302-306](../../../web/src/components/FileList.tsx)；状态 `previewFile`/`setPreviewFile` 在第 38 行）。
- 预览弹窗**渲染前必然 `fetchContentBlob(file.id)` 把整份文件字节拉进浏览器**
  （[FilePreviewModal.tsx:102/110/120](../../../web/src/components/FilePreviewModal.tsx)）；下载按钮只是另存这份已在内存的 blob
  （按钮 [189-193](../../../web/src/components/FilePreviewModal.tsx)，`handleDownload` [171-180](../../../web/src/components/FilePreviewModal.tsx)）。`FilePreviewModal` 当前**无** `canWrite` 概念。
- 后端 `/content`（下载与 PDF/图片渲染共用）只校验 **read** 权限
  （`_get_readable_file_or_404` [files_api.py:591](../../../openrag/src/openrag/api/files_api.py)，`get_file_content` [599-608](../../../openrag/src/openrag/api/files_api.py)）。
- 左侧树：目录节点 `key=<uri>`，文件节点 `key="file-"+id`（[Files.tsx:138](../../../web/src/pages/Files.tsx)）。
- `onSelectDirectory` 用 `if (!key.startsWith('file-'))` **显式忽略文件节点**——这是"点文件无反应"的根因（[Files.tsx:520-527](../../../web/src/pages/Files.tsx)）。
- 右侧 `displayedFiles = selectDisplayedFiles(files, selectedDirectory)`（[Files.tsx:663-666](../../../web/src/pages/Files.tsx)），`selectDisplayedFiles` 过滤掉目录并按目录前缀收窄（[88-90](../../../web/src/pages/Files.tsx)）——**右侧列表从不含目录行**。
- 切换工作区 `handleWorkspaceChange` 会 `setSelectedDirectory('/')`（[Files.tsx:492-496](../../../web/src/pages/Files.tsx)）。

## 4. 方案设计

### 改动 1：操作列「预览」按钮（`FileList.tsx`）

- 操作列改为**始终渲染**（不再被 `...(canWrite ? [...] : [])` 包裹）；列宽 `120 → 160`，容纳最多三个按钮。
- 单行按钮分档：
  - 「预览」`EyeOutlined`：所有用户可见，仅 `!record.is_directory`，`onClick={() => setPreviewFile(record)}`。**图标按钮**（`type="link"`，`title={t('files.actions.preview')}` 作 tooltip），与现有「重新解析」按钮风格一致。复用现成 `FilePreviewModal`。
  - 「重新解析」：`canWrite && !record.is_directory`（维持现状，图标按钮）。
  - 「删除」：`canWrite`（维持现状，图标+文字 + Popconfirm）。
- `FilePreviewModal` 新增可选 prop `canWrite?: boolean`；下载按钮（[189-193](../../../web/src/components/FilePreviewModal.tsx)）仅在 `canWrite` 为真时渲染。FileList 渲染时传 `canWrite={canWrite}`（[FileList.tsx:302-306](../../../web/src/components/FileList.tsx)）。
- 文件名点击行为**完全不变**——新按钮是额外的显式入口。
- i18n：新增 `files.actions.preview`（`zh.json`/`en.json` 各一条："预览"/"Preview"）。

### 改动 2：选中单文件收窄右侧列表（`Files.tsx`）

- 新增状态 `selectedFileId: number | null`（初始 `null`）。
- 扩展 `onSelectDirectory`：
  - key 以 `file-` 开头 → 解析出数字 id，`setSelectedFileId(id)`（不动 `selectedDirectory`）。
  - 否则（目录）→ `setSelectedDirectory(key)` 且 `setSelectedFileId(null)`。
- `displayedFiles` 派生：`selectedFileId != null` 时取 `files.filter(f => f.id === selectedFileId)`；否则维持 `selectDisplayedFiles(files, selectedDirectory)`。`useMemo` 依赖追加 `selectedFileId`。
- `handleWorkspaceChange` 追加 `setSelectedFileId(null)`，避免跨工作区残留。
- 树选中高亮由 antd 自管，无需额外处理。

## 5. 边界与不做（Non-goals）

- 被选文件必在 `files` 中（其树节点来自"根加载/父目录展开"时一并并入 `files`），故可定位到。
- 已知小边界：顶部搜索过滤生效、且被选文件不在过滤结果内时，单文件视图为空（它本就不匹配过滤）——**不特殊处理**。
- 目录不可预览（且右侧列表本就不含目录）。
- **不**改名字点击逻辑、**不**改后端、**不**新增下载权限模型（见 §6 路线 C，属独立任务）。

## 6. 关于"下载权限"的说明（重要）

隐藏下载按钮（路线 B）**不是访问控制边界**：只读用户能打开预览即已在浏览器内持有整份字节，可经 DevTools 或直接调 `GET /files/{id}/content`（只需 read 权限）取得文件。B 的价值是**界面/策略对齐与威慑**，并与仓库内既有的无下载预览面（`EmbeddedDocumentPreview`、`document-source-preview`）一致。

已评估**路线 C**（真正管控下载），拆为三种：

- **C-1** 仅给"下载"加权限：因 `/content` 必须对 reader 开放（预览要用），reader 仍可直接取字节，**实际等价于 B**（陷阱，不选）。
- **C-2** 真·只读视图：`/content`（原始字节）锁到 write/download，另建**服务端派生预览**端点供 reader 用（PDF 逐页栅格化成图、Office 抽取 HTML/文本、图片降清/水印）。是唯一**既挡住下载又保留只读预览**的方案，但属**独立后端工程**（服务端渲染 + 缓存 + 前端预览重构），残留风险为截图。
- **C-3** 把预览也提权到 write：`/content`+`/preview` 改 write、前端隐藏预览入口。便宜，但**取消了本功能对只读用户的意义**。

权限模型现为 `read < write < admin` 三级（无独立 download 能力）；另有 per-file `FilePermission` ACL 模型可承载未来的按文件下载授权。

**决策（2026-06-24，发起人确认）：本次采用 B。** 若后续硬性要求"只读用户绝不能取走文件副本"，另起 **C-2** 独立任务，并届时重新评估只读用户的预览形态。本设计**不含** C。

## 7. 测试

- `FileList.test.tsx`（已 mock `FilePreviewModal` 与 `fetchContentBlob`）：
  - 操作列出现「预览」按钮，点击触发预览（`previewFile` 被设置）。
  - 只读用户（`canWrite=false`）：有「预览」、无「重新解析」/「删除」；下载按钮不渲染。
- `Files.test.tsx`（已测 `selectDisplayedFiles`）：
  - 选中文件节点 → `displayedFiles` 仅含该文件；随后选目录 → 恢复按目录过滤。

## 8. 影响文件清单

- `web/src/components/FileList.tsx`（操作列 + 传 `canWrite`）
- `web/src/components/FilePreviewModal.tsx`（新增 `canWrite` prop，按权限隐藏下载按钮）
- `web/src/pages/Files.tsx`（`selectedFileId` 状态 + 选中逻辑 + 派生 + 切换工作区重置）
- `web/src/i18n/locales/zh.json`、`web/src/i18n/locales/en.json`（`files.actions.preview`）
- 相应测试文件
