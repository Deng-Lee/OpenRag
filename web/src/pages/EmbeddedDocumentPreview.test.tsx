import { useEffect } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import EmbeddedDocumentPreview from './EmbeddedDocumentPreview';

const embedApiMock = vi.hoisted(() => ({
  readPreviewTokenFromHash: vi.fn(),
  embedPreviewAPI: {
    getContext: vi.fn(),
    fetchContentBlob: vi.fn(),
    fetchPreview: vi.fn(),
    fetchChunkSource: vi.fn(),
  },
}));

const previewMock = vi.hoisted(() => ({
  calls: [] as Array<{
    file: { id: number; name: string } | null;
    chunk: { chunk_id: string } | null;
    previewMessages?: {
      unsupported?: string;
      loadFailed?: string;
    };
    fetchers?: {
      fetchContentBlob: () => Promise<Blob>;
      fetchPreview: () => Promise<{ format: 'html' | 'text'; content: string }>;
      fetchChunkSource: () => Promise<{ format: 'text'; content: string }>;
    };
  }>,
}));

vi.mock('../services/embedPreviewApi', () => embedApiMock);

vi.mock('../components/document-source-preview', () => ({
  DocumentSourcePreview: (props: {
    file: { id: number; name: string } | null;
    chunk: { chunk_id: string } | null;
    previewMessages?: {
      unsupported?: string;
      loadFailed?: string;
    };
    fetchers?: {
      fetchContentBlob: () => Promise<Blob>;
      fetchPreview: () => Promise<{ format: 'html' | 'text'; content: string }>;
      fetchChunkSource: () => Promise<{ format: 'text'; content: string }>;
    };
  }) => {
    previewMock.calls.push(props);
    useEffect(() => {
      void props.fetchers?.fetchContentBlob();
      void props.fetchers?.fetchPreview();
      void props.fetchers?.fetchChunkSource();
    }, [props.fetchers]);
    return (
      <div data-testid="embedded-document-preview-child">
        <span>{props.file?.name}</span>
        <span>{props.previewMessages?.unsupported}</span>
        <span>{props.previewMessages?.loadFailed}</span>
      </div>
    );
  },
}));

function contextResponse() {
  return {
    file: {
      id: 9,
      workspace_id: 7,
      name: 'report.txt',
      uri: '/docs/report.txt',
      mime_type: 'text/plain',
      processing_status: 'completed',
      simple_status: 'done',
      total_chunks: 1,
    },
    chunk: {
      file_id: 9,
      workspace_id: 7,
      filename: 'report.txt',
      chunk_id: 'chunk-1',
      chunk_index: 0,
      text: 'hello',
      is_truncated: false,
      source_char_start: 0,
      source_char_end: 5,
    },
    expires_at: '2026-06-03T10:00:00Z',
  };
}

describe('EmbeddedDocumentPreview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    previewMock.calls = [];
    localStorage.setItem('token', 'must-stay');
    window.history.replaceState(null, '', '/embed/document-preview#token=abc');
    embedApiMock.readPreviewTokenFromHash.mockReturnValue('abc');
    embedApiMock.embedPreviewAPI.getContext.mockResolvedValue(contextResponse());
    embedApiMock.embedPreviewAPI.fetchContentBlob.mockResolvedValue(new Blob(['hello']));
    embedApiMock.embedPreviewAPI.fetchPreview.mockResolvedValue({ format: 'text', content: 'hello' });
    embedApiMock.embedPreviewAPI.fetchChunkSource.mockResolvedValue({ format: 'text', content: 'hello' });
  });

  it('loads context from the hash token and passes preview-token fetchers to the preview component', async () => {
    render(<EmbeddedDocumentPreview />);

    expect(embedApiMock.readPreviewTokenFromHash).toHaveBeenCalledWith('#token=abc');
    await screen.findByTestId('embedded-document-preview-child');

    expect(embedApiMock.embedPreviewAPI.getContext).toHaveBeenCalledWith('abc');
    await waitFor(() => {
      expect(embedApiMock.embedPreviewAPI.fetchContentBlob).toHaveBeenCalledWith('abc');
      expect(embedApiMock.embedPreviewAPI.fetchPreview).toHaveBeenCalledWith('abc');
      expect(embedApiMock.embedPreviewAPI.fetchChunkSource).toHaveBeenCalledWith('abc');
    });
    expect(previewMock.calls[0]).toEqual(
      expect.objectContaining({
        file: expect.objectContaining({ id: 9, name: 'report.txt' }),
        chunk: expect.objectContaining({ chunk_id: 'chunk-1' }),
        fetchers: expect.any(Object),
        previewMessages: {
          unsupported: '该文件类型暂不支持内联预览。',
          loadFailed: '无法加载文档预览。',
        },
      })
    );
  });

  it('shows an error and does not call the API when token is missing', () => {
    embedApiMock.readPreviewTokenFromHash.mockReturnValueOnce(null);

    render(<EmbeddedDocumentPreview />);

    expect(screen.getByText(/预览链接缺少 token/)).toBeInTheDocument();
    expect(embedApiMock.embedPreviewAPI.getContext).not.toHaveBeenCalled();
  });

  it('shows auth errors without redirecting to login or clearing local storage', async () => {
    embedApiMock.embedPreviewAPI.getContext.mockRejectedValueOnce({ response: { status: 401 } });

    render(<EmbeddedDocumentPreview />);

    await screen.findByText(/预览链接无效或已过期/);
    expect(localStorage.getItem('token')).toBe('must-stay');
    expect(window.location.pathname).toBe('/embed/document-preview');
  });

  it('does not render download copy', async () => {
    render(<EmbeddedDocumentPreview />);

    await screen.findByTestId('embedded-document-preview-child');
    expect(screen.getByText('该文件类型暂不支持内联预览。')).toBeInTheDocument();
    expect(screen.getByText('无法加载文档预览。')).toBeInTheDocument();
    expect(screen.queryByText(/下载/)).not.toBeInTheDocument();
    expect(screen.queryByText(/download/i)).not.toBeInTheDocument();
  });
});
