import { useMemo } from 'react';
import { Typography, Tag } from 'antd';
import {
  extractContextInfo,
  ContextType,
  type ChunkInfo,
  type ContextInfo,
  getFileTypeDisplayName,
} from '../../utils/chunk-context-util';

const { Text, Paragraph, Title } = Typography;

export interface ChunkContextPreviewProps {
  chunk: ChunkInfo;
  documentUrl?: string;
}

export default function ChunkContextPreview({
  chunk,
  documentUrl,
}: ChunkContextPreviewProps) {
  const contextInfo = useMemo(() => extractContextInfo(chunk), [chunk]);

  switch (contextInfo.type) {
    case ContextType.PDF:
      return <PDFContextPreview chunk={chunk} contextInfo={contextInfo} />;
    case ContextType.IMAGE:
      return <ImageContextPreview chunk={chunk} documentUrl={documentUrl} />;
    case ContextType.TEXT:
      return <TextContextPreview chunk={chunk} />;
    case ContextType.EXCEL:
      return <ExcelContextPreview chunk={chunk} />;
    case ContextType.WORD:
      return <WordContextPreview chunk={chunk} />;
    default:
      return <UnknownContextPreview chunk={chunk} />;
  }
}

// PDF Context Preview
interface PDFContextPreviewProps {
  chunk: ChunkInfo;
  contextInfo: Extract<ContextInfo, { type: ContextType.PDF }>;
}

function PDFContextPreview({ chunk, contextInfo }: PDFContextPreviewProps) {
  return (
    <div style={{ height: '100%' }}>
      <div
        style={{
          backgroundColor: '#fff',
          borderRadius: 4,
          padding: 16,
          marginBottom: 16,
        }}
      >
        <div style={{ marginBottom: 12 }}>
          <Tag color="blue">PDF文档</Tag>
          <Tag color="green">第 {contextInfo.pageNumber} 页</Tag>
          <Tag color="orange">相似度: {(chunk.score * 100).toFixed(1)}%</Tag>
        </div>
        <div
          style={{
            backgroundColor: '#fffbe6',
            border: '1px solid #ffe58f',
            borderRadius: 4,
            padding: 12,
            marginBottom: 12,
          }}
        >
          <Text strong style={{ marginBottom: 8, display: 'block' }}>
            Chunk内容（高亮区域）:
          </Text>
          <Paragraph style={{ margin: 0 }}>{chunk.content}</Paragraph>
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          位置坐标: x={contextInfo.coordinates.x.toFixed(0)}, y=
          {contextInfo.coordinates.y.toFixed(0)}, width=
          {contextInfo.coordinates.width.toFixed(0)}, height=
          {contextInfo.coordinates.height.toFixed(0)}
        </Text>
      </div>
      <div
        style={{
          backgroundColor: '#e6f7ff',
          borderRadius: 4,
          padding: 16,
          textAlign: 'center',
        }}
      >
        <Text type="secondary">
          📄 PDF预览功能需要接入PDF查看器组件
          <br />
          将在此显示第 {contextInfo.pageNumber} 页的预览，并高亮显示chunk位置
        </Text>
      </div>
    </div>
  );
}

// Image Context Preview
interface ImageContextPreviewProps {
  chunk: ChunkInfo;
  documentUrl?: string;
}

function ImageContextPreview({ chunk, documentUrl }: ImageContextPreviewProps) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100%',
      }}
    >
      {documentUrl ? (
        <img
          src={documentUrl}
          alt={chunk.filename}
          style={{
            maxWidth: '100%',
            maxHeight: 350,
            objectFit: 'contain',
            borderRadius: 4,
          }}
        />
      ) : (
        <div
          style={{
            width: 200,
            height: 200,
            backgroundColor: '#f0f0f0',
            borderRadius: 4,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <Text type="secondary">图片预览</Text>
        </div>
      )}
      <div style={{ marginTop: 16, textAlign: 'center' }}>
        <Tag color="blue">{getFileTypeDisplayName(chunk.file_type)}</Tag>
        <Tag color="orange">相似度: {(chunk.score * 100).toFixed(1)}%</Tag>
      </div>
      <div
        style={{
          marginTop: 12,
          backgroundColor: '#fffbe6',
          border: '1px solid #ffe58f',
          borderRadius: 4,
          padding: 12,
          maxWidth: 400,
        }}
      >
        <Text>{chunk.content}</Text>
      </div>
    </div>
  );
}

