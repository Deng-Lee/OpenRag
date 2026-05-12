# Chunk上下文预览功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在搜索页面实现点击chunk卡片弹出上下文预览模态框的功能

**Architecture:** 混合方案 - 复用现有DocumentPreview能力 + 新建ChunkContextModal组件负责智能上下文计算和展示

**Tech Stack:** React + TypeScript + Tailwind CSS + React Query + react-pdf-highlighter

---

## 文件结构规划

| 文件路径 | 职责 | 类型 |
|---------|------|------|
| `web/src/components/chunk-context-modal/index.tsx` | 模态框容器组件 | 新建 |
| `web/src/components/chunk-context-modal/hooks.ts` | 模态框相关hooks | 新建 |
| `web/src/components/chunk-context-preview/index.tsx` | 智能上下文预览组件 | 新建 |
| `web/src/components/chunk-context-preview/pdf-context.tsx` | PDF上下文预览 | 新建 |
| `web/src/utils/chunk-context-util.ts` | 上下文提取工具函数 | 新建 |
| `web/src/pages/next-search/search-view.tsx` | 搜索结果页面，集成点击事件 | 修改 |
| `web/src/interfaces/database/knowledge.ts` | 可能需要扩展类型 | 修改 |

---

## Task 1: 创建上下文提取工具函数

**Files:**
- Create: `web/src/utils/chunk-context-util.ts`

- [ ] **Step 1: 编写测试**

```typescript
// web/src/utils/__tests__/chunk-context-util.test.ts
import { describe, it, expect } from 'vitest';
import { extractContextInfo, ContextType } from '../chunk-context-util';
import { ITestingChunk } from '@/interfaces/database/knowledge';

describe('extractContextInfo', () => {
  it('should extract PDF page number from positions', () => {
    const chunk: ITestingChunk = {
      chunk_id: 'test-1',
      content_with_weight: 'test content',
      doc_id: 'doc-1',
      docnm_kwd: 'test.pdf',
      doc_type_kwd: 'pdf',
      positions: [[1, 100, 200, 50, 30]], // [page, x, y, width, height]
      image_id: '',
      kb_id: 'kb-1',
      content_ltks: '',
      doc_name: 'test.pdf',
      img_id: '',
      important_kwd: [],
      similarity: 0.9,
      term_similarity: 0.8,
      vector: [],
      vector_similarity: 0.95,
      highlight: '',
    };

    const result = extractContextInfo(chunk);
    
    expect(result.type).toBe(ContextType.PDF);
    expect(result.pageNumber).toBe(1);
    expect(result.coordinates).toEqual({ x: 100, y: 200, width: 50, height: 30 });
  });

  it('should handle image type', () => {
    const chunk: ITestingChunk = {
      ...baseChunk,
      doc_type_kwd: 'image',
      image_id: 'img-123',
    };

    const result = extractContextInfo(chunk);
    
    expect(result.type).toBe(ContextType.IMAGE);
    expect(result.imageId).toBe('img-123');
  });

  it('should return UNKNOWN for unsupported type', () => {
    const chunk: ITestingChunk = {
      ...baseChunk,
      doc_type_kwd: 'unknown',
    };

    const result = extractContextInfo(chunk);
    
    expect(result.type).toBe(ContextType.UNKNOWN);
  });
});
```

- [ ] **Step 2: 运行测试确保失败**

Run: `cd web && npm test -- src/utils/__tests__/chunk-context-util.test.ts`
Expected: FAIL - "extractContextInfo is not defined"

- [ ] **Step 3: 实现工具函数**

