# 右侧文件列表按所选目录过滤 —— 方案设计

- 日期：2026-06-24
- 分支：`feat/filelist-filter-by-directory`
- 状态：方案已定稿，待落地实现
- 范围语义决策：**递归（含子文件夹）** —— 与现有 `under_path` 拉取语义一致

## 需求

修改左侧目录树与右侧文件列表的联动：

1. 点击左侧**根目录**：右侧文件列表展示**所有文件**。
2. 点击左侧**某个文件夹**：右侧文件列表**仅展示该文件夹下的文件**（递归，含其所有子文件夹中的文件）。

## 一、问题（根因）

当前右侧列表"恒显示全部文件"，根因有两层：

### 1. `files` 状态是"只增不删"的累积合并

`web/src/pages/Files.tsx` 的 `mergeFilesById`（约 L255-262）用 `Map<id, File>` 合并，只新增/更新、从不移除。
首次加载工作区时（约 L368-392）会用 `under_path: '/'` 把**整个工作区的全部文件**一次性拉进 `files`。

### 2. 右侧 `displayedFiles` 没有按当前目录过滤

`displayedFiles`（约 L648-656）只做了"过滤掉目录行"，没有按 `selectedDirectory` 收窄：

```js
const displayedFiles = useMemo(() => {
  return files.filter((f) => {
    if (f.is_directory) return false;   // 只排除目录
    return true;                         // ← 缺：没有按 selectedDirectory 过滤
  });
}, [files]);                             // ← 缺：依赖里没有 selectedDirectory
```

因此：点目录确实会改 `selectedDirectory`、也会按 `under_path` 拉那一层数据并入 `files`
（`onSelectDirectory` 约 L506-513；effect 约 L394-401），**但右侧展示永远是 `files` 里累积的全部**，
看不出收窄效果。

### 附带问题

在左树里"展开"（而非选中）其它文件夹时，`handleTreeExpand`（约 L432-435）也会把那些文件并进
`files`，进一步污染右侧列表。

### 后端能力（无需改动）

后端 `openrag/src/openrag/api/files_api.py` 已具备路径过滤能力：

- `under_path`（`_is_under_path_uri`，L86-96）：**递归子树**过滤（含任意深度子孙）。
- `parent_path`（`_is_direct_child_uri`，L64-83）：**仅直接子级**过滤（一层）。

应用处见 L556-561。所以这是个**纯前端展示层**问题，不动后端。

## 二、修改方案

**核心思路：不动任何数据拉取逻辑，只让 `displayedFiles` 按 `selectedDirectory` 过滤。**
数据本就在 `files` 里，缺的只是展示时的收窄。

改动均在 `web/src/pages/Files.tsx`：

### 1. 新增路径判断 helper（镜像后端 `_is_under_path_uri`，复用已有的 `normalizeLogicalPath`）

```js
/** uri 是否落在 parentPath 之下（递归，含任意深度子孙）。根目录 '/' 视为包含全部。 */
function isUnderLogicalPath(uri, parentPath) {
  const p = normalizeLogicalPath(parentPath);
  const u = normalizeLogicalPath(uri);
  if (p === '/') return true;
  return u === p || u.startsWith(`${p}/`);
}
```

### 2. 修改 `displayedFiles`：加路径过滤，并把 `selectedDirectory` 加进依赖

```js
const displayedFiles = useMemo(() => {
  return files.filter((f) =>
    !f.is_directory && isUnderLogicalPath(f.uri, selectedDirectory)
  );
}, [files, selectedDirectory]);
```

- 选根目录 `/` → `isUnderLogicalPath` 恒为 true → 显示全部 ✓（需求 1）
- 选某文件夹 → 只显示该文件夹（及子孙）的文件 ✓（需求 2）

### 为什么不用"选中即重置 `files`"的方案

`files` / `filesRef` 还被左树的"隐式目录"推断逻辑用到（约 L434、L549-551）。重置会牵动左树，
风险更大。"展示层过滤"把改动隔离在右侧，最安全。

## 三、影响的文件

| 文件 | 改动 |
|---|---|
| `web/src/pages/Files.tsx` | **唯一生产代码改动**：新增 1 个 helper + 改 `displayedFiles` 的 `useMemo` |
| `web/src/pages/Files.test.tsx`（可选，新增） | 目前无 Files 页级测试，建议补 1 个"切目录→右侧收窄"的测试 |

**不需要改动**：后端 `files_api.py`（已支持）、`api.ts` / `types`（无新参数）、i18n（无新文案）、
`web/src/components/FileList.tsx`（仍只接收已过滤好的列表，零改动）。

## 四、对其它功能的影响（逐项核对）

- **搜索/筛选**：搜索本就按 `under_path: selectedDirectory` 收窄（约 L280-296），结果都落在当前目录内，
  新增的展示过滤对它是 no-op，**一致、不回归**。
- **全局刷新 / 上传 / 删除 / 重处理**：都走 `loadFiles()` 重新加载，过滤在展示层照常生效 ✓。
  上传默认上传到 `selectedDirectory`，刷新后正好出现在当前列表 ✓。
- **左树"展开其它目录"**：从"会污染右侧"变成"不污染"——**附带修复**，是改善不是回归。
- **切换工作区**：`handleWorkspaceChange` 已重置 `selectedDirectory='/'`（约 L479），切换后显示新工作区全部文件 ✓。
- **左树隐式目录推断**：用的是快照 / `filesRef`，不是 `displayedFiles`，**完全不受影响** ✓。
- **文件预览 / 跳转分块**：基于单条记录，无关 ✓。
- **既有小 quirk（非本次引入）**：antd Tree 默认"再次点击已选目录会取消选中"，此时 `selectedKeys` 为空、
  `onSelectDirectory` 忽略、`selectedDirectory` 不变 → 列表保持不变但高亮消失。可后续顺手把目录设成
  不可取消选中，但不在本次范围内。

**结论**：改动面极小、隔离在右侧展示层，且顺带修掉"展开即污染"的次要 bug，无后端/接口/其它页面影响。
