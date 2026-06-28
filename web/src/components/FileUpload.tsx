import { useState, useEffect, useMemo, useRef } from 'react';
import { Upload, message, Select, Space, Card, Form, Input, Button, Modal, Tree, Progress, List } from 'antd';
import { InboxOutlined, FolderOutlined } from '@ant-design/icons';
import type { UploadProps } from 'antd';
import type { DataNode } from 'antd/es/tree';
import { useTranslation } from 'react-i18next';
import { filesAPI } from '../services/api';
import { shouldBlockTaggedBatch } from './fileUploadGuard';
import { classifyUploadError } from './fileUploadError';
import type { DocumentType, File } from '../types';
import {
  precheck,
  remoteParentDir,
  isDuplicateError,
  walkEntry,
  type PickedFile,
  type SkipReason,
} from '../utils/folderUpload';

type ItemStatus = 'uploaded' | 'skipped' | 'failed';
interface ItemResult {
  rel: string;
  status: ItemStatus;
  reason?: string;
}

const { Dragger } = Upload;
const { Option } = Select;

const PARSER_TYPES = [
  { value: 'auto', label: '自动检测 (Auto)' },
  { value: 'pdf', label: 'PDF 文档' },
  { value: 'docx', label: 'Word 文档 (.docx)' },
  { value: 'xlsx', label: 'Excel 表格 (.xlsx)' },
  { value: 'pptx', label: 'PowerPoint (.pptx)' },
  { value: 'txt', label: '纯文本 (.txt)' },
  { value: 'md', label: 'Markdown (.md)' },
  { value: 'html', label: 'HTML 网页' },
  { value: 'json', label: 'JSON 数据' },
  { value: 'csv', label: 'CSV 表格' },
  { value: 'epub', label: 'EPUB 电子书' },
];

const DOCUMENT_TYPES: DocumentType[] = ['general', 'manual', 'laws'];

/** 与 Files 页目录树一致：仅文件夹节点，用于上传目标路径选择。 */
function buildDirectoryOnlyTree(files: File[], rootTitle: string): DataNode[] {
  const root: DataNode = {
    title: rootTitle,
    key: '/',
    icon: <FolderOutlined />,
    children: [],
  };
  const pathMap = new Map<string, DataNode>();
  pathMap.set('/', root);

  files.forEach((f) => {
    if (f.is_directory) {
      const node: DataNode = {
        title: f.name || f.uri.split('/').pop() || f.uri,
        key: f.uri,
        icon: <FolderOutlined />,
        children: [],
      };
      pathMap.set(f.uri, node);
    }
  });

  files.forEach((f) => {
    const parts = f.uri.split('/').filter(Boolean);
    const dirParts = f.is_directory ? parts : parts.slice(0, -1);
    let currentPath = '';
    for (let i = 0; i < dirParts.length; i++) {
      const part = dirParts[i];
      currentPath = currentPath ? `${currentPath}/${part}` : `/${part}`;
      if (!pathMap.has(currentPath)) {
        const node: DataNode = {
          title: part,
          key: currentPath,
          icon: <FolderOutlined />,
          children: [],
        };
        pathMap.set(currentPath, node);
      }
    }
  });

  pathMap.forEach((node, path) => {
    if (path === '/') return;
    const parts = path.split('/').filter(Boolean);
    if (parts.length === 0) return;
    parts.pop();
    const parentPath = parts.length === 0 ? '/' : `/${parts.join('/')}`;
    const parent = pathMap.get(parentPath);
    if (parent?.children && !parent.children.some((child) => child.key === node.key)) {
      parent.children.push(node);
    }
  });

  return [root];
}

interface FileUploadProps {
  onUploadSuccess: () => void;
  workspaceId?: number;
  canWrite?: boolean;
  selectedPath?: string;
}

