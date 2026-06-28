import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
  inst: { get: vi.fn(), post: vi.fn().mockResolvedValue({ data: {} }), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } },
}));
vi.mock('axios', () => ({ default: { create: vi.fn(() => mocks.inst) } }));

describe('filesAPI.upload tag', () => {
  beforeEach(() => { vi.resetModules(); vi.clearAllMocks(); mocks.inst.post.mockResolvedValue({ data: {} }); });

  it('appends tag to FormData when provided', async () => {
    const { filesAPI } = await import('./api');
    const file = new File([new Blob(['x'])], 'a.txt', { type: 'text/plain' });
    await filesAPI.upload(file, 'auto', 1, '/', 'general', 'my-tag');
    const fd = mocks.inst.post.mock.calls[0][1] as FormData;
    expect(fd.get('tag')).toBe('my-tag');
  });

  it('omits tag when not provided', async () => {
    const { filesAPI } = await import('./api');
    const file = new File([new Blob(['x'])], 'a.txt', { type: 'text/plain' });
    await filesAPI.upload(file, 'auto', 1, '/', 'general');
    const fd = mocks.inst.post.mock.calls[0][1] as FormData;
    expect(fd.get('tag')).toBeNull();
  });
});
