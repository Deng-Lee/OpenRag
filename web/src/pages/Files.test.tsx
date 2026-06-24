import { describe, it, expect } from 'vitest';
import { isUnderLogicalPath, selectDisplayedFiles } from './Files';
import type { File } from '../types';

function makeFile(partial: Partial<File> & { id: number; uri: string }): File {
  return {
    name: partial.uri.split('/').pop() || partial.uri,
    owner_id: 1,
    is_directory: false,
    size: 0,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...partial,
  } as File;
}

describe('isUnderLogicalPath', () => {
  it('treats every file as under the root directory', () => {
    expect(isUnderLogicalPath('/uploads/a.txt', '/')).toBe(true);
    expect(isUnderLogicalPath('/a.txt', '/')).toBe(true);
  });

  it('matches a direct child of the folder', () => {
    expect(isUnderLogicalPath('/uploads/a.txt', '/uploads')).toBe(true);
  });

  it('matches a nested descendant of the folder (recursive)', () => {
    expect(isUnderLogicalPath('/uploads/sub/deep/b.txt', '/uploads')).toBe(true);
  });

  it('excludes files in a sibling folder', () => {
    expect(isUnderLogicalPath('/docs/c.txt', '/uploads')).toBe(false);
  });

  it('excludes a folder that merely shares the name prefix', () => {
    expect(isUnderLogicalPath('/uploads-archive/d.txt', '/uploads')).toBe(false);
  });

  it('normalizes a trailing slash on the selected directory', () => {
    expect(isUnderLogicalPath('/uploads/a.txt', '/uploads/')).toBe(true);
  });
});

describe('selectDisplayedFiles', () => {
  const dirUploads = makeFile({ id: 1, uri: '/uploads', is_directory: true });
  const rootFile = makeFile({ id: 2, uri: '/top.txt' });
  const underUploads = makeFile({ id: 3, uri: '/uploads/a.txt' });
  const deepUnderUploads = makeFile({ id: 4, uri: '/uploads/sub/b.txt' });
  const underDocs = makeFile({ id: 5, uri: '/docs/c.txt' });
  const all = [dirUploads, rootFile, underUploads, deepUnderUploads, underDocs];

  it('shows every non-directory file when root is selected', () => {
    const ids = selectDisplayedFiles(all, '/').map((f) => f.id);
    expect(ids).toEqual([2, 3, 4, 5]);
  });

  it('never includes directory rows', () => {
    const result = selectDisplayedFiles(all, '/');
    expect(result.every((f) => !f.is_directory)).toBe(true);
  });

  it('scopes to files under the selected folder (direct and nested)', () => {
    const ids = selectDisplayedFiles(all, '/uploads').map((f) => f.id);
    expect(ids).toEqual([3, 4]);
  });

  it('excludes files outside the selected folder', () => {
    const ids = selectDisplayedFiles(all, '/docs').map((f) => f.id);
    expect(ids).toEqual([5]);
  });
});
