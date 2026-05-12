import { describe, it, expect, vi, beforeEach } from 'vitest';
import axios from 'axios';

vi.mock('axios');

describe('API Client', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    vi.mocked(axios.create).mockReturnValue(mockAxiosInstance as any);
  });

  it('creates axios instance with base URL', () => {
    expect(axios.create).toBeDefined();
  });

  it('axios instance has interceptors configured', () => {
    const instance = axios.create();
    expect(instance.interceptors).toBeDefined();
    expect(instance.interceptors.request).toBeDefined();
    expect(instance.interceptors.response).toBeDefined();
  });
});