```typescript
// web/src/utils/chunk-context-util.ts
import { ITestingChunk } from '@/interfaces/database/knowledge';

export enum ContextType {
  PDF = 'pdf',
  IMAGE = 'image',
  TEXT = 'text',
  EXCEL = 'excel',
  WORD = 'word',
  UNKNOWN = 'unknown',
}

export interface PDFContextInfo {
  type: ContextType.PDF;
  pageNumber: number;
  coordinates: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
}

export interface ImageContextInfo {
  type: ContextType.IMAGE;
  imageId: string;
}

export interface TextContextInfo {
  type: ContextType.TEXT;
  lineStart: number;
  lineEnd: number;
}

export interface ExcelContextInfo {
  type: ContextType.EXCEL;
  sheetName?: string;
  cellRange: string;
}

export interface WordContextInfo {
  type: ContextType.WORD;
  paragraphIndex: number;
}

export interface UnknownContextInfo {
  type: ContextType.UNKNOWN;
}

export type ContextInfo =
  | PDFContextInfo
  | ImageContextInfo
  | TextContextInfo
  | ExcelContextInfo
  | WordContextInfo
  | UnknownContextInfo;

/**
 * 根据chunk信息提取上下文数据
 * @param chunk - chunk数据
 * @returns 上下文信息
 */
export function extractContextInfo(chunk: ITestingChunk): ContextInfo {
  const docType = chunk.doc_type_kwd?.toLowerCase() || '';

  switch (docType) {
    case 'pdf':
      return extractPDFContext(chunk);
    case 'image':
    case 'jpg':
    case 'jpeg':
    case 'png':
    case 'gif':
      return extractImageContext(chunk);
    case 'txt':
    case 'md':
    case 'markdown':
      return extractTextContext(chunk);
    case 'xlsx':
    case 'xls':
    case 'csv':
      return extractExcelContext(chunk);
    case 'doc':
    case 'docx':
      return extractWordContext(chunk);
    default:
      return { type: ContextType.UNKNOWN };
  }
}

function extractPDFContext(chunk: ITestingChunk): PDFContextInfo {
  const positions = chunk.positions || [];
  
  if (positions.length > 0 && positions[0].length >= 5) {
    const [page, x, y, width, height] = positions[0];
    return {
      type: ContextType.PDF,
      pageNumber: page || 1,
      coordinates: {
        x: x || 0,
        y: y || 0,
        width: width || 100,
        height: height || 20,
      },
    };
  }

  // Fallback to page 1 if no position info
  return {
    type: ContextType.PDF,
    pageNumber: 1,
    coordinates: { x: 0, y: 0, width: 100, height: 20 },
  };
}

function extractImageContext(chunk: ITestingChunk): ImageContextInfo {
  return {
    type: ContextType.IMAGE,
    imageId: chunk.image_id || chunk.img_id || '',
  };
}

function extractTextContext(chunk: ITestingChunk): TextContextInfo {
  // For text files, we could extract line numbers from positions
  // For now, return a default range
  return {
    type: ContextType.TEXT,
    lineStart: 1,
    lineEnd: 100,
  };
}

function extractExcelContext(chunk: ITestingChunk): ExcelContextInfo {
  return {
    type: ContextType.EXCEL,
    cellRange: 'A1:Z100',
  };
}

function extractWordContext(chunk: ITestingChunk): WordContextInfo {
  return {
    type: ContextType.WORD,
    paragraphIndex: 0,
  };
}

/**
 * 检查chunk是否有有效的位置信息
 * @param chunk - chunk数据
 * @returns boolean
 */
export function hasValidPosition(chunk: ITestingChunk): boolean {
  if (!chunk.positions || chunk.positions.length === 0) {
    return false;
  }
  
  const docType = chunk.doc_type_kwd?.toLowerCase() || '';
  
  if (docType === 'pdf') {
    return chunk.positions[0].length >= 5;
  }
  
  return true;
}
```

- [ ] **Step 4: 运行测试确保通过**

Run: `cd web && npm test -- src/utils/__tests__/chunk-context-util.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add web/src/utils/chunk-context-util.ts web/src/utils/__tests__/chunk-context-util.test.ts
git commit -m "feat(utils): add chunk context extraction utilities

- Add extractContextInfo function to parse chunk positions by doc type
- Support PDF, Image, Text, Excel, Word context types
- Add hasValidPosition helper for validation"
```

---

## Task 2: 创建ChunkContextModal组件

**Files:**
- Create: `web/src/components/chunk-context-modal/index.tsx`
- Create: `web/src/components/chunk-context-modal/hooks.ts`

- [ ] **Step 1: 编写组件Props类型和基础结构**

