import { useState, useEffect, useMemo } from 'react';
import { Upload, message, Select, Space, Card, Form, Input, Button, Modal, Tree } from 'antd';
import { InboxOutlined, FolderOutlined } from '@ant-design/icons';
import type { UploadProps } from 'antd';
import type { DataNode } from 'antd/es/tree';
import { useTranslation } from 'react-i18next';
import { filesAPI } from '../services/api';
import type { DocumentType, File } from '../types';

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

  const props: UploadProps = {
    name: 'file',
    multiple: false,
    disabled: uploading,
    customRequest: async ({ file, onSuccess, onError }) => {
      setUploading(true);
      try {
        await filesAPI.upload(file as globalThis.File, parserType, workspaceId, uploadPath, documentType);
        message.success('文件上传成功');
        onSuccess?.({});
        onUploadSuccess();
        setParserType('auto');
        setDocumentType('general');
        setUploadPath(selectedPath);
      } catch (error: unknown) {
        const err = error as { response?: { data?: { detail?: string } } };
        const errorMsg = err.response?.data?.detail || '文件上传失败';
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

        <Dragger {...props}>
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽文件到此处上传</p>
          <p className="ant-upload-hint">支持 PDF、Word、Excel、PPT、TXT、Markdown 等格式</p>
        </Dragger>
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
