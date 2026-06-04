import { useCallback, useRef, useState } from 'react';
import { Modal, Space, Button, Tag } from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { File, SearchResult } from '../types';
import { chunkDomId } from '../utils/chunk-preview-navigation';
import { DocumentSourcePreview } from './document-source-preview';

interface ChunkSourcePreviewModalProps {
  open: boolean;
  file: File | null;
  chunk: SearchResult | null;
  onClose: () => void;
}

export default function ChunkSourcePreviewModal({
  open,
  file,
  chunk,
  onClose,
}: ChunkSourcePreviewModalProps) {
  const { t } = useTranslation();
  const downloadBlobRef = useRef<Blob | null>(null);
  const [downloadReady, setDownloadReady] = useState(false);

  const handleBlobReady = useCallback((blob: Blob | null) => {
    downloadBlobRef.current = blob;
    setDownloadReady(blob != null);
  }, []);

  const handleDownload = useCallback(() => {
    const b = downloadBlobRef.current;
    if (!b || !file) return;
    const url = URL.createObjectURL(b);
    const a = document.createElement('a');
    a.href = url;
    a.download = file.name || 'download';
    a.click();
    URL.revokeObjectURL(url);
  }, [file]);

  const title = file && !file.is_directory ? file.name || file.uri?.split('/').pop() || '' : '';
  const chunkLabel = chunk
    ? `${t('searchPage.pos_page')} ${chunk.page ?? 0} - ${chunkDomId(chunk.chunk_id)}`
    : '';

  return (
    <Modal
      title={
        <Space wrap align="center">
          <span>{title}</span>
          {chunk ? <Tag color="gold">{chunkLabel}</Tag> : null}
          {file && !file.is_directory ? (
            <Button
              type="text"
              icon={<DownloadOutlined />}
              onClick={handleDownload}
              size="small"
              disabled={!downloadReady}
            >
              {t('files.preview.download')}
            </Button>
          ) : null}
        </Space>
      }
      open={open && !!file && !file.is_directory && !!chunk}
      onCancel={onClose}
      footer={null}
      width="min(1200px, 94vw)"
      style={{ top: 24 }}
      destroyOnHidden
      maskClosable={false}
      keyboard
      closable
    >
      <DocumentSourcePreview
        file={file}
        chunk={chunk}
        embedded
        active={open}
        onBlobReady={handleBlobReady}
      />
    </Modal>
  );
}
