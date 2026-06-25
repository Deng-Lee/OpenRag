# 文件预览操作按钮 + 单文件列表筛选 — 实施计划

> **致执行本计划的智能体：** 必备子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 来逐任务实施本计划。各步骤使用复选框（`- [ ]`）语法以便跟踪进度。

**目标：** 为文件列表新增一个始终可用的「预览」操作按钮（对任意解析状态都生效，并对只读用户隐藏弹窗中的下载按钮）；同时让在左侧树中选中单个文件节点时，把右侧列表收窄为仅显示该文件。

**架构：** 纯前端改动，仅涉及 `web/` —— 无后端改动。改动 1 复用现有的 `FilePreviewModal`，由操作列中新增的按钮触发；该弹窗新增一个可选的 `canWrite` 属性，用于控制其下载按钮（方案 B —— 仅作为威慑/提示，并非访问权限边界）。改动 2 在 `Files.tsx` 中新增两个导出的纯函数辅助方法（`parseFileNodeId`、`pickDisplayedFiles`）以及一个 `selectedFileId` 状态，沿用现有「对页面纯函数做单元测试而非渲染整个页面」的模式。

**技术栈：** React 18 + TypeScript、Ant Design 5、react-i18next、Vitest + @testing-library/react。

规格文档：[docs/superpowers/specs/2026-06-24-file-preview-action-and-single-file-filter-design.md](../specs/2026-06-24-file-preview-action-and-single-file-filter-design.md)

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `web/src/i18n/locales/zh.json` / `en.json` | UI 文案 | 新增 `files.actions.preview` |
| `web/src/pages/Files.tsx` | 文件页：左侧树 + 右侧列表状态 | 新增 `parseFileNodeId`、`pickDisplayedFiles`、`selectedFileId` 状态及接线 |
| `web/src/pages/Files.test.tsx` | 纯函数单元测试 | 为两个新辅助函数新增测试 |
| `web/src/components/FilePreviewModal.tsx` | 预览弹窗 | 新增 `canWrite?` 属性，控制下载按钮 |
| `web/src/components/FilePreviewModal.test.tsx` | 弹窗下载按钮可见性测试 | **新文件** |
| `web/src/components/FileList.tsx` | 文件表格 + 操作列 | 操作列始终渲染；新增预览按钮；向弹窗传入 `canWrite` |
| `web/src/components/FileList.test.tsx` | FileList 渲染测试 | 增强弹窗 mock；新增预览/只读相关测试 |

**所有命令均在 `web/` 目录下执行。** 测试运行器：`npx vitest run <path>`（项目脚本 `npm test` = `vitest --run`）。类型检查/构建：`npm run build`（`tsc && vite build`）。

---

## 任务 1：新增 i18n 文案 `files.actions.preview`

**文件：**
- 修改：`web/src/i18n/locales/zh.json:152`
- 修改：`web/src/i18n/locales/en.json:152`

- [ ] **步骤 1：新增中文文案**

在 `web/src/i18n/locales/zh.json` 的 `files.actions` 对象（从第 147 行开始的那个）内，紧跟 `reprocess` 行之后新增一个 `preview` 条目：

```json
      "reprocess": "重新处理",
      "preview": "预览",
      "manage_dir": "管理目录",
```

- [ ] **步骤 2：新增英文文案**

在 `web/src/i18n/locales/en.json` 的 `files.actions` 对象（第 147 行）内，紧跟 `reprocess` 之后新增对应条目：

```json
      "reprocess": "Reprocess",
      "preview": "Preview",
      "manage_dir": "Manage Directory",
```

- [ ] **步骤 3：确认两个 JSON 文件仍可正常解析**

运行：`cd web && node -e "JSON.parse(require('fs').readFileSync('src/i18n/locales/zh.json','utf8')); JSON.parse(require('fs').readFileSync('src/i18n/locales/en.json','utf8')); console.log('OK')"`
预期：打印 `OK`（无 JSON 语法错误）。

- [ ] **步骤 4：提交**

