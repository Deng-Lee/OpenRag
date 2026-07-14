import { Layout, Menu, Button, Dropdown, Space, Typography, Table, Tag, Card, Statistic, Row, Col, message, Tooltip, Progress, Modal, Switch } from 'antd';
import { SettingOutlined, LogoutOutlined, DownOutlined, TeamOutlined, PlusOutlined, ReloadOutlined, PauseCircleOutlined, CheckCircleOutlined, CloseCircleOutlined, ClockCircleOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { tasksAPI, workspacesAPI, authAPI } from '../services/api';
import type { Task, Workspace, User, TaskStatus } from '../types';
import { formatDateTime } from '../utils/date';
import { buildAppMenuItems } from '../utils/app-menu';

const { Header, Content, Sider } = Layout;
const { Text } = Typography;

const TASK_STATUS_CONFIG: Record<TaskStatus, { color: string; icon: React.ReactNode; label: string }> = {
  pending: { color: 'default', icon: <ClockCircleOutlined />, label: '等待中' },
  assigned: { color: 'processing', icon: <ReloadOutlined spin />, label: '已分配' },
  started: { color: 'processing', icon: <ReloadOutlined spin />, label: '执行中' },
  success: { color: 'success', icon: <CheckCircleOutlined />, label: '成功' },
  failure: { color: 'error', icon: <CloseCircleOutlined />, label: '失败' },
  retry: { color: 'warning', icon: <ReloadOutlined />, label: '重试中' },
  cancelled: { color: 'default', icon: <PauseCircleOutlined />, label: '已取消' },
};

const TASK_TYPE_LABELS: Record<string, string> = {
  process_document: '文档处理',
  parse_document: '文档解析',
  build_hierarchy: '构建层级',
  embed_document: '文档嵌入',
  delete_file: '删除文件',
};

export default function Tasks() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();

  const toggleLanguage = (checked: boolean) => {
    i18n.changeLanguage(checked ? 'zh' : 'en');
  };
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(false);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null);
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [, setIsCreateModalOpen] = useState(false);
  const [stats, setStats] = useState({ total: 0, running: 0, pending: 0, by_status: {} });
  const [pagination, setPagination] = useState({ current: 1, pageSize: 10, total: 0 });
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [detailModal, setDetailModal] = useState<{ open: boolean; task?: Task }>({ open: false });

  const loadTasks = async (page = 1, pageSize = 10, status?: string) => {
    if (!currentWorkspace?.id) {
      setTasks([]);
      return;
    }
    setLoading(true);
    try {
      const data = await tasksAPI.list(currentWorkspace.id, {
        status,
        skip: (page - 1) * pageSize,
        limit: pageSize,
      });
      setTasks(data.items);
      setPagination({
        current: page,
        pageSize,
        total: data.total,
      });
    } catch (error) {
      console.error('Failed to load tasks:', error);
      message.error('加载任务列表失败');
    } finally {
      setLoading(false);
    }
  };

  const loadStats = async () => {
    if (!currentWorkspace?.id) return;
    try {
      const data = await tasksAPI.getStats(currentWorkspace.id);
      setStats(data);
    } catch (error) {
      console.error('Failed to load task stats:', error);
    }
  };

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
      message.error('加载工作空间失败');
    }
  };

  useEffect(() => {
    loadWorkspaces();
    authAPI.getCurrentUser().then(setCurrentUser).catch(console.error);
  }, []);

  useEffect(() => {
    if (currentWorkspace?.id) {
      loadTasks(pagination.current, pagination.pageSize, statusFilter);
      loadStats();
    }
  }, [currentWorkspace?.id]);

  // Auto-refresh tasks every 5 seconds
  useEffect(() => {
    if (!currentWorkspace?.id) return;
    const interval = setInterval(() => {
      loadTasks(pagination.current, pagination.pageSize, statusFilter);
      loadStats();
    }, 5000);
    return () => clearInterval(interval);
  }, [currentWorkspace?.id, pagination.current, pagination.pageSize, statusFilter]);

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('currentWorkspaceId');
    navigate('/login');
  };

  const handleWorkspaceChange = (workspace: Workspace) => {
    setCurrentWorkspace(workspace);
    localStorage.setItem('currentWorkspaceId', workspace.id.toString());
    setPagination({ ...pagination, current: 1 });
  };

  const handleCancelTask = async (taskId: number) => {
    if (!currentWorkspace?.id) return;
    try {
      await tasksAPI.cancel(currentWorkspace.id, taskId);
      message.success('任务已取消');
      loadTasks(pagination.current, pagination.pageSize, statusFilter);
      loadStats();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '取消任务失败');
    }
  };

  const handleRetryTask = async (taskId: number) => {
    if (!currentWorkspace?.id) return;
    try {
      await tasksAPI.retry(currentWorkspace.id, taskId);
      message.success('任务已重试');
      loadTasks(pagination.current, pagination.pageSize, statusFilter);
      loadStats();
    } catch (error: any) {
      message.error(error.response?.data?.detail || '重试任务失败');
    }
  };

  const handleTableChange = (newPagination: any, filters: any) => {
    const newStatus = filters.status?.[0];
    setStatusFilter(newStatus);
    setPagination({
      current: newPagination.current,
      pageSize: newPagination.pageSize,
      total: pagination.total,
    });
    loadTasks(newPagination.current, newPagination.pageSize, newStatus);
  };

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

  const columns = [
    {
      title: '任务ID',
      dataIndex: 'task_id',
      key: 'task_id',
      width: 200,
      ellipsis: true,
      render: (task_id: string) => (
        <Tooltip title={task_id}>
          <Text code copyable={{ text: task_id }}>
            {task_id.slice(0, 8)}...
          </Text>
        </Tooltip>
      ),
    },
    {
      title: '类型',
      dataIndex: 'task_type',
      key: 'task_type',
      width: 120,
      render: (type: string) => TASK_TYPE_LABELS[type] || type,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      filters: [
        { text: '等待中', value: 'pending' },
        { text: '已分配', value: 'assigned' },
        { text: '执行中', value: 'started' },
        { text: '成功', value: 'success' },
        { text: '失败', value: 'failure' },
        { text: '重试中', value: 'retry' },
        { text: '已取消', value: 'cancelled' },
      ],
      render: (status: TaskStatus) => {
        const config = TASK_STATUS_CONFIG[status];
        return (
          <Tag icon={config.icon} color={config.color}>
            {config.label}
          </Tag>
        );
      },
    },
    {
      title: '进度',
      dataIndex: 'progress',
      key: 'progress',
      width: 160,
      render: (progress: number, record: Task) => {
        const progressStatus =
          record.status === 'started'
            ? 'active'
            : record.status === 'success'
              ? 'success'
              : record.status === 'failure'
                ? 'exception'
                : 'normal';
        return <Progress percent={progress} status={progressStatus} size="small" />;
      },
    },
    {
      title: '文件ID',
      dataIndex: 'file_id',
      key: 'file_id',
      width: 100,
      render: (fileId?: number) => fileId || '-',
    },
    {
      title: '重试',
      dataIndex: 'retry_count',
      key: 'retry_count',
      width: 80,
      render: (count: number, record: Task) => `${count}/${record.max_retries}`,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 150,
      render: (date: string) => formatDateTime(date),
    },
    {
      title: '操作',
      key: 'action',
      width: 150,
      fixed: 'right' as const,
      render: (_: any, record: Task) => {
        const canCancel = record.status === 'pending' || record.status === 'assigned' || record.status === 'started';
        const canRetry = record.status === 'failure';
        return (
          <Space size="small">
            {record.status === 'failure' && (
              <Button
                size="small"
                icon={<InfoCircleOutlined />}
                onClick={() => setDetailModal({ open: true, task: record })}
              >
                详情
              </Button>
            )}
            {canCancel && (
              <Button
                size="small"
                danger
                onClick={() => handleCancelTask(record.id)}
              >
                取消
              </Button>
            )}
            {canRetry && (
              <Button
                size="small"
                type="primary"
                onClick={() => handleRetryTask(record.id)}
              >
                重试
              </Button>
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider style={{ position: 'fixed', height: '100vh', left: 0, top: 0, bottom: 0 }}>
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <div style={{ height: 32, margin: 16, color: 'white', fontSize: 20, fontWeight: 'bold' }}>
            {t('app.title')}
          </div>
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={[location.pathname]}
            items={menuItems}
            onClick={({ key }) => navigate(key)}
            style={{ flex: 1, borderRight: 0, overflowY: 'auto' }}
          />
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
      <Layout style={{ marginLeft: 200 }}>
        <Header
          style={{
            background: '#fff',
            padding: '0 24px',
            display: 'flex',
            justifyContent: 'flex-end',
            alignItems: 'center',
            gap: 16,
          }}
        >
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
        <Content style={{ margin: '24px' }}>
          {/* Statistics Cards */}
          <Row gutter={16} style={{ marginBottom: 24 }}>
            <Col span={8}>
              <Card>
                <Statistic
                  title="总任务数"
                  value={stats.total}
                  valueStyle={{ color: '#1890ff' }}
                />
              </Card>
            </Col>
            <Col span={8}>
              <Card>
                <Statistic
                  title="运行中"
                  value={stats.running}
                  valueStyle={{ color: '#52c41a' }}
                  prefix={<ReloadOutlined spin />}
                />
              </Card>
            </Col>
            <Col span={8}>
              <Card>
                <Statistic
                  title="等待中"
                  value={stats.pending}
                  valueStyle={{ color: '#faad14' }}
                  prefix={<ClockCircleOutlined />}
                />
              </Card>
            </Col>
          </Row>

          {/* Task Table */}
          <Card title="解析任务列表">
            <Table
              columns={columns}
              dataSource={tasks}
              rowKey="id"
              loading={loading}
              pagination={{
                current: pagination.current,
                pageSize: pagination.pageSize,
                total: pagination.total,
                showSizeChanger: true,
                showTotal: (total) => `共 ${total} 条`,
              }}
              onChange={handleTableChange}
              scroll={{ x: 1200 }}
            />
          </Card>

          {/* Task Detail Modal */}
          <Modal
            title={`任务详情 - ${detailModal.task?.task_id?.slice(0, 8) ?? ''}`}
            open={detailModal.open}
            onCancel={() => setDetailModal({ open: false })}
            footer={
              detailModal.task?.status === 'failure' ? (
                <Button type="primary" onClick={() => {
                  setDetailModal({ open: false });
                  handleRetryTask(detailModal.task!.id);
                }}>
                  重试
                </Button>
              ) : null
            }
          >
            {detailModal.task && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <div><strong>任务ID：</strong>{detailModal.task.task_id}</div>
                <div><strong>类型：</strong>{TASK_TYPE_LABELS[detailModal.task.task_type] || detailModal.task.task_type}</div>
                <div><strong>状态：</strong>{detailModal.task.status}</div>
                <div><strong>进度：</strong>{detailModal.task.progress}%</div>
                <div><strong>文件ID：</strong>{detailModal.task.file_id || '-'}</div>
                <div><strong>创建时间：</strong>{formatDateTime(detailModal.task.created_at)}</div>
                <div><strong>更新时间：</strong>{detailModal.task.updated_at ? formatDateTime(detailModal.task.updated_at) : '-'}</div>
                <div><strong>完成时间：</strong>{detailModal.task.completed_at ? formatDateTime(detailModal.task.completed_at) : '-'}</div>
                {detailModal.task.error && (
                  <div>
                    <strong>错误信息：</strong>
                    <div style={{ marginTop: 8, padding: 12, background: '#fff2f0', borderRadius: 6, color: '#ff4d4f', whiteSpace: 'pre-wrap' }}>
                      {detailModal.task.error}
                    </div>
                  </div>
                )}
                {detailModal.task.result && (
                  <div>
                    <strong>结果：</strong>
                    <pre style={{ marginTop: 8, padding: 12, background: '#f5f5f5', borderRadius: 6, overflow: 'auto', maxHeight: 200 }}>
                      {JSON.stringify(detailModal.task.result, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </Modal>
        </Content>
      </Layout>
    </Layout>
  );
}