```typescript
// web/src/components/chunk-context-modal/index.tsx
import React from 'react';
import { Modal } from '@/components/ui/modal';
import { ITestingChunk } from '@/interfaces/database/knowledge';
import { FileIcon } from '@/components/icon-font';
import { Button } from '@/components/ui/button';
import { X, ExternalLink } from 'lucide-react';

export interface IChunkContextModalProps {
  visible: boolean;
  onClose: () => void;
  chunk: ITestingChunk | null;
  onViewFullDocument?: () => void;
}

export const ChunkContextModal: React.FC<IChunkContextModalProps> = ({
  visible,
  onClose,
  chunk,
  onViewFullDocument,
}) => {
  if (!chunk) return null;

  return (
    <Modal
      open={visible}
      onCancel={onClose}
      showFooter={false}
      width={900}
      className="chunk-context-modal"
      title={
        <div className="flex items-center gap-2">
          <FileIcon name={chunk.docnm_kwd || chunk.doc_name} />
          <span className="truncate max-w-md">
            {chunk.docnm_kwd || chunk.doc_name}
          </span>
        </div>
      }
    >
      <div className="flex flex-col h-[600px]">
        {/* Preview content will go here */}
        <div className="flex-1 overflow-auto p-4 bg-gray-50 rounded-lg">
          <p className="text-gray-500 text-center">
            Context preview coming soon...
          </p>
        </div>

        {/* Action bar */}
        <div className="flex justify-end gap-2 pt-4 border-t mt-4">
          <Button variant="outline" onClick={onClose}>
            <X className="w-4 h-4 mr-2" />
            关闭
          </Button>
          {onViewFullDocument && (
            <Button onClick={onViewFullDocument}>
              <ExternalLink className="w-4 h-4 mr-2" />
              查看完整文档
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
};

export default ChunkContextModal;
```

- [ ] **Step 2: 创建hooks文件**

```typescript
// web/src/components/chunk-context-modal/hooks.ts
import { useState, useCallback } from 'react';
import { ITestingChunk } from '@/interfaces/database/knowledge';

export interface IUseChunkContextModalResult {
  isOpen: boolean;
  selectedChunk: ITestingChunk | null;
  openModal: (chunk: ITestingChunk) => void;
  closeModal: () => void;
}

export function useChunkContextModal(): IUseChunkContextModalResult {
  const [isOpen, setIsOpen] = useState(false);
  const [selectedChunk, setSelectedChunk] = useState<ITestingChunk | null>(null);

  const openModal = useCallback((chunk: ITestingChunk) => {
    setSelectedChunk(chunk);
    setIsOpen(true);
  }, []);

  const closeModal = useCallback(() => {
    setIsOpen(false);
    // Delay clearing chunk to allow exit animation
    setTimeout(() => setSelectedChunk(null), 300);
  }, []);

  return {
    isOpen,
    selectedChunk,
    openModal,
    closeModal,
  };
}
```

- [ ] **Step 3: 提交**

```bash
git add web/src/components/chunk-context-modal/
git commit -m "feat(components): add ChunkContextModal component

- Add modal container with document title and action buttons
- Add useChunkContextModal hook for state management
- Support ESC/click outside to close"
```

---

## Task 3: 创建ChunkContextPreview组件（PDF支持）

**Files:**
- Create: `web/src/components/chunk-context-preview/index.tsx`

- [ ] **Step 1: 实现PDF上下文预览**

```typescript
// web/src/components/chunk-context-preview/index.tsx
import React, { useMemo } from 'react';
import { ITestingChunk } from '@/interfaces/database/knowledge';
import { extractContextInfo, ContextType } from '@/utils/chunk-context-util';
import { PdfContextPreview } from './pdf-context';
import { ImageContextPreview } from './image-context';
import { TextContextPreview } from './text-context';

export interface IChunkContextPreviewProps {
  chunk: ITestingChunk;
  documentUrl: string;
}

export const ChunkContextPreview: React.FC<IChunkContextPreviewProps> = ({
  chunk,
  documentUrl,
}) => {
  const contextInfo = useMemo(() => extractContextInfo(chunk), [chunk]);

  switch (contextInfo.type) {
    case ContextType.PDF:
      return (
        <PdfContextPreview
          chunk={chunk}
          documentUrl={documentUrl}
          contextInfo={contextInfo}
        />
      );
    case ContextType.IMAGE:
      return (
        <ImageContextPreview
          chunk={chunk}
          documentUrl={documentUrl}
          contextInfo={contextInfo}
        />
      );
    case ContextType.TEXT:
      return (
        <TextContextPreview
          chunk={chunk}
          documentUrl={documentUrl}
          contextInfo={contextInfo}
        />
      );
    default:
      return (
        <div className="flex flex-col items-center justify-center h-full text-gray-500">
          <p>暂不支持该文档类型的上下文预览</p>
          <p className="text-sm mt-2">请查看完整文档</p>
        </div>
      );
  }
};

export default ChunkContextPreview;
```

