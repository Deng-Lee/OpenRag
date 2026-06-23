# OpenRag 文件夹上传失败诊断结论与完整修复方案

> 状态：诊断已通过阅读源码核实；下文「完整修复方案」为可直接落地的实施方案。
> 核实日期：2026-06-22。
> 修订：已根据「用户指示」+「Codex 审核 / 复审 / 三审 / 收口」多轮意见调整并**收口定稿**，详见「实现基线（收口共识）」与文末三张「修订说明」表。

## 问题

用户在 OpenRag 文件页面直接拖拽文件夹上传时，页面提示「文件上传失败」。

需要确认：

- 代码中是否本来就没有实现文件夹上传功能；
- 是否只是前端改一个传参就可以支持文件夹上传。

## 结论

当前代码中没有完整实现「文件夹上传」功能。

这不是单纯把前端某个参数改一下就能完整解决的问题。Ant Design / rc-upload 上传库本身支持目录上传能力，但 OpenRag 业务代码没有启用，也没有适配目录上传后的相对路径计算、批量上传、并发控制、进度与结果汇总。

更准确地说：

- 当前前端上传组件只按单文件上传实现（`multiple: false`，无 `directory`，不读取 `webkitRelativePath`）。
- 当前后端 `/files/upload` 只接收单个 `UploadFile` 和一个目标父目录 `path`。
- **好消息：现有后端接口已足够支撑文件夹上传的最小可行修复，无需新增后端接口。** 关键在于后端上传时 `require_parent_dir=False`（不强制父目录存在、也不报错），且 Files 页目录树是「按文件 URI 前缀推断」。因此前端只要逐个把目录内文件以正确的 `uri` 上传，**当前 Files 页即可展示这些非空目录层级**。

> **范围限定（依据 Codex 审核第 5 点）**：URI 前缀推断只保证「**非空**目录层级在当前 Files 页可见」，并不等于系统里创建了**正式目录记录**。结合「空目录直接拒绝」的产品决策（见下），本方案**不需要**为目录新增数据模型或建目录记录。

因此，**最小可行修复完全可以放在前端**：在**现有上传区**自动识别拖入的是文件还是文件夹、遍历文件夹、按相对路径拆分父目录、带并发与限流地逐个上传、最后汇总成功/跳过/失败数量。这属于补齐目录上传业务逻辑，不是只改一个上传参数。

---

## 核心交互与产品决策

> 用户要求：**不新增上传入口**，沿用截图中的现有上传区；**检测到文件夹时自动走文件夹上传链路**。

- **单一入口**：沿用现有上传区（antd `Dragger`），**不新增**「上传文件夹」按钮 / 独立 input。
- **拖拽自动识别（核心 / MVP 必做）**：drop 时检测拖入项是否含目录——含目录 → 文件夹链路；否则 → 现有单文件链路。这正是用户最初失败的场景（Codex 审核第 1 点）。
- **不使用 `directory: true`**（Codex 审核第 2 点）：它会给隐藏 `input` 加 `webkitdirectory`，使「点击选单文件」退化为「只能选目录」。改为**自管 drop 事件**识别目录，不动 input。
- **点击行为不变**：仍为单文件选择（浏览器限制：同一 input 单次点击无法既选文件又选目录）。用户反馈的失败是「**拖拽**文件夹」，drop 自动识别已覆盖。
- **空文件夹直接拒绝**（Codex 复审第 3 点，已确认产品决策）：不保留、不建目录记录。递归后无任何文件 → 提示「空文件夹不支持上传」；有文件但过滤后无可上传项 → 提示「没有可上传的文件」。
- **多个散文件（非目录）多选拖入：不支持**（用户确认的产品决策；Codex 复审第 2 点）。维持现状 `multiple: false`——本方案**只**新增「文件夹」链路，不改散文件行为，也**不**将其列为后续增强。验收/测试据此收窄为「单文件行为不变」。注：现状下多选散文件 drop 仅上传第一个（rc-upload 既有行为，不在本次改动范围）。

---

## 实现基线（收口共识）

> 依「Codex 收口结论」：聚焦必要能力；非必要 / 极小概率项放到编码联调时确认，不再作为文档阻塞项继续扩展。

**必须实现（MVP 范围）：**

1. 拖拽文件夹可用：现有上传区 capture 识别目录 → 递归 → 批量上传，目录层级随 `uri` 落库后在 Files 页可见。
2. 现有单文件点击 / 拖拽行为不变。
3. 空文件夹直接拒绝（含「无可上传项」提示）。
4. 上传中防重入：同步 `uploadingRef` 锁。
5. 结果 `uploaded / skipped / failed` 三态统计。
6. 隐藏 / 系统路径 + 不支持类型在前端按**路径段**过滤。
7. `runFolderUpload` 与可变状态走 **ref** 取最新闭包。
8. 重复文件精确匹配 `already exists` → 计入「跳过」。

**非阻塞项（编码 / 联调时顺手确认，不再扩展为文档阻塞项）：**

- 极端旧浏览器（无 `webkitGetAsEntry`）：回退既有行为，不做启发式拦截。
- 部署网关上传大小限制（可能 < 100MB）：以网关 / API 的 413 为准，前端把 413 映射成明确提示。

---

## 诊断补充（已逐项核实的代码证据）