```bash
git add web/src/i18n/locales/zh.json web/src/i18n/locales/en.json
git commit -m "i18n(web): add files.actions.preview string"
```

---

## 任务 2：单文件选择的纯函数辅助方法（`Files.tsx`）

这些是直接测试的纯导出函数 —— 与 `Files.test.tsx` 中现有的 `selectDisplayedFiles` / `isUnderLogicalPath` 模式一致。

**文件：**
- 修改：`web/src/pages/Files.tsx`（在 `selectDisplayedFiles` 之后、第 90 行处新增辅助函数）
- 测试：`web/src/pages/Files.test.tsx`

- [ ] **步骤 1：编写会失败的测试**

在 `web/src/pages/Files.test.tsx` 中，更新第 2 行的 import，并在文件末尾追加两个 `describe` 块：

```typescript
import { isUnderLogicalPath, selectDisplayedFiles, parseFileNodeId, pickDisplayedFiles } from './Files';
```

```typescript
describe('parseFileNodeId', () => {
  it('parses the numeric id from a file node key', () => {
    expect(parseFileNodeId('file-42')).toBe(42);
  });

  it('returns null for directory keys', () => {
    expect(parseFileNodeId('/uploads')).toBeNull();
    expect(parseFileNodeId('/')).toBeNull();
  });

  it('returns null for malformed file keys', () => {
    expect(parseFileNodeId('file-')).toBeNull();
    expect(parseFileNodeId('file-abc')).toBeNull();
  });
});

describe('pickDisplayedFiles', () => {
  const underUploads = makeFile({ id: 3, uri: '/uploads/a.txt' });
  const deepUnderUploads = makeFile({ id: 4, uri: '/uploads/sub/b.txt' });
  const underDocs = makeFile({ id: 5, uri: '/docs/c.txt' });
  const all = [underUploads, deepUnderUploads, underDocs];

  it('shows only the selected file when a file id is set', () => {
    const ids = pickDisplayedFiles(all, '/', 4).map((f) => f.id);
    expect(ids).toEqual([4]);
  });

  it('ignores selectedDirectory when a file id is set', () => {
    const ids = pickDisplayedFiles(all, '/docs', 3).map((f) => f.id);
    expect(ids).toEqual([3]);
  });

  it('falls back to directory scoping when no file is selected', () => {
    const ids = pickDisplayedFiles(all, '/uploads', null).map((f) => f.id);
    expect(ids).toEqual([3, 4]);
  });
});
```

- [ ] **步骤 2：运行测试以确认其失败**

运行：`cd web && npx vitest run src/pages/Files.test.tsx`
预期：失败 —— `parseFileNodeId is not a function` / `pickDisplayedFiles is not a function`（import 解析为 `undefined`）。

- [ ] **步骤 3：实现辅助函数**

在 `web/src/pages/Files.tsx` 中，紧跟 `selectDisplayedFiles` 函数（结束于第 90 行）之后，新增：

```typescript
/** 文件树节点 key 形如 "file-<id>"；解析出正整数 id，非文件节点或非法 key 返回 null。 */
export function parseFileNodeId(key: string): number | null {
  if (!key.startsWith('file-')) return null;
  const id = Number(key.slice('file-'.length));
  return Number.isInteger(id) && id > 0 ? id : null;
}

/** 右侧列表最终展示集：选中单个文件时只显示该文件，否则按选中目录（递归）收窄。 */
export function pickDisplayedFiles(
  files: File[],
  selectedDirectory: string,
  selectedFileId: number | null
): File[] {
  if (selectedFileId != null) {
    return files.filter((f) => f.id === selectedFileId && !f.is_directory);
  }
  return selectDisplayedFiles(files, selectedDirectory);
}
```

- [ ] **步骤 4：运行测试以确认其通过**

运行：`cd web && npx vitest run src/pages/Files.test.tsx`
预期：通过（`parseFileNodeId`、`pickDisplayedFiles` 以及既有辅助函数的测试全部为绿）。

- [ ] **步骤 5：提交**

