import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import FilePreviewModal from './FilePreviewModal';
import type { File } from '../types';

const filesApiMock = vi.hoisted(() => ({
  fetchContentBlob: vi.fn(),
  fetchPreview: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../services/api', () => ({
  filesAPI: filesApiMock,
}));

// react-pdf is imported at module top; stub it so jsdom doesn't load the worker.
vi.mock('react-pdf', () => ({
  Document: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Page: () => <canvas />,
}));

function textBlob(text: string): Blob {
  return {
    text: () => Promise.resolve(text),
    arrayBuffer: () => Promise.resolve(new TextEncoder().encode(text).buffer),
  } as unknown as Blob;
}

function textFile(): File {
  return {
    id: 10,
    uri: '/docs/notes.txt',
    name: 'notes.txt',
    owner_id: 1,
    is_directory: false,
    size: 5,
    mime_type: 'text/plain',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  } as File;
}

describe('FilePreviewModal download button visibility', () => {
  beforeEach(() => {
    filesApiMock.fetchContentBlob.mockReset();
    filesApiMock.fetchContentBlob.mockResolvedValue(textBlob('hello'));
    filesApiMock.fetchPreview.mockReset();
  });

  it('hides the download button for read-only users (canWrite=false)', async () => {
    render(<FilePreviewModal open file={textFile()} onClose={vi.fn()} canWrite={false} />);
    await waitFor(() => expect(screen.getByText('hello')).toBeInTheDocument());
    expect(screen.queryByText('files.preview.download')).toBeNull();
  });

  it('shows the download button when canWrite is true', async () => {
    render(<FilePreviewModal open file={textFile()} onClose={vi.fn()} canWrite />);
    await waitFor(() => expect(screen.getByText('hello')).toBeInTheDocument());
    expect(screen.getByText('files.preview.download')).toBeInTheDocument();
  });

  it('hides the download button by default when canWrite is omitted', async () => {
    render(<FilePreviewModal open file={textFile()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('hello')).toBeInTheDocument());
    expect(screen.queryByText('files.preview.download')).toBeNull();
  });
});
