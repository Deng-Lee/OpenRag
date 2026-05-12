# File Reprocess Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为文件列表添加重新处理功能，支持清除旧切片/向量并重新生成

**Architecture:** 在后端 files_api.py 添加 `/files/{id}/reprocess` 端点，前端 FileList.tsx 添加重新处理按钮和确认对话框

**Tech Stack:** FastAPI, React + Ant Design, Celery, SQLAlchemy

---

## File Structure

| File | Purpose |
|------|---------|
| `openrag/src/openrag/api/files_api.py` | 新增 reprocess API 端点 |
| `web/src/services/api.ts` | 新增 filesAPI.reprocess 方法 |
| `web/src/components/FileList.tsx` | 添加重新处理按钮和对话框 |
| `web/src/i18n/locales/zh-CN.json` | 添加中文翻译 |
| `web/src/i18n/locales/en-US.json` | 添加英文翻译 |

---

## Task 1: 后端 API - Reprocess 端点

**Files:**
- Modify: `openrag/src/openrag/api/files_api.py:99-111` (在 SUPPORTED_PARSER_TYPES 附近)
- Modify: `openrag/src/openrag/api/files_api.py:646-650` (文件末尾)

### Step 1: 添加 ReprocessRequest 数据模型

在 `SUPPORTED_PARSER_TYPES` 之后、`validate_path` 函数之前添加：

```python
class ReprocessRequest(BaseModel):
    """File reprocess request"""

    parser_type: Optional[str] = Field(
        default=None,
        description=f"Parser type: {', '.join(SUPPORTED_PARSER_TYPES)}. If not provided, uses the original parser_type."
    )

    @field_validator('parser_type')
    @classmethod
    def validate_parser_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in SUPPORTED_PARSER_TYPES:
            raise ValueError(f"Invalid parser_type. Supported types: {', '.join(SUPPORTED_PARSER_TYPES)}")
        return v
```

### Step 2: 添加清理旧数据的辅助函数

在 `ReprocessRequest` 类之后添加：

```python
def cleanup_file_processing_data(
    file: FileModel,
    workspace_slug: str,
    db: Session
) -> None:
    """Cleanup existing processing data for a file

    Args:
        file: File record
        workspace_slug: Workspace slug for storage
        db: Database session
    """
    from openrag.hierarchy.hierarchy_storage import HierarchyStorage
    from openrag.storage.minio_storage import MinioStorage
    import shutil

    # Cleanup hierarchy storage files
    try:
        storage = HierarchyStorage()
        file_dir = storage._get_uri_dir(file.uri)
        if file_dir.exists():
            shutil.rmtree(file_dir)
    except Exception as e:
        # Log but don't fail - data might not exist
        import logging
        logging.getLogger(__name__).warning(f"Failed to cleanup hierarchy files: {e}")

    # Cleanup vector data if exists (placeholder for future vector DB integration)
    if file.l0_vector_id:
        # TODO: Delete from vector database when integrated
        # For now, just clear the reference
        file.l0_vector_id = None

    # Reset file processing status
    file.processing_status = ProcessingStatus.pending
    file.l0_path = None
    file.l1_path = None
    file.l2_path = None
    file.total_chunks = 0
    file.total_tokens = 0
    db.commit()
```

### Step 3: 添加 reprocess 端点

在文件末尾（`create_directory` 函数之后）添加：

