import {
  Layout,
  Menu,
  Button,
  Card,
  Select,
  Table,
  Space,
  Modal,
  Form,
  Input,
  Tag,
  message,
  Popconfirm,
} from 'antd';
import {
  SettingOutlined,
  LogoutOutlined,
} from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { workspacesAPI, authAPI, serviceTokensAPI } from '../services/api';
import type { Workspace, User, ServiceTokenListItem, WorkspaceBindingRequest } from '../types';
import { buildAppMenuItems } from '../utils/app-menu';

const { Header, Content, Sider } = Layout;

function formatApiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d))
    return d.map((x: { msg?: string }) => x.msg || JSON.stringify(x)).join('; ');
  return 'Request failed';
}

export default function ServiceTokens() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [tokens, setTokens] = useState<ServiceTokenListItem[]>([]);
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const [bindOpen, setBindOpen] = useState(false);
  const [bindRow, setBindRow] = useState<ServiceTokenListItem | null>(null);
  const [bindForm] = Form.useForm();

  const loadCurrentUser = useCallback(async () => {
    try {
      const user = await authAPI.getCurrentUser();
      setCurrentUser(user);
    } catch {
      console.error('Failed to load current user');
    }
  }, []);

  const loadWorkspaces = useCallback(async () => {
    try {
      const data = await workspacesAPI.list();
      setWorkspaces(data);
    } catch {
      message.error('Failed to load workspaces');
    }
  }, []);

  const loadTokens = useCallback(async () => {
    setLoading(true);
    try {
      const data = await serviceTokensAPI.list();
      setTokens(data);
    } catch (err) {
      message.error(formatApiError(err) || t('serviceTokens.load_failed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    loadWorkspaces();
    loadCurrentUser();
    loadTokens();
  }, [loadWorkspaces, loadCurrentUser, loadTokens]);

  const canCreate = currentUser?.is_admin === true || workspaces.some((w) => w.user_role === 'write');
  const canManageRow = (row: ServiceTokenListItem) =>
    currentUser?.is_admin === true || row.created_by_user_id === currentUser?.id;

  const handleLogout = () => {
    localStorage.removeItem('token');
    navigate('/login');
  };

  const openCreate = () => {
    createForm.resetFields();
    createForm.setFieldsValue({ workspaces: [{ permission: 'write' }] });
    setCreateOpen(true);
  };

  const submitCreate = async () => {
    try {
      const v = await createForm.validateFields();
      const wsBindings: WorkspaceBindingRequest[] = (v.workspaces || [])
        .filter((b: { workspace_id?: number; permission?: string }) => b.workspace_id != null);
      if (wsBindings.length === 0) {
        message.error(t('serviceTokens.at_least_one_binding'));
        return;
      }
      const created = await serviceTokensAPI.create({
        name: v.name?.trim() || undefined,
        workspaces: wsBindings,
      });
      setCreateOpen(false);
      Modal.success({
        title: t('serviceTokens.created_title'),
        width: 560,
        content: (
          <div>
            <p style={{ marginBottom: 8 }}>{t('serviceTokens.created_hint')}</p>
            <Input.TextArea readOnly rows={3} value={created.secret} />
            <Button
              type="primary"
              style={{ marginTop: 12 }}
              onClick={() => {
                void navigator.clipboard.writeText(created.secret);
                message.success(t('serviceTokens.copied'));
              }}
            >
              {t('serviceTokens.copy_secret')}
            </Button>
          </div>
        ),
      });
      void loadTokens();
    } catch (err) {
      if ((err as { errorFields?: unknown }).errorFields) return;
      message.error(formatApiError(err));
    }
  };

  const showSecret = async (tokenId: number) => {
    try {
      const { secret } = await serviceTokensAPI.getSecret(tokenId);
      Modal.info({
        title: t('serviceTokens.secret_title'),
        width: 520,
        content: (
          <div>
            <Input.TextArea readOnly rows={3} value={secret} />
            <Button
              type="link"
              onClick={() => {
                void navigator.clipboard.writeText(secret);
                message.success(t('serviceTokens.copied'));
              }}
            >
              {t('serviceTokens.copy_secret')}
            </Button>
          </div>
        ),
      });
    } catch (err) {
      message.error(formatApiError(err));
    }
  };

  const revoke = async (tokenId: number) => {
    try {
      await serviceTokensAPI.revoke(tokenId);
      message.success(t('serviceTokens.revoked_ok'));
      void loadTokens();
    } catch (err) {
      message.error(formatApiError(err));
    }
  };

  const openManageBindings = (row: ServiceTokenListItem) => {
    setBindRow(row);
    bindForm.resetFields();
    setBindOpen(true);
  };

  const submitAddBinding = async () => {
    if (!bindRow) return;
    try {
      const v = await bindForm.validateFields();
      await serviceTokensAPI.patchBindings(bindRow.id, {
        add: [{ workspace_id: v.workspace_id, permission: v.permission }],
      });
      message.success(t('serviceTokens.binding_added'));
      setBindOpen(false);
      setBindRow(null);
      void loadTokens();
    } catch (err) {
      if ((err as { errorFields?: unknown }).errorFields) return;
      message.error(formatApiError(err));
    }
  };

  const updateBindingPermission = async (tokenId: number, workspaceId: number, permission: 'read' | 'write') => {
    try {
      await serviceTokensAPI.patchBindings(tokenId, {
        update: [{ workspace_id: workspaceId, permission }],
      });
      message.success(t('serviceTokens.permission_updated'));
      void loadTokens();
    } catch (err) {
      message.error(formatApiError(err));
    }
  };

  const removeBinding = async (tokenId: number, workspaceId: number) => {
    try {
      await serviceTokensAPI.patchBindings(tokenId, {
        remove: [{ workspace_id: workspaceId }],
      });
      message.success(t('serviceTokens.binding_removed'));
      void loadTokens();
    } catch (err) {
      message.error(formatApiError(err));
    }
  };

  const menuItems = buildAppMenuItems(t, !!currentUser?.is_admin);

  const columns = [
    { title: 'ID', dataIndex: 'id', key: 'id', width: 72 },
    {
      title: t('serviceTokens.col_name'),
      dataIndex: 'name',
      key: 'name',
      render: (n: string | null) => n || '—',
    },
    {
      title: t('serviceTokens.col_preview'),
      dataIndex: 'secret_preview',
      key: 'secret_preview',
    },
    {
      title: t('serviceTokens.col_workspaces'),
      key: 'workspaces',
      render: (_: unknown, row: ServiceTokenListItem) => (
        <Space wrap>
          {row.workspaces.length === 0 && <span style={{ color: '#999' }}>{t('serviceTokens.no_bindings')}</span>}
          {row.workspaces.map((w) => (
            <Tag key={w.workspace_id} color={w.permission === 'write' ? 'blue' : 'default'}>
              {w.workspace_name}: {w.permission}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: t('serviceTokens.col_revoked'),
      dataIndex: 'revoked_at',
      key: 'revoked_at',
      render: (v: string | null) =>
        v ? <Tag color="red">{new Date(v).toLocaleString()}</Tag> : '—',
    },
    {
      title: t('serviceTokens.col_actions'),
      key: 'actions',
      render: (_: unknown, row: ServiceTokenListItem) => {
        const revoked = !!row.revoked_at;
        const manage = canManageRow(row);
        return (
          <Space wrap>
            {manage && !revoked && (
              <Button type="link" size="small" onClick={() => void showSecret(row.id)}>
                {t('serviceTokens.show_secret')}
              </Button>
            )}
            {manage && !revoked && (
              <Button type="link" size="small" onClick={() => openManageBindings(row)}>
                {t('serviceTokens.manage_bindings')}
              </Button>
            )}
            {manage && !revoked && (
              <Popconfirm
                title={t('serviceTokens.revoke_confirm')}
                onConfirm={() => void revoke(row.id)}
                okText={t('serviceTokens.yes')}
                cancelText={t('serviceTokens.no')}
              >
                <Button type="link" size="small" danger>
                  {t('serviceTokens.revoke')}
                </Button>
              </Popconfirm>
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
          <div
            style={{
              height: 32,
              margin: 16,
              color: 'white',
              fontSize: 20,
              fontWeight: 'bold',
            }}
          >
            OpenRag
          </div>
          <Menu
            theme="dark"
            mode="inline"
            selectedKeys={[location.pathname]}
            items={menuItems}
            onClick={({ key }) => navigate(key)}
            style={{ flex: 1, borderRight: 0, overflowY: 'auto' }}
          />
          <div
            style={{
              padding: '16px',
              borderTop: '1px solid rgba(255, 255, 255, 0.1)',
              marginTop: 'auto',
            }}
          >
            <Button
              type="text"
              icon={<SettingOutlined />}
              onClick={() => navigate('/settings')}
              style={{
                width: '100%',
                color: 'rgba(255, 255, 255, 0.65)',
                textAlign: 'left',
              }}
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
          }}
        >
          <Button icon={<LogoutOutlined />} onClick={handleLogout}>
            {t('app.logout')}
          </Button>
        </Header>
        <Content style={{ margin: '24px' }}>
          <Card
            title={t('serviceTokens.title')}
            extra={
              canCreate ? (
                <Button type="primary" onClick={openCreate}>
                  {t('serviceTokens.create')}
                </Button>
              ) : null
            }
          >
            <p style={{ color: '#666', margin: 0 }}>{t('serviceTokens.hint')}</p>
            <Table
              columns={columns}
              dataSource={tokens}
              rowKey="id"
              loading={loading}
              pagination={false}
              style={{ marginTop: 16 }}
            />
          </Card>
        </Content>
      </Layout>

      <Modal
        title={t('serviceTokens.create_modal_title')}
        open={createOpen}
        onOk={() => void submitCreate()}
        onCancel={() => setCreateOpen(false)}
        destroyOnClose
        width={640}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item name="name" label={t('serviceTokens.field_name')}>
            <Input placeholder={t('serviceTokens.name_placeholder')} />
          </Form.Item>
          <Form.List name="workspaces" initialValue={[{ permission: 'write' }]}>
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name, ...restField }) => (
                  <Space key={key} align="baseline" style={{ display: 'flex', marginBottom: 8 }}>
                    <Form.Item
                      {...restField}
                      name={[name, 'workspace_id']}
                      rules={[{ required: true, message: t('serviceTokens.select_workspace') }]}
                    >
                      <Select
                        style={{ width: 200 }}
                        options={workspaces.map((w) => ({ label: w.name, value: w.id }))}
                        placeholder={t('serviceTokens.select_workspace')}
                      />
                    </Form.Item>
                    <Form.Item
                      {...restField}
                      name={[name, 'permission']}
                      rules={[{ required: true }]}
                    >
                      <Select
                        style={{ width: 120 }}
                        options={[
                          { value: 'read', label: t('serviceTokens.perm_read') },
                          { value: 'write', label: t('serviceTokens.perm_write') },
                        ]}
                      />
                    </Form.Item>
                    {fields.length > 1 && (
                      <Button onClick={() => remove(name)} danger size="small">
                        ×
                      </Button>
                    )}
                  </Space>
                ))}
                <Button type="dashed" onClick={() => add()} block>
                  + {t('serviceTokens.add_workspace')}
                </Button>
              </>
            )}
          </Form.List>
        </Form>
      </Modal>

      <Modal
        title={t('serviceTokens.binding_modal_title')}
        open={bindOpen}
        onCancel={() => {
          setBindOpen(false);
          setBindRow(null);
        }}
        footer={null}
        destroyOnClose
        width={640}
      >
        {bindRow && (
          <div>
            <h4 style={{ marginBottom: 12 }}>{t('serviceTokens.col_workspaces')}</h4>
            {bindRow.workspaces.length === 0 && (
              <p style={{ color: '#999' }}>{t('serviceTokens.no_bindings')}</p>
            )}
            {bindRow.workspaces.map((w) => (
              <div key={w.workspace_id} style={{ display: 'flex', alignItems: 'center', marginBottom: 8, gap: 8 }}>
                <Tag color={w.permission === 'write' ? 'blue' : 'default'}>
                  {w.workspace_name}: {w.permission}
                </Tag>
                <Select
                  size="small"
                  style={{ width: 100 }}
                  defaultValue={w.permission}
                  options={[
                    { value: 'read', label: t('serviceTokens.perm_read') },
                    { value: 'write', label: t('serviceTokens.perm_write') },
                  ]}
                  onChange={(val) => void updateBindingPermission(bindRow.id, w.workspace_id, val)}
                />
                <Popconfirm
                  title={t('serviceTokens.remove_binding') + '?'}
                  onConfirm={() => void removeBinding(bindRow.id, w.workspace_id)}
                  okText={t('serviceTokens.yes')}
                  cancelText={t('serviceTokens.no')}
                >
                  <Button size="small" danger>{t('serviceTokens.remove_binding')}</Button>
                </Popconfirm>
              </div>
            ))}

            <div style={{ borderTop: '1px solid #f0f0f0', paddingTop: 16, marginTop: 16 }}>
              <h4 style={{ marginBottom: 12 }}>{t('serviceTokens.add_binding')}</h4>
              <Form form={bindForm} layout="inline">
                <Form.Item
                  name="workspace_id"
                  rules={[{ required: true, message: t('serviceTokens.select_workspace') }]}
                >
                  <Select
                    style={{ width: 200 }}
                    options={workspaces
                      .filter((w) => !bindRow.workspaces.some((b) => b.workspace_id === w.id))
                      .map((w) => ({ label: w.name, value: w.id }))}
                    placeholder={t('serviceTokens.select_workspace')}
                  />
                </Form.Item>
                <Form.Item
                  name="permission"
                  rules={[{ required: true }]}
                  initialValue="read"
                >
                  <Select
                    style={{ width: 120 }}
                    options={[
                      { value: 'read', label: t('serviceTokens.perm_read') },
                      { value: 'write', label: t('serviceTokens.perm_write') },
                    ]}
                  />
                </Form.Item>
                <Form.Item>
                  <Button type="primary" onClick={() => void submitAddBinding()}>
                    {t('serviceTokens.add_binding')}
                  </Button>
                </Form.Item>
              </Form>
            </div>
          </div>
        )}
      </Modal>
    </Layout>
  );
}