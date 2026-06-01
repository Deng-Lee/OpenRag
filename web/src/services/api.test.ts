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
});