下列结论均已对照源码核实，并修正了初版诊断中遗漏或不准确之处。

### 1. 前端上传组件只支持单文件

`web/src/components/FileUpload.tsx`（约 134–158 行）：

```ts
const props: UploadProps = {
  name: 'file',
  multiple: false,           // ← 仅单文件
  disabled: uploading,
  customRequest: async ({ file, onSuccess, onError }) => {
    setUploading(true);
    try {
      await filesAPI.upload(file as globalThis.File, parserType, workspaceId, uploadPath, documentType);
      message.success('文件上传成功');
      ...
    } catch (error) {
      const errorMsg = err.response?.data?.detail || '文件上传失败';  // ← 笼统报错来源
      message.error(errorMsg);
    }
  },
  showUploadList: false,
};
```

- 没有 `directory: true`，不会递归目录；
- 不读取 `webkitRelativePath`，无法还原目录结构；
- 「文件上传失败」即此处 `catch` 的兜底文案。拖拽文件夹时，浏览器把目录当作一个无内容/异常的「文件」交给上传组件，后端处理失败或前端取内容失败，于是弹出该提示。
- 另：`if (!canWrite) { return null; }`（160–162 行）——无写权限时整个组件不渲染，因此上传区/拖拽区天然不存在（与下文「权限校验」相关）。

### 2. 前端 API 封装：每次只提交一个 file + 一个统一 path

`web/src/services/api.ts`（`filesAPI.upload`，约 103–118 行）：

```ts
const formData = new FormData();
formData.append('file', file);
formData.append('parser_type', parserType);
formData.append('workspace_id', workspaceId.toString());
formData.append('path', path);            // 单一目标目录
formData.append('document_type', documentType);
await api.post('/files/upload', formData);
```

`filesAPI` 中**已存在** `createDirectory(path, workspaceId)`（约 179–185 行，`POST /files/directories`）——但按「空目录直接拒绝」的决策，本方案不需要它。

### 3. 后端上传接口是单文件接口，但对父目录很宽容

`openrag/src/openrag/api/files_api.py`（`upload_file`，约 348–415 行）调用：

```py
file_record, task_record = ingest_new_file(
    db, workspace, current_user.id,
    parent_logical_path=path,
    upload_filename=file.filename or "unnamed",
    file_content=file_content,
    ...
    require_parent_dir=False,                       # ← 关键：不要求父目录已存在
    duplicate_status_code=status.HTTP_400_BAD_REQUEST,
)
```

`openrag/src/openrag/services/file_ingest.py`（`ingest_new_file`，约 338–532 行）核实要点：

- `require_parent_dir=False` 时**跳过**父目录存在性校验（414–415 行），也**不会**自动创建父目录行；
- 文件落库时只写 `uri=/父目录/.../文件名`，**不设置 `parent_id`**（481–491 行）；
- 重复文件（同 `uri` 已存在）抛 **400**「File already exists」（420–442 行）；
- 超过 `MAX_FILE_SIZE = 100MB` 抛 **413**（23 行、396–412 行）；
- MIME 不在 `ALLOWED_MIME_TYPES` 时：**文件照样落库存储，只是不创建解析任务**（497–508 行）——即不会报错，但也不会被处理。

### 4. 目录树按 URI 前缀推断，不依赖 parent_id（仅限非空目录）

- 后端 `list_files` 用 `_is_direct_child_uri` / `_is_under_path_uri` 按 **URI 前缀**过滤（`files_api.py` 约 556–561 行）；
- 前端 `buildDirectoryOnlyTree`（`FileUpload.tsx` 30–84 行）和 `Files.tsx` 目录树都从文件 `uri` 拆分推断中间目录。

**结论（限定）：只要文件以正确的 `uri` 落库，其所在的「非空」目录层级即可在当前 Files 页正确显示，无需预先创建目录记录。** 空目录因产品决策被直接拒绝，不在考虑范围。

### 5. 已有可直接借鉴的参考实现（同款算法）

`openrag/scripts/bulk_import_folder.py` 已经实现了「遍历本地目录树 → 计算每个文件的远端父目录 → 并发调用 `/files/upload`」的完整逻辑，前端只需把它的 `_parent_logical_path()` 思路用 TS + `webkitRelativePath` 复刻：

```py
def _parent_logical_path(remote_prefix: str, relative_file: Path) -> str:
    prefix = _normalize_remote_prefix(remote_prefix)   # 前导 /，去尾部 /
    parts = relative_file.parent.as_posix()            # 相对父目录
    if parts in (".", ""):
        return prefix if prefix != "/" else "/"
    child = parts.lstrip("/")
    if prefix == "/":
        return "/" + child
    return f"{prefix}/{child}"
```

该脚本还用 `asyncio.Semaphore(workers=4)` 限流、用 `skip_hidden` 跳过隐藏文件、并最终打印「成功/失败/合计」——这些都是前端方案要照搬的要点。

> 小坑：脚本里 `MAX_FILE_SIZE = 50MB` 是过时的本地预检值；**后端真实上限是 100MB**（以 `file_ingest.py:23` 为准）。前端预检请用 100MB。

---

## 完整修复方案

### 总览

