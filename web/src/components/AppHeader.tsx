import { Button, Dropdown, Space, Switch, Typography } from 'antd';
import { LogoutOutlined, DownOutlined, TeamOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { Workspace } from '../types';

const { Text } = Typography;

interface AppHeaderProps {
  workspaces: Workspace[];
  currentWorkspace: Workspace | null;
  onWorkspaceChange: (workspace: Workspace) => void;
  onCreateWorkspace?: () => void;
}

export default function AppHeader({
  workspaces,
  currentWorkspace,
  onWorkspaceChange,
  onCreateWorkspace,
}: AppHeaderProps) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();

  const toggleLanguage = (checked: boolean) => {
    i18n.changeLanguage(checked ? 'zh' : 'en');
  };

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('currentWorkspaceId');
    navigate('/login');
  };

  const workspaceMenuItems = [
    ...workspaces.map((workspace) => ({
      key: workspace.id.toString(),
      label: workspace.name,
      onClick: () => onWorkspaceChange(workspace),
    })),
    ...(onCreateWorkspace
      ? [
          { type: 'divider' as const },
          {
            key: 'create',
            label: (
              <Space>
                <span>+</span>
                {t('files.workspace.create')}
              </Space>
            ),
            onClick: onCreateWorkspace,
          },
        ]
      : []),
  ];

  return (
    <div
      style={{
        background: '#fff',
        padding: '0 24px',
        display: 'flex',
        justifyContent: 'flex-end',
        alignItems: 'center',
        gap: 16,
        height: '100%',
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
    </div>
  );
}