```bash
git add web/src/pages/Files.tsx web/src/pages/Files.test.tsx
git commit -m "feat(web): add parseFileNodeId + pickDisplayedFiles helpers for single-file filter"
```

---

## 任务 3：将单文件选择接入文件页（`Files.tsx`）

把任务 2 的辅助函数机械地接入组件状态。按现有模式，本页面的完整渲染不做单元测试（测试文件只覆盖纯函数）；验证方式为类型检查 + 既有测试套件。

**文件：**
- 修改：`web/src/pages/Files.tsx:241`（新增状态）、`:492-496`（`handleWorkspaceChange`）、`:520-527`（`onSelectDirectory`）、`:663-666`（`displayedFiles`）

- [ ] **步骤 1：新增 `selectedFileId` 状态**

在 `web/src/pages/Files.tsx` 中，紧跟第 241 行（`const [selectedDirectory, setSelectedDirectory] = useState<string>('/');`）之后，新增：

```typescript
  const [selectedFileId, setSelectedFileId] = useState<number | null>(null);
```

- [ ] **步骤 2：切换工作区时重置已选文件**

在 `handleWorkspaceChange`（第 492-496 行）中，连同现有的目录重置一起新增文件重置：

```typescript
  const handleWorkspaceChange = (workspace: Workspace) => {
    setSelectedDirectory('/');
    setSelectedFileId(null);
    setCurrentWorkspace(workspace);
    localStorage.setItem('currentWorkspaceId', workspace.id.toString());
  };
```

- [ ] **步骤 3：在 `onSelectDirectory` 中处理文件节点选择**

将整个 `onSelectDirectory` 函数（第 520-527 行）替换为：

```typescript
  const onSelectDirectory = (selectedKeys: Key[]) => {
    if (selectedKeys.length === 0) return;
    const key = selectedKeys[0] as string;
    const fileId = parseFileNodeId(key);
    if (fileId != null) {
      setSelectedFileId(fileId);
    } else {
      setSelectedDirectory(key);
      setSelectedFileId(null);
    }
  };
```

- [ ] **步骤 4：右侧列表改用 `pickDisplayedFiles`**

将 `displayedFiles` 的 memo（第 663-666 行）替换为：

```typescript
  // 右侧列表：选中单个文件时只显示该文件，否则排除目录行并按当前选中目录（递归）收窄。
  const displayedFiles = useMemo(
    () => pickDisplayedFiles(files, selectedDirectory, selectedFileId),
    [files, selectedDirectory, selectedFileId]
  );
```

- [ ] **步骤 5：类型检查**

运行：`cd web && npm run build`
预期：构建成功（无 TS 错误）。`parseFileNodeId` 与 `pickDisplayedFiles` 因在同一模块中定义并使用，已自然被引用 —— 确认被移除的内联 filter 不会引发「声明却从未读取」之类的报错。

- [ ] **步骤 6：运行页面的单元测试（回归）**

运行：`cd web && npx vitest run src/pages/Files.test.tsx`
预期：通过（未改动的辅助函数测试仍为绿）。

- [ ] **步骤 7：提交**

```bash
git add web/src/pages/Files.tsx
git commit -m "feat(web): narrow right file list to the file selected in the left tree"
```

---

## 任务 4：按 `canWrite` 控制预览弹窗的下载按钮（`FilePreviewModal.tsx`）

**文件：**
- 修改：`web/src/components/FilePreviewModal.tsx:29-35`（属性）、`:189-193`（下载按钮）
- 测试：`web/src/components/FilePreviewModal.test.tsx`（**新文件**）

- [ ] **步骤 1：编写会失败的测试（新文件）**

创建 `web/src/components/FilePreviewModal.test.tsx`：

