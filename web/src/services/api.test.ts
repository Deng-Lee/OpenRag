import { describe, it, expect, vi, beforeEach } from 'vitest';
import axios from 'axios';

const mocks = vi.hoisted(() => {
  const mockAxiosInstance = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  };

  return { mockAxiosInstance };
});

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => mocks.mockAxiosInstance),
  },
}));

describe('API Client', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it('creates axios instance with base URL', async () => {
    await import('./api');

    expect(axios.create).toHaveBeenCalledWith({ baseURL: expect.any(String) });
  });

  it('configures axios interceptors', async () => {
    await import('./api');

    expect(mocks.mockAxiosInstance.interceptors.request.use).toHaveBeenCalled();
    expect(mocks.mockAxiosInstance.interceptors.response.use).toHaveBeenCalled();
  });

  it('lists workspace file chunks with params', async () => {
    const data = {
      file: {
        id: 9,
        workspace_id: 7,
        name: 'report.pdf',
        uri: '/docs/report.pdf',
        mime_type: 'application/pdf',
        processing_status: 'completed',
        simple_status: 'done',
        total_chunks: 3,
      },
      items: [],
      total: 0,
      skip: 10,
      limit: 20,
    };
    mocks.mockAxiosInstance.get.mockResolvedValueOnce({ data });

    const { filesAPI } = await import('./api');
    const result = await filesAPI.listChunks(7, 9, { skip: 10, limit: 20, q: 'alpha' });

    expect(mocks.mockAxiosInstance.get).toHaveBeenCalledWith(
      '/workspaces/7/files/9/chunks',
      { params: { skip: 10, limit: 20, q: 'alpha' } }
    );
    expect(result).toBe(data);
  });

  it('fetches workspace file content as a blob', async () => {
    const blob = new Blob(['hello']);
    mocks.mockAxiosInstance.get.mockResolvedValueOnce({ data: blob });

    const { filesAPI } = await import('./api');
    const result = await filesAPI.fetchWorkspaceContentBlob(7, 9);

    expect(mocks.mockAxiosInstance.get).toHaveBeenCalledWith(
      '/workspaces/7/files/9/content',
      { responseType: 'blob' }
    );
    expect(result).toBe(blob);
  });

  it('fetches workspace file preview', async () => {
    const data = { format: 'text' as const, content: 'hello' };
    mocks.mockAxiosInstance.get.mockResolvedValueOnce({ data });

    const { filesAPI } = await import('./api');
    const result = await filesAPI.fetchWorkspacePreview(7, 9);

    expect(mocks.mockAxiosInstance.get).toHaveBeenCalledWith('/workspaces/7/files/9/preview');
    expect(result).toBe(data);
  });

  it('uploads files with document type in FormData', async () => {
    const data = { id: 1, name: 'manual.pdf' };
    mocks.mockAxiosInstance.post.mockResolvedValueOnce({ data });

    const { filesAPI } = await import('./api');
    const file = new File(['hello'], 'manual.pdf', { type: 'application/pdf' });
    const result = await filesAPI.upload(file, 'pdf', 7, '/docs', 'manual');

    const [url, formData] = mocks.mockAxiosInstance.post.mock.calls[0];
    expect(url).toBe('/files/upload');
    expect(formData.get('file')).toBe(file);
    expect(formData.get('parser_type')).toBe('pdf');
    expect(formData.get('workspace_id')).toBe('7');
    expect(formData.get('path')).toBe('/docs');
    expect(formData.get('document_type')).toBe('manual');
    expect(result).toBe(data);
  });

  it('reprocesses files with document type in request body', async () => {
    const data = { id: 1, name: 'laws.pdf' };
    mocks.mockAxiosInstance.post.mockResolvedValueOnce({ data });

    const { filesAPI } = await import('./api');
    const result = await filesAPI.reprocess(1, 'pdf', 'laws');

    expect(mocks.mockAxiosInstance.post).toHaveBeenCalledWith(
      '/files/1/reprocess',
      { parser_type: 'pdf', document_type: 'laws' }
    );
    expect(result).toBe(data);
  });

  it('fetches workspace file chunk source', async () => {
    const data = { format: 'text' as const, content: 'canonical text' };
    mocks.mockAxiosInstance.get.mockResolvedValueOnce({ data });

    const { filesAPI } = await import('./api');
    const result = await filesAPI.fetchWorkspaceChunkSource(7, 9);

    expect(mocks.mockAxiosInstance.get).toHaveBeenCalledWith('/workspaces/7/files/9/chunk-source');
    expect(result).toBe(data);
  });
});