```python
@router.post("/{file_id}/reprocess", response_model=FileUploadResponse)
async def reprocess_file(
    file_id: int,
    request: ReprocessRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reprocess a file - clears existing chunks/vectors and re-triggers processing

    Args:
        file_id: File ID to reprocess
        request: Reprocess request with optional parser_type override
        current_user: Current authenticated user
        db: Database session

    Returns:
        File metadata and new processing task ID
    """
    # Get file
    file = db.query(FileModel).filter(FileModel.id == file_id).first()
    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Check workspace write permission
    from openrag.services.workspace_service import WorkspaceService

    ws_service = WorkspaceService(db)
    if not ws_service.check_user_permission(
        file.workspace_id, current_user.id, "write"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write permission required for this workspace",
        )

    # Cannot reprocess directories
    if file.is_directory:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reprocess directories",
        )

    # Get workspace
    workspace = ws_service.get_workspace(file.workspace_id)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {file.workspace_id} not found",
        )

    # Determine parser type to use
    parser_type = request.parser_type if request.parser_type else file.parser_type
    if not parser_type:
        parser_type = "auto"

    # Cleanup existing processing data
    cleanup_file_processing_data(file, workspace.slug, db)

    # Trigger async document processing
    task = None
    try:
        if file.mime_type in ALLOWED_MIME_TYPES:
            task = process_document_async.delay(
                file_uri=file.uri,
                file_id=file.id,
                user_id=current_user.id,
                workspace_id=file.workspace_id,
                file_size=file.size,
                parser_type=parser_type,
            )
            # Update file record with new parser type
            file.parser_type = parser_type if parser_type != "auto" else None
            db.commit()
    except Exception as e:
        # If Celery is not available, log and continue
        import logging
        logging.getLogger(__name__).error(f"Failed to trigger reprocessing task: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start reprocessing task",
        )

    # Prepare response
    response_data = FileUploadResponse(
        id=file.id,
        uri=file.uri,
        name=file.name,
        owner_id=file.owner_id,
        parent_id=file.parent_id,
        is_directory=file.is_directory,
        size=file.size,
        mime_type=file.mime_type,
        created_at=file.created_at.isoformat(),
        updated_at=file.updated_at.isoformat(),
        task_id=task.id if task else None,
    )

    return response_data
```

### Step 4: 验证 API 端点

运行后端服务并测试 API：

```bash
cd e:/project/openrag/OpenRag
python -m uvicorn openrag.api.main:app --reload --port 8001
```

在另一个终端测试：

```bash
curl -X POST http://localhost:8001/files/1/reprocess \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"parser_type": "pdf"}'
```

Expected: 如果文件存在且有权限，返回包含 task_id 的 JSON

### Step 5: Commit 后端变更

```bash
git add openrag/src/openrag/api/files_api.py
git commit -m "feat(api): add file reprocess endpoint

- Add POST /files/{id}/reprocess endpoint
- Support optional parser_type override
- Cleanup existing processing data before reprocessing
- Require write permission"
```

---

## Task 2: 前端 API 服务

**Files:**
- Modify: `web/src/services/api.ts:65-87`

### Step 1: 在 filesAPI 中添加 reprocess 方法

在 `filesAPI` 对象中的 `move` 方法之后添加：

```typescript
  reprocess: async (id: number, parserType?: string): Promise<FileUploadResponse> => {
    const response = await api.post(`/files/${id}/reprocess`, { parser_type: parserType });
    return response.data;
  },
```

注意：需要确保 `FileUploadResponse` 类型在文件中被导入或定义。

### Step 2: Commit API 服务变更

```bash
git add web/src/services/api.ts
git commit -m "feat(api): add filesAPI.reprocess method

Add reprocess method to filesAPI for triggering file reprocessing"
```

---

## Task 3: 前端组件 - FileList 重新处理功能

**Files:**
- Modify: `web/src/components/FileList.tsx:1-23` (imports)
- Modify: `web/src/components/FileList.tsx:17-22` (props interface)
- Modify: `web/src/components/FileList.tsx:107-125` (columns action)
- Modify: `web/src/components/FileList.tsx:127-161` (JSX return)

### Step 1: 添加导入和图标

在文件顶部导入部分添加 `ReloadOutlined`：

```typescript
import { DeleteOutlined, EditOutlined, FolderOutlined, FileOutlined, ReloadOutlined } from '@ant-design/icons';
```

### Step 2: 扩展 props 接口

将 `FileListProps` 接口修改为：

