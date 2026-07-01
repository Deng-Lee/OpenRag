import { useEffect, useRef, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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
  Document: ({
    children,
    onLoadSuccess,
  }: {
    children: ReactNode;
    onLoadSuccess?: (info: { numPages: number }) => void;
  }) => {
    useEffect(() => {
      onLoadSuccess?.({ numPages: 5 });
    }, [onLoadSuccess]);
    return <div>{children}</div>;
  },
  Page: ({
    pageNumber,
    onRenderSuccess,
  }: {
    pageNumber?: number;
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

    return <canvas ref={canvasRef} data-page-number={pageNumber} />;
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

function pdfFile() {
  return {
    ...textFile(),
    name: 'report.pdf',
    uri: '/docs/report.pdf',
    mime_type: 'application/pdf',
  };
}

function pdfBlob(): Blob {
  const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31]);
  return {
    arrayBuffer: () => Promise.resolve(bytes.buffer),
  } as Blob;
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
  URL.createObjectURL = vi.fn(() => 'blob:openrag-test') as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;
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

  it('scrolls PDF previews to the explicit initial page when provided', async () => {
    filesApiMock.fetchContentBlob.mockResolvedValueOnce(pdfBlob());
    const scrollTo = vi.fn();
    const originalScrollTo = HTMLElement.prototype.scrollTo;
    const originalGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;
    HTMLElement.prototype.scrollTo = scrollTo as unknown as HTMLElement['scrollTo'];
    HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect() {
      const page = Number(this.getAttribute('data-pdf-page'));
      const top = Number.isFinite(page) && page > 0 ? page * 100 : 0;
      return {
        x: 0,
        y: top,
        top,
        left: 0,
        bottom: top + 80,
        right: 100,
        width: 100,
        height: 80,
        toJSON: () => ({}),
      } as DOMRect;
    };

    try {
      render(
        <DocumentSourcePreview
          file={pdfFile()}
          chunk={{
            ...textChunk(0, 5),
            page: 1,
          }}
          initialPage={4}
        />
      );

      await waitFor(() => {
        expect(
          scrollTo.mock.calls.some(
            ([arg]) => typeof arg === 'object' && arg?.top === 376 && arg?.behavior === 'auto'
          )
        ).toBe(true);
      });
    } finally {
      HTMLElement.prototype.scrollTo = originalScrollTo;
      HTMLElement.prototype.getBoundingClientRect = originalGetBoundingClientRect;
    }
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

  it('uses custom fetchers when provided', async () => {
    const fetchers = {
      fetchContentBlob: vi.fn().mockResolvedValue(textBlob('alpha beta gamma')),
      fetchPreview: vi.fn().mockResolvedValue({ format: 'text' as const, content: 'alpha beta gamma' }),
      fetchChunkSource: vi.fn().mockResolvedValue({
        format: 'text' as const,
        content: 'alpha beta gamma',
      }),
    };

    const { container } = render(
      <DocumentSourcePreview
        file={textFile()}
        chunk={textChunk(6, 10)}
        fetchers={fetchers}
      />
    );

    await waitFor(() => {
      expect(container.querySelector('.chunk-text-highlight')?.textContent).toBe('beta');
    });
    expect(fetchers.fetchChunkSource).toHaveBeenCalled();
    expect(filesApiMock.fetchWorkspaceChunkSource).not.toHaveBeenCalled();
    expect(filesApiMock.fetchContentBlob).not.toHaveBeenCalled();
  });

  it('uses custom unsupported preview message', async () => {
    render(
      <DocumentSourcePreview
        file={officeFile('archive.zip', 'application/zip')}
        chunk={textChunk(0, 5)}
        previewMessages={{
          unsupported: '该文件类型暂不支持内联预览。',
        }}
      />
    );

    await screen.findByText('该文件类型暂不支持内联预览。');
    expect(screen.queryByText(/下载/)).not.toBeInTheDocument();
    expect(screen.queryByText(/download/i)).not.toBeInTheDocument();
  });

  it('uses custom load failed preview message', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    filesApiMock.fetchContentBlob.mockRejectedValueOnce(new Error('boom'));

    render(
      <DocumentSourcePreview
        file={textFile()}
        chunk={textChunk(0, 5)}
        previewMessages={{
          loadFailed: '无法加载文档预览。',
        }}
      />
    );

    await screen.findByText('无法加载文档预览。');
    expect(consoleError).toHaveBeenCalled();
    expect(screen.queryByText(/下载/)).not.toBeInTheDocument();
    expect(screen.queryByText(/download/i)).not.toBeInTheDocument();
    consoleError.mockRestore();
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
