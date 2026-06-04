import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Empty, Input, Pagination, Space, Spin, Tag, Typography } from 'antd';
import { ArrowLeftOutlined, FileTextOutlined, SearchOutlined } from '@ant-design/icons';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { filesAPI } from '../services/api';
import type { DocumentChunkItem, File as OpenRagFile, SimpleStatus, WorkspaceFileSummary } from '../types';
import { DocumentSourcePreview } from '../components/document-source-preview';
import { chunkDomId, scrollElementIntoScrollParent } from '../utils/chunk-preview-navigation';
import './DocumentChunks.css';

const { Paragraph, Text, Title } = Typography;
const DEFAULT_LIMIT = 20;

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

function hasPdfPosition(chunk: DocumentChunkItem): boolean {
  return (
    chunk.page != null ||
    (chunk.bbox_x0 != null && chunk.bbox_y0 != null && chunk.bbox_x1 != null && chunk.bbox_y1 != null) ||
    Boolean(chunk.position_int?.length) ||
    Boolean(chunk.positions?.length)
  );
}

function hasTextPosition(chunk: DocumentChunkItem): boolean {
  return chunk.source_char_start != null || chunk.source_char_end != null;
}

export default function DocumentChunks() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const params = useParams();
  const [searchParams] = useSearchParams();
  const [file, setFile] = useState<OpenRagFile | null>(null);
  const [chunks, setChunks] = useState<DocumentChunkItem[]>([]);
  const [selectedChunk, setSelectedChunk] = useState<DocumentChunkItem | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState('');
  const [skip, setSkip] = useState(0);
  const [limit] = useState(DEFAULT_LIMIT);
  const [total, setTotal] = useState(0);
  const requestSeqRef = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);

  function parseRouteIds(): { workspaceId: number; fileId: number } | null {
    const workspaceId = Number(params.workspaceId);
    const fileId = Number(params.fileId);
    if (!Number.isInteger(workspaceId) || workspaceId <= 0 || !Number.isInteger(fileId) || fileId <= 0) {
      return null;
    }
    return { workspaceId, fileId };
  }

  const routeIds = useMemo(() => parseRouteIds(), [params.workspaceId, params.fileId]);
  const workspaceId = routeIds?.workspaceId ?? 0;
  const fileId = routeIds?.fileId ?? 0;
  const routeKey = routeIds ? `${routeIds.workspaceId}:${routeIds.fileId}` : 'invalid';
  const targetChunkId = searchParams.get('chunkId')?.trim() || '';
  const targetChunkIndexRaw = searchParams.get('chunkIndex');
  const targetChunkIndex = useMemo(() => {
    const n = Number(targetChunkIndexRaw);
    return Number.isInteger(n) && n >= 0 ? n : null;
  }, [targetChunkIndexRaw]);
  const targetInitialPage =
    targetChunkIndex != null ? Math.floor(targetChunkIndex / limit) + 1 : 1;

  const loadChunks = useCallback(
    async (page: number, nextQ: string) => {
      const requestId = ++requestSeqRef.current;
      const requestWorkspaceId = workspaceId;
      const requestFileId = fileId;

      if (!requestWorkspaceId || !requestFileId) {
        setLoading(false);
        setError(t('documentChunks.errors.invalidRoute'));
        return;
      }

      const nextSkip = Math.max(0, page - 1) * limit;
      setLoading(true);
      setError(null);
      try {
        const response = await filesAPI.listChunks(requestWorkspaceId, requestFileId, {
          skip: nextSkip,
          limit,
          q: nextQ.trim() || undefined,
        });
        if (requestSeqRef.current !== requestId) {
          return;
        }
        const sortedItems = [...response.items].sort((a, b) => a.chunk_index - b.chunk_index);
        setFile(toFile(response.file));
        setChunks(sortedItems);
        setSkip(response.skip ?? nextSkip);
        setTotal(response.total ?? sortedItems.length);
        setSelectedChunk((current) => {
          const targetChunk = targetChunkId
            ? sortedItems.find((chunk) => chunk.chunk_id === targetChunkId)
            : null;
          if (targetChunk) {
            return targetChunk;
          }
          if (
            current &&
            current.file_id === requestFileId &&
            current.workspace_id === requestWorkspaceId &&
            sortedItems.some((chunk) => chunk.chunk_id === current.chunk_id)
          ) {
            return current;
          }
          return sortedItems[0] ?? null;
        });
      } catch (err: unknown) {
        if (requestSeqRef.current !== requestId) {
          return;
        }
        const detail =
          err && typeof err === 'object' && 'response' in err
            ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
            : undefined;
        setError(detail || t('documentChunks.errors.loadFailed'));
        setChunks([]);
        setSelectedChunk(null);
        setTotal(0);
      } finally {
        if (requestSeqRef.current === requestId) {
          setLoading(false);
        }
      }
    },
    [fileId, limit, t, targetChunkId, workspaceId]
  );

  useEffect(() => {
    requestSeqRef.current += 1;
    setFile(null);
    setChunks([]);
    setSelectedChunk(null);
    setSkip(0);
    setTotal(0);
    setError(null);
    setQ('');

    if (!routeIds) {
      setError(t('documentChunks.errors.invalidRoute'));
      setLoading(false);
      return;
    }

    void loadChunks(targetInitialPage, '');

    return () => {
      requestSeqRef.current += 1;
    };
  }, [loadChunks, routeIds, routeKey, t, targetInitialPage]);

  const handleSelectChunk = (chunk: DocumentChunkItem) => {
    if (chunk.file_id !== fileId || chunk.workspace_id !== workspaceId) {
      return;
    }
    setSelectedChunk(chunk);
  };

  const handleSearch = (value: string) => {
    const nextQ = value.trim();
    setQ(nextQ);
    void loadChunks(1, nextQ);
  };

  const handleBack = () => {
    navigate(location.state?.from === 'search' ? '/search' : '/files');
  };

  const renderEmptyState = () => (
    <div className="document-chunks-empty">
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={q ? t('documentChunks.empty.search') : t('documentChunks.empty.noChunks')}
      />
    </div>
  );

  const renderChunkCard = (chunk: DocumentChunkItem) => {
    const selected = currentSelectedChunk?.chunk_id === chunk.chunk_id;
    const positionTags = [
      chunk.page != null ? t('documentChunks.position.page', { page: chunk.page }) : null,
      hasTextPosition(chunk) ? t('documentChunks.position.textRange') : null,
      hasPdfPosition(chunk) ? t('documentChunks.position.visual') : null,
    ].filter(Boolean);

    return (
      <button
        type="button"
        key={chunk.chunk_id}
        id={`document-chunks-card-${chunkDomId(chunk.chunk_id)}`}
        className={`document-chunks-card${selected ? ' document-chunks-card--selected' : ''}`}
        onClick={() => handleSelectChunk(chunk)}
        aria-pressed={selected}
      >
        <div className="document-chunks-card-head">
          <Space size={6} wrap>
            <Tag color={selected ? 'blue' : 'default'}>
              {t('documentChunks.chunk.index', { index: chunk.chunk_index })}
            </Tag>
            {chunk.is_truncated ? <Tag>{t('documentChunks.chunk.truncated')}</Tag> : null}
          </Space>
          {chunk.page != null ? (
            <Text type="secondary" className="document-chunks-card-page">
              {t('documentChunks.chunk.page', { page: chunk.page })}
            </Text>
          ) : null}
        </div>
        <Paragraph ellipsis={{ rows: 4 }} className="document-chunks-card-text">
          {chunk.text || t('documentChunks.chunk.noText')}
        </Paragraph>
        <div className="document-chunks-card-meta">
          {positionTags.length ? (
            positionTags.map((label) => <span key={String(label)}>{label}</span>)
          ) : (
            <span>{t('documentChunks.position.none')}</span>
          )}
        </div>
      </button>
    );
  };

  const currentFile = file?.id === fileId ? file : null;
  const currentSelectedChunk =
    selectedChunk?.file_id === fileId && selectedChunk?.workspace_id === workspaceId ? selectedChunk : null;
  const routeBoundChunks = chunks.filter((chunk) => chunk.file_id === fileId && chunk.workspace_id === workspaceId);
  const hasOnlyCurrentRouteChunks =
    chunks.every((chunk) => chunk.file_id === fileId && chunk.workspace_id === workspaceId) &&
    (file == null || file.id === fileId);
  const currentTotal = hasOnlyCurrentRouteChunks ? total : 0;
  const currentPage = hasOnlyCurrentRouteChunks ? Math.floor(skip / limit) + 1 : 1;
  const selectedCardId = currentSelectedChunk
    ? `document-chunks-card-${chunkDomId(currentSelectedChunk.chunk_id)}`
    : null;

  useLayoutEffect(() => {
    if (!selectedCardId) return;
    const card = document.getElementById(selectedCardId);
    scrollElementIntoScrollParent(listRef.current, card, 'auto');
  }, [selectedCardId]);

  return (
    <div className="document-chunks-page">
      <header className="document-chunks-header">
        <div className="document-chunks-title-block">
          <Button icon={<ArrowLeftOutlined />} onClick={handleBack}>
            {t('documentChunks.back')}
          </Button>
          <div className="document-chunks-title-text">
            <Title level={3}>{t('documentChunks.title')}</Title>
            <Text type="secondary" ellipsis title={currentFile?.name || undefined}>
              {currentFile?.name || t('documentChunks.fileFallback', { fileId })}
            </Text>
          </div>
        </div>
        <Space size={8} wrap className="document-chunks-route-meta">
          <Tag>{t('documentChunks.workspaceId', { workspaceId })}</Tag>
          <Tag>{t('documentChunks.fileId', { fileId })}</Tag>
        </Space>
      </header>

      {error ? (
        <Alert type="error" message={error} showIcon className="document-chunks-alert" />
      ) : null}

      <main className="document-chunks-workbench">
        <section className="document-chunks-preview-pane" aria-label={t('documentChunks.previewLabel')}>
          {routeIds && currentFile && currentSelectedChunk ? (
            <DocumentSourcePreview file={currentFile} chunk={currentSelectedChunk} workspaceId={workspaceId} embedded />
          ) : (
            <div className="document-chunks-preview-empty">
              <FileTextOutlined />
              <Text type="secondary">{t('documentChunks.empty.selectChunk')}</Text>
            </div>
          )}
        </section>

        <aside className="document-chunks-list-pane" aria-label={t('documentChunks.listLabel')}>
          <div className="document-chunks-list-toolbar">
            <Input.Search
              allowClear
              value={q}
              prefix={<SearchOutlined />}
              placeholder={t('documentChunks.searchPlaceholder')}
              onChange={(event) => setQ(event.target.value)}
              onSearch={handleSearch}
            />
            <Text type="secondary" className="document-chunks-count">
              {t('documentChunks.stats', { total: currentTotal })}
            </Text>
          </div>

          <Spin spinning={loading}>
            <div className="document-chunks-list" ref={listRef}>
              {routeBoundChunks.length ? routeBoundChunks.map(renderChunkCard) : renderEmptyState()}
            </div>
          </Spin>

          <div className="document-chunks-pagination">
            <Pagination
              size="small"
              current={currentPage}
              pageSize={limit}
              total={currentTotal}
              showSizeChanger={false}
              onChange={(page) => void loadChunks(page, q)}
            />
          </div>
        </aside>
      </main>
    </div>
  );
}
