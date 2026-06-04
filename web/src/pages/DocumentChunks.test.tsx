import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import DocumentChunks from './DocumentChunks';
import { filesAPI } from '../services/api';

const i18nMock = vi.hoisted(() => ({
  t: (key: string, values?: Record<string, unknown>) => {
    if (key === 'documentChunks.chunk.index') return `idx ${values?.index}`;
    if (key === 'documentChunks.chunk.page') return `page ${values?.page}`;
    if (key === 'documentChunks.workspaceId') return `workspace ${values?.workspaceId}`;
    if (key === 'documentChunks.fileId') return `file ${values?.fileId}`;
    if (key === 'documentChunks.stats') return `total ${values?.total}`;
    return key;
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: i18nMock.t,
  }),
}));

vi.mock('../services/api', () => ({
  filesAPI: {
    listChunks: vi.fn(),
  },
}));

vi.mock('../components/document-source-preview', () => ({
  DocumentSourcePreview: ({ chunk }: { chunk: { chunk_id: string } | null }) => (
    <div data-testid="document-source-preview">{chunk?.chunk_id}</div>
  ),
}));

function renderRoute(initialEntry: string | { pathname: string; search?: string; state?: unknown }) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/workspaces/:workspaceId/files/:fileId/chunks" element={<DocumentChunks />} />
        <Route path="/search" element={<div>search page</div>} />
        <Route path="/files" element={<div>files page</div>} />
      </Routes>
    </MemoryRouter>
  );
}

function mockChunksResponse() {
  vi.mocked(filesAPI.listChunks).mockResolvedValue({
    file: {
      id: 9,
      workspace_id: 7,
      name: 'report.pdf',
      uri: '/docs/report.pdf',
      mime_type: 'application/pdf',
      processing_status: 'completed',
      simple_status: 'done',
      total_chunks: 1,
    },
    items: [
      {
        file_id: 9,
        workspace_id: 7,
        filename: 'report.pdf',
        chunk_id: 'chunk-1',
        chunk_index: 1,
        text: 'alpha',
        is_truncated: false,
        page: 1,
        position_int: [],
        positions: [],
      },
    ],
    total: 1,
    skip: 0,
    limit: 20,
  });
}

describe('DocumentChunks target chunk navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Element.prototype.scrollTo = vi.fn() as unknown as Element['scrollTo'];
  });

  it('loads the page containing the target chunk and selects it', async () => {
    vi.mocked(filesAPI.listChunks).mockResolvedValue({
      file: {
        id: 9,
        workspace_id: 7,
        name: 'report.pdf',
        uri: '/docs/report.pdf',
        mime_type: 'application/pdf',
        processing_status: 'completed',
        simple_status: 'done',
        total_chunks: 50,
      },
      items: [
        {
          file_id: 9,
          workspace_id: 7,
          filename: 'report.pdf',
          chunk_id: 'chunk-40',
          chunk_index: 40,
          text: 'alpha',
          is_truncated: false,
          page: 4,
          position_int: [],
          positions: [],
        },
        {
          file_id: 9,
          workspace_id: 7,
          filename: 'report.pdf',
          chunk_id: 'chunk-42',
          chunk_index: 42,
          text: 'target text',
          is_truncated: false,
          page: 5,
          position_int: [],
          positions: [],
        },
      ],
      total: 50,
      skip: 40,
      limit: 20,
    });

    renderRoute('/workspaces/7/files/9/chunks?chunkId=chunk-42&chunkIndex=42');

    await waitFor(() => {
      expect(filesAPI.listChunks).toHaveBeenCalledWith(7, 9, {
        skip: 40,
        limit: 20,
        q: undefined,
      });
    });
    expect(await screen.findByTestId('document-source-preview')).toHaveTextContent('chunk-42');
    expect(screen.getByRole('button', { name: /idx 42/i })).toHaveClass(
      'document-chunks-card--selected'
    );
  });
});

describe('DocumentChunks back navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Element.prototype.scrollTo = vi.fn() as unknown as Element['scrollTo'];
    mockChunksResponse();
  });

  it('returns to search when route state comes from search', async () => {
    renderRoute({
      pathname: '/workspaces/7/files/9/chunks',
      state: { from: 'search' },
    });

    fireEvent.click(await screen.findByRole('button', { name: /documentChunks.back/i }));

    expect(screen.getByText('search page')).toBeInTheDocument();
  });

  it('returns to files when route state comes from files', async () => {
    renderRoute({
      pathname: '/workspaces/7/files/9/chunks',
      state: { from: 'files' },
    });

    fireEvent.click(await screen.findByRole('button', { name: /documentChunks.back/i }));

    expect(screen.getByText('files page')).toBeInTheDocument();
  });

  it('returns to files when route state is missing', async () => {
    renderRoute('/workspaces/7/files/9/chunks');

    fireEvent.click(await screen.findByRole('button', { name: /documentChunks.back/i }));

    expect(screen.getByText('files page')).toBeInTheDocument();
  });
});