```tsx
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import FilePreviewModal from './FilePreviewModal';
import type { File } from '../types';

const filesApiMock = vi.hoisted(() => ({
  fetchContentBlob: vi.fn(),
  fetchPreview: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../services/api', () => ({
  filesAPI: filesApiMock,
}));

// react-pdf is imported at module top; stub it so jsdom doesn't load the worker.
vi.mock('react-pdf', () => ({
  Document: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Page: () => <canvas />,
}));

function textBlob(text: string): Blob {
  return {
    text: () => Promise.resolve(text),
    arrayBuffer: () => Promise.resolve(new TextEncoder().encode(text).buffer),
  } as unknown as Blob;
}

function textFile(): File {
  return {
    id: 10,
    uri: '/docs/notes.txt',
    name: 'notes.txt',
    owner_id: 1,
    is_directory: false,
    size: 5,
    mime_type: 'text/plain',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  } as File;
}

describe('FilePreviewModal download button visibility', () => {
  beforeEach(() => {
    filesApiMock.fetchContentBlob.mockReset();
    filesApiMock.fetchContentBlob.mockResolvedValue(textBlob('hello'));
    filesApiMock.fetchPreview.mockReset();
  });

  it('hides the download button for read-only users (canWrite=false)', async () => {
    render(<FilePreviewModal open file={textFile()} onClose={vi.fn()} canWrite={false} />);
    await waitFor(() => expect(screen.getByText('hello')).toBeInTheDocument());
    expect(screen.queryByText('files.preview.download')).toBeNull();
  });

  it('shows the download button when canWrite is true', async () => {
    render(<FilePreviewModal open file={textFile()} onClose={vi.fn()} canWrite />);
    await waitFor(() => expect(screen.getByText('hello')).toBeInTheDocument());
    expect(screen.getByText('files.preview.download')).toBeInTheDocument();
  });

  it('hides the download button by default when canWrite is omitted', async () => {
    render(<FilePreviewModal open file={textFile()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('hello')).toBeInTheDocument());
    expect(screen.queryByText('files.preview.download')).toBeNull();
  });
});
```

- [ ] **步骤 2：运行测试以确认其失败**

运行：`cd web && npx vitest run src/components/FilePreviewModal.test.tsx`
预期：失败 —— 第三个（以及第一个）测试失败，因为当前只要 `file && !file.is_directory` 下载按钮就会渲染，与 `canWrite` 无关，所以 `files.preview.download` 文案存在。（`canWrite` 此时也还不是合法属性 —— 编辑器中可能出现 TS 报错，但 vitest 经由 esbuild 运行仍会执行；失败的是断言部分。）

- [ ] **步骤 3：新增 `canWrite` 属性**

在 `web/src/components/FilePreviewModal.tsx` 中，扩展属性接口（第 29-33 行）：

```tsx
interface FilePreviewModalProps {
  open: boolean;
  file: File | null;
  onClose: () => void;
  canWrite?: boolean;
}
```

并更新组件签名（第 35 行）：

```tsx
export default function FilePreviewModal({ open, file, onClose, canWrite = false }: FilePreviewModalProps) {
```

- [ ] **步骤 4：控制下载按钮**

在弹窗 `title`（第 189-193 行）中，向渲染条件加上 `canWrite &&`：

```tsx
          {file && !file.is_directory && canWrite ? (
            <Button type="text" icon={<DownloadOutlined />} onClick={handleDownload} size="small">
              {t('files.preview.download')}
            </Button>
          ) : null}
```

- [ ] **步骤 5：运行测试以确认其通过**

运行：`cd web && npx vitest run src/components/FilePreviewModal.test.tsx`
预期：通过（`canWrite` 为 false/缺省时隐藏下载，为 true 时显示）。

- [ ] **步骤 6：提交**

```bash
git add web/src/components/FilePreviewModal.tsx web/src/components/FilePreviewModal.test.tsx
git commit -m "feat(web): hide FilePreviewModal download button from read-only users"
```

---

## 任务 5：预览操作按钮 + 始终渲染的操作列（`FileList.tsx`）

**文件：**
- 修改：`web/src/components/FileList.tsx:2`（图标 import）、`:208-236`（操作列）、`:302-306`（弹窗属性）
- 测试：`web/src/components/FileList.test.tsx:12-51`（label 映射）、`:62-64`（弹窗 mock）、追加测试

- [ ] **步骤 1：更新测试 —— 增强弹窗 mock、新增 i18n 文案、编写新测试**

