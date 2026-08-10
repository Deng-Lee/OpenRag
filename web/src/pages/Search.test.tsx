import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
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

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location-probe">
      {`${location.pathname}${location.search}|from=${(location.state as { from?: string } | null)?.from ?? ''}`}
    </div>
  );
}

async function renderSearchPage() {
  await i18n.changeLanguage('en');
  render(
    <MemoryRouter initialEntries={['/search']}>
      <SearchPage />
    </MemoryRouter>
  );
  await screen.findByText('Workspace A');
}

async function renderSearchRoute() {
  await i18n.changeLanguage('en');
  render(
    <MemoryRouter initialEntries={['/search']}>
      <Routes>
        <Route
          path="/search"
          element={
            <>
              <SearchPage />
              <LocationProbe />
            </>
          }
        />
        <Route path="/workspaces/:workspaceId/files/:fileId/chunks" element={<LocationProbe />} />
      </Routes>
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
    sessionStorage.clear();
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

    expect(screen.getByLabelText('Dense channel weight')).toBeInTheDocument();
    expect(screen.getByText(/Weighted RRF rank contribution/)).toBeInTheDocument();
    const payload = await submitSearch();

    expect(payload).toEqual(expect.objectContaining({
      query: 'hybrid retrieval',
      workspace_id: 7,
      vector_similarity_weight: 0.7,
      use_rerank: false,
    }));
  });

  it('submits the user-entered vector similarity weight', async () => {
    await renderSearchPage();

    fireEvent.change(screen.getByLabelText('Dense channel weight'), {
      target: { value: '0.3' },
    });
    fireEvent.blur(screen.getByLabelText('Dense channel weight'));
    const payload = await submitSearch('weighted search');

    expect(payload).toEqual(expect.objectContaining({
      query: 'weighted search',
      workspace_id: 7,
      vector_similarity_weight: 0.3,
    }));
  });

  it('navigates to the target chunk detail route when result text is clicked', async () => {
    apiMocks.search.mockResolvedValueOnce({
      results: [
        {
          text: 'target result text',
          score: 0.9,
          file_id: 9,
          chunk_id: 'chunk-42',
          chunk_index: 42,
          filename: 'report.pdf',
        },
      ],
      total: 1,
      query_time_ms: 12,
    });
    await renderSearchRoute();

    fireEvent.change(screen.getByLabelText('Query'), {
      target: { value: 'target' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Search/i }));

    const resultLink = await screen.findByRole('button', { name: /target result text/i });
    fireEvent.click(resultLink);

    await waitFor(() => {
      expect(screen.getByTestId('location-probe')).toHaveTextContent(
        '/workspaces/7/files/9/chunks?chunkId=chunk-42&chunkIndex=42|from=search'
      );
    });
  });

  it('stores the successful search snapshot in sessionStorage', async () => {
    const response = {
      results: [
        {
          text: 'cached result text',
          score: 0.8,
          file_id: 11,
          chunk_id: 'chunk-11',
          chunk_index: 3,
          filename: 'cached.pdf',
        },
      ],
      total: 1,
      query_time_ms: 9,
    };
    apiMocks.search.mockResolvedValueOnce(response);
    await renderSearchPage();

    fireEvent.change(screen.getByLabelText('Query'), {
      target: { value: 'cache me' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Search/i }));

    await screen.findByRole('button', { name: /cached result text/i });
    const cached = JSON.parse(sessionStorage.getItem('openrag.search.snapshot') || '{}');

    expect(cached).toEqual({
      workspaceId: 7,
      formValues: expect.objectContaining({
        query: 'cache me',
        top_k: 10,
        vector_similarity_weight: 0.7,
        search_mode: 'semantic',
      }),
      response,
    });
  });

  it('restores the matching workspace snapshot without searching again', async () => {
    sessionStorage.setItem(
      'openrag.search.snapshot',
      JSON.stringify({
        workspaceId: 7,
        formValues: {
          query: 'restored query',
          top_k: 6,
          vector_similarity_weight: 0.4,
          search_mode: 'semantic',
          use_rerank: true,
          use_contextual_retrieval: false,
          use_l1_llm_navigation: false,
          retrieval_strategy: 'auto',
          contextual_l0_top_n: 40,
          contextual_l1_top_n: 30,
          contextual_chunk_fetch_multiplier: 4,
        },
        response: {
          results: [
            {
              text: 'restored result text',
              score: 0.7,
              file_id: 12,
              chunk_id: 'chunk-12',
              chunk_index: 4,
              filename: 'restored.pdf',
            },
          ],
          total: 1,
          query_time_ms: 5,
        },
      })
    );

    await renderSearchPage();

    await screen.findByRole('button', { name: /restored result text/i });
    expect(screen.getByLabelText('Query')).toHaveValue('restored query');
    expect(screen.getByLabelText('Top K')).toHaveValue('6');
    expect(apiMocks.search).not.toHaveBeenCalled();
    expect(apiMocks.hierarchical).not.toHaveBeenCalled();
  });
});