- **范围**：以**前端为主**补齐文件夹上传；后端**无需改动**即可上线 MVP。
- **单一入口、拖拽自动识别**：复用现有上传区，drop 时识别文件/文件夹并自动分流。
- **不破坏现有单文件上传**：单文件点击 / 拖拽路径保持原样；仅当 drop 中**含目录**时接管。
- **核心数据流**：drop **含目录才接管** → 前置校验（非上传中 / 有 workspace）→ 递归读取目录得 `{file, relativePath}[]` → 空/无可上传项则提示并中止 → 客户端过滤与预检 → 按 `relativePath` 算远端父目录 → 并发限流逐个 `filesAPI.upload` → 三态汇总并刷新列表。

### 改动文件清单

| 文件 | 改动 |
| --- | --- |
| `web/src/components/FileUpload.tsx` | 引入 `useRef`；在**现有 Dragger** 外层挂 capture 阶段 drop 拦截（识别目录→接管）；`runFolderUpload` 与状态走 ref；防重入用同步 ref 锁 + workspace 校验；新增批量上传器与进度/汇总 UI。**不新增入口、不加 `directory:true`** |
| `web/src/utils/folderUpload.ts`（新增） | 纯函数：路径归一/计算、垃圾文件过滤、目录递归读取（便于单测） |
| `web/src/services/api.ts` | 复用现有 `filesAPI.upload`（无需改） |
| `web/src/i18n/locales/zh.json`、`en.json` | 新增 `files.upload.*` 文案（拖拽提示、进度、汇总、跳过原因、各类拦截提示） |
| 后端 | **不改**；增强项见「可选后端增强」 |

### 前端实现步骤

#### 步骤 1：现有上传区「拖拽目录」自动识别（capture 拦截 + ref 取最新状态 + 前置校验）

要点：**不动 input、不加 `directory:true`**；在包裹 `Dragger` 的容器上以 **capture 阶段**监听原生 `drop`，先于 rc-upload 执行。**capture 拦截绕过了 antd 的 `disabled`**，必须自管：①防重入用**同步 ref 锁**（三审 2）；②`runFolderUpload` 与可变状态都经 **ref 渲染期赋值**取最新闭包，避免被长生命周期监听器持有旧值（三审 1）；③无 `webkitGetAsEntry` 时**不**用启发式拦截正常文件，直接回退既有链路（三审 3）。

```tsx
// 1) 用 ref 持有「最新」值/函数，避免被长生命周期的原生 drop 监听器闭包到旧值（三审 1）
const uploadPathRef = useRef(uploadPath);
const workspaceIdRef = useRef(workspaceId);
const documentTypeRef = useRef(documentType);
const uploadingRef = useRef(false);                       // 重入锁：由 runFolderUpload 同步读写（三审 2）
const runFolderUploadRef = useRef<(items: PickedFile[]) => Promise<void>>();
// 渲染期同步赋值——事件触发时一定拿到最新闭包
uploadPathRef.current = uploadPath;
workspaceIdRef.current = workspaceId;
documentTypeRef.current = documentType;
runFolderUploadRef.current = runFolderUpload;

// 2) capture 阶段 drop 拦截（只挂一次）
const dropZoneRef = useRef<HTMLDivElement>(null);
useEffect(() => {
  const el = dropZoneRef.current;
  if (!el) return;
  const onDrop = (e: DragEvent) => {
    const items = Array.from(e.dataTransfer?.items ?? []);
    const entries = items.map((it) => (it as any).webkitGetAsEntry?.() ?? null).filter(Boolean) as any[];
    const hasDirectory = entries.some((en) => en?.isDirectory);
    if (!hasDirectory) return;   // 纯文件 / 旧浏览器无法识别目录 → 交给现有单文件链路（三审 3，不做启发式）

    e.preventDefault();
    e.stopPropagation();         // capture 阻断，rc-upload 收不到本次 drop
    if (uploadingRef.current) { message.warning(t('files.upload.uploading_busy')); return; }   // 防重入
    if (!workspaceIdRef.current) { message.warning(t('files.upload.no_workspace')); return; }

    void (async () => {
      const picked: PickedFile[] = [];
      for (const en of entries) await walkEntry(en, '', picked);
      await runFolderUploadRef.current?.(picked);          // 调用最新闭包
    })();
  };
  el.addEventListener('drop', onDrop, { capture: true });
  return () => el.removeEventListener('drop', onDrop, { capture: true } as any);
}, [t]);

// 3) JSX：容器包住现有 Dragger（视觉与点击单文件行为完全不变）
<div ref={dropZoneRef}>
  <Dragger {...props}>...</Dragger>
</div>
```

> `canWrite` 无需在此再判：组件在 `!canWrite` 时已 `return null`，dropzone 根本不渲染。
> 备选实现：若 capture 拦截在某些环境不稳，可改为自定义同样式 drop 区，或接管 `Dragger` 的 `onDrop` 并把 `customRequest` 置为 no-op。capture 方案改动最小，优先。

#### 步骤 2：递归读取拖入的目录（drop 无 webkitRelativePath，需自拼相对路径）

放入 `web/src/utils/folderUpload.ts`。注意 `readEntries` **单次最多约 100 项，必须循环读到空**，否则大目录丢文件：