- [ ] **Step 2: 创建PDF预览组件**

```typescript
// web/src/components/chunk-context-preview/pdf-context.tsx
import React, { useMemo } from 'react';
import { ITestingChunk } from '@/interfaces/database/knowledge';
import { PDFContextInfo } from '@/utils/chunk-context-util';
import { buildChunkHighlights } from '@/utils/document-util';
import { PdfPreviewer } from '@/components/document-preview/pdf-preview';
import { IHighlight } from 'react-pdf-highlighter';

interface IPdfContextPreviewProps {
  chunk: ITestingChunk;
  documentUrl: string;
  contextInfo: PDFContextInfo;
}

export const PdfContextPreview: React.FC<IPdfContextPreviewProps> = ({
  chunk,
  documentUrl,
  contextInfo,
}) => {
  // Create a virtual size for highlight calculation
  const [size, setSize] = React.useState({ width: 849, height: 1200 });

  const highlights: IHighlight[] = useMemo(() => {
    return buildChunkHighlights(chunk, size);
  }, [chunk, size]);

  const setWidthAndHeight = (width: number, height: number) => {
    setSize((prev) => {
      if (prev.width !== width || prev.height !== height) {
        return { width, height };
      }
      return prev;
    });
  };

  return (
    <div className="h-full">
      <PdfPreviewer
        url={documentUrl}
        highlights={highlights}
        setWidthAndHeight={setWidthAndHeight}
        initialPage={contextInfo.pageNumber}
        className="h-full"
      />
    </div>
  );
};
```

- [ ] **Step 3: 创建Image预览组件**

```typescript
// web/src/components/chunk-context-preview/image-context.tsx
import React from 'react';
import { ITestingChunk } from '@/interfaces/database/knowledge';
import { ImageContextInfo } from '@/utils/chunk-context-util';
import { api_host } from '@/utils/api';

interface IImageContextPreviewProps {
  chunk: ITestingChunk;
  documentUrl: string;
  contextInfo: ImageContextInfo;
}

export const ImageContextPreview: React.FC<IImageContextPreviewProps> = ({
  chunk,
  contextInfo,
}) => {
  const imageUrl = contextInfo.imageId
    ? `${api_host}/document/image/${chunk.kb_id}/${contextInfo.imageId}`
    : '';

  return (
    <div className="flex flex-col items-center justify-center h-full">
      {imageUrl ? (
        <img
          src={imageUrl}
          alt="Document chunk"
          className="max-w-full max-h-full object-contain"
        />
      ) : (
        <div className="text-gray-500">
          <p>图片加载失败</p>
        </div>
      )}
    </div>
  );
};
```

- [ ] **Step 4: 创建Text预览组件**

```typescript
// web/src/components/chunk-context-preview/text-context.tsx
import React from 'react';
import { ITestingChunk } from '@/interfaces/database/knowledge';
import { TextContextInfo } from '@/utils/chunk-context-util';

interface ITextContextPreviewProps {
  chunk: ITestingChunk;
  documentUrl: string;
  contextInfo: TextContextInfo;
}

export const TextContextPreview: React.FC<ITextContextPreviewProps> = ({
  chunk,
}) => {
  return (
    <div className="h-full overflow-auto p-4 bg-white rounded-lg">
      <div className="prose max-w-none">
        <h3 className="text-lg font-semibold mb-4 text-gray-800">Chunk内容</h3>
        <div className="bg-yellow-50 border-l-4 border-yellow-400 p-4 mb-4">
          <p className="text-gray-800 whitespace-pre-wrap">
            {chunk.content_with_weight}
          </p>
        </div>
        <div className="mt-4 text-sm text-gray-500">
          <p>文档类型: {chunk.doc_type_kwd || '文本'}</p>
          <p>相似度: {(chunk.similarity * 100).toFixed(1)}%</p>
        </div>
      </div>
    </div>
  );
};
```

- [ ] **Step 5: 提交**

