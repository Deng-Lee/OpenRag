import { useEffect, useRef, type ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { DocumentSourcePreview, PdfPageWithBBox, extractPdfPositions } from './index';

const i18nMock = vi.hoisted(() => ({
  t: (key: string) => key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => i18nMock,
}));

vi.mock('../../services/api', () => ({
  filesAPI: {
    fetchWorkspaceContentBlob: vi.fn(() =>
      Promise.resolve({
        text: () => Promise.resolve('alpha beta gamma'),
      } as Blob)
    ),
    fetchContentBlob: vi.fn(() =>
      Promise.resolve({
        text: () => Promise.resolve('alpha beta gamma'),
      } as Blob)
    ),
    fetchWorkspacePreview: vi.fn(),
    fetchPreview: vi.fn(),
  },
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
  it('does not highlight text chunks for non-PDF files', async () => {
    const { container } = render(
      <DocumentSourcePreview
        file={{
          id: 10,
          owner_id: 1,
          name: 'notes.txt',
          uri: '/docs/notes.txt',
          mime_type: 'text/plain',
          is_directory: false,
          size: 16,
          created_at: '2026-06-01T00:00:00Z',
          updated_at: '2026-06-01T00:00:00Z',
        }}
        workspaceId={1}
        chunk={{
          file_id: 10,
          workspace_id: 1,
          filename: 'notes.txt',
          chunk_id: 'chunk-1',
          chunk_index: 0,
          text: 'beta',
          is_truncated: false,
          source_char_start: 6,
          source_char_end: 10,
        }}
      />
    );

    await waitFor(() => {
      expect(container.textContent).toContain('alpha beta gamma');
      expect(container.querySelector('.chunk-text-highlight')).toBeNull();
    });
  });
});
