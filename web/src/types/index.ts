export interface User {
  id: number;
  email: string;
  username: string;
  full_name?: string;
  is_admin: boolean;
  is_active: boolean;
  role?: string;
  team_id?: number;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
}

export interface RegisterRequest {
  email: string;
  password: string;
  username: string;
  full_name: string;
  role?: string;
}

export type SimpleStatus = 'unprocessed' | 'processing' | 'done' | 'failed';

export type DocumentType = 'general' | 'manual' | 'laws';

export interface File {
  id: number;
  uri: string;
  name: string;
  owner_id: number;
  owner_name?: string;
  parent_id?: number;
  is_directory: boolean;
  size: number;
  mime_type?: string;
  created_at: string;
  updated_at: string;
  processing_status?: string | null;
  simple_status?: SimpleStatus | null;
  document_type?: DocumentType;
  error_message?: string | null;
}

export interface WorkspaceFileSummary {
  id: number;
  workspace_id: number;
  name: string;
  uri: string;
  mime_type?: string | null;
  processing_status?: string | null;
  simple_status?: SimpleStatus | string | null;
  total_chunks: number;
}

export interface DocumentChunkItem {
  file_id: number;
  workspace_id: number;
  filename: string;
  chunk_id: string;
  chunk_index: number;
  text: string;
  is_truncated: boolean;
  page?: number | null;
  bbox_x0?: number | null;
  bbox_y0?: number | null;
  bbox_x1?: number | null;
  bbox_y1?: number | null;
  source_char_start?: number | null;
  source_char_end?: number | null;
  position_int?: number[][] | null;
  positions?: number[][] | null;
}

export interface DocumentChunkListResponse {
  file: WorkspaceFileSummary;
  items: DocumentChunkItem[];
  total: number;
  skip: number;
  limit: number;
}

export interface SearchRequest {
  query: string;
  top_k?: number;
  workspace_id?: number;
  use_rerank?: boolean;
  use_contextual_retrieval?: boolean;
  vector_similarity_weight?: number;
  contextual_l0_top_n?: number;
  contextual_l1_top_n?: number;
  contextual_chunk_fetch_multiplier?: number;
  retrieval_strategy?: string;
  use_l1_llm_navigation?: boolean;
  /** @deprecated 后端以 workspace_id + 权限为准 */
  file_ids?: number[];
}

/** 与后端 SearchResult 对齐 */
export interface SearchResult {
  text: string;
  score: number;
  file_id: number;
  chunk_id?: string;
  chunk_index?: number;
  page?: number;
  level?: number;
  block_type?: string;
  start_offset?: number;
  end_offset?: number;
  /** PDF 等解析器写入的包围盒（与 Chunk.bbox 一致，用户空间坐标） */
  bbox_x0?: number | null;
  bbox_y0?: number | null;
  bbox_x1?: number | null;
  bbox_y1?: number | null;
  /** 解析器逻辑块 id（docx/pptx/pdf/xlsx 等） */
  source_block_id?: string | null;
  /** 整文件字符流起始/结束（与后端 document_chunks 一致） */
  source_char_start?: number | null;
  source_char_end?: number | null;
  filename?: string;
  uri?: string;
  object_key?: string;
  object_url?: string;
  local_chunk_path?: string;
  text_preview?: string;
  retrieval_strategy?: string;
  l1_llm_filtered?: boolean;
  /** 兼容旧字段：等价于 text */
  chunk_text?: string;
  /** RagFlow add_positions 对齐字段（可由后端顶层返回或放在 metadata） */
  page_num_int?: number[];
  position_int?: Array<[number, number, number, number, number]>;
  top_int?: number[];
  positions?: number[][];
  metadata?: Record<string, unknown>;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  query_time_ms: number;
  l1_llm_applied?: boolean | null;
  l1_llm_skip_reason?: string | null;
}

export interface Team {
  id: number;
  name: string;
  description?: string;
  created_at: string;
}

export interface Workspace {
  id: number;
  name: string;
  slug: string;
  description?: string;
  owner_id: number;
  max_concurrent_tasks: number;
  max_storage_bytes: number;
  priority_strategy: string;
  created_at: string;
  updated_at?: string;
  user_role?: 'read' | 'write';  // 当前用户在此工作空间的权限
}

export interface WorkspaceMember {
  id: number;
  workspace_id: number;
  user_id: number;
  role: 'read' | 'write';
  user?: User;
  joined_at?: string;
}

export interface TokenWorkspaceBinding {
  workspace_id: number;
  workspace_name: string;
  permission: 'read' | 'write';
}

export interface ServiceTokenListItem {
  id: number;
  name: string | null;
  secret_preview: string;
  revoked_at: string | null;
  created_by_user_id: number;
  workspaces: TokenWorkspaceBinding[];
}

export interface ServiceTokenCreated {
  id: number;
  secret: string;
  name: string | null;
  created_at: string;
  workspaces: TokenWorkspaceBinding[];
}

export interface WorkspaceBindingRequest {
  workspace_id: number;
  permission: 'read' | 'write';
}

export interface BindingPatchRequest {
  add?: WorkspaceBindingRequest[];
  update?: WorkspaceBindingRequest[];
  remove?: { workspace_id: number }[];
}

export type TaskStatus = 'pending' | 'assigned' | 'started' | 'success' | 'failure' | 'retry' | 'cancelled';

export type TaskType = 'process_document' | 'parse_document' | 'build_hierarchy' | 'embed_document' | 'delete_file';

export interface Task {
  id: number;
  task_id: string;
  workspace_id: number;
  user_id: number;
  file_id?: number;
  task_type: TaskType;
  queue: string;
  priority: number;
  status: TaskStatus;
  progress: number;
  retry_count: number;
  max_retries: number;
  created_at: string;
  updated_at?: string;
  started_at?: string;
  completed_at?: string;
  result?: Record<string, any>;
  error?: string;
}

export interface TaskListResponse {
  items: Task[];
  total: number;
  skip: number;
  limit: number;
}

export interface TaskStatsResponse {
  total: number;
  running: number;
  pending: number;
  by_status: Record<string, number>;
}

export interface Role {
  id: number;
  name: string;
  role_code: string;
  is_active: boolean;
}

export interface WorkspacePermissionDetail {
  workspace_id: number;
  workspace_name: string;
  permission: string;
  source: string;
  source_details: {
    role_id: number;
    role_name: string;
  } | null;
}

export interface UserPermissionDetails {
  user_id: number;
  roles: Role[];
  workspace_permissions: WorkspacePermissionDetail[];
}


export interface RoleCreate {
  name: string;
  role_code: string;
  is_active: boolean;
  description?: string;
}

export interface RoleUpdate {
  name?: string;
  role_code?: string;
  is_active?: boolean;
  description?: string;
}

export interface RoleWorkspacePermission {
  id: number;
  workspace_id: number;
  permission: string;
}

