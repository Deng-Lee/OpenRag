import type { File } from '../types';

export type FilePreviewCategory =
  | 'pdf'
  | 'image'
  | 'markdown'
  | 'html'
  | 'text'
  | 'office'
  | 'unsupported';

const OFFICE_MIMES = new Set([
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/msword',
  'application/vnd.ms-powerpoint',
  'application/vnd.ms-excel',
]);

/** 根据 mime / 扩展名判断【文件】页预览策略（目录不在此列） */
export function classifyPreviewFile(file: File): FilePreviewCategory {
  if (file.is_directory) return 'unsupported';

  const mime = (file.mime_type || '').toLowerCase();
  const name = (file.name || '').toLowerCase();

  if (mime === 'application/pdf' || name.endsWith('.pdf')) return 'pdf';
  if (mime.startsWith('image/')) return 'image';

  if (mime === 'text/markdown' || name.endsWith('.md')) return 'markdown';
  if (mime === 'text/html' || name.endsWith('.html') || name.endsWith('.htm')) return 'html';

  if (mime.startsWith('text/') || mime === 'application/json') return 'text';
  if (name.endsWith('.json') || name.endsWith('.csv') || name.endsWith('.txt')) return 'text';

  if (OFFICE_MIMES.has(mime)) return 'office';
  if (name.endsWith('.docx') || name.endsWith('.pptx') || name.endsWith('.xlsx')) return 'office';
  if (name.endsWith('.doc') || name.endsWith('.ppt') || name.endsWith('.xls')) return 'office';

  return 'unsupported';
}
