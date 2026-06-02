import { Table, Button, Space, Popconfirm, message, Typography, Modal, Form, Select, Tag, Popover } from 'antd';
import { DeleteOutlined, FolderOutlined, FileOutlined, ReloadOutlined } from '@ant-design/icons';
import { useState } from 'react';
import type { File, SimpleStatus } from '../types';
import { filesAPI } from '../services/api';
import FilePreviewModal from './FilePreviewModal';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';

const { Text } = Typography;

const STATUS_CFG: Record<SimpleStatus, { color: string; labelKey: string }> = {
  unprocessed: { color: 'default', labelKey: 'files.status.unprocessed' },
  processing: { color: 'processing', labelKey: 'files.status.processing' },
  done: { color: 'success', labelKey: 'files.status.done' },
  failed: { color: 'error', labelKey: 'files.status.failed' },
};

interface FileListProps {
  files: File[];
  onFileDeleted: () => void;
  onFileReprocessed?: () => void;  // NEW
  loading?: boolean;
  canWrite?: boolean;
  workspaceId?: number;
}

export default function FileList({ files, onFileDeleted, onFileReprocessed, loading, canWrite = false, workspaceId }: FileListProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [isReprocessModalOpen, setIsReprocessModalOpen] = useState(false);
  const [reprocessingFile, setReprocessingFile] = useState<File | null>(null);
  const [reprocessForm] = Form.useForm();
  const [reprocessing, setReprocessing] = useState(false);
  const [previewFile, setPreviewFile] = useState<File | null>(null);
  const nowrapCellStyle = { whiteSpace: 'nowrap' as const };

  const handleDelete = async (id: number) => {
    try {
      const res = await filesAPI.delete(id);
      if (res.status === 202) {
        message.success(t('files.messages.delete_queued', { id: res.task_id ?? '' }));
      } else {
        message.success(t('files.messages.delete_success'));
      }
      onFileDeleted();
    } catch (error) {
      message.error(t('files.messages.delete_failed'));
    }
  };

  const handleReprocess = (record: File) => {
    setReprocessingFile(record);
    reprocessForm.setFieldsValue({
      parser_type: 'auto',
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

  const openDocumentChunks = (record: File) => {
    if (record.simple_status === 'done' && workspaceId && !record.is_directory) {
      navigate(`/workspaces/${workspaceId}/files/${record.id}/chunks`, { state: { from: 'files' } });
      return;
    }
    if (!record.is_directory) {
      setPreviewFile(record);
    }
  };

  const columns = [
    {
      title: t('files.columns.name'),
      dataIndex: 'name',
      key: 'name',
      width: 260,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (text: string, record: File) => (
        <Space>
          {record.is_directory ? <FolderOutlined style={{ color: '#1890ff' }} /> : <FileOutlined />}
          {record.is_directory ? (
            <Text strong>{text || record.uri?.split('/').pop()}</Text>
          ) : (
            <Button
              type="link"
              onClick={() => openDocumentChunks(record)}
              style={{ padding: 0, height: 'auto', fontWeight: 600 }}
            >
              {text || record.uri?.split('/').pop()}
            </Button>
          )}
        </Space>
      ),
    },
    {
      title: t('files.columns.location'),
      dataIndex: 'uri',
      key: 'uri',
      width: 420,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (uri: string) => <Text type="secondary" copyable>{uri}</Text>,
    },
    {
      title: t('files.columns.size'),
      dataIndex: 'size',
      key: 'size',
      width: 120,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (size: number, record: File) => record.is_directory ? '-' : `${((size || 0) / 1024).toFixed(2)} KB`,
    },
    {
      title: t('files.columns.type'),
      dataIndex: 'mime_type',
      key: 'mime_type',
      width: 180,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (type: string, record: File) => record.is_directory ? t('files.columns.directory') : (type || t('files.columns.unknown')),
    },
    {
      title: t('files.columns.uploader'),
      dataIndex: 'owner_name',
      key: 'owner_name',
      width: 120,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (name: string, record: File) => record.is_directory ? '-' : (name || '-'),
    },
    {
      title: t('files.columns.status'),
      key: 'status',
      width: 120,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (_: unknown, record: File) => {
        if (record.is_directory) return '-';
        const ss = record.simple_status;
        if (!ss || !STATUS_CFG[ss]) return '-';
        const cfg = STATUS_CFG[ss];
        const tag = <Tag color={cfg.color}>{t(cfg.labelKey)}</Tag>;
        if (ss !== 'failed') return tag;
        const detail =
          record.error_message && record.error_message.trim()
            ? record.error_message
            : t('files.status.no_error');
        return (
          <Popover
            title={t('files.status.failed_title')}
            trigger="click"
            content={(
              <div style={{ maxWidth: 360 }}>
                <pre
                  style={{
                    whiteSpace: 'pre-wrap',
                    maxHeight: 240,
                    overflow: 'auto',
                    margin: 0,
                    fontSize: 12,
                  }}
                >
                  {detail}
                </pre>
                <Button
                  type="link"
                  size="small"
                  icon={<ReloadOutlined />}
                  onClick={() => handleReprocess(record)}
                >
                  {t('files.status.reprocess_from_popover')}
                </Button>
              </div>
            )}
          >
            <span style={{ cursor: 'pointer' }}>{tag}</span>
          </Popover>
        );
      },
    },
    {
      title: t('files.columns.upload_time'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 220,
      onCell: () => ({ style: nowrapCellStyle }),
      render: (time: string) => time ? new Date(time).toLocaleString() : '-',
    },
    ...(canWrite ? [{
      title: t('files.columns.action'),
      key: 'action',
      width: 120,
      fixed: 'right' as const,
      onCell: () => ({ style: nowrapCellStyle }),
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
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    }] : []),
  ];

  return (
    <>
      <Table
        columns={columns}
        dataSource={files}
        rowKey="id"
        loading={loading}
        scroll={{ x: 'max-content' }}
      />

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

      <FilePreviewModal
        open={!!previewFile}
        file={previewFile}
        onClose={() => setPreviewFile(null)}
      />
    </>
  );
}
