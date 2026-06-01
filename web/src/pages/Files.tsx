import { Layout, Menu, Button, Dropdown, Space, Typography, Modal, Form, Input, message, Empty, Switch, Card, Select, Tree } from 'antd';
import { FileOutlined, SearchOutlined, SettingOutlined, LogoutOutlined, DownOutlined, TeamOutlined, PlusOutlined, UploadOutlined, SyncOutlined, FolderOutlined, DeleteOutlined } from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import type { CSSProperties, Key } from 'react';
import { useTranslation } from 'react-i18next';
import type { DataNode } from 'antd/es/tree';
import FileList from '../components/FileList';
import FileUpload from '../components/FileUpload';
import { filesAPI, workspacesAPI, authAPI } from '../services/api';
import type { File, Workspace, User } from '../types';
import { buildAppMenuItems } from '../utils/app-menu';
import './Files.css';

const { Header, Content, Sider } = Layout;
const SIDER_WIDTH = 300;
const FIELD_ITEM_STYLE: CSSProperties = {
  marginBottom: 0,
  flex: '0 1 280px',
  minWidth: 220,
};
const TYPE_ITEM_STYLE: CSSProperties = {
  marginBottom: 0,
  flex: '0 1 220px',
  minWidth: 180,
};
const FILTER_TOOLBAR_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 16,
  flexWrap: 'wrap',
};
const FILTER_ACTIONS_STYLE: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  flexWrap: 'wrap',
  marginLeft: 'auto',
};
const SIDER_SCROLL_STYLE: CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  overflowX: 'hidden',
  paddingBottom: 16,
};
const SIDER_DIRECTORY_STYLE: CSSProperties = {
  margin: '2px 14px 18px',
};
const SIDER_DIRECTORY_BODY_STYLE: CSSProperties = {
  maxHeight: '38vh',
  overflow: 'auto',
};

/** 与后端逻辑路径对齐：必有前导 /、去尾部多余 /。 */
function normalizeLogicalPath(uri: string | undefined): string {
  let s = (uri ?? '').trim().replace(/\/+$/, '');
  if (!s) return '/';
  if (!s.startsWith('/')) s = `/${s}`;
  return s;
}

/** 非目录：逻辑路径落在当前选中目录之下（含任意深度的子路径）。 */
/** 与后端 `_is_direct_child_uri` 一致：用于过滤错误的 list 结果，防止树挂错子节点。 */
function isDirectChildUri(uri: string | undefined, parentPath: string): boolean {
  const p = normalizeLogicalPath(parentPath);
  const u = normalizeLogicalPath(uri);
  if (!u || u === '/') return false;
  if (p === '/') {
    const inner = u.slice(1);
    return inner.length > 0 && !inner.includes('/');
  }
  const pref = `${p}/`;
  if (!u.startsWith(pref)) return false;
  const suffix = u.slice(pref.length);
  return suffix.length > 0 && !suffix.includes('/');
}

function joinChildDirectoryPath(parentKey: string, rawName: string): string {
  const segment = rawName
    .trim()
    .replace(/\\/g, '/')
    .replace(/^\/+|\/+$/g, '');
  if (!segment || segment.includes('/') || segment.includes('..')) {
    throw new Error('INVALID_DIR_NAME');
  }
  const parent = parentKey === '/' ? '' : String(parentKey).replace(/\/+$/, '');
  return parent ? `${parent}/${segment}` : `/${segment}`;
}

const FETCH_LIMIT = 10_000;

function updateTreeData(list: DataNode[], nodeKey: Key, children: DataNode[]): DataNode[] {
  return list.map((node) => {
    if (node.key === nodeKey) {
      return { ...node, children };
    }
    if (node.children) {
      return { ...node, children: updateTreeData(node.children, nodeKey, children) };
    }
    return node;
  });
}