```ts
export interface PickedFile { file: File; relativePath: string; }

export function walkEntry(entry: any, prefix: string, out: PickedFile[]): Promise<void> {
  return new Promise((resolve) => {
    if (entry?.isFile) {
      entry.file(
        (file: File) => { out.push({ file, relativePath: prefix + entry.name }); resolve(); },
        () => resolve(),
      );
    } else if (entry?.isDirectory) {
      const reader = entry.createReader();
      const children: any[] = [];
      const readBatch = () => {
        reader.readEntries(async (batch: any[]) => {
          if (batch.length === 0) {
            for (const child of children) await walkEntry(child, `${prefix}${entry.name}/`, out);
            resolve();
          } else {
            children.push(...batch);
            readBatch();                       // 关键：继续读，直到返回空
          }
        }, () => resolve());
      };
      readBatch();
    } else {
      resolve();
    }
  });
}
```

#### 步骤 3：路径计算（复刻 `_parent_logical_path`，纯函数可单测）

```ts
export function normalizeDir(p: string): string {
  const s = (p || '/').trim().replace(/\\/g, '/');
  if (!s || s === '/') return '/';
  const withLead = s.startsWith('/') ? s : '/' + s;
  return withLead.replace(/\/+$/, '') || '/';
}

/** base=选中的上传目标目录；relativePath=相对路径（含文件名，顶层为被拖文件夹名） */
export function remoteParentDir(base: string, relativePath: string): string {
  const b = normalizeDir(base);
  const relParts = relativePath.split('/').filter(Boolean);
  relParts.pop();                          // 去掉文件名，余下为相对父目录
  const child = relParts.join('/');
  if (!child) return b;
  return b === '/' ? `/${child}` : `${b}/${child}`;
}
```

示例：目标 `/personal`，拖入 `报告/` 内含 `报告/2024/Q1.pdf` → 远端父目录 `/personal/报告/2024`，最终 `uri=/personal/报告/2024/Q1.pdf`（**被拖文件夹名作为顶层保留**）。

#### 步骤 4：客户端过滤与预检（按「路径段」过滤，Codex 审核第 3 点）

```ts
const MAX_FILE_SIZE = 100 * 1024 * 1024;   // 与后端 file_ingest.py:23 一致
const SUPPORTED_EXT = new Set([
  'pdf','docx','doc','xlsx','xls','pptx','ppt','txt','md','markdown','html','htm','json','csv','epub',
]);
const SKIP_DIR_SEGMENTS = new Set(['__MACOSX', '.git', '.svn', '.hg', 'node_modules']);
const SKIP_FILE_NAMES = new Set(['.DS_Store', 'Thumbs.db', 'desktop.ini']);

export function isJunkPath(relativePath: string): boolean {
  const segs = relativePath.split('/').filter(Boolean);
  const name = segs[segs.length - 1] ?? '';
  for (let i = 0; i < segs.length - 1; i++) {          // 逐个「目录段」检查
    if (segs[i].startsWith('.') || SKIP_DIR_SEGMENTS.has(segs[i])) return true;
  }
  return name.startsWith('.') || SKIP_FILE_NAMES.has(name);
}

export interface Skipped { rel: string; reason: string; }
export function precheck(items: PickedFile[]) {
  const accepted: PickedFile[] = [];
  const skipped: Skipped[] = [];
  for (const it of items) {
    const rel = it.relativePath;
    const name = rel.split('/').pop() || it.file.name;
    if (isJunkPath(rel)) { skipped.push({ rel, reason: '隐藏/系统文件，已跳过' }); continue; }
    const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : '';
    if (!SUPPORTED_EXT.has(ext)) { skipped.push({ rel, reason: `不支持的类型 .${ext}` }); continue; }
    if (it.file.size > MAX_FILE_SIZE) { skipped.push({ rel, reason: '超过 100MB' }); continue; }
    accepted.push(it);
  }
  return { accepted, skipped };
}
```

#### 步骤 5：批量上传器（防重入 + 空目录拒绝 + 三态统计）

```ts
export type ItemStatus = 'uploaded' | 'skipped' | 'failed';
export interface ItemResult { rel: string; status: ItemStatus; reason?: string }

async function runFolderUpload(items: PickedFile[]) {
  if (uploadingRef.current) return;            // 读锁
  uploadingRef.current = true;                 // 同步占锁——关闭 setUploading 异步带来的防重入窗口（三审 2）
  try {
    if (items.length === 0) { message.info(t('files.upload.empty_folder')); return; }   // 空文件夹拒绝
    const { accepted, skipped } = precheck(items);
    if (accepted.length === 0) {                                                         // 有文件但全被过滤
      message.info(t('files.upload.no_uploadable'));
      showSummary({ uploaded: 0, skipped: skipped.length, failed: 0 },
                  skipped.map((s) => ({ rel: s.rel, status: 'skipped' as const, reason: s.reason })));
      return;
    }

    const results: ItemResult[] = skipped.map((s) => ({ rel: s.rel, status: 'skipped', reason: s.reason }));
    const total = accepted.length;
    let done = 0, cursor = 0;
    setUploading(true);                          // 仅 UI 态；真正的锁是 uploadingRef
    setProgress({ total, done });

    const CONCURRENCY = 5;
    async function worker() {
      while (cursor < accepted.length) {
        const it = accepted[cursor++];
        const path = remoteParentDir(uploadPathRef.current, it.relativePath);
        try {
          await filesAPI.upload(it.file, 'auto', workspaceIdRef.current!, path, documentTypeRef.current);
          results.push({ rel: it.relativePath, status: 'uploaded' });
        } catch (err: any) {
          const code = err?.response?.status;
          const detail = String(err?.response?.data?.detail ?? '');
          const isDup = code === 400 && /already exists/i.test(detail);   // 精确匹配，勿用过宽 /exist/i（三审 4）
          results.push(isDup
            ? { rel: it.relativePath, status: 'skipped', reason: '已存在，已跳过' }
            : { rel: it.relativePath, status: 'failed', reason: detail || `失败(${code ?? '网络错误'})` });
        } finally {
          done++; setProgress({ total, done });
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, total) }, worker));

    const counts = {
      uploaded: results.filter((r) => r.status === 'uploaded').length,
      skipped:  results.filter((r) => r.status === 'skipped').length,
      failed:   results.filter((r) => r.status === 'failed').length,
    };
    showSummary(counts, results);   // 一条汇总 + 可展开明细
    onUploadSuccess();              // 刷新父组件列表
  } finally {
    uploadingRef.current = false;                // 同步释放
    setUploading(false);
  }
}
```