```typescript
interface FileListProps {
  files: File[];
  onFileDeleted: () => void;
  onFileReprocessed?: () => void;  // 新增
  loading?: boolean;
  canWrite?: boolean;
}
```

### Step 3: 修改组件函数签名

将函数定义修改为：

```typescript
export default function FileList({ files, onFileDeleted, onFileReprocessed, loading, canWrite = false }: FileListProps) {
```

### Step 4: 添加状态和处理器

在组件内部（`editForm` 之后）添加状态：

```typescript
  const [isReprocessModalOpen, setIsReprocessModalOpen] = useState(false);
  const [reprocessingFile, setReprocessingFile] = useState<File | null>(null);
  const [reprocessForm] = Form.useForm();
  const [reprocessing, setReprocessing] = useState(false);
```

在 `handleUpdatePath` 之后添加处理函数：

```typescript
  const handleReprocess = (record: File) => {
    setReprocessingFile(record);
    reprocessForm.setFieldsValue({
      parser_type: record.parser_type || 'auto',
    });
    setIsReprocessModalOpen(true);
  };

  const confirmReprocess = async (values: { parser_type: string }) => {
    if (!reprocessingFile) return;

    setReprocessing(true);
    try {
      const parserType = values.parser_type === 'auto' ? undefined : values.parser_type;
      await filesAPI.reprocess(reprocessingFile.id, parserType);
      message.success(t('files.messages.reprocess_success'));
      setIsReprocessModalOpen(false);
      reprocessForm.resetFields();
      if (onFileReprocessed) {
        onFileReprocessed();
      }
    } catch (error: any) {
      console.error('Failed to reprocess file:', error);
      message.error(error.response?.data?.detail || t('files.messages.reprocess_failed'));
    } finally {
      setReprocessing(false);
    }
  };
```

### Step 5: 修改操作列

将 action 列的 render 函数修改为：

```typescript
    ...(canWrite ? [{
      title: t('files.columns.action'),
      key: 'action',
      render: (_: any, record: File) => (
        <Space>
          {!record.is_directory && (
            <Button
              type="link"
              icon={<ReloadOutlined />}
              onClick={() => handleReprocess(record)}
              title={t('files.actions.reprocess')}
            />
          )}
          <Popconfirm
            title={t('files.messages.delete_confirm')}
            onConfirm={() => handleDelete(record.id)}
            okText={t('files.messages.yes')}
            cancelText={t('files.messages.no')}
          >
            <Button type="link" danger icon={<DeleteOutlined />}>
              {t('files.columns.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    }] : []),
```

### Step 6: 添加重新处理对话框

在文件末尾的 `</>` 之前添加对话框：

```typescript
      {/* Reprocess Modal */}
      <Modal
        title={t('files.actions.reprocess')}
        open={isReprocessModalOpen}
        onOk={() => reprocessForm.submit()}
        onCancel={() => {
          setIsReprocessModalOpen(false);
          reprocessForm.resetFields();
        }}
        confirmLoading={reprocessing}
      >
        <Form
          form={reprocessForm}
          layout="vertical"
          onFinish={confirmReprocess}
        >
          <Form.Item
            name="parser_type"
            label={t('files.fields.parser_type')}
            initialValue="auto"
          >
            <Select
              options={[
                { value: 'auto', label: t('files.parser_types.auto') },
                { value: 'pdf', label: t('files.parser_types.pdf') },
                { value: 'docx', label: t('files.parser_types.docx') },
                { value: 'xlsx', label: t('files.parser_types.xlsx') },
                { value: 'pptx', label: t('files.parser_types.pptx') },
                { value: 'txt', label: t('files.parser_types.txt') },
                { value: 'md', label: t('files.parser_types.md') },
                { value: 'html', label: t('files.parser_types.html') },
                { value: 'json', label: t('files.parser_types.json') },
                { value: 'csv', label: t('files.parser_types.csv') },
                { value: 'epub', label: t('files.parser_types.epub') },
              ]}
            />
          </Form.Item>
          <Text type="secondary">{t('files.messages.reprocess_hint')}</Text>
        </Form>
      </Modal>
```

