import { describe, it, expect } from 'vitest';
import { shouldBlockTaggedBatch } from './fileUploadGuard';

describe('shouldBlockTaggedBatch', () => {
  it('blocks when tag set and a directory is dropped', () => {
    expect(shouldBlockTaggedBatch('t', true, 1)).toBe(true);
  });
  it('blocks when tag set and multiple loose files dropped', () => {
    expect(shouldBlockTaggedBatch('t', false, 3)).toBe(true);
  });
  it('allows single file with a tag', () => {
    expect(shouldBlockTaggedBatch('t', false, 1)).toBe(false);
  });
  it('allows folder/batch when tag is empty', () => {
    expect(shouldBlockTaggedBatch('', true, 5)).toBe(false);
    expect(shouldBlockTaggedBatch('   ', false, 4)).toBe(false);
  });
});
