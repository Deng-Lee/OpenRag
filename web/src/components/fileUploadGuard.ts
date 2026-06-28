/** When a tag is entered, only a single file may be uploaded.
 * Returns true if this drop must be blocked (tag present AND folder-or-multi-file). */
export function shouldBlockTaggedBatch(tag: string, hasDirectory: boolean, looseFileCount: number): boolean {
  if (!tag.trim()) return false;
  return hasDirectory || looseFileCount > 1;
}