### Step 7: 添加 Select 导入

在文件顶部导入部分添加：

```typescript
import { Table, Button, Space, Popconfirm, message, Typography, Modal, Form, Input, Select } from 'antd';
```

### Step 8: Commit 组件变更

```bash
git add web/src/components/FileList.tsx
git commit -m "feat(ui): add reprocess button to FileList

- Add reload icon button for file reprocessing
- Add reprocess modal with parser type selection
- Support onFileReprocessed callback prop"
```

---

## Task 4: 更新父组件传入回调

**Files:**
- Modify: `web/src/pages/Files.tsx` (根据实际情况)

检查 Files.tsx 是否使用 FileList 组件，如有需要添加 `onFileReprocessed` 回调：

```typescript
<FileList
  files={files}
  onFileDeleted={fetchFiles}
  onFileReprocessed={fetchFiles}  // 添加这行
  loading={loading}
  canWrite={canWrite}
/>
```

如果没有 Files.tsx 或不需要修改，跳过此任务。

---

## Task 5: 添加国际化翻译

**Files:**
- Modify: `web/src/i18n/locales/zh-CN.json`
- Modify: `web/src/i18n/locales/en-US.json`

### Step 1: 中文翻译

在 zh-CN.json 的 `files` 部分添加：

```json
{
  "files": {
    "actions": {
      "reprocess": "重新处理"
    },
    "fields": {
      "parser_type": "解析器类型"
    },
    "parser_types": {
      "auto": "自动检测",
      "pdf": "PDF 解析器",
      "docx": "Word 文档解析器",
      "xlsx": "Excel 解析器",
      "pptx": "PowerPoint 解析器",
      "txt": "纯文本解析器",
      "md": "Markdown 解析器",
      "html": "HTML 解析器",
      "json": "JSON 解析器",
      "csv": "CSV 解析器",
      "epub": "EPUB 解析器"
    },
    "messages": {
      "reprocess_success": "文件重新处理已开始",
      "reprocess_failed": "重新处理失败",
      "reprocess_hint": "选择解析器类型重新处理文件。原有切片和向量数据将被清除。"
    },
    "columns": {
      "delete": "删除"
    }
  }
}
```

### Step 2: 英文翻译

在 en-US.json 的 `files` 部分添加：

```json
{
  "files": {
    "actions": {
      "reprocess": "Reprocess"
    },
    "fields": {
      "parser_type": "Parser Type"
    },
    "parser_types": {
      "auto": "Auto Detect",
      "pdf": "PDF Parser",
      "docx": "Word Document Parser",
      "xlsx": "Excel Parser",
      "pptx": "PowerPoint Parser",
      "txt": "Plain Text Parser",
      "md": "Markdown Parser",
      "html": "HTML Parser",
      "json": "JSON Parser",
      "csv": "CSV Parser",
      "epub": "EPUB Parser"
    },
    "messages": {
      "reprocess_success": "File reprocessing started",
      "reprocess_failed": "Reprocessing failed",
      "reprocess_hint": "Select parser type to reprocess the file. Existing chunks and vectors will be cleared."
    },
    "columns": {
      "delete": "Delete"
    }
  }
}
```

### Step 3: Commit 翻译

```bash
git add web/src/i18n/locales/
git commit -m "feat(i18n): add reprocess translations

- Add Chinese and English translations for reprocess feature"
```

---

## Verification Checklist

- [ ] 后端 API `/files/{id}/reprocess` 可用
- [ ] 需要 write 权限才能调用
- [ ] 目录不能重新处理
- [ ] 原有数据被正确清理
- [ ] 新任务成功创建
- [ ] 前端按钮正确显示/隐藏
- [ ] 对话框可以打开和关闭
- [ ] 可以选择不同的 parser_type
- [ ] 成功后有提示消息
- [ ] 列表自动刷新
