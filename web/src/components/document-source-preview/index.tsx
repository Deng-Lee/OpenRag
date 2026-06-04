import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Button, Alert, Space, Spin, Typography } from 'antd';
import { ZoomInOutlined, ZoomOutOutlined } from '@ant-design/icons';
import { Document, Page } from 'react-pdf';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import DOMPurify from 'dompurify';
import { useTranslation } from 'react-i18next';
import type { DocumentChunkItem, File, SearchResult } from '../../types';
import { filesAPI } from '../../services/api';
import { classifyPreviewFile } from '../../utils/file-preview-kind';
import {
  chunkDomId,
  scrollElementIntoScrollParent,
} from '../../utils/chunk-preview-navigation';

import '../file-preview.css';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';

const { Text } = Typography;

type SourcePreviewChunk = SearchResult | DocumentChunkItem;
type PdfPos = [number, number, number, number, number];

const PREVIEW_SCROLL_STYLE: CSSProperties = {
  maxHeight: 'calc(100vh - 140px)',
  overflow: 'auto',
  padding: '8px 4px',
};

export interface DocumentSourcePreviewProps {
  file: File | null;
  chunk: SourcePreviewChunk | null;
  workspaceId?: number;
  embedded?: boolean;
  active?: boolean;
  onBlobReady?: (blob: Blob | null) => void;
  fetchers?: DocumentSourcePreviewFetchers;
  previewMessages?: DocumentSourcePreviewMessages;
}

export interface DocumentSourcePreviewFetchers {
  fetchContentBlob: () => Promise<Blob>;
  fetchPreview: () => Promise<{ format: 'html' | 'text'; content: string }>;
  fetchChunkSource: () => Promise<{ format: 'text'; content: string }>;
}

export interface DocumentSourcePreviewMessages {
  unsupported?: string;
  loadFailed?: string;
}

function getChunkStartOffset(chunk: SourcePreviewChunk): number | null {
  const legacy = 'start_offset' in chunk ? chunk.start_offset : null;
  return legacy ?? chunk.source_char_start ?? null;
}

function getChunkEndOffset(chunk: SourcePreviewChunk): number | null {
  const legacy = 'end_offset' in chunk ? chunk.end_offset : null;
  return legacy ?? chunk.source_char_end ?? null;
}

function fetchContentBlob(
  fileId: number,
  workspaceId?: number,
  fetchers?: DocumentSourcePreviewFetchers
): Promise<Blob> {
  if (fetchers) return fetchers.fetchContentBlob();
  if (workspaceId != null) {
    return filesAPI.fetchWorkspaceContentBlob(workspaceId, fileId);
  }
  return filesAPI.fetchContentBlob(fileId);
}

function fetchPreview(
  fileId: number,
  workspaceId?: number,
  fetchers?: DocumentSourcePreviewFetchers
): Promise<{ format: 'html' | 'text'; content: string }> {
  if (fetchers) return fetchers.fetchPreview();
  if (workspaceId != null) {
    return filesAPI.fetchWorkspacePreview(workspaceId, fileId);
  }
  return filesAPI.fetchPreview(fileId);
}

function fetchChunkSource(
  fileId: number,
  workspaceId?: number,
  fetchers?: DocumentSourcePreviewFetchers
): Promise<{ format: 'text'; content: string }> {
  if (fetchers) return fetchers.fetchChunkSource();
  if (workspaceId == null) {
    return Promise.reject(new Error('workspaceId required'));
  }
  return filesAPI.fetchWorkspaceChunkSource(workspaceId, fileId);
}

function isDocxPreviewFile(file: File): boolean {
  const name = (file.name || file.uri || '').toLowerCase();
  const mime = (file.mime_type || '').toLowerCase();
  return (
    name.endsWith('.docx') ||
    mime === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  );
}

