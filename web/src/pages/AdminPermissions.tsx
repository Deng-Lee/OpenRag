import { Layout, Menu, Button, Card, Table, Tag, Typography, Space, Tabs, message, Modal, Form, Input, Switch, Select, Popconfirm, Radio } from 'antd';
import { LogoutOutlined, PlusOutlined, DeleteOutlined, SettingOutlined } from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { authAPI, rolesAPI, usersAdminAPI, workspacesAPI, permissionsAPI } from '../services/api';
import type { User, Role, Workspace, RoleWorkspacePermission, UserPermissionDetails } from '../types';
import { buildAppMenuItems } from '../utils/app-menu';

const { Header, Content, Sider } = Layout;
const { Title } = Typography;

export default function AdminPermissions() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  
  const [roles, setRoles] = useState<Role[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [currentUser, setCurrentUser] = useState<User | null>(null);
  
  const [loadingRoles, setLoadingRoles] = useState(false);
  const [loadingUsers, setLoadingUsers] = useState(false);
  
  const [isRoleModalVisible, setIsRoleModalVisible] = useState(false);
  const [isRolePermModalVisible, setIsRolePermModalVisible] = useState(false);
  const [isUserRoleModalVisible, setIsUserRoleModalVisible] = useState(false);
  const [isWorkspaceAuthModalVisible, setIsWorkspaceAuthModalVisible] = useState(false);
  
  const [roleForm] = Form.useForm();
  const [rolePermForm] = Form.useForm();
  const [userRoleForm] = Form.useForm();
  const [workspaceAuthForm] = Form.useForm();
  
  const [selectedRole, setSelectedRole] = useState<Role | null>(null);
  const [selectedUser, setSelectedUser] = useState<User | null>(null);
  
  const [rolePermissions, setRolePermissions] = useState<RoleWorkspacePermission[]>([]);
  const [userPermissions, setUserPermissions] = useState<UserPermissionDetails | null>(null);

  useEffect(() => {
    initPage();
  }, []);

  const initPage = async () => {
    try {
      const user = await authAPI.getCurrentUser();
      setCurrentUser(user);
      if (!user.is_admin) {
        message.error(t('adminPermissions.access_denied'));
        navigate('/');
        return;
      }
      
      fetchRoles();
      fetchUsers();
      fetchWorkspaces();
    } catch (error) {
      navigate('/login');
    }
  };

  const fetchRoles = async () => {
    setLoadingRoles(true);
    try {
      const data = await rolesAPI.list();
      setRoles(data);
    } catch (e) {
      message.error(t('adminPermissions.load_roles_failed'));
    } finally {
      setLoadingRoles(false);
    }
  };

  const fetchUsers = async () => {
    setLoadingUsers(true);
    try {
      const data = await usersAdminAPI.listUsers();
      setUsers(data);
    } catch (e) {
      message.error(t('adminPermissions.load_users_failed'));
    } finally {
      setLoadingUsers(false);
    }
  };

  const fetchWorkspaces = async () => {
    try {
      const data = await workspacesAPI.list();
      setWorkspaces(data);
    } catch (e) {
      // ignore
    }
  };

  const fetchRolePermissions = async (roleId: number) => {
    try {
      const perms = await rolesAPI.getPermissions(roleId);
      setRolePermissions(perms);
    } catch (e) {
      message.error(t('adminPermissions.load_role_permissions_failed'));
    }
  };

  const fetchUserPermissions = async (userId: number) => {
    try {
      const perms = await permissionsAPI.getPermissionDetails(userId);
      setUserPermissions(perms);
    } catch (e) {
      message.error(t('adminPermissions.load_user_permissions_failed'));
    }
  };

  // UI Handlers - Roles
  const handleCreateRole = async (values: any) => {
    try {
      if (selectedRole) {
        await rolesAPI.update(selectedRole.id, values);
        message.success(t('adminPermissions.role_updated'));
      } else {
        await rolesAPI.create(values);
        message.success(t('adminPermissions.role_created'));
      }
      setIsRoleModalVisible(false);
      fetchRoles();
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('adminPermissions.save_role_failed'));
    }
  };

  const handleDeleteRole = async (roleId: number) => {
    try {
      await rolesAPI.delete(roleId);
      message.success(t('adminPermissions.role_deleted'));
      fetchRoles();
    } catch (e) {
      message.error(t('adminPermissions.delete_role_failed'));
    }
  };

  const handleAddRolePermission = async (values: any) => {
    if (!selectedRole) return;
    try {
      await rolesAPI.setPermission(selectedRole.id, values.workspace_id, values.permission);
      message.success(t('adminPermissions.permission_added'));
      setIsRolePermModalVisible(false);
      fetchRolePermissions(selectedRole.id);
    } catch (e) {
      message.error(t('adminPermissions.add_permission_failed'));
    }
  };

  const handleRemoveRolePermission = async (workspaceId: number) => {
    if (!selectedRole) return;
    try {
      await rolesAPI.removePermission(selectedRole.id, workspaceId);
      message.success(t('adminPermissions.permission_removed'));
      fetchRolePermissions(selectedRole.id);
    } catch (e) {
      message.error(t('adminPermissions.remove_permission_failed'));
    }
  };

  // UI Handlers - Users
  const handleAssignUserRole = async (values: any) => {
    const targetUserId = selectedUser?.id || values.user_id;
    if (!targetUserId) return;
    try {
      await usersAdminAPI.assignRole(targetUserId, values.role_id);
      message.success(t('adminPermissions.role_assigned'));
      setIsUserRoleModalVisible(false);
      if (selectedUser && selectedUser.id === targetUserId) {
        fetchUserPermissions(selectedUser.id);
      }
    } catch (e) {
      message.error(t('adminPermissions.assign_role_failed'));
    }
  };

  const handleRemoveUserRole = async (roleId: number) => {
    if (!selectedUser) return;
    try {
      await usersAdminAPI.removeRole(selectedUser.id, roleId);
      message.success(t('adminPermissions.role_removed'));
      fetchUserPermissions(selectedUser.id);
    } catch (e) {
      message.error(t('adminPermissions.remove_role_failed'));
    }
  };

  const handleAuthorizeWorkspace = async (values: any) => {
    if (!selectedUser) return;
    try {
      await workspacesAPI.addMember(values.workspace_id, selectedUser.id, values.role);
      message.success(t('adminPermissions.workspace_authorized') || 'Workspace authorized successfully');
      setIsWorkspaceAuthModalVisible(false);
      fetchUserPermissions(selectedUser.id);
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('adminPermissions.authorize_workspace_failed') || 'Failed to authorize workspace');
    }
  };

  const handleRemoveWorkspaceAuth = async (workspaceId: number) => {
    if (!selectedUser) return;
    try {
      await workspacesAPI.removeMember(workspaceId, selectedUser.id);
      message.success(t('adminPermissions.workspace_auth_removed'));
      fetchUserPermissions(selectedUser.id);
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('adminPermissions.remove_workspace_auth_failed'));
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('token');
    navigate('/login');
  };

  const menuItems = buildAppMenuItems(t, !!currentUser?.is_admin);

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider style={{ position: 'fixed', height: '100vh', left: 0, top: 0, bottom: 0 }}>
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <div style={{ height: 32, margin: 16, color: 'white', fontSize: 20, fontWeight: 'bold' }}>OpenRag</div>
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
        <Header style={{ background: '#fff', padding: '0 24px', display: 'flex', justifyContent: 'flex-end' }}>
          <Button icon={<LogoutOutlined />} onClick={handleLogout}>{t('app.logout')}</Button>
        </Header>
        <Content style={{ margin: '24px' }}>
          <Card title={<Title level={4} style={{ margin: 0 }}>{t('adminPermissions.title')}</Title>}>
            <Tabs defaultActiveKey="1" items={[
              {
                key: '1',
                label: t('adminPermissions.role_management'),
                children: (
                  <div style={{ display: 'flex', gap: 24 }}>
                    <div style={{ flex: 1 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
                        <Title level={5}>{t('adminPermissions.roles')}</Title>
                        <Button type="primary" icon={<PlusOutlined />} onClick={() => {
                          setSelectedRole(null);
                          roleForm.resetFields();
                          setIsRoleModalVisible(true);
                        }}>{t('adminPermissions.create_role')}</Button>
                      </div>
                      <Table 
                        dataSource={roles} 
                        rowKey="id" 
                        loading={loadingRoles}
                        onRow={(record) => ({
                          onClick: () => {
                            setSelectedRole(record);
                            fetchRolePermissions(record.id);
                          },
                          style: { cursor: 'pointer', background: selectedRole?.id === record.id ? '#e6f7ff' : '' }
                        })}
                        columns={[
                          { title: t('adminPermissions.name'), dataIndex: 'name', key: 'name' },
                          { title: t('adminPermissions.code'), dataIndex: 'role_code', key: 'role_code' },
                          { title: t('adminPermissions.status'), dataIndex: 'is_active', render: (val) => <Tag color={val ? 'green' : 'red'}>{val ? t('adminPermissions.active') : t('adminPermissions.inactive')}</Tag> },
                          { title: t('adminPermissions.actions'), render: (_, record) => (
                            <Space onClick={e => e.stopPropagation()}>
                              <Button type="link" onClick={() => {
                                setSelectedRole(record);
                                roleForm.setFieldsValue(record);
                                setIsRoleModalVisible(true);
                              }}>{t('adminPermissions.edit')}</Button>
                              <Popconfirm title={t('adminPermissions.delete_confirm')} onConfirm={() => handleDeleteRole(record.id)}>
                                <Button type="link" danger>{t('adminPermissions.delete')}</Button>
                              </Popconfirm>
                            </Space>
                          )}
                        ]}
                      />
                    </div>
                    {selectedRole && (
                      <div style={{ flex: 1 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
                          <Title level={5}>{t('adminPermissions.permissions_for', { role: selectedRole.name })}</Title>
                          <Button type="dashed" icon={<PlusOutlined />} onClick={() => {
                            rolePermForm.resetFields();
                            setIsRolePermModalVisible(true);
                          }}>{t('adminPermissions.add_permission')}</Button>
                        </div>
                        <Table 
                          dataSource={rolePermissions}
                          rowKey="workspace_id"
                          columns={[
                            { title: t('adminPermissions.workspace_id'), dataIndex: 'workspace_id' },
                            { title: t('adminPermissions.permission'), dataIndex: 'permission', render: p => <Tag color={p === 'write' ? 'volcano' : 'default'}>{p}</Tag> },
                            { title: t('adminPermissions.actions'), render: (_, r) => (
                              <Popconfirm title={t('adminPermissions.remove_confirm')} onConfirm={() => handleRemoveRolePermission(r.workspace_id)}>
                                <Button type="link" danger icon={<DeleteOutlined />} />
                              </Popconfirm>
                            )}
                          ]}
                        />
                      </div>
                    )}
                  </div>
                )
              },
              {
                key: '2',
                label: t('adminPermissions.user_authorization'),
                children: (
                  <div style={{ display: 'flex', gap: 24 }}>
                    <div style={{ flex: 1 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
                        <Title level={5}>{t('adminPermissions.users')}</Title>
                        <Button type="primary" icon={<PlusOutlined />} onClick={() => {
                          setSelectedUser(null);
                          userRoleForm.resetFields();
                          setIsUserRoleModalVisible(true);
                        }}>{t('adminPermissions.authorize_user')}</Button>
                      </div>
                      <Table 
                        dataSource={users}
                        rowKey="id"
                        loading={loadingUsers}
                        onRow={(record) => ({
                          onClick: () => {
                            setSelectedUser(record);
                            fetchUserPermissions(record.id);
                          },
                          style: { cursor: 'pointer', background: selectedUser?.id === record.id ? '#e6f7ff' : '' }
                        })}
                        columns={[
                          { title: t('adminPermissions.name'), dataIndex: 'full_name' },
                          { title: t('adminPermissions.email'), dataIndex: 'email' },
                          { title: t('adminPermissions.admin'), dataIndex: 'is_admin', render: val => <Tag color={val ? 'purple' : 'default'}>{val ? t('adminPermissions.admin_label') : t('adminPermissions.user_label')}</Tag> }
                        ]}
                      />
                    </div>
                    {selectedUser && (
                      <div style={{ flex: 1 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
                           <Title level={5}>{t('adminPermissions.user_roles', { user: selectedUser.full_name })}</Title>
                           <Space>
                             <Button type="dashed" icon={<PlusOutlined />} onClick={() => {
                               userRoleForm.resetFields();
                               setIsUserRoleModalVisible(true);
                             }}>{t('adminPermissions.assign_role')}</Button>
                             <Button type="dashed" onClick={() => {
                               workspaceAuthForm.resetFields();
                               setIsWorkspaceAuthModalVisible(true);
                             }}>{t('adminPermissions.authorize_workspace') || 'Authorize Workspace'}</Button>
                           </Space>
                        </div>
                        <Table 
                          dataSource={userPermissions?.roles || []}
                          rowKey="id"
                          pagination={false}
                          columns={[
                            { title: t('adminPermissions.role'), dataIndex: 'name' },
                            { title: t('adminPermissions.code'), dataIndex: 'role_code' },
                            { title: t('adminPermissions.actions'), render: (_, r) => (
                              <Popconfirm title={t('adminPermissions.remove_role_confirm')} onConfirm={() => handleRemoveUserRole(r.id)}>
                                <Button type="link" danger icon={<DeleteOutlined />} />
                              </Popconfirm>
                            )}
                          ]}
                        />
                        <Title level={5} style={{ marginTop: 24 }}>{t('adminPermissions.effective_workspace_permissions')}</Title>
                        <Table 
                          dataSource={userPermissions?.workspace_permissions || []}
                          rowKey={(r: any, i) => `${r.workspace_id}-${r.source}-${i}`}
                          pagination={false}
                          columns={[
                            { title: t('adminPermissions.workspace'), dataIndex: 'workspace_name' },
                            { title: t('adminPermissions.permission'), dataIndex: 'permission', render: p => <Tag color={p === 'write' ? 'volcano' : 'default'}>{p}</Tag> },
                            { title: t('adminPermissions.source'), dataIndex: 'source', render: s => <Tag color={s === 'direct' ? 'cyan' : 'purple'}>{s === 'direct' ? t('permissions.source_direct') : s}</Tag> },
                            { title: t('adminPermissions.actions'), render: (_, r) => (
                              r.source === 'direct' ? (
                                <Popconfirm title={t('adminPermissions.remove_workspace_auth_confirm')} onConfirm={() => handleRemoveWorkspaceAuth(r.workspace_id)}>
                                  <Button type="link" danger icon={<DeleteOutlined />} />
                                </Popconfirm>
                              ) : null
                            )}
                          ]}
                        />
                      </div>
                    )}
                  </div>
                )
              }
            ]} />
          </Card>
        </Content>

        <Modal title={selectedRole ? t('adminPermissions.edit_role') : t('adminPermissions.create_role')} open={isRoleModalVisible} onCancel={() => setIsRoleModalVisible(false)} onOk={() => roleForm.submit()}>
          <Form form={roleForm} onFinish={handleCreateRole} layout="vertical">
            <Form.Item name="name" label={t('adminPermissions.role_name')} rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="role_code" label={t('adminPermissions.role_code_hint')} rules={[{ required: true }]}><Input disabled={!!selectedRole} /></Form.Item>
            <Form.Item name="description" label={t('adminPermissions.description')}><Input.TextArea /></Form.Item>
            <Form.Item name="is_active" label={t('adminPermissions.active')} valuePropName="checked" initialValue={true}><Switch /></Form.Item>
          </Form>
        </Modal>

        <Modal title={t('adminPermissions.add_role_permission')} open={isRolePermModalVisible} onCancel={() => setIsRolePermModalVisible(false)} onOk={() => rolePermForm.submit()}>
          <Form form={rolePermForm} onFinish={handleAddRolePermission} layout="vertical">
            <Form.Item name="workspace_id" label={t('adminPermissions.workspace')} rules={[{ required: true }]}>
              <Select options={workspaces.map(w => ({ label: w.name, value: w.id }))} />
            </Form.Item>
            <Form.Item name="permission" label={t('adminPermissions.permission')} rules={[{ required: true }]}>
              <Select options={[{ label: t('adminPermissions.read_only'), value: 'read' }, { label: t('adminPermissions.read_write'), value: 'write' }]} />
            </Form.Item>
          </Form>
        </Modal>

         <Modal title={t('adminPermissions.assign_role_to_user')} open={isUserRoleModalVisible} onCancel={() => setIsUserRoleModalVisible(false)} onOk={() => userRoleForm.submit()}>
           <Form form={userRoleForm} onFinish={handleAssignUserRole} layout="vertical">
             {!selectedUser && (
               <Form.Item name="user_id" label={t('adminPermissions.user')} rules={[{ required: true }]}>
                 <Select
                   showSearch
                   optionFilterProp="label"
                   options={users.map(u => ({ label: `${u.full_name || ''} (${u.email})`, value: u.id }))}
                 />
               </Form.Item>
             )}
             <Form.Item name="role_id" label={t('adminPermissions.role')} rules={[{ required: true }]}>
               <Select options={roles.filter(r => r.is_active).map(r => ({ label: r.name, value: r.id }))} />
             </Form.Item>
           </Form>
         </Modal>

         <Modal 
           title={t('adminPermissions.authorize_workspace') || 'Authorize Workspace'} 
           open={isWorkspaceAuthModalVisible} 
           onCancel={() => setIsWorkspaceAuthModalVisible(false)} 
           onOk={() => workspaceAuthForm.submit()}
         >
           <Form form={workspaceAuthForm} onFinish={handleAuthorizeWorkspace} layout="vertical">
             <Form.Item 
               name="workspace_id" 
               label={t('adminPermissions.workspace') || 'Workspace'} 
               rules={[{ required: true, message: t('adminPermissions.select_workspace') || 'Please select a workspace' }]}
             >
               <Select 
                 options={workspaces.map(w => ({ label: w.name, value: w.id }))} 
                 placeholder={t('adminPermissions.select_workspace') || 'Select a workspace'}
               />
             </Form.Item>
             <Form.Item 
               name="role" 
               label={t('adminPermissions.permission') || 'Permission'} 
               rules={[{ required: true, message: t('adminPermissions.select_permission') || 'Please select a permission' }]}
             >
               <Radio.Group>
                 <Space direction="vertical">
                   <Radio value="read">{t('adminPermissions.read_only') || 'Read Only'}</Radio>
                   <Radio value="write">{t('adminPermissions.read_write') || 'Read & Write'}</Radio>
                 </Space>
               </Radio.Group>
             </Form.Item>
           </Form>
         </Modal>

      </Layout>
    </Layout>
  );
}