#### 步骤 6：进度与结果汇总 UI（取代逐文件 `message` 刷屏）

- 上传中：`Progress`（`done/total`）+ 当前文件名；
- 结束：**一条**汇总「成功 X，跳过 Y，失败 Z」（三态独立计数）；
- 失败/跳过明细：可折叠列表或 `Modal`，逐项 `rel` + 原因；
- 不再逐文件弹 `message`。

### 边界与坑（务必覆盖）

1. **drop 接管边界**：仅当拖入项**含目录**时接管；纯单文件 drop / 点击保持原有 rc-upload 行为。
2. **防重入用同步 ref 锁**（三审 2）：capture 绕过 antd `disabled`；`runFolderUpload` 起始同步 `uploadingRef.current = true`、`finally` 置回 `false`，**不**依赖 `useEffect` 同步 `uploading`（否则存在 set→effect 之间的重入窗口）。
3. **闭包新鲜度**（三审 1）：listener 只挂一次；`uploadPath/workspaceId/documentType` 及 **`runFolderUpload` 本身**都在渲染期写入 ref 取最新，避免被原生监听器长期持有旧闭包。
4. **workspace 校验**：drop 时校验 `workspaceIdRef`；`canWrite` 由组件 `!canWrite` 时 `return null` 天然拦截。
5. **空目录直接拒绝**：递归后无文件 → 「空文件夹不支持上传」；过滤后无可上传项 → 「没有可上传的文件」。
6. **旧浏览器不做启发式**（三审 3）：无 `webkitGetAsEntry` 时**不**用 `type/size` 启发式（会误伤正常文件）；此时拖文件夹回退既有单文件行为（极小概率、低优先级，按收口在联调确认）。主流浏览器均支持该 API。
7. **capture 阻断**：必须 `stopPropagation`，否则 rc-upload 仍处理该 drop。
8. **readEntries 分批**：单次≤100，循环读到空。
9. **drop 无 webkitRelativePath**：遍历时自拼 `relativePath`。
10. **垃圾文件按路径段过滤**（审核 3）。
11. **三态统计**（审核 4）：`uploaded/skipped/failed` 分开，别把失败算进跳过。
12. **重复=400 精确匹配**（三审 4）：用 `/already exists/i`，勿用过宽 `/exist/i`（会误伤 "does not exist" 类错误）；其它 400 记 failed。
13. **不支持类型「静默落库」→ 客户端过滤**。
14. **路径安全**：不含 `..`；后端 `validate_path` 兜底。
15. **并发上限**（建议 5），勿一次性 `Promise.all` 几百个。
16. **大文件夹**：进度 + 可取消（`AbortController`，增强）。
17. **多个散文件多选 drop：不支持（产品决策）**。维持现状，仅上传第一个；本次不新增散文件批量，也不列为增强。
18. **部署上传大小限制**（三审 5，非阻塞）：前端 100MB 仅粗筛；入口网关可能更小（曾见 ingress `proxy-body-size: 20m`、`MAX_UPLOAD_SIZE=20971520`）。以网关/API 返回的 413 为准，把 413 映射成「文件过大/被网关拒绝」明确提示；实际上限联调确认。

### i18n 文案新增（`files.upload.*`）

`zh.json` 示例（`en.json` 对应补英文）：

```jsonc
"upload": {
  "drag_hint": "点击上传单个文件，或将文件/文件夹拖拽到此处（文件夹会保持目录结构）",
  "progress": "正在上传 {{done}}/{{total}}",
  "summary": "上传完成：成功 {{uploaded}}，跳过 {{skipped}}，失败 {{failed}}",
  "detail_title": "上传明细",
  "uploading_busy": "正在上传中，请等待当前任务完成",
  "no_workspace": "请先选择工作区",
  "empty_folder": "空文件夹不支持上传",
  "no_uploadable": "没有可上传的文件（已全部过滤）",
  "skip_junk": "隐藏/系统文件，已跳过",
  "skip_unsupported": "不支持的类型，已跳过",
  "skip_too_large": "超过 100MB，已跳过",
  "skip_duplicate": "已存在，已跳过"
}
```

> 现有上传区提示（`FileUpload.tsx` 220–221 行「点击或拖拽文件到此处上传」）建议改引用 `drag_hint`，明确支持文件夹。