在 `web/src/components/FileList.test.tsx` 中：

(a) 向 mock 的 `labels` 映射新增预览文案（在 `files.actions.reprocess` 条目之后，第 33 行）：

```typescript
        'files.actions.reprocess': 'Reprocess',
        'files.actions.preview': 'Preview',
```

(b) 替换 `FilePreviewModal` 的 mock（第 62-64 行），使其暴露 `open` / `file` / `canWrite`：

```tsx
vi.mock('./FilePreviewModal', () => ({
  default: (props: { open: boolean; file: { name?: string } | null; canWrite?: boolean }) =>
    props.open && props.file ? (
      <div data-testid="preview-modal" data-can-write={String(!!props.canWrite)}>
        {props.file.name}
      </div>
    ) : null,
}));
```

(c) 在 `describe('FileList', ...)` 块内（其结尾的 `});`，即第 300 行之前）追加这些测试：

```tsx
  const previewFile = {
    id: 1,
    uri: '/uploads/test.pdf',
    name: 'test.pdf',
    owner_id: 1,
    is_directory: false,
    size: 1024,
    mime_type: 'application/pdf',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  };

  it('opens the preview modal when the preview action is clicked', () => {
    renderFileList(<FileList files={[previewFile]} onFileDeleted={vi.fn()} />);
    expect(screen.queryByTestId('preview-modal')).toBeNull();
    fireEvent.click(screen.getByTitle('Preview'));
    expect(screen.getByTestId('preview-modal')).toHaveTextContent('test.pdf');
  });

  it('shows preview for read-only users but hides reprocess and delete', () => {
    renderFileList(<FileList files={[previewFile]} onFileDeleted={vi.fn()} />);
    expect(screen.getByTitle('Preview')).toBeInTheDocument();
    expect(screen.queryByTitle('Reprocess')).toBeNull();
    expect(screen.queryByText('Delete')).toBeNull();
    fireEvent.click(screen.getByTitle('Preview'));
    expect(screen.getByTestId('preview-modal')).toHaveAttribute('data-can-write', 'false');
  });

  it('passes canWrite to the preview modal for writers', () => {
    renderFileList(<FileList files={[previewFile]} onFileDeleted={vi.fn()} canWrite />);
    fireEvent.click(screen.getByTitle('Preview'));
    expect(screen.getByTestId('preview-modal')).toHaveAttribute('data-can-write', 'true');
  });
```

- [ ] **步骤 2：运行测试以确认其失败**

运行：`cd web && npx vitest run src/components/FileList.test.tsx`
预期：失败 —— `getByTitle('Preview')` 找不到任何元素（尚无预览按钮；并且当前对只读用户而言，操作列根本不渲染）。

- [ ] **步骤 3：引入图标**

在 `web/src/components/FileList.tsx` 中，向图标 import（第 2 行）加入 `EyeOutlined`：

```tsx
import { DeleteOutlined, FolderOutlined, FileOutlined, ReloadOutlined, EyeOutlined } from '@ant-design/icons';
```

- [ ] **步骤 4：让操作列始终渲染，并带上预览按钮**

将整个末尾的操作列条目（第 208-236 行，即 `...(canWrite ? [{ ... }] : [])` 展开式）替换为一个始终存在的普通列：

```tsx
    {
      title: t('files.columns.action'),
      key: 'action',
      width: 160,
      fixed: 'right' as const,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (_: any, record: File) => (
        <Space>
          {!record.is_directory && (
            <Button
              type="link"
              icon={<EyeOutlined />}
              onClick={() => setPreviewFile(record)}
              title={t('files.actions.preview')}
            />
          )}
          {canWrite && !record.is_directory && (
            <Button
              type="link"
              icon={<ReloadOutlined />}
              onClick={() => handleReprocess(record)}
              title={t('files.actions.reprocess')}
            />
          )}
          {canWrite && (
            <Popconfirm
              title={t('files.messages.delete_confirm')}
              onConfirm={() => handleDelete(record.id)}
              okText={t('files.messages.yes')}
              cancelText={t('files.messages.no')}
            >
              <Button type="link" danger icon={<DeleteOutlined />}>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];
```

