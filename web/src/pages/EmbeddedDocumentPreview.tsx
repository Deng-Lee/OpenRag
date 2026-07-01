import { useEffect, useMemo, useState } from 'react';
import { Alert, Spin, Typography } from 'antd';
import { DocumentSourcePreview, type DocumentSourcePreviewFetchers } from '../components/document-source-preview';
import { embedPreviewAPI, readPreviewPageFromUrl, readPreviewTokenFromHash } from '../services/embedPreviewApi';
import type {
  DocumentChunkItem,
  EmbedDocumentPreviewResponse,
  File as OpenRagFile,
  SimpleStatus,
  WorkspaceFileSummary,
} from '../types';
import './EmbeddedDocumentPreview.css';

const { Text, Title } = Typography;

const EMBED_PREVIEW_MESSAGES = {
  unsupported: '该文件类型暂不支持内联预览。',
  loadFailed: '无法加载文档预览。',
};

function toFile(summary: WorkspaceFileSummary): OpenRagFile {
  const simpleStatus = ['unprocessed', 'processing', 'done', 'failed'].includes(String(summary.simple_status))
    ? (summary.simple_status as SimpleStatus)
    : null;

  return {
    id: summary.id,
    name: summary.name,
    uri: summary.uri,
    mime_type: summary.mime_type ?? undefined,
    processing_status: summary.processing_status ?? null,
    simple_status: simpleStatus,
    is_directory: false,
    owner_id: 0,
    size: 0,
    created_at: '',
    updated_at: '',
  };
}

function errorMessage(err: unknown): string {
  const status =
    err && typeof err === 'object' && 'response' in err
      ? (err as { response?: { status?: number } }).response?.status
      : undefined;
  if (status === 401 || status === 403) {
    return '预览链接无效或已过期。';
  }
  return '无法加载文档预览。';
}

function formatExpiresAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export default function EmbeddedDocumentPreview() {
  const token = useMemo(() => readPreviewTokenFromHash(window.location.hash), []);
  const initialPage = useMemo(() => readPreviewPageFromUrl(window.location.search, window.location.hash), []);
  const [context, setContext] = useState<EmbedDocumentPreviewResponse | null>(null);
  const [loading, setLoading] = useState(Boolean(token));
  const [error, setError] = useState<string | null>(token ? null : '预览链接缺少 token。');

  useEffect(() => {
    if (!token) return;
    let active = true;
    setLoading(true);
    setError(null);

    embedPreviewAPI
      .getContext(token)
      .then((response) => {
        if (active) setContext(response);
      })
      .catch((err) => {
        if (active) {
          setContext(null);
          setError(errorMessage(err));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [token]);

  const fetchers = useMemo<DocumentSourcePreviewFetchers | undefined>(() => {
    if (!token) return undefined;
    return {
      fetchContentBlob: () => embedPreviewAPI.fetchContentBlob(token),
      fetchPreview: () => embedPreviewAPI.fetchPreview(token),
      fetchChunkSource: () => embedPreviewAPI.fetchChunkSource(token),
    };
  }, [token]);

  const file = context ? toFile(context.file) : null;
  const chunk = context?.chunk ?? null;

  return (
    <main className="embedded-document-preview-page">
      <Spin spinning={loading}>
        <section className="embedded-document-preview-shell">
          {error ? <Alert type="error" message={error} showIcon /> : null}
          {context && file && chunk && fetchers ? (
            <>
              <header className="embedded-document-preview-header">
                <div className="embedded-document-preview-title">
                  <Title level={4}>{file.name || file.uri}</Title>
                  <Text type="secondary">{file.uri}</Text>
                </div>
                <Text type="secondary">有效期至 {formatExpiresAt(context.expires_at)}</Text>
              </header>
              <div className="embedded-document-preview-body">
                <DocumentSourcePreview
                  file={file}
                  chunk={chunk as DocumentChunkItem}
                  embedded
                  initialPage={initialPage ?? undefined}
                  fetchers={fetchers}
                  previewMessages={EMBED_PREVIEW_MESSAGES}
                />
              </div>
            </>
          ) : null}
        </section>
      </Spin>
    </main>
  );
}
