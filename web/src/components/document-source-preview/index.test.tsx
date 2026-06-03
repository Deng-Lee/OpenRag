import { useEffect, useRef, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { DocumentSourcePreview, PdfPageWithBBox, extractPdfPositions } from './index';

const i18nMock = vi.hoisted(() => ({
  t: (key: string) => key,
}));

const filesApiMock = vi.hoisted(() => ({
  fetchWorkspaceContentBlob: vi.fn(),
  fetchContentBlob: vi.fn(),
  fetchWorkspacePreview: vi.fn(),
  fetchPreview: vi.fn(),
  fetchWorkspaceChunkSource: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => i18nMock,
}));

vi.mock('../../services/api', () => ({
  filesAPI: filesApiMock,
}));

vi.mock('react-pdf', () => ({
  Document: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Page: ({
    onRenderSuccess,
  }: {
    onRenderSuccess?: (page: { getViewport: (p: { scale: number }) => { width: number; height: number } }) => void;
  }) => {
    const canvasRef = useRef<HTMLCanvasElement>(null);

    useEffect(() => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      Object.defineProperty(canvas, 'clientWidth', { configurable: true, value: 200 });
      Object.defineProperty(canvas, 'clientHeight', { configurable: true, value: 100 });
      onRenderSuccess?.({
        getViewport: () => ({ width: 200, height: 100 }),
      });
    }, [onRenderSuccess]);

    return <canvas ref={canvasRef} />;
  },
}));

function textBlob(text: string): Blob {
  return {
    text: () => Promise.resolve(text),
  } as Blob;
}

function textFile() {
  return {
    id: 10,
    owner_id: 1,
    name: 'notes.txt',
    uri: '/docs/notes.txt',
    mime_type: 'text/plain',
    is_directory: false,
    size: 16,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
  };
}

function officeFile(name: string, mimeType: string) {
  return {
    ...textFile(),
    name,
    uri: `/docs/${name}`,
    mime_type: mimeType,
  };
}

function textChunk(sourceCharStart: number, sourceCharEnd: number, text = 'beta') {
  return {
    file_id: 10,
    workspace_id: 1,
    filename: 'notes.txt',
    chunk_id: 'chunk-1',
    chunk_index: 0,
    text,
    is_truncated: false,
    source_char_start: sourceCharStart,
    source_char_end: sourceCharEnd,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  filesApiMock.fetchWorkspaceContentBlob.mockResolvedValue(textBlob('alpha beta gamma'));
  filesApiMock.fetchContentBlob.mockResolvedValue(textBlob('alpha beta gamma'));
  filesApiMock.fetchWorkspacePreview.mockResolvedValue({ format: 'text', content: 'alpha beta gamma' });
  filesApiMock.fetchPreview.mockResolvedValue({ format: 'text', content: 'alpha beta gamma' });
  filesApiMock.fetchWorkspaceChunkSource.mockResolvedValue({
    format: 'text',
    content: 'alpha beta gamma',
  });
});

describe('document-source-preview PDF positions', () => {
  it('keeps multiple top-level PDF positions', () => {
    const positions = extractPdfPositions({
      text: 'chunk',
      score: 1,
      file_id: 1,
      position_int: [
        [1, 10, 20, 30, 40],
        [1, 50, 60, 70, 80],
      ],
    });

    expect(positions).toEqual([
      [1, 10, 20, 30, 40],
      [1, 50, 60, 70, 80],
    ]);
  });

  it('renders one highlight box for each position on the current page', async () => {
    const { container } = render(
      <PdfPageWithBBox
        pageNumber={2}
        scale={1}
        highlightPage={1}
        chunk={{
          text: 'chunk',
          score: 1,
          file_id: 1,
          position_int: [
            [2, 10, 30, 20, 40],
            [2, 50, 70, 60, 80],
            [3, 1, 2, 3, 4],
          ],
        }}
      />
    );

    await waitFor(() => {
      expect(container.querySelectorAll('.chunk-pdf-highlight')).toHaveLength(2);
    });
  });
});

describe('document-source-preview non-PDF chunk behavior', () => {
  it('highlights text chunks against workspace canonical source', async () => {
    const { container } = render(
      <DocumentSourcePreview
        file={textFile()}
        workspaceId={1}
        chunk={textChunk(6, 10)}
      />
    );

    await waitFor(() => {
      expect(container.querySelector('.chunk-text-highlight')?.textContent).toBe('beta');
    });
    expect(filesApiMock.fetchWorkspaceChunkSource).toHaveBeenCalledWith(1, 10);
  });

  it('falls back to raw preview without offset highlight when chunk source is missing', async () => {
    filesApiMock.fetchWorkspaceChunkSource.mockRejectedValueOnce(new Error('not found'));

    const { container } = render(
      <DocumentSourcePreview
        file={textFile()}
        workspaceId={1}
        chunk={textChunk(6, 10)}
      />
    );

    await waitFor(() => {
      expect(container.textContent).toContain('alpha beta gamma');
      expect(container.querySelector('.chunk-text-highlight')).toBeNull();
    });
  });

  it('converts Python code point offsets before highlighting canonical source', async () => {
    filesApiMock.fetchWorkspaceChunkSource.mockResolvedValueOnce({
      format: 'text',
      content: '😀 beta gamma',
    });

    const { container } = render(
      <DocumentSourcePreview
        file={textFile()}
        workspaceId={1}
        chunk={textChunk(2, 6)}
      />
    );

    await waitFor(() => {
      expect(container.querySelector('.chunk-text-highlight')?.textContent).toBe('beta');
    });
  });

  it('uses canonical source for DOCX chunks', async () => {
    const { container } = render(
      <DocumentSourcePreview
        file={officeFile(
          'report.docx',
          'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )}
        workspaceId={1}
        chunk={textChunk(6, 10)}
      />
    );

    await waitFor(() => {
      expect(container.querySelector('.chunk-text-highlight')?.textContent).toBe('beta');
    });
    expect(filesApiMock.fetchWorkspaceChunkSource).toHaveBeenCalledWith(1, 10);
  });

  it('keeps non-DOCX office files on the existing preview path', async () => {
    filesApiMock.fetchWorkspacePreview.mockResolvedValueOnce({
      format: 'text',
      content: 'spreadsheet preview',
    });

    const { container } = render(
      <DocumentSourcePreview
        file={officeFile(
          'sheet.xlsx',
          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )}
        workspaceId={1}
        chunk={textChunk(6, 10)}
      />
    );

    await waitFor(() => {
      expect(container.textContent).toContain('spreadsheet preview');
    });
    expect(filesApiMock.fetchWorkspaceChunkSource).not.toHaveBeenCalled();
    expect(container.querySelector('.chunk-text-highlight')).toBeNull();
  });
});
