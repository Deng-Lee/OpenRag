import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '../i18n';
import Tasks from './Tasks';

const apiMocks = vi.hoisted(() => ({
  listTasks: vi.fn(),
  getStats: vi.fn(),
  listWorkspaces: vi.fn(),
  getCurrentUser: vi.fn(),
}));

vi.mock('../services/api', () => ({
  tasksAPI: {
    list: apiMocks.listTasks,
    getStats: apiMocks.getStats,
  },
  workspacesAPI: {
    list: apiMocks.listWorkspaces,
  },
  authAPI: {
    getCurrentUser: apiMocks.getCurrentUser,
  },
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

describe('Tasks progress', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    apiMocks.listWorkspaces.mockResolvedValue([
      {
        id: 7,
        name: 'Workspace A',
        slug: 'workspace-a',
        owner_id: 1,
      },
    ]);
    apiMocks.getCurrentUser.mockResolvedValue({
      id: 1,
      email: 'admin@example.com',
      username: 'admin',
      is_admin: true,
      is_active: true,
    });
    apiMocks.listTasks.mockResolvedValue({
      items: [
        {
          id: 1,
          task_id: 'task-progress-1',
          workspace_id: 7,
          user_id: 1,
          file_id: 11,
          task_type: 'process_document',
          queue: 'normal',
          priority: 5,
          status: 'started',
          progress: 5,
          retry_count: 0,
          max_retries: 3,
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
      total: 1,
      skip: 0,
      limit: 10,
    });
    apiMocks.getStats.mockResolvedValue({
      total: 1,
      running: 1,
      pending: 0,
      by_status: { started: 1 },
    });
  });

  it('renders a real active progress bar for a started task', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/tasks']}>
        <Tasks />
      </MemoryRouter>
    );

    await waitFor(() => expect(apiMocks.listTasks).toHaveBeenCalled());
    expect(await screen.findByText('5%')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '5');
    expect(container.querySelector('.ant-progress-status-active')).not.toBeNull();
  });
});