/** 懒加载一层：目录不挂 children（由展开时拉取）；文件为叶。 */
function mapFilesToLazyTreeNodes(rows: File[]): DataNode[] {
  const dirs = rows
    .filter((f) => f.is_directory)
    .sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  const fls = rows
    .filter((f) => !f.is_directory)
    .sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  const out: DataNode[] = [];
  for (const f of dirs) {
    out.push({
      title: f.name || f.uri.split('/').pop() || f.uri,
      key: f.uri,
      icon: <FolderOutlined />,
      isLeaf: false,
    });
  }
  for (const f of fls) {
    out.push({
      title: f.name || f.uri.split('/').pop() || f.uri,
      key: `file-${f.id}`,
      icon: <FileOutlined />,
      isLeaf: true,
    });
  }
  return out;
}

function mergeFileSnapshotsById(a: File[], b: File[]): File[] {
  const m = new Map<number, File>();
  for (const f of a) m.set(f.id, f);
  for (const f of b) m.set(f.id, f);
  return Array.from(m.values());
}

/**
 * 库中尚无「中间目录」行时，根据工作区内更深的路径插入可展开的隐式目录节点，
 * 否则 `/uploads/a.txt` 在根 `parent_path=/` 下不可见，但与右侧 under_path 列表不一致。
 */
function impliedDirectoryDataNodesForParent(
  parentPath: string,
  directRows: File[],
  workspaceFiles: File[]
): DataNode[] {
  const p = normalizeLogicalPath(parentPath);
  const pref = p === '/' ? '/' : `${p.replace(/\/+$/, '')}/`;
  const directUris = new Set(directRows.map((r) => normalizeLogicalPath(r.uri)));
  const impliedPaths = new Set<string>();

  for (const f of workspaceFiles) {
    const u = normalizeLogicalPath(f.uri);
    if (u === '/' || u === p) continue;
    if (p === '/') {
      if (!u.startsWith('/') || u.length < 2) continue;
    } else if (!u.startsWith(pref)) {
      continue;
    }

    const rel = p === '/' ? u.slice(1) : u.slice(pref.length);
    if (!rel.includes('/')) continue;

    const seg = rel.split('/')[0];
    if (!seg) continue;
    const childPath = p === '/' ? `/${seg}` : `${p.replace(/\/+$/, '')}/${seg}`;

    if (directUris.has(childPath)) continue;

    const occupant = workspaceFiles.find((x) => normalizeLogicalPath(x.uri) === childPath);
    if (occupant && !occupant.is_directory) continue;

    impliedPaths.add(childPath);
  }

  return Array.from(impliedPaths)
    .sort((a, b) => a.localeCompare(b))
    .map((uri) => ({
      title: uri.split('/').filter(Boolean).pop() || uri,
      key: uri,
      icon: <FolderOutlined />,
      isLeaf: false,
    }));
}

function mergeLazyTreeNodesWithImpliedDirs(
  parentPath: string,
  directRows: File[],
  workspaceFiles: File[]
): DataNode[] {
  const implied = impliedDirectoryDataNodesForParent(parentPath, directRows, workspaceFiles);
  const base = mapFilesToLazyTreeNodes(directRows);
  const byKey = new Map<string, DataNode>();
  for (const n of implied) {
    byKey.set(String(n.key), n);
  }
  for (const n of base) {
    if (String(n.key).startsWith('file-')) continue;
    byKey.set(String(n.key), n);
  }
  const dirs = Array.from(byKey.values()).sort((a, b) => String(a.title).localeCompare(String(b.title)));
  const fls = base.filter((n) => String(n.key).startsWith('file-'));
  return [...dirs, ...fls];
}

const { Text } = Typography;

