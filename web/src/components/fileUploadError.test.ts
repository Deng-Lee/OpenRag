import { describe, it, expect } from 'vitest';
import { classifyUploadError } from './fileUploadError';

describe('classifyUploadError', () => {
  it('413 without backend detail -> too_large', () => {
    expect(classifyUploadError(413, '')).toBe('too_large');
  });

  it('409 with tag-uniqueness detail -> tag_conflict', () => {
    expect(classifyUploadError(409, 'Tag already in use')).toBe('tag_conflict');
  });

  it('409 path-component conflict -> backend_detail (NOT tag_conflict)', () => {
    expect(classifyUploadError(409, 'Path component already exists as a file: /x')).toBe('backend_detail');
  });

  it('409 file-already-exists -> backend_detail (NOT tag_conflict)', () => {
    expect(classifyUploadError(409, 'File already exists at /x')).toBe('backend_detail');
  });

  it('400 filename too long -> name_too_long', () => {
    expect(classifyUploadError(400, 'File name too long: 260 bytes (max 200)')).toBe('name_too_long');
  });

  it('no detail -> generic', () => {
    expect(classifyUploadError(500, '')).toBe('generic');
  });
});