### 可选后端增强（非 MVP 必需，按需排期）

1. ~~保留空目录 / 建目录记录~~ **已取消**：按产品决策，空文件夹直接拒绝上传，因此**无需**为目录引入数据模型、也**无需**调用 `createDirectory` / 新增 mkdir-p 接口。非空目录层级由 URI 推断展示即可。
2. **批量上传端点**：新增 `POST /files/upload-batch`（multipart 多文件 + 每文件相对路径），后端循环调用 `ingest_new_file`，一次请求返回每文件结果。收益：减少前端往返、统一事务与限流。属性能/体验优化，非第一步必需。
3. **统一上限常量**：把 `bulk_import_folder.py` 里过时的 `MAX_FILE_SIZE=50MB` 对齐到 100MB。

### 测试方案

**单元测试（前端，纯函数优先）**

- `normalizeDir` / `remoteParentDir`：根目录、多层、Windows 反斜杠、首尾斜杠、文件直接在所选文件夹根下。
- `isJunkPath`：`.git/config`、`__MACOSX/a.pdf`、`sub/.hidden`、`Thumbs.db`、正常文件。
- `precheck`：隐藏/系统路径、超大、不支持扩展名、混合大小写、三态分类正确、`accepted` 为空的两种场景。
- （可选）`walkEntry`：mock entry 验证递归与 `readEntries` 分批。

**手动 / 集成测试**

1. 拖含子目录文件夹（目标 `/`）→ 自动文件夹链路，层级与 `uri` 正确。
2. 拖 / 点击**单个**文件 → 原有单文件链路不变（多个散文件：现状只传第一个，本次不改）。
3. **上传进行中再拖入文件夹** → 被忽略并提示「正在上传中」。
4. **拖入空文件夹** → 提示「空文件夹不支持上传」。
5. 文件夹内全为被过滤项（`.DS_Store`/`.png` 等）→ 提示「没有可上传的文件」。
6. 目标 `/docs` 重复拖同一文件夹 → 全「跳过（已存在）」，`failed=0`。
7. 混入 `.DS_Store`/`__MACOSX/x.pdf`/`.png`/>100MB → 各归「跳过」，明细给原因。
8. 上千小文件 → 进度平滑、并发受限、三态汇总正确、无 `message` 刷屏。
9. 未选工作区 → 提示「请先选择工作区」；无写权限 → 上传区不渲染。
10. 旧浏览器（无 entry API，极小概率）拖文件夹 → 回退既有单文件链路（不做启发式拦截，避免误伤正常文件；按收口在联调确认）。
11. 中文目录名/文件名 → 正常上传与显示。

### 验收标准

- **在现有上传区直接拖拽文件夹**即可成功上传，目录结构按原层级出现在目标目录下，支持类型已建解析任务（直接解决用户最初反馈）。
- 拖拽/点击单文件行为不受影响。
- 不再出现笼统「文件上传失败」；失败/跳过有可定位明细，三态计数准确。
- 上传中防重入：进行中再次拖入不触发并发批量（同步 ref 锁）。
- 空文件夹 / 无可上传项：明确提示，不静默无反应。
- 旧浏览器（无 entry API，极小概率）：拖文件夹回退既有行为，不做启发式误伤正常文件。

### 分阶段交付

- **MVP（前端，1 天内）**：步骤 1–6（含拖拽目录自动识别、防重入、空目录拒绝、浏览器兼容提示），覆盖「边界与坑」1–15 与 i18n。无后端改动即可上线。
- **增强 1**：可取消（`AbortController`）。
- **增强 2**：后端批量端点 + 常量对齐。

### 风险与回退

- 风险集中在前端 drop 拦截与浏览器目录 API 兼容性；后端零改动。
- **回退**：移除容器上的 capture-drop 监听即可恢复纯单文件 `Dragger`，不影响既有上传。
- `webkitGetAsEntry` 不可用的老旧内核（极小概率）：回退既有行为，不做启发式拦截（避免误伤正常文件）；按收口意见在联调时确认。

---

## Codex 审核结论（追加）

来源：Codex。

Codex 对上述「完整修复方案」的总体判断是：主方向基本成立，即不必立即新增后端批量上传接口，前端可以复用现有 `/files/upload` 单文件接口逐个上传目录内文件。但方案中仍有几个需要修正或收窄表述的点，否则可能导致实现后仍不能满足原始问题的验收。

1. 原始问题是「直接拖拽文件夹上传失败」，因此拖拽目录上传不应只是可选增强。如果 MVP 只新增「上传文件夹」按钮，而不处理原有拖拽区域的目录拖入路径，用户最初反馈的失败路径仍然存在。

2. 不建议直接在现有单文件 `Dragger` 上简单添加 `directory: true`。rc-upload 会把隐藏 file input 变成目录选择 input，这可能破坏现有点击上传单文件的行为。更稳妥的实现是：保留现有单文件上传入口；文件夹选择使用独立入口；文件夹拖拽用独立的 drop 处理逻辑或明确区分单文件/目录上传路径。

3. 文档中的隐藏/系统文件过滤示例不够完整。仅判断文件名是否以 `.` 开头，无法跳过 `.git/README.md`、`__MACOSX/a.pdf` 这类路径。应检查 `webkitRelativePath` 的每个路径段，过滤 `.git`、`__MACOSX`、`.DS_Store`、`Thumbs.db` 等系统文件或目录。