export default function FileUpload({
  onUploadSuccess,
  workspaceId = 1,
  canWrite = false,
  selectedPath = '/',
}: FileUploadProps) {
  const { t } = useTranslation();
  const [parserType, setParserType] = useState<string>('auto');
  const [documentType, setDocumentType] = useState<DocumentType>('general');
  const [tag, setTag] = useState<string>('');
  const [uploadPath, setUploadPath] = useState<string>(selectedPath);
  const [uploading, setUploading] = useState(false);
  const [dirPickerOpen, setDirPickerOpen] = useState(false);
  const [fetchedFiles, setFetchedFiles] = useState<File[]>([]);

  useEffect(() => {
    setUploadPath(selectedPath);
  }, [selectedPath]);

  useEffect(() => {
    if (!dirPickerOpen || !workspaceId) {
      return;
    }
    let cancelled = false;
    filesAPI
      .list(workspaceId, { underPath: '/', limit: 10_000 })
      .then((data) => {
        if (!cancelled) setFetchedFiles(data);
      })
      .catch(() => {
        if (!cancelled) message.error(t('files.messages.list_failed'));
      });
    return () => {
      cancelled = true;
    };
  }, [dirPickerOpen, workspaceId, t]);

  const directoryTree = useMemo(
    () => buildDirectoryOnlyTree(fetchedFiles, t('files.upload.root')),
    [fetchedFiles, t]
  );

  // —— 文件夹拖拽上传：在现有上传区 drop 时自动识别目录并接管（不动 input、不加 directory:true）——
  const uploadPathRef = useRef(uploadPath);
  const workspaceIdRef = useRef(workspaceId);
  const documentTypeRef = useRef(documentType);
  const tagRef = useRef('');
  const uploadingRef = useRef(false); // 重入锁：同步读写，不靠 effect
  const runFolderUploadRef = useRef<(items: PickedFile[]) => Promise<void>>(async () => {});
  const dropZoneRef = useRef<HTMLDivElement>(null);
  const [folderProgress, setFolderProgress] = useState<{ total: number; done: number } | null>(null);

  // 渲染期同步赋值——事件触发时一定拿到最新闭包/值
  uploadPathRef.current = uploadPath;
  workspaceIdRef.current = workspaceId;
  documentTypeRef.current = documentType;
  tagRef.current = tag;

  const skipReasonText = (reason: SkipReason, ext?: string): string => {
    if (reason === 'unsupported') return t('files.upload.skip_unsupported', { ext: ext ? `.${ext}` : '' });
    if (reason === 'too_large') return t('files.upload.skip_too_large');
    if (reason === 'name_too_long') return t('files.upload.skip_name_too_long');
    return t('files.upload.skip_junk');
  };

  // 命令式 Modal：脱离组件树，父级上传弹窗关闭后汇总仍可见
  const showFolderSummary = (
    counts: { uploaded: number; skipped: number; failed: number },
    results: ItemResult[]
  ) => {
    const detail = results.filter((r) => r.status !== 'uploaded');
    Modal.info({
      title: t('files.upload.summary', counts),
      width: 520,
      content: detail.length ? (
        <div style={{ maxHeight: 320, overflow: 'auto' }}>
          <List
            size="small"
            dataSource={detail}
            renderItem={(r) => (
              <List.Item>
                <span style={{ color: r.status === 'failed' ? '#cf1322' : '#8c8c8c' }}>
                  [{r.status === 'failed' ? t('files.upload.tag_failed') : t('files.upload.tag_skipped')}] {r.rel}
                  {r.reason ? ` — ${r.reason}` : ''}
                </span>
              </List.Item>
            )}
          />
        </div>
      ) : null,
    });
  };

  async function runFolderUpload(items: PickedFile[]) {
    if (uploadingRef.current) return;
    uploadingRef.current = true; // 同步占锁，关闭防重入窗口
    try {
      if (items.length === 0) {
        message.info(t('files.upload.empty_folder'));
        return;
      }
      const { accepted, skipped } = precheck(items);
      const results: ItemResult[] = skipped.map((s) => ({
        rel: s.rel,
        status: 'skipped' as const,
        reason: skipReasonText(s.reason, s.ext),
      }));
      if (accepted.length === 0) {
        message.info(t('files.upload.no_uploadable'));
        showFolderSummary({ uploaded: 0, skipped: results.length, failed: 0 }, results);
        return;
      }

      const total = accepted.length;
      let done = 0;
      let cursor = 0;
      setUploading(true);
      setFolderProgress({ total, done });

      const CONCURRENCY = 5;
      const worker = async () => {
        while (cursor < accepted.length) {
          const it = accepted[cursor++];
          const path = remoteParentDir(uploadPathRef.current, it.relativePath);
          try {
            await filesAPI.upload(it.file, 'auto', workspaceIdRef.current, path, documentTypeRef.current);
            results.push({ rel: it.relativePath, status: 'uploaded' });
          } catch (error: unknown) {
            const err = error as { response?: { status?: number; data?: { detail?: string } } };
            const code = err.response?.status;
            const detailMsg = String(err.response?.data?.detail ?? '');
            if (isDuplicateError(code, detailMsg)) {
              results.push({ rel: it.relativePath, status: 'skipped', reason: t('files.upload.skip_duplicate') });
            } else {
              results.push({
                rel: it.relativePath,
                status: 'failed',
                reason: detailMsg || t('files.upload.upload_failed', { code: code ?? '-' }),
              });
            }
          } finally {
            done++;
            setFolderProgress({ total, done });
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(CONCURRENCY, total) }, () => worker()));

      const counts = {
        uploaded: results.filter((r) => r.status === 'uploaded').length,
        skipped: results.filter((r) => r.status === 'skipped').length,
        failed: results.filter((r) => r.status === 'failed').length,
      };
      showFolderSummary(counts, results);
      onUploadSuccess();
    } finally {
      uploadingRef.current = false;
      setUploading(false);
      setFolderProgress(null);
    }
  }
  runFolderUploadRef.current = runFolderUpload;

  // capture 阶段拦截 drop：含目录则接管，先于 rc-upload 执行
  useEffect(() => {
    const el = dropZoneRef.current;
    if (!el) return;
    const onDrop = (e: DragEvent) => {
      const items = Array.from(e.dataTransfer?.items ?? []);
      const entries = items
        .map((it) => (it as unknown as { webkitGetAsEntry?: () => unknown }).webkitGetAsEntry?.() ?? null)
        .filter(Boolean) as Array<{ isDirectory?: boolean }>;
      const hasDirectory = entries.some((en) => en?.isDirectory);

      // 填写了唯一 tag 时只允许单文件：拖入目录或多个散文件一律拦截
      const looseCount = Array.from(e.dataTransfer?.files ?? []).length;
      if (shouldBlockTaggedBatch(tagRef.current, hasDirectory, looseCount)) {
        e.preventDefault();
        e.stopPropagation();
        message.warning(t('files.upload.tag_batch_blocked'));
        return;
      }

      if (!hasDirectory) return; // 纯文件 / 旧浏览器无法识别目录 → 交给现有单文件链路

      e.preventDefault();
      e.stopPropagation();
      if (uploadingRef.current) {
        message.warning(t('files.upload.uploading_busy'));
        return;
      }
      if (!workspaceIdRef.current) {
        message.warning(t('files.upload.no_workspace'));
        return;
      }
      void (async () => {
        const picked: PickedFile[] = [];
        for (const en of entries) await walkEntry(en, '', picked);
        await runFolderUploadRef.current(picked);
      })();
    };
    el.addEventListener('drop', onDrop, { capture: true });
    return () => el.removeEventListener('drop', onDrop, { capture: true } as EventListenerOptions);
  }, [t]);

  const props: UploadProps = {
    name: 'file',
    multiple: false,
    disabled: uploading,
    customRequest: async ({ file, onSuccess, onError }) => {
      setUploading(true);
      try {
        await filesAPI.upload(file as globalThis.File, parserType, workspaceId, uploadPath, documentType, tag);
        message.success('文件上传成功');
        onSuccess?.({});
        onUploadSuccess();
        setParserType('auto');
        setDocumentType('general');
        setTag('');
        setUploadPath(selectedPath);
      } catch (error: unknown) {
        const err = error as { response?: { status?: number; data?: { detail?: string } } };
        const code = err.response?.status;
        const detailMsg = String(err.response?.data?.detail ?? '');
        const kind = classifyUploadError(code, detailMsg);
        const errorMsg =
          kind === 'tag_conflict'
            ? t('files.upload.tag_conflict')
            : kind === 'name_too_long'
            ? t('files.upload.name_too_long')
            : kind === 'backend_detail'
            ? detailMsg
            : '文件上传失败';
        message.error(errorMsg);
        onError?.(error as Error);
      } finally {
        setUploading(false);
      }
    },
    showUploadList: false,
  };

  if (!canWrite) {
    return null;
  }

  return (
    <Card size="small" bordered={false}>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Form layout="vertical">
          <Form.Item label="选择解析器类型：">
            <Select
              value={parserType}
              onChange={setParserType}
              style={{ width: '100%' }}
              placeholder="选择解析器类型"
              disabled={uploading}
            >
              {PARSER_TYPES.map((type) => (
                <Option key={type.value} value={type.value}>
                  {type.label}
                </Option>
              ))}
            </Select>
            <div style={{ marginTop: 4, fontSize: 12, color: '#666' }}>
              提示：如果自动检测无法正确识别文档类型，请手动选择
            </div>
          </Form.Item>

          <Form.Item label={t('files.fields.document_type')}>
            <Select
              value={documentType}
              onChange={setDocumentType}
              style={{ width: '100%' }}
              disabled={uploading}
            >
              {DOCUMENT_TYPES.map((type) => (
                <Option key={type} value={type}>
                  {t(`files.document_types.${type}`)}
                </Option>
              ))}
            </Select>
          </Form.Item>

          <Form.Item label={t('files.upload.tag_label')} tooltip="^[A-Za-z0-9._:-]{1,128}$">
            <Input
              value={tag}
              onChange={(e) => setTag(e.target.value)}
              disabled={uploading}
              placeholder="report-2024"
              allowClear
            />
          </Form.Item>

          <Form.Item
            label={`${t('files.upload.target_dir')}：`}
            required
            tooltip={t('files.upload.path_tip')}
          >
            <Space.Compact style={{ width: '100%' }}>
              <Input readOnly value={uploadPath} placeholder="/" disabled={uploading} />
              <Button type="default" disabled={uploading} onClick={() => setDirPickerOpen(true)}>
                {t('files.upload.select_dir')}
              </Button>
            </Space.Compact>
          </Form.Item>
        </Form>

        <div ref={dropZoneRef}>
          <Dragger {...props}>
            <p className="ant-upload-drag-icon">
              <InboxOutlined />
            </p>
            <p className="ant-upload-text">{t('files.upload.drag_hint')}</p>
            <p className="ant-upload-hint">支持 PDF、Word、Excel、PPT、TXT、Markdown 等格式</p>
          </Dragger>
        </div>
        {folderProgress && (
          <Progress
            percent={
              folderProgress.total ? Math.round((folderProgress.done / folderProgress.total) * 100) : 0
            }
            format={() =>
              t('files.upload.progress', { done: folderProgress.done, total: folderProgress.total })
            }
          />
        )}
      </Space>

      <Modal
        title={t('files.upload.modal_title')}
        open={dirPickerOpen}
        onCancel={() => setDirPickerOpen(false)}
        footer={null}
        destroyOnClose
        width={480}
      >
        <Tree
          showIcon
          defaultExpandAll
          blockNode
          treeData={directoryTree}
          selectedKeys={[uploadPath]}
          onSelect={(keys) => {
            if (keys.length === 0) return;
            const key = keys[0] as string;
            setUploadPath(key);
            setDirPickerOpen(false);
          }}
        />
      </Modal>
    </Card>
  );
}
