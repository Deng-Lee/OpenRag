# File Reprocess Feature Design

**Date:** 2026-04-09  
**Topic:** 文件重新处理功能 - 支持单个文件重新切片和生成向量  
**Status:** Approved

## Overview

在文件列表页面为单个文件增加"重新处理"功能，允许用户清除该文件已有的切片和向量数据，并根据选择的解析器类型重新生成。

## Goals

1. 支持单个文件的重新切片和向量化
2. 保留原有的解析器类型，或允许用户选择新的解析器类型
3. 清理已存在的层次结构文件（L0/L1/L2）和向量数据
4. 触发新的异步处理任务

## Non-Goals

1. 不支持批量文件重新处理（未来可扩展）
2. 不保留历史版本
3. 不支持部分重新处理（如只重新生成向量）

## Architecture

### Backend API

新增端点：`POST /files/{file_id}/reprocess`

**Request:**
```json
{
  "parser_type": "pdf"  // 可选，不传则使用原来的 parser_type
}
```

**Response:**
```json
{
  "message": "File reprocessing started",
  "task_id": "celery-task-id"
}
```

**处理流程：**

1. **权限检查**
   - 验证用户 write 权限
   - 验证文件存在且不是目录

2. **清理旧数据**
   - 删除层次结构文件（L0/L1/L2）
   - 如果存在 l0_vector_id，删除向量数据库中的记录

3. **重置文件状态**
   - processing_status → pending
   - 清空 l0_path, l1_path, l2_path
   - total_chunks, total_tokens 重置为 0

4. **触发新任务**
   - 调用 `process_document_async.delay()`
   - 使用原 parser_type 或用户指定的新类型

### Frontend Changes

修改文件：`web/src/components/FileList.tsx`

**UI 变更：**

1. **操作列新增按钮**
   - 按钮图标：`<ReloadOutlined />`（旋转箭头图标）
   - 仅对非目录文件显示
   - 需要 write 权限

2. **确认对话框**
   - 标题：确认重新处理
   - 内容：显示文件名和警告信息（数据将被清除）
   - 选择器：Parser Type 下拉框（可选，默认"使用原类型"）

3. **处理流程**
   ```
   点击"重新处理"按钮
     ↓
   弹出确认对话框
     ↓
   选择 parser_type（可选，默认使用原类型）
     ↓
   调用 POST /files/{id}/reprocess
     ↓
   显示成功/失败消息
     ↓
   刷新文件列表
   ```

## Data Flow

```
User
  │
  ▼
FileList.tsx ──► api.ts ──► POST /files/{id}/reprocess
                              │
                              ▼
                         files_api.py
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         权限检查        清理旧数据       重置状态
              │               │               │
              │               ▼               │
              │    - 删除 L0/L1/L2 文件      │
              │    - 删除向量数据            │
              │               │               │
              └───────────────┴───────────────┘
                              │
                              ▼
                   celery_tasks.py
                   process_document_async
```

## Error Handling

| 场景 | 处理 |
|------|------|
| 文件不存在 | 404 Not Found |
| 用户无 write 权限 | 403 Forbidden |
| 文件是目录 | 400 Bad Request |
| 无效的 parser_type | 400 Bad Request |
| 清理旧数据失败 | 记录警告日志，继续处理 |
| Celery 任务创建失败 | 500 Internal Server Error |

## Security Considerations

1. **权限控制**：必须验证用户有工作空间的 write 权限
2. **资源保护**：仅允许非目录文件重新处理
3. **数据隔离**：确保只影响指定文件的数据

## API Implementation Details

### files_api.py 新增代码

```python
class ReprocessRequest(BaseModel):
    parser_type: Optional[str] = Field(
        default=None,
        description=f"Parser type: {', '.join(SUPPORTED_PARSER_TYPES)}. If not provided, uses the original parser_type."
    )

@router.post("/{file_id}/reprocess", response_model=FileUploadResponse)
async def reprocess_file(
    file_id: int,
    request: ReprocessRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reprocess a file - clears existing chunks/vectors and re-triggers processing"""
    # 实现见后续...
```

## Frontend Implementation Details

### api.ts 新增

```typescript
export const filesAPI = {
  // ... existing methods
  reprocess: async (id: number, parserType?: string): Promise<{ message: string; task_id: string }> => {
    const response = await api.post(`/files/${id}/reprocess`, { parser_type: parserType });
    return response.data;
  },
};
```

### FileList.tsx 新增

```typescript
const handleReprocess = async (record: File) => {
  setReprocessingFile(record);
  setIsReprocessModalOpen(true);
};

const confirmReprocess = async (parserType?: string) => {
  if (!reprocessingFile) return;
  
  try {
    await filesAPI.reprocess(reprocessingFile.id, parserType);
    message.success(t('files.messages.reprocess_success'));
    onFileDeleted(); // Refresh list
  } catch (error) {
    message.error(t('files.messages.reprocess_failed'));
  }
};
```

## Testing Considerations

1. **权限测试**：验证 read 权限用户无法调用
2. **数据清理测试**：验证旧数据被正确删除
3. **任务创建测试**：验证新任务成功创建
4. **边界测试**：目录、不存在文件、无效 parser_type