4. 批量上传示例里的结果统计存在歧义。示例用 `ok: false` 同时表示「跳过」和「失败」，最后用 `!ok` 统计 skipped，会把真正失败的文件也算入跳过。建议把结果状态拆成 `uploaded | skipped | failed`，分别统计。

5. 「后端无需改动即可正确显示」这一结论需要限定范围。当前 Files 页确实会根据 URI 前缀推断隐式目录，因此非空目录层级可以显示；但这不等同于系统中已经创建了正式目录记录，也不覆盖空目录、目录 owner 元数据、目录 API 语义等场景。文档表述应限定为「当前 Files 页可展示非空目录层级」。

Codex 建议在落地前至少调整三点：把拖拽目录路径列为 MVP 必做；不要用 `directory: true` 直接改造现有单文件 Dragger；把路径过滤与结果统计抽成可单测的纯函数。

---

## 修订说明（第一轮：用户指示 + Codex 审核）

| 来源 | 意见 | 处理 | 落点 |
| --- | --- | --- | --- |
| 用户 | 不新增上传入口；现有入口自动识别文件夹 | 采纳：单一入口 + drop 自动识别（capture 拦截） | 核心交互、步骤 1 |
| Codex 1 | 拖拽目录上传应为 MVP 必做 | 采纳：列为 MVP 核心 | 核心交互、步骤 1、分阶段交付 |
| Codex 2 | 不要给现有 Dragger 加 `directory:true` | 采纳其顾虑；**调整补救**：因用户要求单一入口，用 capture 阶段自管 drop（不动 input、不加 `directory`） | 核心交互、步骤 1 |
| Codex 3 | 垃圾文件按路径段过滤 | 采纳：`isJunkPath` 逐段检查 + denylist | 步骤 4 |
| Codex 4 | 结果三态统计 | 采纳：`uploaded \| skipped \| failed` | 步骤 5 |
| Codex 5 | 「后端无需改动即可显示」需限定范围 | 采纳：收窄为「非空目录层级可见」 | 结论、诊断补充 §4 |

> 说明：Codex 第 2 点的「独立入口」与用户「不新增入口」冲突；按用户优先，保留单一入口，用 capture 阶段 drop 拦截满足其底层技术顾虑。

---

## Codex 复审结论（追加）

来源：Codex。

Codex 复审修订版后的总体判断是：修订后的方案已经采纳了上一轮核心问题，方向基本可作为实现依据。尤其是「不新增入口」「拖拽目录列为 MVP」「不直接给现有 Dragger 加 `directory:true`」「按路径段过滤垃圾文件」「结果三态统计」「限定 URI 推断目录的适用范围」这些调整是正确的。

仍建议在实施前补充或修正以下剩余风险：

1. capture 阶段 drop 拦截会绕过 antd Upload 自身的 `disabled` 行为，因此实现时必须显式检查 `uploading`、`workspaceId`、`canWrite` 等状态，避免上传中再次拖入文件夹触发并发批量上传。文档示例目前没有展示这个保护。

2. 文档的手动测试写了「拖拽单个/多个文件（非目录）→ 走原有单文件链路」。但当前 `FileUpload.tsx` 配置是 `multiple: false`，rc-upload 对多文件 drop 会只取第一个文件。因此这里要么把测试描述收窄为「单个文件行为不变」，要么明确本次也要支持多文件上传，并补对应实现。

3. 空目录/空文件夹的产品决策已确认：直接拒绝，不做保留。因此不需要引入「空目录增强的数据模型」，也不需要为真正的空目录调用 `createDirectory` 或新增 mkdir-p 接口。实现时应在递归目录后判断：如果没有发现任何文件，提示「空文件夹不支持上传」；如果有文件但经过隐藏/系统文件、类型、大小过滤后没有可上传项，提示「没有可上传的文件」。

4. 实现 capture listener 时要注意 React 闭包新鲜度。示例 `useEffect` 依赖里只列了部分状态，真实代码应把 `runFolderUpload`、`uploading`/`canWrite` 等用到的状态纳入依赖，或用 ref 保存最新值，否则可能出现目标目录、权限或上传状态使用旧值的问题。

5. 对不支持 `webkitGetAsEntry` 的浏览器，文档说「降级为原有单文件行为」。如果用户拖入的是文件夹，这个降级可能仍回到原先的失败体验。建议在能识别异常目录项但无法递归时给出明确提示，而不是静默交给旧链路。

综上，修订版方案主线可以保留；落地前最需要补的是「上传中防重入」「多文件 drop 语义澄清」「空目录直接拒绝的判断与提示」这三处。

---

## 修订说明（第二轮：Codex 复审）

