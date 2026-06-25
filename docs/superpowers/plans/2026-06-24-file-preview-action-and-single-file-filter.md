# File Preview Action Button + Single-File List Filter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-available "preview" action button to the file list (works for any parse status, hides the modal's download button from read-only users), and make selecting a single file node in the left tree narrow the right list to just that file.

**Architecture:** Pure-frontend change in `web/` only — no backend edits. Change 1 reuses the existing `FilePreviewModal`, driven by a new action-column button; the modal gains an optional `canWrite` prop that gates its download button (route B — deterrent, not an access boundary). Change 2 adds two pure exported helpers in `Files.tsx` (`parseFileNodeId`, `pickDisplayedFiles`) plus a `selectedFileId` state, following the existing pattern of unit-testing the page's pure helpers rather than rendering the whole page.

**Tech Stack:** React 18 + TypeScript, Ant Design 5, react-i18next, Vitest + @testing-library/react.

Spec: [docs/superpowers/specs/2026-06-24-file-preview-action-and-single-file-filter-design.md](../specs/2026-06-24-file-preview-action-and-single-file-filter-design.md)

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `web/src/i18n/locales/zh.json` / `en.json` | UI strings | Add `files.actions.preview` |
| `web/src/pages/Files.tsx` | Files page: left tree + right list state | Add `parseFileNodeId`, `pickDisplayedFiles`, `selectedFileId` state + wiring |
| `web/src/pages/Files.test.tsx` | Pure-helper unit tests | Add tests for the two new helpers |
| `web/src/components/FilePreviewModal.tsx` | Preview modal | Add `canWrite?` prop, gate download button |
| `web/src/components/FilePreviewModal.test.tsx` | Modal download-visibility tests | **New file** |
| `web/src/components/FileList.tsx` | File table + actions column | Always-render action column; add Preview button; pass `canWrite` to modal |
| `web/src/components/FileList.test.tsx` | FileList render tests | Enhance modal mock; add preview/read-only tests |

**All commands run from the `web/` directory.** Test runner: `npx vitest run <path>` (project script `npm test` = `vitest --run`). Typecheck/build: `npm run build` (`tsc && vite build`).

---

## Task 1: i18n key `files.actions.preview`

**Files:**
- Modify: `web/src/i18n/locales/zh.json:152`
- Modify: `web/src/i18n/locales/en.json:152`

- [ ] **Step 1: Add the Chinese string**

In `web/src/i18n/locales/zh.json`, inside the `files.actions` object (the one starting at line 147), add a `preview` entry right after the `reprocess` line:

```json
      "reprocess": "重新处理",
      "preview": "预览",
      "manage_dir": "管理目录",
```

- [ ] **Step 2: Add the English string**

In `web/src/i18n/locales/en.json`, inside the `files.actions` object (line 147), add the matching entry after `reprocess`:

```json
      "reprocess": "Reprocess",
      "preview": "Preview",
      "manage_dir": "Manage Directory",
```

- [ ] **Step 3: Verify both JSON files still parse**

Run: `cd web && node -e "JSON.parse(require('fs').readFileSync('src/i18n/locales/zh.json','utf8')); JSON.parse(require('fs').readFileSync('src/i18n/locales/en.json','utf8')); console.log('OK')"`
Expected: prints `OK` (no JSON syntax error).

- [ ] **Step 4: Commit**

```bash
git add web/src/i18n/locales/zh.json web/src/i18n/locales/en.json
git commit -m "i18n(web): add files.actions.preview string"
```

---

## Task 2: Pure helpers for single-file selection (`Files.tsx`)

These are pure, exported functions tested directly — matching the existing `selectDisplayedFiles` / `isUnderLogicalPath` pattern in `Files.test.tsx`.

**Files:**
- Modify: `web/src/pages/Files.tsx` (add helpers after `selectDisplayedFiles`, line 90)
- Test: `web/src/pages/Files.test.tsx`

- [ ] **Step 1: Write the failing tests**

In `web/src/pages/Files.test.tsx`, update the import on line 2 and append two `describe` blocks at the end of the file:

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

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd web && npx vitest run src/pages/Files.test.tsx`
Expected: FAIL — `parseFileNodeId is not a function` / `pickDisplayedFiles is not a function` (import resolves to `undefined`).

- [ ] **Step 3: Implement the helpers**

In `web/src/pages/Files.tsx`, immediately after the `selectDisplayedFiles` function (ends line 90), add:

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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd web && npx vitest run src/pages/Files.test.tsx`
Expected: PASS (all `parseFileNodeId`, `pickDisplayedFiles`, and pre-existing helper tests green).

- [ ] **Step 5: Commit**

