import { Layout, Menu, Button, Card, Table, Tag, Typography, Space } from 'antd';
import { SettingOutlined, LogoutOutlined } from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { authAPI, permissionsAPI } from '../services/api';
import type { UserPermissionDetails, WorkspacePermissionDetail } from '../types';
import { buildAppMenuItems } from '../utils/app-menu';

const { Header, Content, Sider } = Layout;
const { Title } = Typography;

export default function Permissions() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [permissionDetails, setPermissionDetails] = useState<UserPermissionDetails | null>(null);
  const [currentUser, setCurrentUser] = useState<{ is_admin: boolean } | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    loadCurrentUserAndPermissions();
  }, []);

  const loadCurrentUserAndPermissions = async () => {
    setLoading(true);
    try {
      const user = await authAPI.getCurrentUser();
      setCurrentUser(user);
      
      const details = await permissionsAPI.getPermissionDetails(user.id);
      setPermissionDetails(details);
    } catch (error) {
      console.error('Failed to load user permissions', error);
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('token');
    navigate('/login');
  };

  const menuItems = buildAppMenuItems(t, !!currentUser?.is_admin);

  const roleColumns = [
    {
      title: t('permissions.role_name'),
      dataIndex: 'name',
      key: 'name',
    },
    {
      title: t('permissions.role_code'),
      dataIndex: 'role_code',
      key: 'role_code',
      render: (text: string) => <Tag color="blue">{text}</Tag>,
    },
    {
      title: t('permissions.status'),
      dataIndex: 'is_active',
      key: 'is_active',
      render: (active: boolean) => (
        <Tag color={active ? 'success' : 'error'}>
          {active ? t('permissions.active') : t('permissions.inactive')}
        </Tag>
      ),
    },
  ];

  const workspacePermissionColumns = [
    {
      title: t('permissions.workspace'),
      dataIndex: 'workspace_name',
      key: 'workspace_name',
      render: (text: string, record: WorkspacePermissionDetail) => (
        <Space>
          <span>{text}</span>
          <span style={{ color: '#888', fontSize: '12px' }}>
            {t('permissions.workspace_id', { id: record.workspace_id })}
          </span>
        </Space>
      ),
    },
    {
      title: t('permissions.permission'),
      dataIndex: 'permission',
      key: 'permission',
      render: (perm: string) => (
        <Tag color={perm === 'write' ? 'volcano' : 'default'}>
          {perm === 'write' ? t('permissions.read_write') : t('permissions.read_only')}
        </Tag>
      ),
    },
    {
      title: t('permissions.source'),
      key: 'source',
      render: (_: any, record: WorkspacePermissionDetail) => {
        if (record.source === 'direct') {
          return <Tag color="cyan">{t('permissions.source_direct')}</Tag>;
        }
        return (
          <Tag color="purple">
            {t('permissions.source_role', { role: record.source_details?.role_name })}
          </Tag>
        );
      },
    },
  ];

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider style={{ position: 'fixed', height: '100vh', left: 0, top: 0, bottom: 0 }}>
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <div style={{ height: 32, margin: 16, color: 'white', fontSize: 20, fontWeight: 'bold' }}>
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
        <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', justifyContent: 'flex-end' }}>
          <Button icon={<LogoutOutlined />} onClick={handleLogout}>
            {t('app.logout')}
          </Button>
        </Header>
        <Content style={{ margin: '24px' }}>
          <Card title={<Title level={4} style={{ margin: 0 }}>{t('permissions.title')}</Title>} loading={loading}>
            <div style={{ marginBottom: 24 }}>
              <Title level={5}>{t('permissions.my_roles')}</Title>
              <Table 
                dataSource={permissionDetails?.roles || []} 
                columns={roleColumns} 
                rowKey="id"
                pagination={false}
              />
            </div>
            
            <div>
              <Title level={5}>{t('permissions.workspace_access')}</Title>
              <Table 
                dataSource={permissionDetails?.workspace_permissions || []} 
                columns={workspacePermissionColumns} 
                rowKey={(record, index) => `${record.workspace_id}-${record.source}-${index}`}
                pagination={false}
              />
            </div>
          </Card>
        </Content>
      </Layout>
    </Layout>
  );
}