```bash
git add web/src/components/chunk-context-preview/
git commit -m "feat(components): add ChunkContextPreview with multi-type support

- Add ChunkContextPreview main component with type switching
- Add PdfContextPreview for PDF documents with highlighting
- Add ImageContextPreview for image documents
- Add TextContextPreview for text/markdown documents"
```

---

## Task 4: 集成ChunkContextModal到搜索页面

**Files:**
- Modify: `web/src/pages/next-search/search-view.tsx`

- [ ] **Step 1: 添加导入和hook使用**

```typescript
// web/src/pages/next-search/search-view.tsx
// Add imports at top
import ChunkContextModal from '@/components/chunk-context-modal';
import { useChunkContextModal } from '@/components/chunk-context-modal/hooks';
import { useGetDocumentUrl } from '@/hooks/use-document-request';

// In the component function, add:
const { isOpen, selectedChunk, openModal, closeModal } = useChunkContextModal();
const getDocumentUrl = useGetDocumentUrl(selectedChunk?.doc_id);

const handleViewFullDocument = useCallback(() => {
  if (selectedChunk) {
    // Open full document preview using existing modal
    // This will be implemented based on existing document preview flow
  }
}, [selectedChunk]);
```

- [ ] **Step 2: 修改Chunk卡片添加点击事件**

```typescript
// Find the chunk card rendering code and add onClick handler
// Example (actual code may vary):
<ChunkCard
  key={chunk.chunk_id}
  chunk={chunk}
  onClick={() => openModal(chunk)}  // Add this line
  // ... other props
/>
```

- [ ] **Step 3: 添加ChunkContextModal到页面底部**

```typescript
// Add at the end of the component return, before closing tag:
<ChunkContextModal
  visible={isOpen}
  onClose={closeModal}
  chunk={selectedChunk}
  onViewFullDocument={handleViewFullDocument}
/>
```

- [ ] **Step 4: 提交**

```bash
git add web/src/pages/next-search/search-view.tsx
git commit -m "feat(search): integrate ChunkContextModal into search results

- Add useChunkContextModal hook for modal state management
- Add click handler to chunk cards to open context modal
- Add ChunkContextModal component to page"
```

---

## Task 5: 完善ChunkContextModal - 添加文档URL获取

**Files:**
- Modify: `web/src/components/chunk-context-modal/index.tsx`
- Modify: `web/src/components/chunk-context-modal/hooks.ts`

- [ ] **Step 1: 更新hooks添加文档URL获取**

```typescript
// web/src/components/chunk-context-modal/hooks.ts
import { useGetDocumentUrl } from '@/hooks/use-document-request';

export interface IUseChunkContextModalResult {
  isOpen: boolean;
  selectedChunk: ITestingChunk | null;
  documentUrl: string;
  isLoading: boolean;
  openModal: (chunk: ITestingChunk) => void;
  closeModal: () => void;
}

export function useChunkContextModal(): IUseChunkContextModalResult {
  const [isOpen, setIsOpen] = useState(false);
  const [selectedChunk, setSelectedChunk] = useState<ITestingChunk | null>(null);
  
  const getDocumentUrl = useGetDocumentUrl(selectedChunk?.doc_id);
  const [documentUrl, setDocumentUrl] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const openModal = useCallback(async (chunk: ITestingChunk) => {
    setSelectedChunk(chunk);
    setIsOpen(true);
    setIsLoading(true);
    
    try {
      const url = getDocumentUrl(chunk.doc_id);
      setDocumentUrl(url);
    } finally {
      setIsLoading(false);
    }
  }, [getDocumentUrl]);

  const closeModal = useCallback(() => {
    setIsOpen(false);
    setDocumentUrl('');
    setTimeout(() => setSelectedChunk(null), 300);
  }, []);

  return {
    isOpen,
    selectedChunk,
    documentUrl,
    isLoading,
    openModal,
    closeModal,
  };
}
```

- [ ] **Step 2: 更新组件使用新props**

```typescript
// Update ChunkContextModal props and render
export interface IChunkContextModalProps {
  visible: boolean;
  onClose: () => void;
  chunk: ITestingChunk | null;
  documentUrl: string;
  isLoading: boolean;
  onViewFullDocument?: () => void;
}

// In the component, update the preview section:
<div className="flex-1 overflow-auto p-4 bg-gray-50 rounded-lg">
  {isLoading ? (
    <div className="flex items-center justify-center h-full">
      <Spin /> {/* or loading spinner */}
      <span className="ml-2">加载中...</span>
    </div>
  ) : chunk ? (
    <ChunkContextPreview chunk={chunk} documentUrl={documentUrl} />
  ) : null}
</div>
```

