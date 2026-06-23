import { describe, it, expect } from 'vitest';
import {
  normalizeDir,
  remoteParentDir,
  isJunkPath,
  precheck,
  isDuplicateError,
  walkEntry,
  MAX_FILE_SIZE,
  type PickedFile,
} from './folderUpload';

/** 用最小对象伪造 File（precheck 只读 size） */
function fakeFile(size: number): File {
  return { size } as unknown as File;
}

describe('normalizeDir', () => {
  it('roots empty / slash to "/"', () => {
    expect(normalizeDir('')).toBe('/');
    expect(normalizeDir('/')).toBe('/');
  });

  it('keeps a leading slash and strips trailing slash', () => {
    expect(normalizeDir('/docs')).toBe('/docs');
    expect(normalizeDir('/docs/')).toBe('/docs');
  });

  it('adds a missing leading slash', () => {
    expect(normalizeDir('docs')).toBe('/docs');
  });

  it('converts Windows backslashes to forward slashes', () => {
    expect(normalizeDir('\\docs\\a\\')).toBe('/docs/a');
  });
});

describe('remoteParentDir', () => {
  it('joins a single-level folder under root', () => {
    expect(remoteParentDir('/', 'folder/a.pdf')).toBe('/folder');
  });

  it('joins a nested path under a target directory', () => {
    expect(remoteParentDir('/docs', 'folder/sub/a.pdf')).toBe('/docs/folder/sub');
  });

  it('returns the base when the file sits directly under it', () => {
    expect(remoteParentDir('/docs', 'a.pdf')).toBe('/docs');
    expect(remoteParentDir('/', 'a.pdf')).toBe('/');
  });

  it('preserves the dropped folder name as the top segment (unicode)', () => {
    expect(remoteParentDir('/personal', '报告/2024/Q1.pdf')).toBe('/personal/报告/2024');
  });
});

describe('isJunkPath', () => {
  it('passes normal files', () => {
    expect(isJunkPath('folder/a.pdf')).toBe(false);
    expect(isJunkPath('folder/sub/b.docx')).toBe(false);
  });

  it('rejects hidden basenames', () => {
    expect(isJunkPath('.DS_Store')).toBe(true);
    expect(isJunkPath('folder/.hiddenfile')).toBe(true);
  });

  it('rejects known junk filenames', () => {
    expect(isJunkPath('folder/Thumbs.db')).toBe(true);
    expect(isJunkPath('folder/desktop.ini')).toBe(true);
  });

  it('rejects when ANY directory segment is hidden or denylisted', () => {
    expect(isJunkPath('.git/config')).toBe(true);
    expect(isJunkPath('folder/.git/HEAD')).toBe(true);
    expect(isJunkPath('__MACOSX/a.pdf')).toBe(true);
    expect(isJunkPath('folder/__MACOSX/x.pdf')).toBe(true);
    expect(isJunkPath('node_modules/pkg/index.js')).toBe(true);
    expect(isJunkPath('folder/.hidden/a.pdf')).toBe(true);
  });
});

describe('precheck', () => {
  it('accepts a supported, in-size file', () => {
    const items: PickedFile[] = [{ file: fakeFile(10), relativePath: 'f/a.pdf' }];
    const { accepted, skipped } = precheck(items);
    expect(accepted).toHaveLength(1);
    expect(skipped).toHaveLength(0);
  });

  it('treats supported extensions case-insensitively', () => {
    const { accepted } = precheck([{ file: fakeFile(10), relativePath: 'f/A.PDF' }]);
    expect(accepted).toHaveLength(1);
  });

  it('skips junk paths with reason "junk"', () => {
    const { accepted, skipped } = precheck([{ file: fakeFile(10), relativePath: 'f/.DS_Store' }]);
    expect(accepted).toHaveLength(0);
    expect(skipped).toEqual([{ rel: 'f/.DS_Store', reason: 'junk' }]);
  });

  it('skips unsupported extensions with the ext carried', () => {
    const { skipped } = precheck([{ file: fakeFile(10), relativePath: 'f/image.png' }]);
    expect(skipped).toEqual([{ rel: 'f/image.png', reason: 'unsupported', ext: 'png' }]);
  });

  it('skips files over the size limit', () => {
    const { skipped } = precheck([{ file: fakeFile(MAX_FILE_SIZE + 1), relativePath: 'f/big.pdf' }]);
    expect(skipped).toEqual([{ rel: 'f/big.pdf', reason: 'too_large' }]);
  });

  it('partitions a mixed batch', () => {
    const items: PickedFile[] = [
      { file: fakeFile(10), relativePath: 'f/a.pdf' },
      { file: fakeFile(10), relativePath: 'f/__MACOSX/b.pdf' },
      { file: fakeFile(10), relativePath: 'f/c.png' },
      { file: fakeFile(MAX_FILE_SIZE + 1), relativePath: 'f/d.pdf' },
    ];
    const { accepted, skipped } = precheck(items);
    expect(accepted.map((a) => a.relativePath)).toEqual(['f/a.pdf']);
    expect(skipped.map((s) => s.reason)).toEqual(['junk', 'unsupported', 'too_large']);
  });
});

describe('isDuplicateError', () => {
  it('is true only for 400 + "already exists"', () => {
    expect(isDuplicateError(400, 'File already exists at /a')).toBe(true);
    expect(isDuplicateError(400, 'already exists at /a')).toBe(true);
  });

  it('does not misclassify "does not exist" or other errors', () => {
    expect(isDuplicateError(400, 'Parent directory does not exist')).toBe(false);
    expect(isDuplicateError(413, 'too large')).toBe(false);
    expect(isDuplicateError(undefined, '')).toBe(false);
  });
});

// —— walkEntry：用 mock FileSystem entry 验证递归与 readEntries 分批 ——

interface MockOpts {
  name: string;
  children?: MockEntry[]; // 目录时存在
}
type MockEntry = ReturnType<typeof makeFileEntry> | ReturnType<typeof makeDirEntry>;

function makeFileEntry(name: string) {
  return {
    isFile: true,
    isDirectory: false,
    name,
    file(onOk: (f: File) => void) {
      onOk({ name } as unknown as File);
    },
  };
}

function makeDirEntry({ name, children = [] }: MockOpts) {
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader() {
      // 模拟 readEntries 单次≤100、需循环读到空：每次返回一项，最后返回空
      const queue = [...children];
      return {
        readEntries(onOk: (batch: MockEntry[]) => void) {
          if (queue.length === 0) {
            onOk([]);
          } else {
            onOk([queue.shift() as MockEntry]);
          }
        },
      };
    },
  };
}

describe('walkEntry', () => {
  it('collects a single file with its relative path', async () => {
    const out: PickedFile[] = [];
    await walkEntry(makeFileEntry('a.pdf'), '', out);
    expect(out).toHaveLength(1);
    expect(out[0].relativePath).toBe('a.pdf');
  });

  it('recurses directories and prefixes the folder name (multi-batch reads)', async () => {
    const tree = makeDirEntry({
      name: 'folder',
      children: [
        makeFileEntry('a.pdf'),
        makeDirEntry({ name: 'sub', children: [makeFileEntry('b.pdf')] }),
      ],
    });
    const out: PickedFile[] = [];
    await walkEntry(tree, '', out);
    expect(out.map((p) => p.relativePath).sort()).toEqual(['folder/a.pdf', 'folder/sub/b.pdf']);
  });
});
