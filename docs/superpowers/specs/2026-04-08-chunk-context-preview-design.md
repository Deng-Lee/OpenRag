# Chunk上下文预览功能设计文档

## 1. 功能概述

在搜索页面，当用户点击搜索结果中的chunk卡片时，弹出一个模态框，显示该chunk在原文档中的上下文位置，并高亮显示chunk。用户可以进一步点击查看完整文档。

## 2. 需求澄清结果

| 问题 | 答案 |
|------|------|
| 预览显示内容 | chunk内容 + 原文档上下文预览（高亮显示chunk位置） |
| 触发方式 | 点击整个chunk卡片 |
| 预览形式 | 大尺寸居中模态框，支持ESC/点击空白关闭 |
| 高亮策略 | 只高亮当前点击的chunk |
| 文档类型支持 | 支持所有类型，根据类型智能显示上下文 |
| 加载策略 | 懒加载：先显示chunk和上下文，"查看完整文档"按钮按需加载 |
| 上下文范围 | 智能上下文：根据文档类型显示合适范围 |

## 3. 架构设计

### 3.1 组件关系

```
ChunkCard (点击触发)
    ↓
ChunkContextModal (新组件)
    ├── 智能上下文计算模块
    │   ├── PDF: 提取chunk所在页码，加载单页
    │   ├── Word: 提取段落上下文
    │   ├── Excel: 提取单元格区域
    │   ├── Markdown/文本: 提取前后N行
    │   └── 图片: 直接显示原图
    ├── 高亮渲染 (复用buildChunkHighlights)
    └── 操作栏
        ├── "查看完整文档" 按钮 → 打开原DocumentPreviewModal
        └── 关闭按钮
```

### 3.2 数据流

```
1. 用户点击ChunkCard
   ↓
2. 打开ChunkContextModal
   - 显示文档名称（标题）
   - 显示智能上下文预览（主体）
   - 显示操作栏（底部）
   ↓
3. 用户看到chunk在原文档中的上下文环境
   - chunk位置被高亮显示
   ↓
4. 可选操作
   - 点击查看完整文档 → 打开DocumentPreviewModal
   - 点击关闭/ESC → 关闭模态框
```

## 4. 组件设计

### 4.1 新增组件

#### ChunkContextModal

**位置**: `web/src/components/chunk-context-modal/index.tsx`

**Props**:
```typescript
interface IChunkContextModalProps {
  visible: boolean;
  onClose: () => void;
  chunk: ITestingChunk;
  onViewFullDocument: () => void;
}
```

**职责**:
- 模态框容器（居中，支持ESC/点击关闭）
- 协调子组件：标题、预览内容、操作栏
- 管理文档URL获取状态

#### ChunkContextPreview

**位置**: `web/src/components/chunk-context-preview/index.tsx`

**Props**:
```typescript
interface IChunkContextPreviewProps {
  chunk: ITestingChunk;
  documentUrl: string;
}
```

**职责**:
- 根据doc_type_kwd决定预览策略
- 渲染对应类型的上下文预览
- 集成高亮渲染

### 4.2 修改的组件

#### ChunkCard

**位置**: `web/src/pages/next-search/search-view.tsx`（或相关位置）

**修改**:
- 添加onClick事件处理
- 触发打开ChunkContextModal

### 4.3 复用的现有能力

| 能力 | 来源 | 用途 |
|------|------|------|
| useGetDocumentUrl | hooks/use-document-request.ts | 获取文档URL |
| buildChunkHighlights | utils/document-util.ts | 构建高亮数据 |
| DocumentPreview | components/document-preview | 完整文档预览（通过onViewFullDocument调用） |
| PdfPreviewer | components/document-preview/pdf-preview | PDF预览和高亮 |
| Modal | components/ui/modal | 模态框容器 |

## 5. 智能上下文策略

根据`doc_type_kwd`字段决定如何提取和显示上下文：

| 文档类型 | 上下文提取策略 | 高亮方式 | 技术实现 |
|---------|--------------|---------|---------|
| `pdf` | 提取positions中的页码，加载单页 | PDF高亮区域 | 复用PdfPreviewer |
| `image` | 直接显示原图 | 图片高亮框（如有坐标） | ImagePreviewer |
| `txt`/`md` | 提取前后各N个token | 文本高亮 | 自定义渲染 |
| `xlsx` | 提取单元格区域 | 单元格高亮 | ExcelCsvPreviewer |
| `doc`/`docx` | 提取段落上下文 | 段落高亮 | DocPreviewer（需扩展） |

## 6. 状态管理

- `isModalOpen`: boolean - 控制模态框显隐
- `selectedChunk`: ITestingChunk | null - 当前选中的chunk
- `isLoadingFullDocument`: boolean - 完整文档加载状态（用于"查看完整文档"按钮）

使用React hooks管理，无需引入全局状态。

## 7. 接口与类型

复用现有类型：
- `ITestingChunk` from `interfaces/database/knowledge.ts`
- `IHighlight` from `react-pdf-highlighter`

## 8. 边界情况处理

| 场景 | 处理策略 |
|------|---------|
| chunk没有positions信息 | 只显示chunk内容，提示"无法定位原文档位置" |
| 文档URL获取失败 | 显示错误信息，提供重试按钮 |
| 文档类型不支持预览 | 只显示chunk内容，隐藏"查看完整文档"按钮 |
| 高亮数据构建失败 | 正常显示上下文，不显示高亮 |

## 9. 性能考虑

1. **懒加载**：模态框打开时才获取文档URL
2. **缓存**：使用React Query缓存文档URL，避免重复请求
3. **按需渲染**：只有PDF等需要复杂渲染的类型才加载对应预览器
4. **清理**：模态框关闭时清理临时状态

## 10. 可访问性

1. **键盘导航**：支持ESC关闭模态框
2. **焦点管理**：打开模态框时焦点移入，关闭时恢复
3. **ARIA标签**：模态框添加适当的aria-label

## 11. 测试要点

1. 各种文档类型的预览是否正确
2. 高亮是否准确显示
3. "查看完整文档"按钮是否正常跳转
4. 边界情况（无positions、URL失败等）
5. 键盘交互（ESC关闭）

## 12. 实现顺序

1. 创建ChunkContextModal基础结构
2. 实现PDF类型的上下文预览（最成熟）
3. 实现图片类型预览
4. 实现文本/Markdown类型
5. 实现Excel类型
6. 实现Word类型
7. 集成到搜索页面
8. 添加测试

---

**创建日期**: 2026-04-08
**作者**: Claude
**状态**: 待实现