// Text Context Preview
function TextContextPreview({ chunk }: { chunk: ChunkInfo }) {
  return (
    <div
      style={{
        height: '100%',
        overflow: 'auto',
        padding: 16,
        backgroundColor: '#fff',
        borderRadius: 4,
      }}
    >
      <div style={{ marginBottom: 12 }}>
        <Tag color="blue">{getFileTypeDisplayName(chunk.file_type)}</Tag>
        <Tag color="orange">相似度: {(chunk.score * 100).toFixed(1)}%</Tag>
      </div>
      <div
        style={{
          backgroundColor: '#fffbe6',
          borderLeft: '4px solid #ffd666',
          padding: 12,
          marginBottom: 16,
          borderRadius: '0 4px 4px 0',
        }}
      >
        <Title level={5} style={{ marginBottom: 8 }}>
          Chunk内容
        </Title>
        <Paragraph style={{ margin: 0, fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>
          {chunk.content}
        </Paragraph>
      </div>
      {chunk.metadata && Object.keys(chunk.metadata).length > 0 && (
        <div style={{ marginTop: 16 }}>
          <Text strong>元数据:</Text>
          <pre
            style={{
              backgroundColor: '#f5f5f5',
              padding: 8,
              borderRadius: 4,
              fontSize: 12,
            }}
          >
            {JSON.stringify(chunk.metadata, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

// Excel Context Preview
function ExcelContextPreview({ chunk }: { chunk: ChunkInfo }) {
  return (
    <div style={{ padding: 16 }}>
      <div style={{ marginBottom: 12 }}>
        <Tag color="blue">Excel表格</Tag>
        <Tag color="orange">相似度: {(chunk.score * 100).toFixed(1)}%</Tag>
      </div>
      <div
        style={{
          backgroundColor: '#f6ffed',
          border: '1px solid #b7eb8f',
          borderRadius: 4,
          padding: 12,
          marginBottom: 12,
        }}
      >
        <Text strong style={{ marginBottom: 8, display: 'block' }}>
          单元格内容:
        </Text>
        <Paragraph>{chunk.content}</Paragraph>
      </div>
      <Text type="secondary">
        📊 Excel预览功能将显示相关单元格区域
      </Text>
    </div>
  );
}

// Word Context Preview
function WordContextPreview({ chunk }: { chunk: ChunkInfo }) {
  return (
    <div style={{ padding: 16 }}>
      <div style={{ marginBottom: 12 }}>
        <Tag color="blue">Word文档</Tag>
        <Tag color="orange">相似度: {(chunk.score * 100).toFixed(1)}%</Tag>
      </div>
      <div
        style={{
          backgroundColor: '#fff',
          border: '1px solid #d9d9d9',
          borderRadius: 4,
          padding: 16,
          marginBottom: 12,
        }}
      >
        <Paragraph>{chunk.content}</Paragraph>
      </div>
      <Text type="secondary">
        📝 Word预览功能将显示包含该chunk的段落
      </Text>
    </div>
  );
}

// Unknown Type Preview
function UnknownContextPreview({ chunk }: { chunk: ChunkInfo }) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100%',
        textAlign: 'center',
      }}
    >
      <div
        style={{
          fontSize: 48,
          marginBottom: 16,
        }}
      >
        📄
      </div>
      <Text strong style={{ fontSize: 16, marginBottom: 8 }}>
        暂不支持该文档类型的上下文预览
      </Text>
      <Text type="secondary" style={{ marginBottom: 16 }}>
        文件类型: {chunk.file_type || '未知'}
      </Text>
      <div
        style={{
          backgroundColor: '#f5f5f5',
          borderRadius: 4,
          padding: 16,
          maxWidth: 400,
        }}
      >
        <Text strong style={{ marginBottom: 8, display: 'block' }}>
          Chunk内容:
        </Text>
        <Paragraph ellipsis={{ rows: 5 }}>{chunk.content}</Paragraph>
      </div>
    </div>
  );
}
