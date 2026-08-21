// 文件夹上传纯逻辑（与 React/DOM 解耦，便于单测）。
// 详见 docs/2026-06-22-folder-upload-diagnosis.md「完整修复方案」。

export interface PickedFile {
  file: File;
  /** 相对路径（含文件名，顶层为被拖文件夹名），如 "报告/2024/Q1.pdf" */
  relativePath: string;
}

export type SkipReason = 'junk' | 'unsupported' | 'too_large' | 'name_too_long';

export interface SkippedItem {
  rel: string;
  reason: SkipReason;
  /** unsupported 时携带扩展名，便于提示 */
  ext?: string;
}

export const DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024;

// 叶子文件名字节上限，与后端 file_ingest.py 的 MAX_FILENAME_BYTES 一致。
// 后端 worker 落盘临时文件名受操作系统单分量 255 字节限制，按字节判断（中文每字 3 字节）。
export const MAX_FILENAME_BYTES = 200;

/** UTF-8 字节长度（与后端按字节判断一致；中文每字 3 字节）。 */
export function filenameBytes(name: string): number {
  return new TextEncoder().encode(name).length;
}

// 与后端 SUPPORTED_PARSER_TYPES / _EXTENSION_MIME_OVERRIDES 对齐
export const SUPPORTED_EXTENSIONS = new Set([
  'pdf', 'docx', 'doc', 'xlsx', 'xls', 'pptx', 'ppt',
  'txt', 'md', 'markdown', 'html', 'htm', 'json', 'csv', 'epub',
]);

const SKIP_DIR_SEGMENTS = new Set(['__MACOSX', '.git', '.svn', '.hg', 'node_modules']);
const SKIP_FILE_NAMES = new Set(['.DS_Store', 'Thumbs.db', 'desktop.ini']);

/** 逻辑目录归一：前导 /，去尾部 /，根为 /；兼容 Windows 反斜杠。 */
export function normalizeDir(p: string): string {
  const s = (p || '/').trim().replace(/\\/g, '/');
  if (!s || s === '/') return '/';
  const withLead = s.startsWith('/') ? s : '/' + s;
  return withLead.replace(/\/+$/, '') || '/';
}

/** base=选中的上传目标目录；relativePath=相对路径（含文件名）。返回该文件的远端父目录。 */
export function remoteParentDir(base: string, relativePath: string): string {
  const b = normalizeDir(base);
  const relParts = relativePath.split('/').filter(Boolean);
  relParts.pop(); // 去掉文件名，余下为相对父目录
  const child = relParts.join('/');
  if (!child) return b;
  return b === '/' ? `/${child}` : `${b}/${child}`;
}

/** 任一目录段为隐藏/系统目录，或文件本身为隐藏/系统文件，则判为垃圾。 */
export function isJunkPath(relativePath: string): boolean {
  const segs = relativePath.split('/').filter(Boolean);
  const name = segs[segs.length - 1] ?? '';
  for (let i = 0; i < segs.length - 1; i++) {
    if (segs[i].startsWith('.') || SKIP_DIR_SEGMENTS.has(segs[i])) return true;
  }
  return name.startsWith('.') || SKIP_FILE_NAMES.has(name);
}

/** 客户端预检：分出可上传项与跳过项（按路径段过滤垃圾、扩展名白名单、大小上限）。 */
export function precheck(
  items: PickedFile[],
  maxFileSize: number = DEFAULT_MAX_FILE_SIZE,
): { accepted: PickedFile[]; skipped: SkippedItem[] } {
  const accepted: PickedFile[] = [];
  const skipped: SkippedItem[] = [];
  for (const it of items) {
    const rel = it.relativePath;
    const name = rel.split('/').pop() || '';
    if (isJunkPath(rel)) {
      skipped.push({ rel, reason: 'junk' });
      continue;
    }
    const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : '';
    if (!SUPPORTED_EXTENSIONS.has(ext)) {
      skipped.push({ rel, reason: 'unsupported', ext });
      continue;
    }
    if (it.file.size > maxFileSize) {
      skipped.push({ rel, reason: 'too_large' });
      continue;
    }
    if (filenameBytes(name) > MAX_FILENAME_BYTES) {
      skipped.push({ rel, reason: 'name_too_long' });
      continue;
    }
    accepted.push(it);
  }
  return { accepted, skipped };
}

/** 仅当 400 且报文明确为「已存在」时才算重复（精确匹配，勿用过宽 /exist/i）。 */
export function isDuplicateError(status: number | undefined, detail: string): boolean {
  return status === 400 && /already exists/i.test(detail);
}

/** 后端「文件名过长」400：detail 以 "File name too long" 开头（见 file_ingest.py）。 */
export function isFilenameTooLongError(status: number | undefined, detail: string): boolean {
  return status === 400 && /File name too long/i.test(detail);
}

/**
 * 递归读取拖入的 FileSystemEntry，产出 {file, relativePath}。
 * 注意：readEntries 单次最多约 100 项，必须循环读到空，否则大目录会丢文件。
 */
export function walkEntry(entry: any, prefix: string, out: PickedFile[]): Promise<void> {
  return new Promise((resolve) => {
    if (entry?.isFile) {
      entry.file(
        (file: File) => {
          out.push({ file, relativePath: prefix + entry.name });
          resolve();
        },
        () => resolve(),
      );
    } else if (entry?.isDirectory) {
      const reader = entry.createReader();
      const children: any[] = [];
      const readBatch = () => {
        reader.readEntries(
          async (batch: any[]) => {
            if (batch.length === 0) {
              for (const child of children) {
                await walkEntry(child, `${prefix}${entry.name}/`, out);
              }
              resolve();
            } else {
              children.push(...batch);
              readBatch();
            }
          },
          () => resolve(),
        );
      };
      readBatch();
    } else {
      resolve();
    }
  });
}
