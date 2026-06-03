import axios from 'axios';
import type {
  LoginRequest,
  LoginResponse,
  RegisterRequest,
  User,
  File,
  SearchRequest,
  SearchResponse,
  Team,
  Workspace,
  Task,
  TaskListResponse,
  TaskStatsResponse,
  ServiceTokenListItem,
  ServiceTokenCreated,
  WorkspaceBindingRequest,
  BindingPatchRequest,
  DocumentChunkListResponse,
  DocumentType,
  Role,
  RoleCreate,
  RoleUpdate,
  RoleWorkspacePermission,
} from '../types';

const runtimeApiBaseUrl = window.__OPENRAG_CONFIG__?.apiBaseUrl?.trim();
const envApiBaseUrl = import.meta.env.VITE_API_URL?.trim();
const resolvedApiBaseUrl = runtimeApiBaseUrl || envApiBaseUrl || '/api';

const api = axios.create({
  baseURL: resolvedApiBaseUrl,
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  // Add workspace_id header if available
  const workspaceId = localStorage.getItem('currentWorkspaceId');
  if (workspaceId) {
    config.headers['X-Workspace-ID'] = workspaceId;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('token');
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);

export const authAPI = {
  login: async (data: LoginRequest): Promise<LoginResponse> => {
    const formData = new URLSearchParams();
    formData.append('email', data.email);
    formData.append('password', data.password);
    const response = await api.post('/users/login', formData, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    return response.data;
  },
  register: async (data: RegisterRequest): Promise<User> => {
    const response = await api.post('/users/register', data);
    return response.data;
  },
  getCurrentUser: async (): Promise<User> => {
    const response = await api.get('/users/me');
    return response.data;
  },
};

export const filesAPI = {
  list: async (
    workspaceId?: number,
    opts?: {
      parentPath?: string; underPath?: string; skip?: number; limit?: number;
      filename?: string; fileType?: string; ownerUsername?: string;
      simpleStatus?: string; createdAfter?: string; createdBefore?: string;
    }
  ): Promise<File[]> => {
    const params: Record<string, string | number> = {};
    if (workspaceId != null) params.workspace_id = workspaceId;
    if (opts?.parentPath != null) params.parent_path = opts.parentPath;
    if (opts?.underPath != null) params.under_path = opts.underPath;
    if (opts?.skip != null) params.skip = opts.skip;
    if (opts?.limit != null) params.limit = opts.limit;
    if (opts?.filename != null) params.filename = opts.filename;
    if (opts?.fileType != null) params.file_type = opts.fileType;
    if (opts?.ownerUsername != null) params.owner_username = opts.ownerUsername;
    if (opts?.simpleStatus != null) params.simple_status = opts.simpleStatus;
    if (opts?.createdAfter != null) params.created_after = opts.createdAfter;
    if (opts?.createdBefore != null) params.created_before = opts.createdBefore;
    const response = await api.get('/files/', { params });
    return response.data.items ? response.data.items : (Array.isArray(response.data) ? response.data : []);
  },
  upload: async (
    file: globalThis.File,
    parserType: string = 'auto',
    workspaceId: number = 1,
    path: string = '/',
    documentType: DocumentType = 'general'
  ): Promise<File> => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('parser_type', parserType);
    formData.append('workspace_id', workspaceId.toString());
    formData.append('path', path);
    formData.append('document_type', documentType);
    const response = await api.post('/files/upload', formData);
    return response.data;
  },
  delete: async (
    id: number,
    options?: { background?: boolean }
  ): Promise<{ message: string; task_id?: number; async?: boolean; status: number }> => {
    const response = await api.delete(`/files/${id}`, {
      params: { background: options?.background ?? true },
    });
    const data = response.data as { message?: string; task_id?: number; async?: boolean };
    return {
      message: data.message ?? '',
      task_id: data.task_id,
      async: data.async,
      status: response.status,
    };
  },
  /** 按路径前缀级联删除（虚拟目录 + 其下文件），默认异步任务 */
  deletePathPrefix: async (
    workspaceId: number,
    path: string,
    options?: { background?: boolean }
  ): Promise<{
    message: string;
    task_id?: number;
    async?: boolean;
    status: number;
    deleted_count?: number;
    path?: string;
  }> => {
    const response = await api.post(
      '/files/delete-path-prefix',
      { workspace_id: workspaceId, path },
      { params: { background: options?.background ?? true } }
    );
    const data = response.data as {
      message?: string;
      task_id?: number;
      async?: boolean;
      deleted_count?: number;
      path?: string;
    };
    return {
      message: data.message ?? '',
      task_id: data.task_id,
      async: data.async,
      status: response.status,
      deleted_count: data.deleted_count,
      path: data.path,
    };
  },
  move: async (id: number, newPath: string): Promise<File> => {
    const response = await api.put(`/files/${id}/move`, { new_path: newPath });
    return response.data;
  },
  reprocess: async (id: number, parserType?: string, documentType?: DocumentType): Promise<File> => {
    const body: { parser_type?: string; document_type?: DocumentType } = {};
    if (parserType !== undefined) body.parser_type = parserType;
    if (documentType !== undefined) body.document_type = documentType;
    const response = await api.post(`/files/${id}/reprocess`, body);
    return response.data;
  },
  createDirectory: async (path: string, workspaceId: number): Promise<File> => {
    const formData = new FormData();
    formData.append('path', path);
    formData.append('workspace_id', workspaceId.toString());
    const response = await api.post('/files/directories', formData);
    return response.data;
  },
  get: async (id: number): Promise<File> => {
    const response = await api.get(`/files/${id}`);
    return response.data as File;
  },
  /** 原始文件流（需鉴权）；用于 PDF / 图片 / 文本等浏览器预览 */
  fetchContentBlob: async (id: number): Promise<Blob> => {
    const response = await api.get(`/files/${id}/content`, { responseType: 'blob' });
    return response.data as Blob;
  },
  /** Office / 部分文本的后端结构化预览 */
  fetchPreview: async (id: number): Promise<{ format: 'html' | 'text'; content: string }> => {
    const response = await api.get(`/files/${id}/preview`);
    return response.data as { format: 'html' | 'text'; content: string };
  },
  listChunks: async (
    workspaceId: number,
    fileId: number,
    params?: { skip?: number; limit?: number; q?: string }
  ): Promise<DocumentChunkListResponse> => {
    const response = await api.get(`/workspaces/${workspaceId}/files/${fileId}/chunks`, { params });
    return response.data as DocumentChunkListResponse;
  },
  fetchWorkspaceContentBlob: async (workspaceId: number, fileId: number): Promise<Blob> => {
    const response = await api.get(`/workspaces/${workspaceId}/files/${fileId}/content`, { responseType: 'blob' });
    return response.data as Blob;
  },
  fetchWorkspacePreview: async (
    workspaceId: number,
    fileId: number
  ): Promise<{ format: 'html' | 'text'; content: string }> => {
    const response = await api.get(`/workspaces/${workspaceId}/files/${fileId}/preview`);
    return response.data as { format: 'html' | 'text'; content: string };
  },
  fetchWorkspaceChunkSource: async (
    workspaceId: number,
    fileId: number
  ): Promise<{ format: 'text'; content: string }> => {
    const response = await api.get(`/workspaces/${workspaceId}/files/${fileId}/chunk-source`);
    return response.data as { format: 'text'; content: string };
  },
};

export const searchAPI = {
  /** POST /search 与 /search/semantic 等价（OpenRag 后端） */
  search: async (data: SearchRequest): Promise<SearchResponse> => {
    const response = await api.post<SearchResponse>('/search', data);
    return response.data;
  },
  semantic: async (data: SearchRequest): Promise<SearchResponse> => {
    const response = await api.post<SearchResponse>('/search/semantic', data);
    return response.data;
  },
  hierarchical: async (data: SearchRequest): Promise<SearchResponse> => {
    const response = await api.post<SearchResponse>('/search/hierarchical', data);
    return response.data;
  },
};

export interface WorkspaceMember {
  id: number;
  workspace_id: number;
  user_id: number;
  role: 'read' | 'write';
  joined_at: string;
}

export const teamsAPI = {
  list: async (): Promise<Team[]> => {
    const response = await api.get('/teams');
    return response.data;
  },
  create: async (name: string, description?: string): Promise<Team> => {
    const response = await api.post('/teams', { name, description });
    return response.data;
  },
};

export const workspacesAPI = {
  list: async (): Promise<Workspace[]> => {
    const response = await api.get('/workspaces');
    return response.data;
  },
  create: async (data: {
    name: string;
    slug: string;
    description?: string;
    max_concurrent_tasks?: number;
    max_storage_bytes?: number;
    priority_strategy?: string;
  }): Promise<Workspace> => {
    const response = await api.post('/workspaces', data);
    return response.data;
  },
  get: async (id: number): Promise<Workspace> => {
    const response = await api.get(`/workspaces/${id}`);
    return response.data;
  },
  update: async (id: number, data: { name?: string; description?: string }): Promise<Workspace> => {
    const response = await api.put(`/workspaces/${id}`, data);
    return response.data;
  },
  delete: async (id: number): Promise<void> => {
    await api.delete(`/workspaces/${id}`);
  },
  // Workspace member management
  listMembers: async (workspaceId: number): Promise<WorkspaceMember[]> => {
    const response = await api.get(`/workspaces/${workspaceId}/members`);
    return response.data;
  },
  addMember: async (workspaceId: number, userId: number, role: 'read' | 'write'): Promise<WorkspaceMember> => {
    const response = await api.post(`/workspaces/${workspaceId}/members`, { user_id: userId, role });
    return response.data;
  },
  removeMember: async (workspaceId: number, userId: number): Promise<void> => {
    await api.delete(`/workspaces/${workspaceId}/members/${userId}`);
  },
};

export const serviceTokensAPI = {
  create: async (
    body: { name?: string; workspaces: WorkspaceBindingRequest[] }
  ): Promise<ServiceTokenCreated> => {
    const response = await api.post('/service-tokens', body);
    return response.data;
  },
  list: async (
    workspaceId?: number
  ): Promise<ServiceTokenListItem[]> => {
    const params: Record<string, number> = {};
    if (workspaceId != null) params.workspace_id = workspaceId;
    const response = await api.get('/service-tokens', { params });
    return response.data;
  },
  patchBindings: async (
    tokenId: number,
    body: BindingPatchRequest
  ): Promise<ServiceTokenListItem> => {
    const response = await api.patch(`/service-tokens/${tokenId}/workspaces`, body);
    return response.data;
  },
  revoke: async (tokenId: number): Promise<{ message: string }> => {
    const response = await api.delete(`/service-tokens/${tokenId}`);
    return response.data;
  },
  getSecret: async (tokenId: number): Promise<{ secret: string }> => {
    const response = await api.get(`/service-tokens/${tokenId}/secret`);
    return response.data;
  },
};

export const tasksAPI = {
  list: async (workspaceId: number, params?: { status?: string; skip?: number; limit?: number }): Promise<TaskListResponse> => {
    const response = await api.get(`/workspaces/${workspaceId}/tasks`, { params });
    return response.data;
  },
  getStats: async (workspaceId: number): Promise<TaskStatsResponse> => {
    const response = await api.get(`/workspaces/${workspaceId}/tasks/stats`);
    return response.data;
  },
  getDetail: async (workspaceId: number, taskId: number): Promise<Task> => {
    const response = await api.get(`/workspaces/${workspaceId}/tasks/${taskId}`);
    return response.data;
  },
  cancel: async (workspaceId: number, taskId: number): Promise<{ message: string }> => {
    const response = await api.post(`/workspaces/${workspaceId}/tasks/${taskId}/cancel`);
    return response.data;
  },
  retry: async (workspaceId: number, taskId: number): Promise<Task> => {
    const response = await api.post(`/workspaces/${workspaceId}/tasks/${taskId}/retry`);
    return response.data;
  },
};

export default api;

export const permissionsAPI = {
  getPermissionDetails: async (userId: number) => {
    const response = await api.get(`/users/${userId}/permissions/details`);
    return response.data;
  }
};

export const rolesAPI = {
  list: async (): Promise<Role[]> => {
    const response = await api.get("/roles/");
    return response.data;
  },
  create: async (data: RoleCreate): Promise<Role> => {
    const response = await api.post("/roles/", data);
    return response.data;
  },
  update: async (id: number, data: RoleUpdate): Promise<Role> => {
    const response = await api.put(`/roles/${id}`, data);
    return response.data;
  },
  delete: async (id: number): Promise<{ message: string }> => {
    const response = await api.delete(`/roles/${id}`);
    return response.data;
  },
  getPermissions: async (id: number): Promise<RoleWorkspacePermission[]> => {
    const response = await api.get(`/roles/${id}/permissions`);
    return response.data;
  },
  setPermission: async (id: number, workspaceId: number, permission: string): Promise<{ message: string }> => {
    const response = await api.post(`/roles/${id}/permissions`, { workspace_id: workspaceId, permission });
    return response.data;
  },
  removePermission: async (id: number, workspaceId: number): Promise<{ message: string }> => {
    const response = await api.delete(`/roles/${id}/permissions/${workspaceId}`);
    return response.data;
  }
};

export const usersAdminAPI = {
  listUsers: async (): Promise<User[]> => {
    const response = await api.get("/users/");
    return response.data;
  },
  assignRole: async (userId: number, roleId: number): Promise<{ message: string }> => {
    const response = await api.post(`/users/${userId}/roles`, { role_id: roleId });
    return response.data;
  },
  removeRole: async (userId: number, roleId: number): Promise<{ message: string }> => {
    const response = await api.delete(`/users/${userId}/roles/${roleId}`);
    return response.data;
  }
};

