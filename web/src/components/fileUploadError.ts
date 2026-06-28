import { isFilenameTooLongError } from '../utils/folderUpload';

export type UploadErrorKind = 'tag_conflict' | 'name_too_long' | 'backend_detail' | 'generic';

/** Pick which message a failed single-file upload should show.
 *
 * Only a 409 whose backend detail is the tag-uniqueness error maps to the
 * tag-conflict copy. Other 409s (e.g. "Path component already exists as a file"
 * from directory materialisation, "File already exists at ...") keep the backend
 * detail, so a user who never typed a tag is not misled into thinking their tag
 * clashed. */
export function classifyUploadError(code: number | undefined, detailMsg: string): UploadErrorKind {
  if (code === 409 && /Tag already in use/i.test(detailMsg)) return 'tag_conflict';
  if (isFilenameTooLongError(code, detailMsg)) return 'name_too_long';
  if (detailMsg) return 'backend_detail';
  return 'generic';
}
