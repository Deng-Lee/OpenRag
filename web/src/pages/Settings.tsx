import { Layout, Menu, Button, Card, Descriptions } from 'antd';
import { SettingOutlined, LogoutOutlined, UserOutlined } from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { authAPI } from '../services/api';
import type { User } from '../types';
import { buildAppMenuItems } from '../utils/app-menu';

const { Header, Content, Sider } = Layout;

export default function Settings() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    const loadUser = async () => {
      try {
        const data = await authAPI.getCurrentUser();
        setUser(data);
      } catch (error) {
        console.error('Failed to load user:', error);
      }
    };
    loadUser();
  }, []);

  const handleLogout = () => {
    localStorage.removeItem('token');
    navigate('/login');
  };

  const menuItems = buildAppMenuItems(t, !!user?.is_admin);

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
            Logout
          </Button>
        </Header>
        <Content style={{ margin: '24px' }}>
          <Card title={<><UserOutlined /> User Profile</>}>
            {user && (
              <Descriptions column={1}>
                <Descriptions.Item label="Email">{user.email}</Descriptions.Item>
                <Descriptions.Item label="Username">{user.username}</Descriptions.Item>
                <Descriptions.Item label="Role">{user.role}</Descriptions.Item>
                <Descriptions.Item label="User ID">{user.id}</Descriptions.Item>
                {user.team_id && (
                  <Descriptions.Item label="Team ID">{user.team_id}</Descriptions.Item>
                )}
              </Descriptions>
            )}
          </Card>
        </Content>
      </Layout>
    </Layout>
  );
}
