import { describe, it, expect, vi } from 'vitest';
import type { ReactElement } from 'react';
import { render, screen, within, fireEvent } from '@testing-library/react';
import FileList from './FileList';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => {
      const labels: Record<string, string> = {
        'files.columns.name': 'Name',
        'files.columns.location': 'Location (URI)',
        'files.columns.size': 'Size',
        'files.columns.type': 'Type',
        'files.columns.status': 'Status',
        'files.columns.upload_time': 'Upload Time',
        'files.columns.action': 'Action',
        'files.columns.directory': 'Directory',
        'files.columns.unknown': 'Unknown',
        'files.status.unprocessed': 'Unprocessed',
        'files.status.processing': 'Processing',
        'files.status.done': 'Done',
        'files.status.failed': 'Failed',
        'files.status.failed_title': 'Processing failed',
        'files.status.no_error': 'No error detail',
        'files.status.reprocess_from_popover': 'Reprocess',
        'files.messages.delete_confirm': 'Delete?',
        'files.messages.yes': 'Yes',
        'files.messages.no': 'No',
        'common.delete': 'Delete',
        'files.actions.reprocess': 'Reprocess',
        'files.fields.parser_type': 'Parser',
        'files.messages.reprocess_hint': 'Hint',
        'files.parser_types.auto': 'auto',
        'files.parser_types.pdf': 'pdf',
        'files.parser_types.docx': 'docx',
        'files.parser_types.xlsx': 'xlsx',
        'files.parser_types.pptx': 'pptx',
        'files.parser_types.txt': 'txt',
        'files.parser_types.md': 'md',
        'files.parser_types.html': 'html',
        'files.parser_types.json': 'json',
        'files.parser_types.csv': 'csv',
        'files.parser_types.epub': 'epub',
      };
      return labels[key] ?? key;
    },
  }),
  initReactI18next: { type: '3rdParty', init: () => {} },
}));

function renderFileList(ui: ReactElement) {
  return render(ui);
}

describe('FileList', () => {

  it('renders empty state when no files', () => {
    renderFileList(<FileList files={[]} onFileDeleted={vi.fn()} />);
    const table = screen.getByRole('table');
    expect(table).toBeInTheDocument();
  });

  it('renders file list with data', () => {
    const mockFiles = [
      {
        id: 1,
        uri: '/uploads/test.pdf',
        name: 'test.pdf',
        owner_id: 1,
        parent_id: undefined,
        is_directory: false,
        size: 1024,
        mime_type: 'application/pdf',
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-01-01T00:00:00Z',
      },
    ];
    renderFileList(<FileList files={mockFiles} onFileDeleted={vi.fn()} />);
    expect(screen.getByText('test.pdf')).toBeInTheDocument();
  });

  it('shows Done tag when simple_status is done', () => {
    renderFileList(
      <FileList
        files={[
          {
            id: 1,
            uri: '/a.pdf',
            name: 'a.pdf',
            owner_id: 1,
            is_directory: false,
            size: 100,
            mime_type: 'application/pdf',
            created_at: '2024-01-01T00:00:00Z',
            updated_at: '2024-01-01T00:00:00Z',
            simple_status: 'done',
          },
        ]}
        onFileDeleted={vi.fn()}
      />,
    );
    expect(screen.getByText('Done')).toBeInTheDocument();
  });

  it('shows dash for directory status', () => {
    renderFileList(
      <FileList
        files={[
          {
            id: 2,
            uri: '/dir',
            name: 'dir',
            owner_id: 1,
            is_directory: true,
            size: 0,
            created_at: '2024-01-01T00:00:00Z',
            updated_at: '2024-01-01T00:00:00Z',
          },
        ]}
        onFileDeleted={vi.fn()}
      />,
    );
    const row = screen.getByText('dir').closest('tr');
    expect(row).toBeTruthy();
    expect(within(row as HTMLElement).getAllByText('-').length).toBeGreaterThan(0);
  });

  it('opens failed popover with error and reprocess control', async () => {
    renderFileList(
      <FileList
        files={[
          {
            id: 3,
            uri: '/bad.pdf',
            name: 'bad.pdf',
            owner_id: 1,
            is_directory: false,
            size: 10,
            mime_type: 'application/pdf',
            created_at: '2024-01-01T00:00:00Z',
            updated_at: '2024-01-01T00:00:00Z',
            simple_status: 'failed',
            error_message: 'parse failed',
          },
        ]}
        onFileDeleted={vi.fn()}
        canWrite
      />,
    );
    fireEvent.click(screen.getByText('Failed'));
    expect(await screen.findByText('parse failed')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reprocess/i })).toBeInTheDocument();
  });
});
