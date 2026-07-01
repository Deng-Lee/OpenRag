import { beforeEach, describe, expect, it, vi } from 'vitest';
import axios from 'axios';

const mocks = vi.hoisted(() => {
  const mockAxiosInstance = {
    get: vi.fn(),
  };

  return { mockAxiosInstance };
});

vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => mocks.mockAxiosInstance),
  },
}));

describe('embedPreviewAPI', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    localStorage.setItem('token', 'user-token');
    localStorage.setItem('currentWorkspaceId', '7');
  });

  it('reads preview token from hash', async () => {
    const { readPreviewTokenFromHash } = await import('./embedPreviewApi');

    expect(readPreviewTokenFromHash('#token=abc')).toBe('abc');
    expect(readPreviewTokenFromHash('#token=abc&page=1')).toBe('abc');
    expect(readPreviewTokenFromHash('#token=abc?page=1')).toBe('abc');
    expect(readPreviewTokenFromHash('#token=abc#page=1')).toBe('abc');
    expect(readPreviewTokenFromHash('#foo=1&token=a%20b')).toBe('a b');
    expect(readPreviewTokenFromHash('#foo=1')).toBeNull();
    expect(readPreviewTokenFromHash('')).toBeNull();
  });

  it('reads preview page from query or hash parameters', async () => {
    const { readPreviewPageFromUrl } = await import('./embedPreviewApi');

    expect(readPreviewPageFromUrl('?page=2', '#token=abc')).toBe(2);
    expect(readPreviewPageFromUrl('', '#token=abc&page=3')).toBe(3);
    expect(readPreviewPageFromUrl('', '#token=abc?page=4')).toBe(4);
    expect(readPreviewPageFromUrl('', '#token=abc#page=5')).toBe(5);
    expect(readPreviewPageFromUrl('?page=0', '#token=abc')).toBeNull();
    expect(readPreviewPageFromUrl('', '#token=abc&page=bad')).toBeNull();
  });

  it('uses a separate axios client with the same base URL shape', async () => {
    await import('./embedPreviewApi');

    expect(axios.create).toHaveBeenCalledWith({ baseURL: expect.any(String) });
  });

  it('sends only the preview token header to context and file endpoints', async () => {
    const context = { file: { id: 9 }, chunk: { chunk_id: 'c1' }, expires_at: '2026-06-03T10:00:00Z' };
    const blob = new Blob(['hello']);
    const preview = { format: 'text' as const, content: 'hello' };
    const source = { format: 'text' as const, content: 'canonical' };
    mocks.mockAxiosInstance.get
      .mockResolvedValueOnce({ data: context })
      .mockResolvedValueOnce({ data: blob })
      .mockResolvedValueOnce({ data: preview })
      .mockResolvedValueOnce({ data: source });

    const { embedPreviewAPI } = await import('./embedPreviewApi');

    await expect(embedPreviewAPI.getContext('abc')).resolves.toBe(context);
    await expect(embedPreviewAPI.fetchContentBlob('abc')).resolves.toBe(blob);
    await expect(embedPreviewAPI.fetchPreview('abc')).resolves.toBe(preview);
    await expect(embedPreviewAPI.fetchChunkSource('abc')).resolves.toBe(source);

    expect(mocks.mockAxiosInstance.get).toHaveBeenNthCalledWith(1, '/embed/v1/document-preview', {
      headers: { 'X-OpenRag-Preview-Token': 'abc' },
    });
    expect(mocks.mockAxiosInstance.get).toHaveBeenNthCalledWith(2, '/embed/v1/files/content', {
      headers: { 'X-OpenRag-Preview-Token': 'abc' },
      responseType: 'blob',
    });
    expect(mocks.mockAxiosInstance.get).toHaveBeenNthCalledWith(3, '/embed/v1/files/preview', {
      headers: { 'X-OpenRag-Preview-Token': 'abc' },
    });
    expect(mocks.mockAxiosInstance.get).toHaveBeenNthCalledWith(4, '/embed/v1/files/chunk-source', {
      headers: { 'X-OpenRag-Preview-Token': 'abc' },
    });

    const allConfigs = mocks.mockAxiosInstance.get.mock.calls.map((call) => call[1]);
    expect(allConfigs).toEqual(
      expect.arrayContaining([
        expect.not.objectContaining({
          headers: expect.objectContaining({
            Authorization: expect.any(String),
            'X-Workspace-ID': expect.any(String),
          }),
        }),
      ])
    );
    expect(localStorage.getItem('token')).toBe('user-token');
  });

  it('does not redirect or clear local storage on 401', async () => {
    const error = Object.assign(new Error('unauthorized'), { response: { status: 401 } });
    mocks.mockAxiosInstance.get.mockRejectedValueOnce(error);
    localStorage.setItem('token', 'keep-me');

    const { embedPreviewAPI } = await import('./embedPreviewApi');
    await expect(embedPreviewAPI.getContext('bad')).rejects.toBe(error);

    expect(localStorage.getItem('token')).toBe('keep-me');
    expect(window.location.pathname).not.toBe('/login');
  });
});
