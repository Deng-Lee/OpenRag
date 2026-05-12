import { Modal, Button, Space, Typography, Spin } from 'antd';
import { FileOutlined, CloseOutlined, EyeOutlined } from '@ant-design/icons';
import type { ChunkInfo } from '../../utils/chunk-context-util';
import ChunkContextPreview from '../chunk-context-preview';

const { Text } = Typography;

export interface ChunkContextModalProps {
  visible: boolean;
  onClose: () => void;
  chunk: ChunkInfo | null;
  documentUrl?: string;
  isLoading?: boolean;
  onViewFullDocument?: () => void;
}

export default function ChunkContextModal({
  visible,
  onClose,
  chunk,
  documentUrl,
  isLoading = false,
  onViewFullDocument,
}: ChunkContextModalProps) {
  if (!chunk) return null;

  const fileExtension = chunk.filename?.split('.').pop()?.toUpperCase() || '';

  return (
    <Modal
      open={visible}
      onCancel={onClose}
      footer={null}
      width={900}
      centered
      destroyOnClose
      title={
        <Space>
          <FileOutlined />
          <Text strong style={{ maxWidth: 400 }} ellipsis={{ tooltip: chunk.filename }}>
            {chunk.filename}
          </Text>
          {fileExtension && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              ({fileExtension})
            </Text>
          )}
        </Space>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', height: 500 }}>
        {/* Preview content */}
        <div
          style={{
            flex: 1,
            overflow: 'auto',
            padding: 16,
            backgroundColor: '#f5f5f5',
            borderRadius: 8,
            marginBottom: 16,
          }}
        >
          {isLoading ? (
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                height: '100%',
              }}
            >
              <Spin size="large" />
              <Text style={{ marginTop: 16 }}>加载中...</Text>
            </div>
          ) : (
            <ChunkContextPreview chunk={chunk} documentUrl={documentUrl} />
          )}
        </div>

        {/* Action bar */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'flex-end',
            gap: 12,
            paddingTop: 16,
            borderTop: '1px solid #f0f0f0',
          }}
        >
          <Button icon={<CloseOutlined />} onClick={onClose}>
            关闭
          </Button>
          {onViewFullDocument && (
            <Button
              type="primary"
              icon={<EyeOutlined />}
              onClick={onViewFullDocument}
            >
              查看完整文档
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}
