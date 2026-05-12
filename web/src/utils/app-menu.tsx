import type { MenuProps } from 'antd';
import {
  FileOutlined,
  FileSearchOutlined,
  SafetyOutlined,
  UnorderedListOutlined,
  KeyOutlined,
} from '@ant-design/icons';
import type { TFunction } from 'i18next';

export function buildAppMenuItems(t: TFunction, isAdmin: boolean): MenuProps['items'] {
  return [
    {
      key: '/files',
      icon: <FileOutlined />,
      label: t('app.menu.files'),
    },
    {
      key: '/search',
      icon: <FileSearchOutlined />,
      label: t('app.menu.search'),
    },
    {
      key: '/tasks',
      icon: <UnorderedListOutlined />,
      label: t('app.menu.tasks'),
    },
    {
      key: '/permissions',
      icon: <SafetyOutlined />,
      label: t('app.menu.permissions'),
    },
    ...(isAdmin
      ? [
          {
            key: '/service-tokens',
            icon: <KeyOutlined />,
            label: t('app.menu.serviceTokens'),
          },
          {
            key: '/admin-permissions',
            icon: <SafetyOutlined />,
            label: t('app.menu.adminAuth'),
          },
        ]
      : []),
  ];
}
