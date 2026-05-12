import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { Document, Page } from 'react-pdf';
import type { File } from '../types';
import { filesAPI } from '../services/api';
import { Modal, Spin, Alert, Space, Button, Typography } from 'antd';
import {
  ZoomOutOutlined,
  ZoomInOutlined,
  DownloadOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import DOMPurify from 'dompurify';
import { classifyPreviewFile } from '../utils/file-preview-kind';

import './file-preview.css';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';

const { Text } = Typography;

const BODY_SCROLL_STYLE: CSSProperties = {
  maxHeight: 'calc(100vh - 140px)',
  overflow: 'auto',
  padding: '8px 4px',
};

interface FilePreviewModalProps {
  open: boolean;
  file: File | null;
  onClose: () => void;
}

export default function FilePreviewModal({ open, file, onClose }: FilePreviewModalProps) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pdfScale, setPdfScale] = useState(1);
  const [numPages, setNumPages] = useState(0);
  const [pdfSource, setPdfSource] = useState<string | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [textBody, setTextBody] = useState<string | null>(null);
  const [htmlBody, setHtmlBody] = useState<string | null>(null);
  const [markdownBody, setMarkdownBody] = useState<string | null>(null);
  const [officeHtml, setOfficeHtml] = useState<string | null>(null);
  const [officeText, setOfficeText] = useState<string | null>(null);
  const [kind, setKind] = useState<ReturnType<typeof classifyPreviewFile>>('unsupported');
  const downloadBlobRef = useRef<Blob | null>(null);
  const objectUrlsRef = useRef<string[]>([]);

  const revokeAll = useCallback(() => {
    objectUrlsRef.current.forEach((u) => URL.revokeObjectURL(u));
    objectUrlsRef.current = [];
    downloadBlobRef.current = null;
  }, []);

  const pushObjectUrl = useCallback((url: string) => {
    objectUrlsRef.current.push(url);
    return url;
  }, []);

  const resetState = useCallback(() => {
    setError(null);
    setLoading(false);
    setPdfScale(1);
    setNumPages(0);
    setPdfSource(null);
    setImageUrl(null);
    setTextBody(null);
    setHtmlBody(null);
    setMarkdownBody(null);
    setOfficeHtml(null);
    setOfficeText(null);
    setKind('unsupported');
    revokeAll();
  }, [revokeAll]);

  useEffect(() => {
    if (!open || !file || file.is_directory) {
      resetState();
      return;
    }

    resetState();
    const cat = classifyPreviewFile(file);
    setKind(cat);
    setLoading(true);
    setError(null);

    const run = async () => {
      try {
        if (cat === 'office') {
          const prev = await filesAPI.fetchPreview(file.id);
          if (prev.format === 'html') {
            setOfficeHtml(prev.content);
            setOfficeText(null);
          } else {
            setOfficeText(prev.content);
            setOfficeHtml(null);
          }
          const blob = await filesAPI.fetchContentBlob(file.id);
          downloadBlobRef.current = blob;
          setLoading(false);
          return;
        }

        if (cat === 'unsupported') {
          try {
            const blob = await filesAPI.fetchContentBlob(file.id);
            downloadBlobRef.current = blob;
          } catch {
            /* 仍展示不支持提示 */
          }
          setError(t('files.preview.unsupported'));
          setLoading(false);
          return;
        }

        const blob = await filesAPI.fetchContentBlob(file.id);
        downloadBlobRef.current = blob;

        if (cat === 'pdf') {
          const bytes = new Uint8Array(await blob.arrayBuffer());
          const isPdf =
            bytes.length >= 5 &&
            bytes[0] === 0x25 &&
            bytes[1] === 0x50 &&
            bytes[2] === 0x44 &&
            bytes[3] === 0x46 &&
            bytes[4] === 0x2d;
          if (!isPdf) {
            const head = new TextDecoder().decode(bytes.slice(0, Math.min(120, bytes.length)));
            setError(`文件内容不是有效 PDF。前120字节：${head}`);
            setLoading(false);
            return;
          }
          const pdfBlob = new Blob([bytes], { type: 'application/pdf' });
          const url = pushObjectUrl(URL.createObjectURL(pdfBlob));
          setPdfSource(url);
          setLoading(false);
          return;
        }

        if (cat === 'image') {
          const url = pushObjectUrl(URL.createObjectURL(blob));
          setImageUrl(url);
          setLoading(false);
          return;
        }

        const text = await blob.text();
        if (cat === 'markdown') setMarkdownBody(text);
        else if (cat === 'html') setHtmlBody(text);
        else setTextBody(text);
        setLoading(false);
      } catch (e: unknown) {
        console.error(e);
        setError(t('files.preview.load_failed'));
        setLoading(false);
      }
    };

    void run();

    return () => {
      revokeAll();
    };
  }, [open, file, resetState, revokeAll, pushObjectUrl, t]);

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

  const title = file && !file.is_directory ? file.name || file.uri.split('/').pop() : '';

  return (
    <Modal
      title={
        <Space wrap>
          <span>{title}</span>
          {file && !file.is_directory ? (
            <Button type="text" icon={<DownloadOutlined />} onClick={handleDownload} size="small">
              {t('files.preview.download')}
            </Button>
          ) : null}
        </Space>
      }
      open={open && !!file && !file.is_directory}
      onCancel={onClose}
      footer={null}
      width="min(1200px, 94vw)"
      style={{ top: 24 }}
      destroyOnHidden
      maskClosable={false}
      keyboard
      closable
    >
      <Spin spinning={loading}>
        <div style={BODY_SCROLL_STYLE}>
          {error ? (
            <Alert type="warning" message={error} showIcon />
          ) : kind === 'pdf' && pdfSource ? (
            <>
              <Space style={{ marginBottom: 12 }} wrap>
                <Button icon={<ZoomOutOutlined />} onClick={() => setPdfScale((s) => Math.max(0.5, s - 0.15))} />
                <Text>{Math.round(pdfScale * 100)}%</Text>
                <Button icon={<ZoomInOutlined />} onClick={() => setPdfScale((s) => Math.min(2.5, s + 0.15))} />
                {numPages > 0 ? (
                  <Text type="secondary">
                    {t('files.preview.page_info', { count: numPages })}
                  </Text>
                ) : null}
              </Space>
              <Document
                file={pdfSource}
                onLoadSuccess={(info) => setNumPages(info.numPages)}
                onSourceError={(err) => {
                  const msg = err instanceof Error ? err.message : String(err);
                  setError(`${t('files.preview.pdf_error')}: ${msg}`);
                }}
                onLoadError={(err) => {
                  const msg = err instanceof Error ? err.message : String(err);
                  setError(`${t('files.preview.pdf_error')}: ${msg}`);
                }}
                loading={t('files.preview.loading')}
                error={t('files.preview.pdf_error')}
              >
                {Array.from({ length: numPages }, (_, i) => (
                  <div key={i + 1} style={{ marginBottom: 16 }}>
                    <Page pageNumber={i + 1} scale={pdfScale} renderTextLayer renderAnnotationLayer />
                  </div>
                ))}
              </Document>
            </>
          ) : kind === 'image' && imageUrl ? (
            <div style={{ textAlign: 'center' }}>
              <img
                src={imageUrl}
                alt={title}
                style={{ maxWidth: '100%', height: 'auto' }}
              />
            </div>
          ) : kind === 'markdown' && markdownBody != null ? (
            <div className="file-preview-markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdownBody}</ReactMarkdown>
            </div>
          ) : kind === 'html' && htmlBody != null ? (
            <div
              className="file-preview-html"
              // DOMPurify 清洗后端以外的 HTML；此处来自用户上传文件的原始渲染
              dangerouslySetInnerHTML={{
                __html: DOMPurify.sanitize(htmlBody, { USE_PROFILES: { html: true } }),
              }}
            />
          ) : kind === 'text' && textBody != null ? (
            <pre
              style={{
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontFamily: 'monospace',
                fontSize: 13,
                margin: 0,
              }}
            >
              {textBody}
            </pre>
          ) : kind === 'office' && officeHtml != null ? (
            <div
              className="file-preview-office-html"
              dangerouslySetInnerHTML={{
                __html: DOMPurify.sanitize(officeHtml, { USE_PROFILES: { html: true } }),
              }}
            />
          ) : kind === 'office' && officeText != null ? (
            <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>{officeText}</pre>
          ) : !loading && kind === 'unsupported' ? (
            <Alert type="info" message={t('files.preview.unsupported')} showIcon />
          ) : null}
        </div>
      </Spin>
    </Modal>
  );
}