（此处展示的结尾 `];` 是现有 `columns` 数组的末尾 —— 请勿重复添加。）

- [ ] **步骤 5：向预览弹窗传入 `canWrite`**

更新 `FilePreviewModal` 的使用处（第 302-306 行）：

```tsx
      <FilePreviewModal
        open={!!previewFile}
        file={previewFile}
        onClose={() => setPreviewFile(null)}
        canWrite={canWrite}
      />
```

- [ ] **步骤 6：运行测试以确认其通过**

运行：`cd web && npx vitest run src/components/FileList.test.tsx`
预期：通过 —— 新增的预览测试为绿；既有测试（文件名跳转、reprocess 弹窗、失败气泡卡片等）仍然通过，因为文件名链接按钮保留了可访问名 `a.pdf`/`b.pdf`，且对写入用户而言 reprocess/delete 未变。

- [ ] **步骤 7：提交**

```bash
git add web/src/components/FileList.tsx web/src/components/FileList.test.tsx
git commit -m "feat(web): add always-available preview action button to the file list"
```

---

## 任务 6：完整验证

- [ ] **步骤 1：运行整个 web 测试套件**

运行：`cd web && npm test`
预期：所有套件通过（Files、FileList、FilePreviewModal 以及其余所有既有测试）。

- [ ] **步骤 2：类型检查 + 生产构建**

运行：`cd web && npm run build`
预期：`tsc` 无报错，且 `vite build` 完成。

- [ ] **步骤 3：手动冒烟检查清单（运行 `npm run dev` 后逐项验证）**

  - 操作列现在会在每一文件行（包括只读用户与 `done` 状态文件）上显示一个眼睛/预览按钮。
  - 点击预览能为 `done` 文件打开 `FilePreviewModal`（这正是本特性补上的缺口）。
  - 作为只读用户，弹窗中没有下载按钮；作为写入用户，则有。
  - 在左侧树中点击文件夹，会把右侧列表收窄到该文件夹（行为不变）。
  - 在左侧树中点击单个**文件**节点，会把右侧列表精确收窄到该文件；再次点击文件夹则恢复按文件夹收窄。
  - 切换工作区会清除任何单文件选择状态。

- [ ] **步骤 4：最终提交（仅当步骤 3 暴露出需修复之处时）**

```bash
git add -A -- web/
git commit -m "fix(web): address preview/single-file-filter smoke findings"
```

---

## 自查记录

**规格覆盖情况：**
- §4 改动 1（操作列预览按钮、对所有用户、操作列始终渲染、宽度 160、复用弹窗、文件名点击行为不变）→ 任务 1、5。
- §4 改动 1 下载按钮方案 B（通过 `canWrite` 对只读用户隐藏）→ 任务 4；FileList 传入 `canWrite` → 任务 5 步骤 5。
- §4 改动 2（`selectedFileId`、`onSelectDirectory` 的文件分支、`pickDisplayedFiles` 派生、切换工作区时重置）→ 任务 2、3。
- §7 测试（FileList 预览 + 只读；Files 单文件 vs 目录）→ 任务 5 + 任务 2。下载隐藏在任务 4 中有专门的聚焦测试（规格中关于在 `FileList.test.tsx` 里断言它的备注，改为针对真实弹窗来验证更可靠，因为 `FileList.test.tsx` 已把弹窗 mock 掉了）。
- §6 方案 C → 经决策定为不在范围内；无对应任务。正确。

**类型一致性：** `parseFileNodeId(key: string): number | null` 与 `pickDisplayedFiles(files, selectedDirectory, selectedFileId)` 在任务 2（定义 + 测试）和任务 3（调用处）中的命名与签名完全一致。`canWrite?: boolean` 在 `FilePreviewModal`（任务 4）与 `FileList` 调用处 + 测试 mock（任务 5）之间保持一致。

**无占位符：** 每个代码步骤都包含字面代码；每个运行步骤都列出确切命令与预期结果。