```bash
git add web/src/pages/Files.tsx web/src/pages/Files.test.tsx
git commit -m "feat(web): add parseFileNodeId + pickDisplayedFiles helpers for single-file filter"
```

---

## Task 3: Wire single-file selection into the Files page (`Files.tsx`)

Mechanical wiring of the Task 2 helpers into component state. Per the existing pattern, this page's full render is not unit-tested (the test file covers pure helpers only); verification is typecheck + existing suite.

**Files:**
- Modify: `web/src/pages/Files.tsx:241` (add state), `:492-496` (`handleWorkspaceChange`), `:520-527` (`onSelectDirectory`), `:663-666` (`displayedFiles`)

- [ ] **Step 1: Add the `selectedFileId` state**

In `web/src/pages/Files.tsx`, directly after line 241 (`const [selectedDirectory, setSelectedDirectory] = useState<string>('/');`), add:

```typescript
  const [selectedFileId, setSelectedFileId] = useState<number | null>(null);
```

- [ ] **Step 2: Reset selected file on workspace change**

In `handleWorkspaceChange` (lines 492-496), add the reset alongside the existing directory reset:

```typescript
  const handleWorkspaceChange = (workspace: Workspace) => {
    setSelectedDirectory('/');
    setSelectedFileId(null);
    setCurrentWorkspace(workspace);
    localStorage.setItem('currentWorkspaceId', workspace.id.toString());
  };
```

- [ ] **Step 3: Handle file-node selection in `onSelectDirectory`**

Replace the whole `onSelectDirectory` function (lines 520-527) with:

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

- [ ] **Step 4: Use `pickDisplayedFiles` for the right list**

Replace the `displayedFiles` memo (lines 663-666) with:

```typescript
  // 右侧列表：选中单个文件时只显示该文件，否则排除目录行并按当前选中目录（递归）收窄。
  const displayedFiles = useMemo(
    () => pickDisplayedFiles(files, selectedDirectory, selectedFileId),
    [files, selectedDirectory, selectedFileId]
  );
```

- [ ] **Step 5: Typecheck**

Run: `cd web && npm run build`
Expected: build succeeds (no TS errors). `parseFileNodeId` and `pickDisplayedFiles` are already imported because they're defined and used in the same module — confirm no "declared but never read" error for the removed inline filter.

- [ ] **Step 6: Run the page's unit tests (regression)**

Run: `cd web && npx vitest run src/pages/Files.test.tsx`
Expected: PASS (unchanged helper tests still green).

- [ ] **Step 7: Commit**

```bash
git add web/src/pages/Files.tsx
git commit -m "feat(web): narrow right file list to the file selected in the left tree"
```

---

## Task 4: Gate the preview modal's download button by `canWrite` (`FilePreviewModal.tsx`)

**Files:**
- Modify: `web/src/components/FilePreviewModal.tsx:29-35` (prop), `:189-193` (download button)
- Test: `web/src/components/FilePreviewModal.test.tsx` (**new**)

- [ ] **Step 1: Write the failing test (new file)**

