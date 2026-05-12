/**
 * 检索结果「源文件预览」：携带 chunk 的定位元数据（页码、bbox、偏移、chunk_id），
 * PDF 跳页 + 覆盖层高亮；文本类按偏移/片段高亮；Office HTML 按正文片段滚动（Range）。
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { Document, Page } from 'react-pdf';
import type { File, SearchResult } from '../types';
import { filesAPI } from '../services/api';
import { Modal, Spin, Alert, Space, Button, Typography, Tag } from 'antd';
import { ZoomOutOutlined, ZoomInOutlined, DownloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import DOMPurify from 'dompurify';
import { classifyPreviewFile } from '../utils/file-preview-kind';
import {
  chunkDomId,
  scrollContainerToChildCenter,
  scrollByRatio,
  scrollElementIntoScrollParent,
  scrollToSnippetInElement,
} from '../utils/chunk-preview-navigation';

import './file-preview.css';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';

const { Text } = Typography;
type PdfPos = [number, number, number, number, number];

const SCROLL_BODY: CSSProperties = {
  maxHeight: 'calc(100vh - 140px)',
  overflow: 'auto',
  padding: '8px 4px',
};

function PdfPageWithBBox({
  pageNumber,
  scale,
  highlightPage,
  chunk,
}: {
  pageNumber: number;
  scale: number;
  highlightPage: number;
  chunk: SearchResult;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [boxes, setBoxes] = useState<Array<{ left: number; top: number; width: number; height: number }>>([]);

  const extractPdfPositions = useCallback((c: SearchResult): PdfPos[] => {
    const topLevel = Array.isArray(c.position_int) ? c.position_int : null;
    const meta = (c.metadata || {}) as Record<string, unknown>;
    const inMeta = Array.isArray(meta.position_int) ? (meta.position_int as unknown[]) : null;
    const legacy = Array.isArray(c.positions) ? (c.positions as unknown[]) : null;
    const raw = topLevel ?? inMeta ?? legacy ?? [];
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
  }, []);

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
      id={`openrag-pdf-page-${pageNumber}`}
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

function chunkAnchorId(chunk: SearchResult): string {
  return chunk.chunk_id ? `chunk-${chunkDomId(chunk.chunk_id)}` : 'chunk-nav-anchor';
}

/** 在正文中定位检索片段起点（多长度子串） */
function findTextSnippetStart(text: string, chunk: SearchResult): number {
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

function renderTextWithNav(text: string, chunk: SearchResult): React.ReactNode {
  const aid = chunkAnchorId(chunk);
  const s = chunk.start_offset ?? 0;
  const e = chunk.end_offset ?? 0;
  const snippet = (chunk.text || '').trim().slice(0, 200);
  if (e > s && e <= text.length) {
    return (
      <>
        {text.slice(0, s)}
        <mark id={aid} className="chunk-text-highlight">
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
          <mark id={aid} className="chunk-text-highlight">
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
        <mark id={aid} className="chunk-text-highlight">
          {text.slice(fb, fb + elen)}
        </mark>
        {text.slice(fb + elen)}
      </>
    );
  }
  return text;
}

function renderMarkdownWithAnchor(md: string, chunk: SearchResult): React.ReactNode {
  const aid = chunkAnchorId(chunk);
  const snippet = (chunk.text || '').trim().slice(0, 200);
  let idx = -1;
  if (snippet.length > 2) {
    for (const len of [80, 48, 32]) {
      const needle = snippet.slice(0, len);
      if (needle.length < 4) break;
      idx = md.indexOf(needle);
      if (idx >= 0) break;
    }
  }
  if (idx < 0) idx = findTextSnippetStart(md, chunk);
  if (idx >= 0) {
    const mid = md.slice(idx, idx + Math.min(Math.max(snippet.length, 40), 400));
    const before = md.slice(0, idx);
    const after = md.slice(idx + mid.length);
    return (
      <>
        {before ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{before}</ReactMarkdown> : null}
        <div id={aid} className="chunk-html-highlight-wrap">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{mid}</ReactMarkdown>
        </div>
        {after ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{after}</ReactMarkdown> : null}
      </>
    );
  }
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>;
}

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
  const scrollRef = useRef<HTMLDivElement>(null);
  const officeHostRef = useRef<HTMLDivElement>(null);

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
    if (!open || !file || file.is_directory || !chunk) {
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
            /* ignore */
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
  }, [open, file, chunk?.chunk_id, resetState, revokeAll, pushObjectUrl, t]);

  /** PDF：跳转到 chunk.page（解析侧为 1-based；0 / 缺失则落在第 1 页） */
  const highlightPdfPage: number =
    chunk != null && (chunk.page ?? 0) > 0 ? (chunk.page as number) : 1;

  const scrollPdfToTarget = useCallback(() => {
    if (!open || kind !== 'pdf' || !numPages) return;
    const sp = scrollRef.current;
    if (!sp) return;
    const pid = Math.min(Math.max(1, highlightPdfPage), numPages);
    const el = document.getElementById(`openrag-pdf-page-${pid}`);
    /** auto：避免短时间多次 smooth 互相取消，导致看起来「没跳转」 */
    scrollElementIntoScrollParent(sp, el, 'auto');
  }, [open, kind, numPages, highlightPdfPage]);

  /** PDF：多时机重试（Page 异步渲染完才有目标节点） */
  useLayoutEffect(() => {
    if (!open || kind !== 'pdf' || !numPages) return;
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
  }, [open, kind, numPages, highlightPdfPage, pdfScale, scrollPdfToTarget]);

  /** 文本 / MD / 源 HTML：滚到锚点 */
  const runTextNav = useCallback(() => {
    if (!open || !chunk || !scrollRef.current) return;
    const root = scrollRef.current;
    const aid = chunkAnchorId(chunk);
    const anchor = document.getElementById(aid);
    if (anchor) {
      scrollContainerToChildCenter(root, anchor, 'auto');
      anchor.classList.add('chunk-highlight-pulse');
      window.setTimeout(() => anchor.classList.remove('chunk-highlight-pulse'), 2000);
      return;
    }
    if (!['text', 'markdown', 'html', 'office'].includes(kind)) return;
    if (kind === 'text' && textBody && textBody.length > 0 && chunk.start_offset != null) {
      scrollByRatio(root, Math.min(1, chunk.start_offset / Math.max(textBody.length, 1)));
      return;
    }
    if (
      kind === 'office' &&
      officeText != null &&
      officeText.length > 0 &&
      chunk.start_offset != null
    ) {
      scrollByRatio(root, Math.min(1, chunk.start_offset / Math.max(officeText.length, 1)));
      return;
    }
    if (kind === 'markdown' && markdownBody && markdownBody.length > 0 && chunk.start_offset != null) {
      scrollByRatio(root, Math.min(1, chunk.start_offset / markdownBody.length));
      return;
    }
    if (kind === 'html' && htmlBody) {
      const el = root.querySelector('.file-preview-html') as HTMLElement | null;
      if (el) {
        const found = scrollToSnippetInElement(el, root, chunk.text || '', {
          flashClass: 'chunk-text-highlight',
        });
        if (!found && chunk.start_offset != null) {
          scrollByRatio(root, Math.min(1, chunk.start_offset / Math.max(htmlBody.length, 1)));
        }
      }
    }
  }, [open, kind, chunk, textBody, markdownBody, htmlBody, officeText]);

  useLayoutEffect(() => {
    if (!open || loading) return;
    if (!['text', 'markdown', 'html', 'office'].includes(kind)) return;
    if (kind === 'text' && textBody == null) return;
    if (kind === 'markdown' && markdownBody == null) return;
    if (kind === 'html' && htmlBody == null) return;
    if (kind === 'office' && officeText == null && officeHtml == null) return;
    requestAnimationFrame(() => {
      requestAnimationFrame(runTextNav);
    });
    const t1 = window.setTimeout(runTextNav, 180);
    const t2 = window.setTimeout(runTextNav, 450);
    const t3 = window.setTimeout(runTextNav, 900);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      clearTimeout(t3);
    };
  }, [
    open,
    loading,
    kind,
    textBody,
    markdownBody,
    htmlBody,
    officeText,
    officeHtml,
    chunk?.chunk_id,
    runTextNav,
  ]);

  /** Office 预览 HTML：在渲染容器内按片段滚动 */
  useLayoutEffect(() => {
    if (!open || loading || kind !== 'office' || !officeHtml || !chunk) return;
    const root = scrollRef.current;
    const run = () => {
      const host = officeHostRef.current;
      if (!root || !host) return;
      scrollToSnippetInElement(host, root, chunk.text || '', { flashClass: 'chunk-text-highlight' });
    };
    requestAnimationFrame(() => requestAnimationFrame(run));
    const t1 = window.setTimeout(run, 150);
    const t2 = window.setTimeout(run, 450);
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, [open, loading, kind, officeHtml, chunk?.chunk_id, chunk]);

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
    ? `${t('searchPage.pos_page')} ${chunk.page ?? 0} · ${chunkDomId(chunk.chunk_id)}`
    : '';

  const bumpNavigation = useCallback(() => {
    scrollPdfToTarget();
    runTextNav();
    if (open && !loading && kind === 'office' && officeHtml && chunk) {
      const root = scrollRef.current;
      const host = officeHostRef.current;
      if (root && host) {
        scrollToSnippetInElement(host, root, chunk.text || '', { flashClass: 'chunk-text-highlight' });
      }
    }
  }, [open, loading, kind, officeHtml, chunk, scrollPdfToTarget, runTextNav]);

  return (
    <Modal
      title={
        <Space wrap align="center">
          <span>{title}</span>
          {chunk ? <Tag color="gold">{chunkLabel}</Tag> : null}
          {file && !file.is_directory ? (
            <Button type="text" icon={<DownloadOutlined />} onClick={handleDownload} size="small">
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
      afterOpenChange={(vis) => {
        if (!vis) return;
        requestAnimationFrame(() => requestAnimationFrame(bumpNavigation));
        window.setTimeout(bumpNavigation, 320);
        window.setTimeout(bumpNavigation, 700);
      }}
      maskClosable={false}
      keyboard
      closable
    >
      <Spin spinning={loading}>
        <div ref={scrollRef} style={SCROLL_BODY} data-chunk-preview-scroll>
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
                  const pid = Math.min(
                    Math.max(1, highlightPdfPage),
                    info.numPages || 1
                  );
                  const retry = () => {
                    const sp = scrollRef.current;
                    const el = document.getElementById(`openrag-pdf-page-${pid}`);
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
                    />
                  );
                })}
              </Document>
            </>
          ) : kind === 'image' && imageUrl ? (
            <div style={{ textAlign: 'center' }}>
              <img src={imageUrl} alt={title} style={{ maxWidth: '100%', height: 'auto' }} />
            </div>
          ) : kind === 'markdown' && markdownBody != null && chunk ? (
            <div className="file-preview-markdown">{renderMarkdownWithAnchor(markdownBody, chunk)}</div>
          ) : kind === 'html' && htmlBody != null ? (
            <div
              className="file-preview-html"
              dangerouslySetInnerHTML={{
                __html: DOMPurify.sanitize(htmlBody, { USE_PROFILES: { html: true } }),
              }}
            />
          ) : kind === 'text' && textBody != null && chunk ? (
            <pre
              style={{
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                fontFamily: 'monospace',
                fontSize: 13,
                margin: 0,
              }}
            >
              {renderTextWithNav(textBody, chunk)}
            </pre>
          ) : kind === 'office' && officeHtml != null ? (
            <div
              ref={officeHostRef}
              className="file-preview-office-html"
              dangerouslySetInnerHTML={{
                __html: DOMPurify.sanitize(officeHtml, { USE_PROFILES: { html: true } }),
              }}
            />
          ) : kind === 'office' && officeText != null && chunk ? (
            <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>
              {renderTextWithNav(officeText, chunk)}
            </pre>
          ) : !loading && kind === 'unsupported' ? (
            <Alert type="info" message={t('files.preview.unsupported')} showIcon />
          ) : null}
        </div>
      </Spin>
    </Modal>
  );
}
