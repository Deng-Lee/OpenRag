import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '../i18n';
import i18n from '../i18n';
import SearchPage from './Search';

const apiMocks = vi.hoisted(() => ({
  search: vi.fn(),
  hierarchical: vi.fn(),
  listWorkspaces: vi.fn(),
  getCurrentUser: vi.fn(),
  getFile: vi.fn(),
}));

vi.mock('../services/api', () => ({
  searchAPI: {
    search: apiMocks.search,
    hierarchical: apiMocks.hierarchical,
  },
  workspacesAPI: {
    list: apiMocks.listWorkspaces,
  },
  authAPI: {
    getCurrentUser: apiMocks.getCurrentUser,
  },
  filesAPI: {
    get: apiMocks.getFile,
  },
}));

vi.mock('../components/ChunkSourcePreviewModal', () => ({
  default: () => null,
}));

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(window, 'ResizeObserver', {
  writable: true,
  value: ResizeObserverMock,
});

Object.defineProperty(window.HTMLElement.prototype, 'scrollIntoView', {
  writable: true,
  value: vi.fn(),
});

const workspace = {
  id: 7,
  name: 'Workspace A',
  slug: 'workspace-a',
  owner_id: 1,
  max_concurrent_tasks: 2,
  max_storage_bytes: 1024,
  priority_strategy: 'fifo',
  created_at: '2026-01-01T00:00:00Z',
};

async function renderSearchPage() {
  await i18n.changeLanguage('en');
  render(
    <MemoryRouter initialEntries={['/search']}>
      <SearchPage />
    </MemoryRouter>
  );
  await screen.findByText('Workspace A');
}

async function submitSearch(queryText = 'hybrid retrieval') {
  fireEvent.change(screen.getByLabelText('Query'), {
    target: { value: queryText },
  });
  fireEvent.submit(screen.getByLabelText('Query').closest('form') as HTMLFormElement);
  await waitFor(() => expect(apiMocks.search).toHaveBeenCalledTimes(1));
  return apiMocks.search.mock.calls[0][0];
}

describe('SearchPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    apiMocks.listWorkspaces.mockResolvedValue([workspace]);
    apiMocks.getCurrentUser.mockResolvedValue({
      id: 1,
      email: 'admin@example.com',
      username: 'admin',
      is_admin: true,
      is_active: true,
    });
    apiMocks.search.mockResolvedValue({
      results: [],
      total: 0,
      query_time_ms: 1,
    });
  });

  it('submits the default vector similarity weight', async () => {
    await renderSearchPage();

    expect(screen.getByLabelText('Vector weight')).toBeInTheDocument();
    const payload = await submitSearch();

    expect(payload).toEqual(expect.objectContaining({
      query: 'hybrid retrieval',
      workspace_id: 7,
      vector_similarity_weight: 0.7,
    }));
  });

  it('submits the user-entered vector similarity weight', async () => {
    await renderSearchPage();

    fireEvent.change(screen.getByLabelText('Vector weight'), {
      target: { value: '0.3' },
    });
    fireEvent.blur(screen.getByLabelText('Vector weight'));
    const payload = await submitSearch('weighted search');

    expect(payload).toEqual(expect.objectContaining({
      query: 'weighted search',
      workspace_id: 7,
      vector_similarity_weight: 0.3,
    }));
  });
});
