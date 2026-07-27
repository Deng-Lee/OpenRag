import { beforeEach, describe, it, expect, vi } from 'vitest';
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { message } from 'antd';
import FileUpload from './FileUpload';
import { filesAPI } from '../services/api';

const folderUploadMock = vi.hoisted(() => ({
  picked: [] as Array<{ file: File; relativePath: string }>,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => {
      const labels: Record<string, string> = {
        'files.fields.document_type': 'Document Type',
        'files.document_types.general': 'General',
        'files.document_types.manual': 'Manual',
        'files.document_types.laws': 'Laws',
        'files.upload.tag_label': 'Tag',
        'files.upload.target_dir': 'Target Directory',
        'files.upload.path_tip': 'Path tip',
        'files.upload.select_dir': 'Select Directory',
        'files.upload.drag_hint': 'Drop file here',
        'files.upload.root': 'Root',
        'files.upload.pdf_paddleocr_only': 'PaddleOCR PDF only',
      };
      return labels[key] ?? key;
    },
  }),
  initReactI18next: { type: '3rdParty', init: () => {} },
}));

vi.mock('../services/api', () => ({
  filesAPI: {
    upload: vi.fn(),
    list: vi.fn(() => Promise.resolve([])),
  },
}));

vi.mock('../utils/folderUpload', () => ({
  precheck: (items: Array<{ file: File; relativePath: string }>) => {
    const accepted: Array<{ file: File; relativePath: string }> = [];
    const skipped: Array<{ rel: string; reason: string; ext?: string }> = [];
    for (const item of items) {
      const ext = item.relativePath.split('.').pop()?.toLowerCase() ?? '';
      if (['pdf', 'docx'].includes(ext)) {
        accepted.push(item);
      } else {
        skipped.push({ rel: item.relativePath, reason: 'unsupported', ext });
      }
    }
    return { accepted, skipped };
  },
  remoteParentDir: (_base: string, relativePath: string) => {
    const parts = relativePath.split('/').filter(Boolean);
    parts.pop();
    return parts.length ? `/${parts.join('/')}` : '/';
  },
  isDuplicateError: () => false,
  walkEntry: async (_entry: unknown, _prefix: string, out: Array<{ file: File; relativePath: string }>) => {
    out.push(...folderUploadMock.picked);
  },
}));

describe('FileUpload', () => {
  beforeEach(() => {
    cleanup();
    folderUploadMock.picked = [];
    vi.mocked(filesAPI.upload).mockReset();
    vi.mocked(filesAPI.upload).mockResolvedValue({} as any);
    vi.mocked(filesAPI.list).mockClear();
  });

  it('shows the public auto, pdf, and deepdoc parser labels', async () => {
    render(<FileUpload onUploadSuccess={vi.fn()} canWrite />);

    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]);

    expect((await screen.findAllByText('自动检测（PDF 默认使用 PaddleOCR）')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('PDF（PaddleOCR 解析）').length).toBeGreaterThan(0);
    expect(screen.getAllByText('PDF（DeepDoc 解析）').length).toBeGreaterThan(0);
    expect(screen.queryByText('PaddleOCR')).not.toBeInTheDocument();
  });

  it('passes pdf for PaddleOCR folder upload when all accepted files are PDFs', async () => {
    const onUploadSuccess = vi.fn();
    folderUploadMock.picked = [
      {
        file: new File(['%PDF-1.7'], 'a.pdf', { type: 'application/pdf' }),
        relativePath: 'folder/a.pdf',
      },
    ];
    render(<FileUpload onUploadSuccess={onUploadSuccess} canWrite />);

    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]);
    const paddleOptions = await screen.findAllByText('PDF（PaddleOCR 解析）');
    fireEvent.click(paddleOptions[paddleOptions.length - 1]);
    const dropZones = screen.getAllByText('Drop file here');
    fireEvent.drop(dropZones[dropZones.length - 1], {
      dataTransfer: {
        items: [{ webkitGetAsEntry: () => ({ isDirectory: true }) }],
        files: [],
      },
    });

    await waitFor(() => expect(filesAPI.upload).toHaveBeenCalledTimes(1));
    expect(filesAPI.upload).toHaveBeenCalledWith(
      expect.any(File),
      'pdf',
      1,
      '/folder',
      'general'
    );
    expect(onUploadSuccess).toHaveBeenCalled();
  });

  it('blocks PaddleOCR PDF folder upload when an accepted file is not PDF', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockImplementation(() => undefined as any);
    folderUploadMock.picked = [
      {
        file: new File(['%PDF-1.7'], 'a.pdf', { type: 'application/pdf' }),
        relativePath: 'folder/a.pdf',
      },
      {
        file: new File(['docx'], 'b.docx', {
          type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        }),
        relativePath: 'folder/b.docx',
      },
    ];
    render(<FileUpload onUploadSuccess={vi.fn()} canWrite />);

    fireEvent.mouseDown(screen.getAllByRole('combobox')[0]);
    const paddleOptions = await screen.findAllByText('PDF（PaddleOCR 解析）');
    fireEvent.click(paddleOptions[paddleOptions.length - 1]);
    const dropZones = screen.getAllByText('Drop file here');
    fireEvent.drop(dropZones[dropZones.length - 1], {
      dataTransfer: {
        items: [{ webkitGetAsEntry: () => ({ isDirectory: true }) }],
        files: [],
      },
    });

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('PaddleOCR PDF only'));
    expect(filesAPI.upload).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });
});