export function extractPdfPositions(chunk: SourcePreviewChunk | null | undefined): PdfPos[] {
  if (!chunk) return [];
  const topLevel = Array.isArray(chunk.position_int) ? chunk.position_int : null;
  const legacy = Array.isArray(chunk.positions) ? chunk.positions : null;
  const meta =
    'metadata' in chunk && chunk.metadata && Array.isArray(chunk.metadata.position_int)
      ? (chunk.metadata.position_int as unknown[])
      : null;
  const raw = topLevel ?? meta ?? legacy ?? [];
  const out: PdfPos[] = [];
  for (const it of raw) {
    if (!Array.isArray(it) || it.length < 5) continue;
    const p = Number(it[0]);
    const x0 = Number(it[1]);
    const x1 = Number(it[2]);
    const y0 = Number(it[3]);
    const y1 = Number(it[4]);
    if (![p, x0, x1, y0, y1].every(Number.isFinite)) continue;
    out.push([p, x0, x1, y0, y1]);
  }
  return out;
}

export function chunkAnchorId(chunk: SourcePreviewChunk): string {
  return chunk.chunk_id ? `chunk-${chunkDomId(chunk.chunk_id)}` : 'chunk-nav-anchor';
}

function chunkAnchorDomId(chunk: SourcePreviewChunk, instanceId?: string): string {
  const aid = chunkAnchorId(chunk);
  return instanceId ? `${instanceId}-${aid}` : aid;
}

function chunkAnchorAttrs(chunk: SourcePreviewChunk, instanceId?: string) {
  return {
    id: chunkAnchorDomId(chunk, instanceId),
    'data-chunk-anchor': chunkAnchorId(chunk),
  };
}

function findPdfPage(root: HTMLElement, pageNumber: number): HTMLElement | null {
  return root.querySelector<HTMLElement>(`[data-pdf-page="${pageNumber}"]`);
}

function findTextSnippetStart(text: string, chunk: SourcePreviewChunk): number {
  const raw = (chunk.text || '').trim();
  if (raw.length < 2) return -1;
  for (const len of [200, 120, 80, 50, 30]) {
    const c = raw.slice(0, len);
    if (c.length < 4) continue;
    const i = text.indexOf(c);
    if (i >= 0) return i;
  }
  return -1;
}

export function codePointOffsetToUtf16Index(text: string, offset: number): number {
  if (offset <= 0) return 0;
  let codePoints = 0;
  let utf16Index = 0;
  for (const char of text) {
    if (codePoints >= offset) break;
    utf16Index += char.length;
    codePoints += 1;
  }
  return utf16Index;
}

export function renderTextWithNav(
  text: string,
  chunk: SourcePreviewChunk,
  instanceId?: string
): React.ReactNode {
  const anchorAttrs = chunkAnchorAttrs(chunk, instanceId);
  const rawS = getChunkStartOffset(chunk) ?? 0;
  const rawE = getChunkEndOffset(chunk) ?? 0;
  const s = codePointOffsetToUtf16Index(text, rawS);
  const e = codePointOffsetToUtf16Index(text, rawE);
  const snippet = (chunk.text || '').trim().slice(0, 200);
  if (e > s && e <= text.length) {
    return (
      <>
        {text.slice(0, s)}
        <mark {...anchorAttrs} className="chunk-text-highlight">
          {text.slice(s, e)}
        </mark>
        {text.slice(e)}
      </>
    );
  }
  const needle = snippet.slice(0, Math.min(80, snippet.length));
  if (needle.length > 2) {
    const idx = text.indexOf(needle);
    if (idx >= 0) {
      return (
        <>
          {text.slice(0, idx)}
          <mark {...anchorAttrs} className="chunk-text-highlight">
            {text.slice(idx, idx + needle.length)}
          </mark>
          {text.slice(idx + needle.length)}
        </>
      );
    }
  }
  const fb = findTextSnippetStart(text, chunk);
  if (fb >= 0) {
    const elen = Math.min(220, Math.max(24, (chunk.text || '').trim().length || 80));
    return (
      <>
        {text.slice(0, fb)}
        <mark {...anchorAttrs} className="chunk-text-highlight">
          {text.slice(fb, fb + elen)}
        </mark>
        {text.slice(fb + elen)}
      </>
    );
  }
  return text;
}