export default function Files() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [files, setFiles] = useState<File[]>([]);
  const filesRef = useRef<File[]>([]);
  filesRef.current = files;
  const [treeData, setTreeData] = useState<DataNode[]>([]);
  const [loading, setLoading] = useState(false);
  const treeLoadedPathRef = useRef<Set<string>>(new Set());
  const treeLoadInFlightRef = useRef<Set<string>>(new Set());
  /** 用于区分「切换工作区」与「仅切换选中目录」：后者不重置左侧树。 */
  const prevWorkspaceIdForTreeRef = useRef<number | undefined>(undefined);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null);
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false);
  const [selectedDirectory, setSelectedDirectory] = useState<string>('/');

  const toggleLanguage = (checked: boolean) => {
    i18n.changeLanguage(checked ? 'zh' : 'en');
  };

  const canWrite = currentWorkspace?.user_role === 'write' || currentUser?.is_admin;
  const [createForm] = Form.useForm();
  const [searchForm] = Form.useForm();
  const [creating, setCreating] = useState(false);
  const [createDirModalOpen, setCreateDirModalOpen] = useState(false);
  const [createDirParentKey, setCreateDirParentKey] = useState<string>('/');
  const [createDirSubmitting, setCreateDirSubmitting] = useState(false);
  const [createDirForm] = Form.useForm<{ dir_name: string }>();
  const [searchFilters, setSearchFilters] = useState<{
    filename?: string;
    type?: string;
  }>({});
  const searchFiltersRef = useRef(searchFilters);
  searchFiltersRef.current = searchFilters;

  function searchFiltersToOpts(filters: typeof searchFilters): Record<string, string> {
    const opts: Record<string, string> = {};
    if (filters.filename) opts.filename = filters.filename;
    if (filters.type) opts.fileType = filters.type;
    return opts;
  }

  const mergeFilesById = useCallback((incoming: File[]) => {
    setFiles((prev) => {
      const m = new Map<number, File>();
      prev.forEach((f) => m.set(f.id, f));
      incoming.forEach((f) => m.set(f.id, f));
      return Array.from(m.values());
    });
  }, []);

  /** 全局刷新：重建根节点 + 重新拉当前列表子树（不依赖懒加载子节点缓存）。 */
  const loadFiles = useCallback(
    async (workspaceId?: number) => {
      const wid = workspaceId ?? currentWorkspace?.id;
      if (!wid) {
        setFiles([]);
        setTreeData([]);
        treeLoadedPathRef.current.clear();
        treeLoadInFlightRef.current.clear();
        return;
      }
      setLoading(true);
      try {
        setFiles([]);
        treeLoadedPathRef.current.clear();
        treeLoadInFlightRef.current.clear();
        const [rootRaw, subtree] = await Promise.all([
          filesAPI.list(wid, { parentPath: '/', limit: FETCH_LIMIT }),
          filesAPI.list(wid, {
            underPath: selectedDirectory,
            limit: FETCH_LIMIT,
            ...searchFiltersToOpts(searchFiltersRef.current),
          }),
        ]);
        const rootChildren = rootRaw.filter((f) => isDirectChildUri(f.uri, '/'));
        // When filters are active, only use the filtered subtree for display
        const hasFilters = Object.values(searchFiltersToOpts(searchFiltersRef.current)).some(v => v);
        if (hasFilters) {
          mergeFilesById(subtree);
        } else {
          const snapshot = mergeFileSnapshotsById(rootRaw, subtree);
          mergeFilesById(snapshot);
        }
        setTreeData([
          {
            title: t('files.upload.root'),
            key: '/',
            icon: <FolderOutlined />,
            children: mergeLazyTreeNodesWithImpliedDirs('/', rootChildren, rootRaw),
          },
        ]);
        treeLoadedPathRef.current.add('/');
      } catch (error) {
        console.error('Failed to load files:', error);
        throw error;
      } finally {
        setLoading(false);
      }
    },
    [currentWorkspace?.id, selectedDirectory, mergeFilesById, t]
  );

  const loadWorkspaces = async () => {
    try {
      const data = await workspacesAPI.list();
      setWorkspaces(data);
      const savedWorkspaceId = localStorage.getItem('currentWorkspaceId');
      if (savedWorkspaceId) {
        const saved = data.find((w: Workspace) => w.id.toString() === savedWorkspaceId);
        if (saved) {
          setCurrentWorkspace(saved);
        } else if (data.length > 0) {
          setCurrentWorkspace(data[0]);
          localStorage.setItem('currentWorkspaceId', data[0].id.toString());
        }
      } else if (data.length > 0) {
        setCurrentWorkspace(data[0]);
        localStorage.setItem('currentWorkspaceId', data[0].id.toString());
      }
    } catch (error) {
      console.error('Failed to load workspaces:', error);
      message.error('Failed to load workspaces');
    }
  };

  useEffect(() => {
    loadWorkspaces();
    authAPI.getCurrentUser().then(setCurrentUser).catch(console.error);
  }, []);

  /**
   * 工作区 id 变化：清空 files、重建树根、重置懒加载标记，再拉 under_path。
   * 仅 selectedDirectory 变化：只拉 under_path 合并到 files，不碰左侧树（避免重复展开/错乱）。
   */
  useEffect(() => {
    if (!currentWorkspace?.id) {
      setFiles([]);
      setTreeData([]);
      treeLoadedPathRef.current.clear();
      treeLoadInFlightRef.current.clear();
      prevWorkspaceIdForTreeRef.current = undefined;
      return;
    }
    const wid = currentWorkspace.id;
    const resetTree = prevWorkspaceIdForTreeRef.current !== wid;
    prevWorkspaceIdForTreeRef.current = wid;
    let cancelled = false;
    (async () => {
      try {
        if (resetTree) {
          setLoading(true);
          setFiles([]);
          treeLoadedPathRef.current.clear();
          treeLoadInFlightRef.current.clear();
          const [rootRaw, subtree] = await Promise.all([
            filesAPI.list(wid, { parentPath: '/', limit: FETCH_LIMIT }),
            filesAPI.list(wid, {
              underPath: selectedDirectory,
              limit: FETCH_LIMIT,
              ...searchFiltersToOpts(searchFiltersRef.current),
            }),
          ]);
          if (cancelled) return;
          const rootChildren = rootRaw.filter((f) => isDirectChildUri(f.uri, '/'));
          const hasFilters = Object.values(searchFiltersToOpts(searchFiltersRef.current)).some(v => v);
          if (hasFilters) {
            mergeFilesById(subtree);
          } else {
            const snapshot = mergeFileSnapshotsById(rootRaw, subtree);
            mergeFilesById(snapshot);
          }
          setTreeData([
            {
              title: t('files.upload.root'),
              key: '/',
              icon: <FolderOutlined />,
              children: mergeLazyTreeNodesWithImpliedDirs('/', rootChildren, rootRaw),
            },
          ]);
          treeLoadedPathRef.current.add('/');
        } else if (!cancelled) {
          const subtree = await filesAPI.list(wid, {
            underPath: selectedDirectory,
            limit: FETCH_LIMIT,
            ...searchFiltersToOpts(searchFiltersRef.current),
          });
          mergeFilesById(subtree);
        }
      } catch (error) {
        if (!cancelled) console.error('Failed to load workspace files:', error);
      } finally {
        if (resetTree && !cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [currentWorkspace?.id, selectedDirectory, mergeFilesById, t]);

  /**
   * 用 onExpand 拉子节点（不用 loadData：部分环境下 loadData 拿到的 key 不可靠，会把根目录子项挂到子目录下）。
   * 根结点 `/` 仅在 effect 里填充，这里忽略。
   */
  const handleTreeExpand = useCallback(
    async (_expandedKeys: Key[], info: { expanded: boolean; node: DataNode }) => {
      if (!info.expanded) return;
      const raw = info.node?.key;
      const key = raw == null ? '' : String(raw);
      if (!key || key === 'undefined') return;
      if (key.startsWith('file-')) return;
      if (key === '/') return;
      if (treeLoadedPathRef.current.has(key)) return;
      if (treeLoadInFlightRef.current.has(key)) return;
      const wid = currentWorkspace?.id;
      if (!wid) return;
      const parentPath = normalizeLogicalPath(key);
      treeLoadInFlightRef.current.add(parentPath);
      try {
        const rawRows = await filesAPI.list(wid, { parentPath: parentPath, limit: FETCH_LIMIT });
        const rows = rawRows.filter((f) => isDirectChildUri(f.uri, parentPath));
        const snap = mergeFileSnapshotsById(filesRef.current, rawRows);
        mergeFilesById(rows);
        setTreeData((prev) =>
          updateTreeData(prev, parentPath, mergeLazyTreeNodesWithImpliedDirs(parentPath, rows, snap))
        );
        treeLoadedPathRef.current.add(parentPath);
      } catch (error) {
        console.error('Failed to load directory children:', error);
        message.error(t('files.messages.list_failed'));
      } finally {
        treeLoadInFlightRef.current.delete(parentPath);
      }
    },
    [currentWorkspace?.id, mergeFilesById, t]
  );

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('currentWorkspaceId');
    navigate('/login');
  };

  const handleCreateWorkspace = async (values: { name: string; slug: string; description?: string }) => {
    setCreating(true);
    try {
      const newWorkspace = await workspacesAPI.create({
        name: values.name,
        slug: values.slug,
        description: values.description,
      });
      message.success('Workspace created successfully');
      setIsCreateModalOpen(false);
      createForm.resetFields();
      setWorkspaces([...workspaces, newWorkspace]);
      setCurrentWorkspace(newWorkspace);
      localStorage.setItem('currentWorkspaceId', newWorkspace.id.toString());
    } catch (error: any) {
      console.error('Failed to create workspace:', error);
      message.error(error.response?.data?.detail || 'Failed to create workspace');
    } finally {
      setCreating(false);
    }
  };

  const handleWorkspaceChange = (workspace: Workspace) => {
    setSelectedDirectory('/');
    setCurrentWorkspace(workspace);
    localStorage.setItem('currentWorkspaceId', workspace.id.toString());
  };

  const handleSearch = async (values: Record<string, unknown>) => {
    const newFilters = {
      filename: typeof values.filename === 'string' && values.filename.trim() ? values.filename.trim() : undefined,
      type: (values.type as string | undefined) || undefined,
    };
    setSearchFilters(newFilters);
    searchFiltersRef.current = newFilters;
    try {
      await loadFiles();
    } catch (e) {
      console.error(e);
      message.error(t('files.messages.list_failed'));
    }
  };

  const generateSlug = (name: string) => {
    return name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '');
  };

  const onSelectDirectory = (selectedKeys: Key[]) => {
    if (selectedKeys.length > 0) {
      const key = selectedKeys[0] as string;
      if (!key.startsWith('file-')) {
        setSelectedDirectory(key);
      }
    }
  };

  const handleDeleteDir = async (uri: string) => {
    if (!currentWorkspace) return;
    try {
      const res = await filesAPI.deletePathPrefix(currentWorkspace.id, uri);
      if (res.status === 202 && res.async) {
        message.success(t('files.messages.delete_queued', { id: res.task_id ?? '' }));
      } else if (res.message.includes('No files')) {
        message.info(res.message);
      } else {
        message.success(t('files.messages.delete_success'));
      }
      void loadFiles().catch(() => {});
    } catch (error: any) {
      console.error('Failed to delete directory:', error);
      message.error(error.response?.data?.detail || t('files.messages.delete_failed'));
    }
  };

  /**
   * 局部刷新某个目录分支，避免全量重建树导致已展开结构闪断/消失。
   */
  const refreshTreeBranch = useCallback(
    async (parentPath: string) => {
      const wid = currentWorkspace?.id;
      if (!wid) return;
      const normalizedParent = normalizeLogicalPath(parentPath);
      const [rawRows, subtree] = await Promise.all([
        filesAPI.list(wid, { parentPath: normalizedParent, limit: FETCH_LIMIT }),
        filesAPI.list(wid, {
          underPath: selectedDirectory,
          limit: FETCH_LIMIT,
        }),
      ]);
      const rows = rawRows.filter((f) => isDirectChildUri(f.uri, normalizedParent));
      const mergedIncoming = mergeFileSnapshotsById(rawRows, subtree);
      const snapshot = mergeFileSnapshotsById(filesRef.current, mergedIncoming);
      mergeFilesById(mergedIncoming);
      setTreeData((prev) =>
        updateTreeData(prev, normalizedParent, mergeLazyTreeNodesWithImpliedDirs(normalizedParent, rows, snapshot))
      );
      treeLoadedPathRef.current.add(normalizedParent);
    },
    [currentWorkspace?.id, selectedDirectory, mergeFilesById]
  );

  const openCreateDirModal = (parentKey: string) => {
    setCreateDirParentKey(parentKey);
    createDirForm.resetFields();
    setCreateDirModalOpen(true);
  };

  const submitCreateDir = async () => {
    if (!currentWorkspace) return;
    try {
      const { dir_name } = await createDirForm.validateFields();
      const fullPath = joinChildDirectoryPath(createDirParentKey, dir_name);
      setCreateDirSubmitting(true);
      await filesAPI.createDirectory(fullPath, currentWorkspace.id);
      message.success(t('files.messages.create_dir_success'));
      setCreateDirModalOpen(false);
      createDirForm.resetFields();
      try {
        await refreshTreeBranch(createDirParentKey);
      } catch {
        // 局部刷新失败时回退全量刷新，保证数据最终一致。
        void loadFiles().catch(() => {});
      }
    } catch (error: any) {
      if (error?.errorFields) return;
      console.error('Failed to create directory:', error);
      message.error(error.response?.data?.detail || t('files.messages.create_dir_failed'));
    } finally {
      setCreateDirSubmitting(false);
    }
  };

  const renderTreeTitle = (nodeData: any) => {
    const isRoot = nodeData.key === '/';
    // 检查是否是文件节点（文件节点以 "file-" 开头）
    const isFileNode = typeof nodeData.key === 'string' && nodeData.key.startsWith('file-');
    // 管理目录下拉菜单项（含虚拟目录：按 URI 前缀异步级联删除）
    const manageDirMenuItems = [
      {
        key: 'create',
        icon: <PlusOutlined />,
        label: t('files.actions.add_dir'),
        onClick: (e: any) => {
          e.domEvent.stopPropagation();
          openCreateDirModal(String(nodeData.key));
        },
      },
      {
        key: 'delete',
        icon: <DeleteOutlined />,
        label: t('files.actions.delete_dir'),
        danger: true,
        onClick: (e: any) => {
          e.domEvent.stopPropagation();
          Modal.confirm({
            title: t('files.actions.delete_dir'),
            content: t('files.messages.delete_dir_confirm'),
            okText: t('files.messages.yes'),
            cancelText: t('files.messages.no'),
            onOk: () => handleDeleteDir(nodeData.key as string),
          });
        },
      },
    ];

    const menuItems = isRoot
      ? manageDirMenuItems.filter((i) => i.key === 'create')
      : manageDirMenuItems;

    return (
      <span className="files-tree-title">
        <span className="files-tree-title-text" title={String(nodeData.title ?? '')}>
          {nodeData.title}
        </span>
        {canWrite && !isFileNode && menuItems.length > 0 && (
          <Dropdown menu={{ items: menuItems }} placement="bottomLeft">
            <Button
              type="link"
              size="small"
              icon={<SettingOutlined />}
              style={{ paddingInline: 0, height: 'auto' }}
              onClick={(e) => e.stopPropagation()}
            />
          </Dropdown>
        )}
      </span>
    );
  };

  const displayedFiles = useMemo(() => {
    return files.filter((f) => {
      // 仅文件：不展示目录行
      if (f.is_directory) {
        return false;
      }
      return true;
    });
  }, [files]);

  const workspaceMenuItems = [
    ...workspaces.map((workspace) => ({
      key: workspace.id.toString(),
      label: workspace.name,
      onClick: () => handleWorkspaceChange(workspace),
    })),
    { type: 'divider' as const },
    {
      key: 'create',
      label: (
        <Space>
          <PlusOutlined />
          {t('files.workspace.create')}
        </Space>
      ),
      onClick: () => setIsCreateModalOpen(true),
    },
  ];

  const menuItems = buildAppMenuItems(t, !!currentUser?.is_admin);
  const filesMenuItems = (menuItems ?? []).filter((item) => (item as { key?: Key } | null)?.key === '/files');
  const otherMenuItems = (menuItems ?? []).filter((item) => (item as { key?: Key } | null)?.key !== '/files');

  const renderEmptyState = () => (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        <Space direction="vertical" align="center">
          <Text>{t('files.workspace.no_workspace')}</Text>
          <Text type="secondary">{t('files.workspace.create_hint')}</Text>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setIsCreateModalOpen(true)}
          >
            {t('files.workspace.create')}
          </Button>
        </Space>
      }
    />
  );

  const renderSiderDirectoryTree = () => (
    <div className="files-sider-directory" style={SIDER_DIRECTORY_STYLE}>
      <div className="files-sider-directory-title">
        {t('files.workspace.directory')}
      </div>
      <div className="files-sider-directory-body custom-dark-scrollbar" style={SIDER_DIRECTORY_BODY_STYLE}>
        <div className="files-sider-tree-wrap">
          <Tree
            showIcon
            blockNode
            defaultExpandedKeys={['/']}
            defaultSelectedKeys={['/']}
            treeData={treeData}
            onExpand={(keys, info) => {
              void handleTreeExpand(keys, info);
            }}
            onSelect={onSelectDirectory}
            titleRender={renderTreeTitle}
          />
        </div>
      </div>
    </div>
  );

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        width={SIDER_WIDTH}
        className="app-sider-smooth"
        style={{ position: 'fixed', height: '100vh', left: 0, top: 0, bottom: 0 }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <div style={{ height: 32, margin: 16, color: 'white', fontSize: 20, fontWeight: 'bold' }}>
            {t('app.title')}
          </div>
          <div className="files-sider-scroll custom-dark-scrollbar" style={SIDER_SCROLL_STYLE}>
            <Menu
              theme="dark"
              mode="inline"
              selectedKeys={[location.pathname]}
              items={filesMenuItems}
              onClick={({ key }) => navigate(key)}
              style={{ borderRight: 0 }}
            />
            {workspaces.length > 0 && renderSiderDirectoryTree()}
            <Menu
              theme="dark"
              mode="inline"
              selectedKeys={[location.pathname]}
              items={otherMenuItems}
              onClick={({ key }) => navigate(key)}
              style={{ borderRight: 0 }}
            />
          </div>
          <div style={{ padding: '16px', borderTop: '1px solid rgba(255, 255, 255, 0.1)', marginTop: 'auto' }}>
            <Button 
              type="text" 
              icon={<SettingOutlined />} 
              onClick={() => navigate('/settings')}
              style={{ width: '100%', color: 'rgba(255, 255, 255, 0.65)', textAlign: 'left' }}
            />
          </div>
        </div>
      </Sider>
      <Layout className="app-main-layout-smooth" style={{ marginLeft: SIDER_WIDTH, minWidth: 0 }}>
        <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 16 }}>
          <Space>
            <Text>EN</Text>
            <Switch checked={i18n.language === 'zh'} onChange={toggleLanguage} />
            <Text>中文</Text>
          </Space>
          <Dropdown menu={{ items: workspaceMenuItems }} placement="bottomRight">
            <Button type="text">
              <Space>
                <TeamOutlined />
                <Text strong>{currentWorkspace?.name || t('files.workspace.select')}</Text>
                <DownOutlined />
              </Space>
            </Button>
          </Dropdown>
          <Button icon={<LogoutOutlined />} onClick={handleLogout}>
            {t('app.logout')}
          </Button>
        </Header>
        <Content style={{ margin: '24px', display: 'flex', flexDirection: 'column', gap: '24px', minWidth: 0 }}>
          {workspaces.length === 0 ? (
            renderEmptyState()
          ) : (
            <>
              <Card>
                <Form
                  form={searchForm}
                  layout="horizontal"
                  onFinish={(v) => void handleSearch(v as Record<string, unknown>)}
                  onFinishFailed={() => message.warning(t('files.messages.search_form_invalid'))}
                >
                  <div style={FILTER_TOOLBAR_STYLE}>
                    <Form.Item name="filename" label={t('files.search_filters.filename')} style={FIELD_ITEM_STYLE}>
                      <Input placeholder={t('files.search_filters.filename')} allowClear />
                    </Form.Item>
                    <Form.Item name="type" label={t('files.search_filters.type')} style={TYPE_ITEM_STYLE}>
                      <Select placeholder={t('files.search_filters.type')} allowClear>
                        <Select.Option value="pdf">PDF</Select.Option>
                        <Select.Option value="docx">Word</Select.Option>
                        <Select.Option value="txt">Text</Select.Option>
                      </Select>
                    </Form.Item>
                    <div style={FILTER_ACTIONS_STYLE}>
                      <Button
                        type="primary"
                        htmlType="button"
                        icon={<SearchOutlined />}
                        onClick={() => searchForm.submit()}
                      >
                        {t('files.search_filters.search')}
                      </Button>
                      {canWrite && (
                        <Button
                          icon={<UploadOutlined />}
                          onClick={() => setIsUploadModalOpen(true)}
                        >
                          {t('files.actions.upload')}
                        </Button>
                      )}
                      <Button icon={<SyncOutlined />} onClick={() => void loadFiles().catch(() => {})}>
                        {t('files.actions.global_update')}
                      </Button>
                    </div>
                  </div>
                </Form>
              </Card>

              <Card style={{ flex: 1, minWidth: 0 }}>
                <FileList
                  files={displayedFiles}
                  onFileDeleted={() => void loadFiles().catch(() => {})}
                  onFileReprocessed={() => void loadFiles().catch(() => {})}
                  loading={loading}
                  canWrite={canWrite}
                  workspaceId={currentWorkspace?.id}
                />
              </Card>
            </>
          )}
        </Content>
      </Layout>

      <Modal
        title={t('files.actions.upload')}
        open={isUploadModalOpen}
        footer={null}
        onCancel={() => setIsUploadModalOpen(false)}
        width={600}
      >
        <FileUpload 
          onUploadSuccess={() => {
            setIsUploadModalOpen(false);
            void loadFiles().catch(() => {});
          }} 
          workspaceId={currentWorkspace?.id} 
          canWrite={canWrite} 
          selectedPath={selectedDirectory}
        />
      </Modal>

      <Modal
        title={t('files.create_subdir.title')}
        open={createDirModalOpen}
        onOk={() => void submitCreateDir()}
        onCancel={() => {
          setCreateDirModalOpen(false);
          createDirForm.resetFields();
        }}
        confirmLoading={createDirSubmitting}
        destroyOnClose
      >
        <Form form={createDirForm} layout="vertical">
          <Form.Item label={t('files.create_subdir.parent_label')}>
            <Input readOnly value={createDirParentKey || '/'} />
          </Form.Item>
          <Form.Item
            name="dir_name"
            label={t('files.create_subdir.name_label')}
            rules={[
              { required: true, message: t('files.create_subdir.name_required') },
              {
                validator: async (_, value) => {
                  const s = (value || '').trim();
                  if (!s) return Promise.resolve();
                  if (s.includes('/') || s.includes('\\') || s.includes('..')) {
                    return Promise.reject(new Error(t('files.create_subdir.name_invalid_chars')));
                  }
                  return Promise.resolve();
                },
              },
            ]}
          >
            <Input placeholder={t('files.create_subdir.name_placeholder')} autoComplete="off" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('files.workspace.create')}
        open={isCreateModalOpen}
        onOk={() => createForm.submit()}
        onCancel={() => {
          setIsCreateModalOpen(false);
          createForm.resetFields();
        }}
        confirmLoading={creating}
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={handleCreateWorkspace}
          onValuesChange={(changedValues) => {
            if (changedValues.name) {
              const slug = generateSlug(changedValues.name);
              createForm.setFieldsValue({ slug });
            }
          }}
        >
          <Form.Item
            name="name"
            label="Workspace Name"
            rules={[{ required: true, message: 'Please enter workspace name' }]}
          >
            <Input placeholder="e.g., My Team" />
          </Form.Item>
          <Form.Item
            name="slug"
            label="Slug"
            rules={[
              { required: true, message: 'Please enter slug' },
              { pattern: /^[a-z0-9-]+$/, message: 'Only lowercase letters, numbers, and hyphens allowed' },
            ]}
          >
            <Input placeholder="e.g., my-team" />
          </Form.Item>
          <Form.Item name="description" label="Description (optional)">
            <Input.TextArea placeholder="Brief description of this workspace" rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </Layout>
  );
}