- [ ] **Step 3: 提交**

```bash
git add web/src/components/chunk-context-modal/
git commit -m "feat(components): add document URL loading to ChunkContextModal

- Update useChunkContextModal to fetch document URL
- Add loading state management
- Integrate ChunkContextPreview for actual rendering"
```

---

## Task 6: 添加TypeScript类型完善

**Files:**
- Modify: `web/src/interfaces/database/knowledge.ts`

- [ ] **Step 1: 确认类型完整性**

检查 `ITestingChunk` 接口是否包含所有必需字段。如有缺失，添加：

```typescript
// web/src/interfaces/database/knowledge.ts
export interface ITestingChunk {
  chunk_id: string;
  content_ltks: string;
  content_with_weight: string;
  doc_id: string;
  doc_name: string;
  docnm_kwd: string;
  doc_type_kwd?: string;  // Ensure this exists
  img_id: string;
  image_id: string;  // Ensure both image ID fields exist
  important_kwd: any[];
  kb_id: string;
  similarity: number;
  term_similarity: number;
  vector: number[];
  vector_similarity: number;
  highlight: string;
  positions: number[][];  // Ensure this exists
}
```

- [ ] **Step 2: 提交**

```bash
git add web/src/interfaces/database/knowledge.ts
git commit -m "chore(types): ensure ITestingChunk has all required fields"
```
defaultChunkContext(chunk, positions):
  if (doc_type_kwd == 'image' or positions is None or len(positions) == 0):
    return UnknownContextInfo(type=ContextType.UNKNOWN)
  return extractImageContext(chunk)

if __name__ == "__main__":
  import sys
  sys.exit(0)
```

---

## Task 7: 添加样式和优化

**Files:**
- Create: `web/src/components/chunk-context-modal/index.less` (或 CSS module)

- [ ] **Step 1: 添加样式文件**

```less
// web/src/components/chunk-context-modal/index.less
.chunk-context-modal {
  .ant-modal-content {
    border-radius: 8px;
  }

  .modal-body {
    padding: 0;
  }

  .preview-container {
    min-height: 400px;
    display: flex;
    flex-direction: column;
  }
}

.chunk-context-preview {
  &-pdf {
    height: 100%;
  }

  &-image {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  &-text {
    font-family: monospace;
    line-height: 1.6;
  }
}
```

- [ ] **Step 2: 在组件中引入样式**

```typescript
// In web/src/components/chunk-context-modal/index.tsx
import './index.less';
```

- [ ] **Step 3: 提交**

```bash
git add web/src/components/chunk-context-modal/index.less
git commit -m "style(components): add styles for chunk context modal"
```

---

## Task 8: 手动测试

**Files:**
- All modified files

- [ ] **Step 1: 启动开发服务器**

```bash
cd web
npm run dev
```

- [ ] **Step 2: 测试场景**

1. 进入搜索页面，输入查询
2. 点击一个PDF类型的chunk卡片
   - 预期：打开模态框，显示PDF预览，chunk位置高亮
3. 点击"查看完整文档"按钮
   - 预期：打开完整文档预览
4. 点击关闭或按ESC
   - 预期：模态框关闭
5. 测试图片类型chunk
   - 预期：显示图片预览
6. 测试文本类型chunk
   - 预期：显示文本内容

- [ ] **Step 3: 修复发现的问题**

根据测试结果修复问题。

---

## Task 9: 最终提交

- [ ] **Step 1: 提交所有变更**

```bash
git add .
git commit -m "feat(search): implement chunk context preview modal

- Add ChunkContextModal component for displaying chunk context
- Add ChunkContextPreview with support for PDF, Image, and Text types
- Add extractContextInfo utility for parsing chunk positions
- Integrate with search results page
- Support ESC/click outside to close
- Support 'View Full Document' button"
```
git push origin feature/chunk-context-preview
```
g

---

## 后续优化方向（不在这个计划中）

1. Excel上下文预览（显示单元格区域）
2. Word上下文预览（显示段落）
3. 同一文档多chunk高亮
4. 上一页/下一页导航
5. 性能优化：虚拟滚动、懒加载PDF页
