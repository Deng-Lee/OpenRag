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

export interface ChunkInfo {
  chunk_id: string;
  content: string;
  file_id: number;
  filename: string;
  file_type?: string;
  positions?: number[][];
  score: number;
  metadata?: Record<string, any>;
}

/**
 * 根据chunk信息提取上下文数据
 * @param chunk - chunk数据
 * @returns 上下文信息
 */
export function extractContextInfo(chunk: ChunkInfo): ContextInfo {
  const fileType = chunk.file_type?.toLowerCase() || '';
  const filename = chunk.filename?.toLowerCase() || '';

  // 先从file_type判断，如果没有则从filename后缀判断
  let docType = fileType;
  if (!docType && filename) {
    const ext = filename.split('.').pop();
    if (ext) docType = ext.toLowerCase();
  }

  switch (docType) {
    case 'pdf':
      return extractPDFContext(chunk);
    case 'image':
    case 'jpg':
    case 'jpeg':
    case 'png':
    case 'gif':
    case 'bmp':
    case 'svg':
      return extractImageContext(chunk);
    case 'txt':
    case 'md':
    case 'markdown':
    case 'text':
      return extractTextContext(chunk);
    case 'xlsx':
    case 'xls':
    case 'csv':
      return extractExcelContext(chunk);
    case 'doc':
    case 'docx':
      return extractWordContext(chunk);
    default:
      // 尝试从文件名判断
      if (filename.endsWith('.pdf')) return extractPDFContext(chunk);
      if (/\.(jpg|jpeg|png|gif|bmp|svg)$/i.test(filename)) return extractImageContext(chunk);
      if (/\.(txt|md|markdown)$/i.test(filename)) return extractTextContext(chunk);
      if (/\.(xlsx|xls|csv)$/i.test(filename)) return extractExcelContext(chunk);
      if (/\.(doc|docx)$/i.test(filename)) return extractWordContext(chunk);
      return { type: ContextType.UNKNOWN };
  }
}

function extractPDFContext(chunk: ChunkInfo): PDFContextInfo {
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

function extractImageContext(_chunk: ChunkInfo): ImageContextInfo {
  return {
    type: ContextType.IMAGE,
    imageId: _chunk.file_id?.toString() || '',
  };
}

function extractTextContext(_chunk: ChunkInfo): TextContextInfo {
  // For text files, return a default range
  return {
    type: ContextType.TEXT,
    lineStart: 1,
    lineEnd: 100,
  };
}

function extractExcelContext(_chunk: ChunkInfo): ExcelContextInfo {
  return {
    type: ContextType.EXCEL,
    cellRange: 'A1:Z100',
  };
}

function extractWordContext(_chunk: ChunkInfo): WordContextInfo {
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
export function hasValidPosition(chunk: ChunkInfo): boolean {
  if (!chunk.positions || chunk.positions.length === 0) {
    return false;
  }

  const fileType = chunk.file_type?.toLowerCase() || '';

  if (fileType === 'pdf' || chunk.filename?.toLowerCase().endsWith('.pdf')) {
    return chunk.positions[0].length >= 5;
  }

  return true;
}

/**
 * 获取文件类型的显示名称
 * @param fileType - 文件类型
 * @returns 显示名称
 */
export function getFileTypeDisplayName(fileType?: string): string {
  if (!fileType) return '未知类型';

  const typeMap: Record<string, string> = {
    'pdf': 'PDF文档',
    'image': '图片',
    'jpg': '图片',
    'jpeg': '图片',
    'png': '图片',
    'gif': '图片',
    'txt': '文本文件',
    'md': 'Markdown文档',
    'xlsx': 'Excel表格',
    'xls': 'Excel表格',
    'csv': 'CSV文件',
    'doc': 'Word文档',
    'docx': 'Word文档',
  };

  return typeMap[fileType.toLowerCase()] || fileType.toUpperCase();
}