export function renderMarkdownWithAnchor(
  markdown: string,
  chunk: SourcePreviewChunk,
  instanceId?: string
): React.ReactNode {
  const anchorAttrs = chunkAnchorAttrs(chunk, instanceId);
  const snippet = (chunk.text || '').trim().slice(0, 200);
  let idx = -1;
  if (snippet.length > 2) {
    for (const len of [80, 48, 32]) {
      const needle = snippet.slice(0, len);
      if (needle.length < 4) break;
      idx = markdown.indexOf(needle);
      if (idx >= 0) break;
    }
  }
  if (idx < 0) idx = findTextSnippetStart(markdown, chunk);
  if (idx >= 0) {
    const mid = markdown.slice(idx, idx + Math.min(Math.max(snippet.length, 40), 400));
    const before = markdown.slice(0, idx);
    const after = markdown.slice(idx + mid.length);
    return (
      <>
        {before ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{before}</ReactMarkdown> : null}
        <div {...anchorAttrs} className="chunk-html-highlight-wrap">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{mid}</ReactMarkdown>
        </div>
        {after ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{after}</ReactMarkdown> : null}
      </>
    );
  }
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>;
}

export function PdfPageWithBBox({
  pageNumber,
  scale,
  highlightPage,
  chunk,
  instanceId,
}: {
  pageNumber: number;
  scale: number;
  highlightPage: number;
  chunk: SourcePreviewChunk;
  instanceId?: string;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [boxes, setBoxes] = useState<Array<{ left: number; top: number; width: number; height: number }>>([]);

  const pdfPositions = extractPdfPositions(chunk);
  const pagePositions = pdfPositions.filter((p) => p[0] === pageNumber);

  const hasBbox =
    chunk.bbox_x0 != null &&
    chunk.bbox_y0 != null &&
    chunk.bbox_x1 != null &&
    chunk.bbox_y1 != null;
  const usePositionOverlay = pagePositions.length > 0;
  const showOverlay = usePositionOverlay || (pageNumber === highlightPage && hasBbox);

  const onRenderSuccess = useCallback(
    (page: { getViewport: (p: { scale: number }) => { width: number; height: number } }) => {
      if (!showOverlay || !wrapRef.current) {
        setBoxes([]);
        return;
      }
      const measure = () => {
        const wrap = wrapRef.current;
        const canvas = wrap?.querySelector('canvas');
        if (!wrap || !canvas || canvas.clientWidth < 2) {
          requestAnimationFrame(measure);
          return;
        }
        const vp = page.getViewport({ scale });
        const sx = canvas.clientWidth / vp.width;
        const sy = canvas.clientHeight / vp.height;
        if (usePositionOverlay) {
          const next = pagePositions.map(([, x0, x1, y0, y1]) => ({
            left: x0 * sx,
            top: y0 * sy,
            width: Math.max(4, (x1 - x0) * sx),
            height: Math.max(4, (y1 - y0) * sy),
          }));
          setBoxes(next);
          return;
        }
        const x0 = chunk.bbox_x0!;
        const y0 = chunk.bbox_y0!;
        const x1 = chunk.bbox_x1!;
        const y1 = chunk.bbox_y1!;
        setBoxes([
          {
            left: x0 * sx,
            top: y0 * sy,
            width: Math.max(4, (x1 - x0) * sx),
            height: Math.max(4, (y1 - y0) * sy),
          },
        ]);
      };
      requestAnimationFrame(() => requestAnimationFrame(measure));
    },
    [
      showOverlay,
      scale,
      usePositionOverlay,
      pagePositions,
      chunk.bbox_x0,
      chunk.bbox_y0,
      chunk.bbox_x1,
      chunk.bbox_y1,
    ]
  );

  return (
    <div
      id={instanceId ? `${instanceId}-openrag-pdf-page-${pageNumber}` : undefined}
      data-pdf-page={pageNumber}
      ref={wrapRef}
      style={{ marginBottom: 16, position: 'relative', display: 'inline-block' }}
    >
      <Page
        pageNumber={pageNumber}
        scale={scale}
        renderTextLayer
        renderAnnotationLayer
        onRenderSuccess={showOverlay ? onRenderSuccess : undefined}
      />
      {showOverlay &&
        boxes.map((box, i) => (
          <div
            key={`${pageNumber}-${i}-${box.left}-${box.top}`}
            className="chunk-pdf-highlight"
            style={{
              position: 'absolute',
              left: box.left,
              top: box.top,
              width: box.width,
              height: box.height,
              zIndex: 6,
              pointerEvents: 'none',
              background: 'rgba(255, 235, 59, 0.38)',
              border: '2px solid #ff9800',
              boxSizing: 'border-box',
              borderRadius: 2,
            }}
          />
        ))}
    </div>
  );
}

export function DocumentSourcePreview({
  file,
  chunk,
  workspaceId,
  embedded = false,
  active = true,
  onBlobReady,
  fetchers,
  previewMessages,
}: DocumentSourcePreviewProps) {
  const { t } = useTranslation();
  const reactInstanceId = useId();
  const scrollRef = useRef<HTMLDivElement>(null);
  const officeHostRef = useRef<HTMLDivElement>(null);
  const instanceDomId = useMemo(
    () => `document-source-preview-${reactInstanceId.replace(/[^A-Za-z0-9_-]/g, '')}`,
    [reactInstanceId]
  );

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
  const [chunkSourceText, setChunkSourceText] = useState<string | null>(null);
  const [usingChunkSource, setUsingChunkSource] = useState(false);
  const [kind, setKind] = useState<ReturnType<typeof classifyPreviewFile>>('unsupported');
  const objectUrlsRef = useRef<string[]>([]);

  const fileId = file?.id ?? null;
  const fileName = file?.name ?? '';
  const fileMimeType = file?.mime_type ?? '';
  const fileIsDirectory = file?.is_directory ?? false;
  const fileUri = file?.uri ?? '';
  const chunkId = chunk?.chunk_id ?? null;
  const chunkText = chunk?.text ?? '';
  const chunkPage = chunk?.page ?? null;
  const chunkStartOffset = chunk ? getChunkStartOffset(chunk) : null;
  const chunkEndOffset = chunk ? getChunkEndOffset(chunk) : null;
  const chunkBboxX0 = chunk?.bbox_x0 ?? null;
  const chunkBboxY0 = chunk?.bbox_y0 ?? null;
  const chunkBboxX1 = chunk?.bbox_x1 ?? null;
  const chunkBboxY1 = chunk?.bbox_y1 ?? null;
  const chunkPositionsKey = JSON.stringify(extractPdfPositions(chunk));
  const chunkLoadKey = chunk
    ? [
        chunkId ?? '',
        chunkPage ?? '',
        chunkStartOffset ?? '',
        chunkEndOffset ?? '',
        chunkBboxX0 ?? '',
        chunkBboxY0 ?? '',
        chunkBboxX1 ?? '',
        chunkBboxY1 ?? '',
        chunkText,
        chunkPositionsKey,
      ].join('\u001f')
    : '';
  const unsupportedMessage = previewMessages?.unsupported ?? t('files.preview.unsupported');
  const loadFailedMessage = previewMessages?.loadFailed ?? t('files.preview.load_failed');

  const revokeAll = useCallback(() => {
    objectUrlsRef.current.forEach((u) => URL.revokeObjectURL(u));
    objectUrlsRef.current = [];
    onBlobReady?.(null);
  }, [onBlobReady]);

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
    setChunkSourceText(null);
    setUsingChunkSource(false);
    setKind('unsupported');
    revokeAll();
  }, [revokeAll]);

  useEffect(() => {
    if (!active || fileId == null || fileIsDirectory || !chunk) {
      resetState();
      return;
    }

    let cancelled = false;
    resetState();
    const cat = classifyPreviewFile({
      id: fileId,
      name: fileName,
      mime_type: fileMimeType,
      is_directory: fileIsDirectory,
      uri: fileUri,
    } as File);
    setKind(cat);
    setLoading(true);
    setError(null);

    const run = async () => {
      try {
        const chunkSourceWorkspaceId = workspaceId;
        const shouldUseChunkSource =
          (chunkSourceWorkspaceId != null || fetchers != null) &&
          (cat === 'markdown' ||
            cat === 'text' ||
            (cat === 'office' &&
              isDocxPreviewFile({
                id: fileId,
                name: fileName,
                mime_type: fileMimeType,
                is_directory: fileIsDirectory,
                uri: fileUri,
              } as File)));
        if (shouldUseChunkSource) {
          try {
            const src = await fetchChunkSource(fileId, chunkSourceWorkspaceId, fetchers);
            if (cancelled) return;
            setChunkSourceText(src.content);
            setUsingChunkSource(true);
            setLoading(false);
            if (onBlobReady) {
              void fetchContentBlob(fileId, chunkSourceWorkspaceId, fetchers)
                .then((blob) => {
                  if (!cancelled) onBlobReady(blob);
                })
                .catch(() => {
                  /* download remains unavailable */
                });
            }
            return;
          } catch {
            if (cancelled) return;
            setUsingChunkSource(false);
          }
        }

        if (cat === 'office') {
          const prev = await fetchPreview(fileId, workspaceId, fetchers);
          if (cancelled) return;
          if (prev.format === 'html') {
            setOfficeHtml(prev.content);
            setOfficeText(null);
          } else {
            setOfficeText(prev.content);
            setOfficeHtml(null);
          }
          const blob = await fetchContentBlob(fileId, workspaceId, fetchers);
          if (cancelled) return;
          onBlobReady?.(blob);
          setLoading(false);
          return;
        }

        if (cat === 'unsupported') {
          try {
            const blob = await fetchContentBlob(fileId, workspaceId, fetchers);
            if (!cancelled) onBlobReady?.(blob);
          } catch {
            /* ignore */
          }
          if (cancelled) return;
          setError(unsupportedMessage);
          setLoading(false);
          return;
        }

        const blob = await fetchContentBlob(fileId, workspaceId, fetchers);
        if (cancelled) return;
        onBlobReady?.(blob);

        if (cat === 'pdf') {
          const bytes = new Uint8Array(await blob.arrayBuffer());
          if (cancelled) return;
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
          setPdfSource(pushObjectUrl(URL.createObjectURL(pdfBlob)));
          setLoading(false);
          return;
        }

        if (cat === 'image') {
          setImageUrl(pushObjectUrl(URL.createObjectURL(blob)));
          setLoading(false);
          return;
        }

        const text = await blob.text();
        if (cancelled) return;
        if (cat === 'markdown') setMarkdownBody(text);
        else if (cat === 'html') setHtmlBody(text);
        else setTextBody(text);
        setLoading(false);
      } catch (e: unknown) {
        if (cancelled) return;
        console.error(e);
        setError(loadFailedMessage);
        setLoading(false);
      }
    };

    void run();
    return () => {
      cancelled = true;
      revokeAll();
    };
  }, [
    active,
    fileId,
    fileName,
    fileMimeType,
    fileIsDirectory,
    fileUri,
    workspaceId,
    fetchers,
    chunkLoadKey,
    resetState,
    revokeAll,
    pushObjectUrl,
    unsupportedMessage,
    loadFailedMessage,
    onBlobReady,
  ]);

  const highlightPdfPage: number =
    chunk != null && (chunk.page ?? 0) > 0 ? (chunk.page as number) : 1;

  const scrollPdfToTarget = useCallback(() => {
    if (!active || kind !== 'pdf' || !numPages) return;
    const sp = scrollRef.current;
    if (!sp) return;
    const pid = Math.min(Math.max(1, highlightPdfPage), numPages);
    const el = findPdfPage(sp, pid);
    scrollElementIntoScrollParent(sp, el, 'auto');
  }, [active, kind, numPages, highlightPdfPage]);

  useLayoutEffect(() => {
    if (!active || kind !== 'pdf' || !numPages) return;
    scrollPdfToTarget();
    const t1 = window.setTimeout(scrollPdfToTarget, 100);
    const t2 = window.setTimeout(scrollPdfToTarget, 400);
    const t3 = window.setTimeout(scrollPdfToTarget, 900);
    const t4 = window.setTimeout(scrollPdfToTarget, 1600);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      clearTimeout(t3);
      clearTimeout(t4);
    };
  }, [active, kind, numPages, highlightPdfPage, pdfScale, scrollPdfToTarget]);

  const title = file && !file.is_directory ? file.name || file.uri?.split('/').pop() || '' : '';

  return (
    <Spin spinning={loading}>
      <div className={`document-source-preview${embedded ? ' document-source-preview--embedded' : ''}`}>
        <div
          ref={scrollRef}
          className="document-source-preview-scroll"
          style={PREVIEW_SCROLL_STYLE}
          data-chunk-preview-scroll
        >
          {error ? (
            <Alert type="warning" message={error} showIcon />
          ) : kind === 'pdf' && pdfSource && chunk ? (
            <>
              <Space style={{ marginBottom: 12 }} wrap>
                <Button icon={<ZoomOutOutlined />} onClick={() => setPdfScale((s) => Math.max(0.5, s - 0.15))} />
                <Text>{Math.round(pdfScale * 100)}%</Text>
                <Button icon={<ZoomInOutlined />} onClick={() => setPdfScale((s) => Math.min(2.5, s + 0.15))} />
                {numPages > 0 ? (
                  <Text type="secondary">{t('files.preview.page_info', { count: numPages })}</Text>
                ) : null}
              </Space>
              <Document
                file={pdfSource}
                onSourceError={(err) => {
                  const msg = err instanceof Error ? err.message : String(err);
                  setError(`${t('files.preview.pdf_error')}: ${msg}`);
                }}
                onLoadError={(err) => {
                  const msg = err instanceof Error ? err.message : String(err);
                  setError(`${t('files.preview.pdf_error')}: ${msg}`);
                }}
                onLoadSuccess={(info) => {
                  setNumPages(info.numPages);
                  const pid = Math.min(Math.max(1, highlightPdfPage), info.numPages || 1);
                  const retry = () => {
                    const sp = scrollRef.current;
                    const el = sp ? findPdfPage(sp, pid) : null;
                    if (sp && el) scrollElementIntoScrollParent(sp, el, 'auto');
                  };
                  requestAnimationFrame(() => requestAnimationFrame(retry));
                  window.setTimeout(retry, 120);
                  window.setTimeout(retry, 500);
                  window.setTimeout(retry, 1200);
                }}
                loading={t('files.preview.loading')}
                error={t('files.preview.pdf_error')}
              >
                {Array.from({ length: numPages }, (_, i) => {
                  const pn = i + 1;
                  return (
                    <PdfPageWithBBox
                      key={pn}
                      pageNumber={pn}
                      scale={pdfScale}
                      highlightPage={highlightPdfPage}
                      chunk={chunk}
                      instanceId={instanceDomId}
                    />
                  );
                })}
              </Document>
            </>
          ) : kind === 'image' && imageUrl ? (
            <div style={{ textAlign: 'center' }}>
              <img src={imageUrl} alt={title} style={{ maxWidth: '100%', height: 'auto' }} />
            </div>
          ) : usingChunkSource && chunkSourceText != null && chunk ? (
            <pre
              style={{
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontFamily: 'monospace',
                fontSize: 13,
                margin: 0,
              }}
            >
              {renderTextWithNav(chunkSourceText, chunk, instanceDomId)}
            </pre>
          ) : kind === 'markdown' && markdownBody != null ? (
            <div className="file-preview-markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdownBody}</ReactMarkdown>
            </div>
          ) : kind === 'html' && htmlBody != null ? (
            <div
              className="file-preview-html"
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
              ref={officeHostRef}
              className="file-preview-office-html"
              dangerouslySetInnerHTML={{
                __html: DOMPurify.sanitize(officeHtml, { USE_PROFILES: { html: true } }),
              }}
            />
          ) : kind === 'office' && officeText != null ? (
            <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>
              {officeText}
            </pre>
          ) : !loading && kind === 'unsupported' ? (
            <Alert type="info" message={unsupportedMessage} showIcon />
          ) : null}
        </div>
      </div>
    </Spin>
  );
}