Create `web/src/components/FilePreviewModal.test.tsx`:

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

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd web && npx vitest run src/components/FilePreviewModal.test.tsx`
Expected: FAIL — the third test (and the first) fail because the download button currently renders whenever `file && !file.is_directory` regardless of `canWrite`, so `files.preview.download` text is present. (`canWrite` is also not yet a valid prop — a TS error may surface in the editor, but vitest runs via esbuild and will execute; the assertions are what fail.)

- [ ] **Step 3: Add the `canWrite` prop**

In `web/src/components/FilePreviewModal.tsx`, extend the props interface (lines 29-33):

```tsx
interface FilePreviewModalProps {
  open: boolean;
  file: File | null;
  onClose: () => void;
  canWrite?: boolean;
}
```

And update the component signature (line 35):

```tsx
export default function FilePreviewModal({ open, file, onClose, canWrite = false }: FilePreviewModalProps) {
```

- [ ] **Step 4: Gate the download button**

In the modal `title` (lines 189-193), add `canWrite &&` to the render condition:

```tsx
          {file && !file.is_directory && canWrite ? (
            <Button type="text" icon={<DownloadOutlined />} onClick={handleDownload} size="small">
              {t('files.preview.download')}
            </Button>
          ) : null}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd web && npx vitest run src/components/FilePreviewModal.test.tsx`
Expected: PASS (download hidden when `canWrite` is false/omitted, shown when true).

- [ ] **Step 6: Commit**

```bash
git add web/src/components/FilePreviewModal.tsx web/src/components/FilePreviewModal.test.tsx
git commit -m "feat(web): hide FilePreviewModal download button from read-only users"
```

---

## Task 5: Preview action button + always-on action column (`FileList.tsx`)

**Files:**
- Modify: `web/src/components/FileList.tsx:2` (icon import), `:208-236` (action column), `:302-306` (modal props)
- Test: `web/src/components/FileList.test.tsx:12-51` (label map), `:62-64` (modal mock), append tests

- [ ] **Step 1: Update the test — enhance the modal mock, add the i18n label, write new tests**

In `web/src/components/FileList.test.tsx`:

(a) Add the preview label to the mock `labels` map (after the `files.actions.reprocess` entry, line 33):

```typescript
        'files.actions.reprocess': 'Reprocess',
        'files.actions.preview': 'Preview',
```

(b) Replace the `FilePreviewModal` mock (lines 62-64) so it surfaces `open` / `file` / `canWrite`:

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

(c) Append these tests inside the `describe('FileList', ...)` block (before its closing `});` on line 300):

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

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd web && npx vitest run src/components/FileList.test.tsx`
Expected: FAIL — `getByTitle('Preview')` finds nothing (no preview button yet; for read-only users the action column isn't even rendered today).

- [ ] **Step 3: Import the icon**

In `web/src/components/FileList.tsx`, add `EyeOutlined` to the icon import (line 2):

```tsx
import { DeleteOutlined, FolderOutlined, FileOutlined, ReloadOutlined, EyeOutlined } from '@ant-design/icons';
```

- [ ] **Step 4: Make the action column always render with a Preview button**

Replace the entire trailing action-column entry (lines 208-236, the `...(canWrite ? [{ ... }] : [])` spread) with a plain always-present column:

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

(The closing `];` shown is the existing end of the `columns` array — do not duplicate it.)

- [ ] **Step 5: Pass `canWrite` to the preview modal**

Update the `FilePreviewModal` usage (lines 302-306):

```tsx
      <FilePreviewModal
        open={!!previewFile}
        file={previewFile}
        onClose={() => setPreviewFile(null)}
        canWrite={canWrite}
      />
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd web && npx vitest run src/components/FileList.test.tsx`
Expected: PASS — new preview tests green; pre-existing tests (name navigation, reprocess modal, failed popover, etc.) still pass because the name-link button keeps accessible name `a.pdf`/`b.pdf` and reprocess/delete are unchanged for writers.

- [ ] **Step 7: Commit**

```bash
git add web/src/components/FileList.tsx web/src/components/FileList.test.tsx
git commit -m "feat(web): add always-available preview action button to the file list"
```

---

## Task 6: Full verification

- [ ] **Step 1: Run the entire web test suite**

Run: `cd web && npm test`
Expected: all suites PASS (Files, FileList, FilePreviewModal, and every other existing test).

- [ ] **Step 2: Typecheck + production build**

Run: `cd web && npm run build`
Expected: `tsc` reports no errors and `vite build` completes.

- [ ] **Step 3: Manual smoke checklist (run `npm run dev`, then verify)**

  - Action column now shows an eye/preview button on every file row (including read-only users and `done` files).
  - Clicking preview opens `FilePreviewModal` for a `done` file (the gap the feature closes).
  - As a read-only user, the modal has no Download button; as a writer, it does.
  - Clicking a folder in the left tree scopes the right list to that folder (unchanged behavior).
  - Clicking a single **file** node in the left tree narrows the right list to exactly that one file; clicking a folder again restores folder scoping.
  - Switching workspaces clears any single-file selection.

- [ ] **Step 4: Final commit (only if Step 3 surfaced fixes)**

```bash
git add -A -- web/
git commit -m "fix(web): address preview/single-file-filter smoke findings"
```

---

## Self-Review Notes

**Spec coverage:**
- §4 Change 1 (action-column preview button, all users, always-render column, width 160, reuse modal, name-click unchanged) → Tasks 1, 5.
- §4 Change 1 download-button route B (hide for read-only via `canWrite`) → Task 4; FileList passes `canWrite` → Task 5 Step 5.
- §4 Change 2 (`selectedFileId`, `onSelectDirectory` file branch, `pickDisplayedFiles` derivation, workspace-change reset) → Tasks 2, 3.
- §7 tests (FileList preview + read-only; Files single-file vs directory) → Task 5 + Task 2. Download-hide gets its own focused test in Task 4 (the spec's note about asserting it in `FileList.test.tsx` is satisfied more reliably against the real modal, since `FileList.test.tsx` mocks the modal away).
- §6 route C → out of scope by decision; no task. Correct.

**Type consistency:** `parseFileNodeId(key: string): number | null` and `pickDisplayedFiles(files, selectedDirectory, selectedFileId)` are named and signed identically in Task 2 (definition + tests) and Task 3 (call site). `canWrite?: boolean` matches across `FilePreviewModal` (Task 4) and the `FileList` call site + test mock (Task 5).

**No placeholders:** every code step contains the literal code; every run step lists the exact command and expected result.