| 复审点 | 意见 | 处理 | 落点 |
| --- | --- | --- | --- |
| 1 | capture 拦截绕过 `disabled`，需防重入与状态校验 | 采纳：`onDrop` 加 `uploadingRef`/`workspaceId` 校验，`runFolderUpload` 再加双保险 | 步骤 1、步骤 5、边界 #2/#4 |
| 2 | 多文件 drop 语义（`multiple:false` 只取第一个） | 采纳：测试收窄为「单文件行为不变」；**用户确认不支持多个散文件上传**，不列为增强 | 核心交互、测试 #2、边界 #17 |
| 3 | 空目录直接拒绝，不保留/不建记录 | 采纳：递归后空 → 提示拒绝；无可上传项 → 提示；删除空目录后端增强 | 核心交互、步骤 5、边界 #5、可选后端增强 1 |
| 4 | React 闭包新鲜度 | 采纳：状态走 ref，listener 只挂一次 | 步骤 1、边界 #3 |
| 5 | 旧浏览器降级会复现失败体验 | 采纳（**后被三审 3 取代**：启发式误伤正常文件，改为不拦截、直接回退既有链路） | 步骤 1、边界 #6、测试 #10 |

---

## Codex 三审结论（追加）

来源：Codex。

Codex 再次审查补充版后的总体判断是：方案主线已经可执行，前两轮指出的关键问题基本都已被吸收，包括单一入口、拖拽目录作为 MVP、防重入、三态统计、空目录拒绝、旧浏览器不静默回退等。当前剩余问题主要是实现细节风险，建议在真正编码前再收紧以下几点：

1. capture listener 示例仍然直接调用闭包里的 `runFolderUpload(picked)`，但监听器只以 `[t]` 为依赖挂载。即使 `uploadPath/workspaceId/documentType/uploading` 走了 ref，`runFolderUpload` 本身仍可能闭包旧的 `showSummary`、`onUploadSuccess`、`message/t` 或其它状态。实现时要么把 `runFolderUpload` 也放入 ref（如 `runFolderUploadRef.current`），要么用稳定的 `useCallback` 并把 listener effect 依赖补完整，避免旧函数被原生事件监听器长期持有。

2. `uploadingRef` 只靠 `useEffect` 同步 `uploading` 仍有很小的防重入窗口：`setUploading(true)` 后到 effect 执行前，第二次 drop 可能仍读到旧的 `false`。批量上传开始时应同步写 `uploadingRef.current = true`，结束时同步写回 `false`，再配合 `setUploading` 更新 UI。

3. 旧浏览器「疑似文件夹」判断中的 `f.type === '' && f.size % 4096 === 0` 启发式容易误伤普通未知类型文件，尤其是 0 字节或无 MIME 的文件。既然产品目标是支持主流浏览器文件夹拖拽，建议不要依赖该启发式拦截正常文件；如果没有 `webkitGetAsEntry`，可直接让普通文件走原链路，并仅在能够确定是目录但无法递归时提示不支持。

4. 重复文件判断不要使用过宽的 `/exist/i`。它可能误把未来的「Parent directory does not exist」之类错误归类成跳过。建议只匹配明确文案，如 `File already exists` / `already exists at`，并保留其它 400 为 failed。

5. 前端硬编码 100MB 与后端代码常量一致，但部署层可能另有限制；当前仓库里 K8s ingress 曾出现 `proxy-body-size: 20m`，Compose/K8s 也有 `MAX_UPLOAD_SIZE=20971520` 配置痕迹。若生产入口存在代理限制，100MB 前端预检会给用户造成误导。实现时应确认实际部署限制，或在文档中说明「以当前部署网关/API 较小值为准」。

综上，补充版方案可以进入实现阶段；实现前最需要补齐的是 `runFolderUpload` 的闭包新鲜度、防重入 ref 的同步写入、以及旧浏览器/重复文件/上传大小限制这三个判断的精确性。

---

## Codex 收口结论（追加）

来源：Codex。

当前文档已经足够作为实施依据，不建议继续反复追加审查项。后续应收敛到必要能力：拖拽文件夹可用、现有单文件行为不变、空目录直接拒绝、上传中防重入、结果按 uploaded/skipped/failed 三态统计、隐藏/系统路径与不支持类型在前端过滤。

对于非必要项或极小概率事件，例如极端旧浏览器启发式、部署网关上传大小限制与后端常量不一致等，可以在真正编码和联调时顺手确认；不需要再作为新的文档阻塞项继续扩展。

---

## 修订说明（第三轮：Codex 三审 + 收口）

| 三审点 | 意见 | 处理 | 落点 |
| --- | --- | --- | --- |
| 1 | `runFolderUpload` 仍可能被原生监听器闭包到旧值 | 采纳：`runFolderUploadRef` 渲染期赋值，`onDrop` 调 `runFolderUploadRef.current` | 步骤 1、边界 #3 |
| 2 | `uploadingRef` 靠 effect 同步有重入窗口 | 采纳：`runFolderUpload` 起始同步置锁、`finally` 释放，不靠 effect | 步骤 1、步骤 5、边界 #2 |
| 3 | 旧浏览器「疑似文件夹」启发式误伤正常文件 | 采纳并简化：**删除启发式**，无 `webkitGetAsEntry` 即回退既有链路 | 步骤 1、边界 #6、i18n（移除 `unsupported_browser`）、测试 #10 |
| 4 | 重复判断 `/exist/i` 过宽 | 采纳：精确 `/already exists/i`，其它 400 记 failed | 步骤 5、边界 #12 |
| 5 | 前端 100MB 可能 > 部署网关上限 | 采纳（按收口为非阻塞）：文档化为以网关 413 为准，联调确认 | 边界 #18、实现基线 |
| 收口 | 聚焦必要能力，停止扩展 | 采纳：新增「实现基线（收口共识）」必要能力清单 + 非阻塞项 | 实现基线 |
