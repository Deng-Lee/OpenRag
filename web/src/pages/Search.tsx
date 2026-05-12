import {
  Layout,
  Menu,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Switch,
  Select,
  Radio,
  Space,
  Typography,
  Table,
  Tag,
  Dropdown,
  Collapse,
  message,
  Empty,
} from 'antd';
import {
  SettingOutlined,
  LogoutOutlined,
  SearchOutlined,
  DownOutlined,
  TeamOutlined,
} from '@ant-design/icons';
import { useNavigate, useLocation } from 'react-router-dom';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { searchAPI, workspacesAPI, authAPI, filesAPI } from '../services/api';
import type { File, SearchResponse, SearchResult, Workspace } from '../types';
import ChunkSourcePreviewModal from '../components/ChunkSourcePreviewModal';
import { buildAppMenuItems } from '../utils/app-menu';

const { Header, Content, Sider } = Layout;
const { Text, Paragraph } = Typography;

type SearchMode = 'semantic' | 'hierarchical';

export default function SearchPage() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [form] = Form.useForm();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [currentWorkspace, setCurrentWorkspace] = useState<Workspace | null>(null);
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [chunkPreviewOpen, setChunkPreviewOpen] = useState(false);
  const [chunkPreviewFile, setChunkPreviewFile] = useState<File | null>(null);
  const [chunkPreviewRow, setChunkPreviewRow] = useState<SearchResult | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);

  const openChunkSourcePreview = async (row: SearchResult) => {
    if (!row.file_id) return;
    try {
      const f = await filesAPI.get(row.file_id);
      setChunkPreviewFile(f);
      setChunkPreviewRow(row);
      setChunkPreviewOpen(true);
    } catch {
      message.error(t('searchPage.preview_open_failed'));
    }
  };

  const toggleLanguage = (checked: boolean) => {
    i18n.changeLanguage(checked ? 'zh' : 'en');
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
    } catch {
      message.error(t('searchPage.load_workspace_failed'));
    }
  };

  useEffect(() => {
    loadWorkspaces();
    authAPI
      .getCurrentUser()
      .then((user) => setIsAdmin(user.is_admin === true))
      .catch(console.error);
  }, []);

  const handleWorkspaceChange = (workspace: Workspace) => {
    setCurrentWorkspace(workspace);
    localStorage.setItem('currentWorkspaceId', workspace.id.toString());
  };

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('currentWorkspaceId');
    navigate('/login');
  };

  const workspaceMenuItems = workspaces.map((workspace) => ({
    key: workspace.id.toString(),
    label: workspace.name,
    onClick: () => handleWorkspaceChange(workspace),
  }));

  const menuItems = buildAppMenuItems(t, isAdmin);

  const onFinish = async (values: Record<string, unknown>) => {
    if (!currentWorkspace?.id) {
      message.warning(t('searchPage.empty_workspace'));
      return;
    }
    const mode = values.search_mode as SearchMode;
    const query = String(values.query || '').trim();
    if (!query) {
      message.warning(t('searchPage.query_required'));
      return;
    }

    setLoading(true);
    setResponse(null);
    try {
      const payload = {
        query,
        top_k: Number(values.top_k) || 10,
        workspace_id: currentWorkspace.id,
        use_rerank: Boolean(values.use_rerank),
        use_contextual_retrieval: Boolean(values.use_contextual_retrieval),
        use_l1_llm_navigation: Boolean(values.use_l1_llm_navigation),
        retrieval_strategy: String(values.retrieval_strategy || 'auto'),
        contextual_l0_top_n: Number(values.contextual_l0_top_n) || 40,
        contextual_l1_top_n: Number(values.contextual_l1_top_n) || 30,
        contextual_chunk_fetch_multiplier: Number(values.contextual_chunk_fetch_multiplier) || 4,
      };
      const res =
        mode === 'hierarchical'
          ? await searchAPI.hierarchical(payload)
          : await searchAPI.search(payload);
      setResponse(res);
      if (!res.results?.length) {
        message.info(t('searchPage.none'));
      }
    } catch (err: unknown) {
      const detail =
        err && typeof err === 'object' && 'response' in err
          ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
          : undefined;
      message.error(detail || t('searchPage.search_failed'));
    } finally {
      setLoading(false);
    }
  };

  const strategyOptions = ['auto', 'light', 'deep', 'precise', 'flat'].map((v) => ({
    value: v,
    label: v,
  }));

  const columns = [
    {
      title: t('searchPage.col_score'),
      dataIndex: 'score',
      key: 'score',
      width: 88,
      render: (s: number) => s?.toFixed?.(4) ?? s,
    },
    {
      title: t('searchPage.col_file'),
      key: 'file',
      width: 160,
      ellipsis: true,
      render: (_: unknown, row: { filename?: string; file_id?: number }) => (
        <Text ellipsis title={row.filename}>
          {row.filename || `#${row.file_id}`}
        </Text>
      ),
    },
    {
      title: t('searchPage.col_chunk'),
      key: 'chunk',
      width: 140,
      render: (_: unknown, row: { chunk_index?: number; chunk_id?: string }) => (
        <Space direction="vertical" size={0}>
          {row.chunk_index != null && <Text type="secondary">idx {row.chunk_index}</Text>}
          {row.chunk_id && (
            <Text copyable={{ text: row.chunk_id }} code style={{ fontSize: 12 }}>
              {row.chunk_id.slice(0, 8)}…
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: t('searchPage.col_position'),
      key: 'position',
      width: 200,
      render: (
        _: unknown,
        row: {
          page?: number;
          level?: number;
          block_type?: string;
          start_offset?: number;
          end_offset?: number;
        }
      ) => (
        <Space direction="vertical" size={0}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t('searchPage.pos_page')}: {row.page ?? 0} · {t('searchPage.pos_level')}: {row.level ?? 0}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {row.block_type ?? 'text'} · [{row.start_offset ?? 0}, {row.end_offset ?? 0})
          </Text>
        </Space>
      ),
    },
    {
      title: t('searchPage.col_strategy'),
      key: 'tags',
      width: 160,
      render: (_: unknown, row: { retrieval_strategy?: string; l1_llm_filtered?: boolean }) => (
        <Space wrap>
          {row.retrieval_strategy && <Tag color="blue">{row.retrieval_strategy}</Tag>}
          {row.l1_llm_filtered && <Tag color="purple">L1 LLM</Tag>}
        </Space>
      ),
    },
    {
      title: t('searchPage.col_text'),
      dataIndex: 'text',
      key: 'text',
      ellipsis: true,
      render: (text: string, row: SearchResult) => (
        <Paragraph
          ellipsis={{ rows: 2, tooltip: text }}
          style={{ marginBottom: 0, cursor: 'pointer', color: '#1677ff' }}
          onClick={() => openChunkSourcePreview(row)}
        >
          {text || ''}
        </Paragraph>
      ),
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
          <Card title={t('searchPage.title')}>
            <Form
              form={form}
              layout="vertical"
              onFinish={onFinish}
              initialValues={{
                top_k: 10,
                search_mode: 'semantic' as SearchMode,
                use_rerank: true,
                use_contextual_retrieval: false,
                use_l1_llm_navigation: false,
                retrieval_strategy: 'auto',
                contextual_l0_top_n: 40,
                contextual_l1_top_n: 30,
                contextual_chunk_fetch_multiplier: 4,
              }}
            >
              <Form.Item
                name="query"
                label={t('searchPage.query')}
                rules={[{ required: true, message: t('searchPage.query_required') }]}
              >
                <Input.TextArea rows={3} placeholder={t('searchPage.query_placeholder')} />
              </Form.Item>
              <Space wrap size="large" style={{ marginBottom: 16 }}>
                <Form.Item name="top_k" label={t('searchPage.top_k')} style={{ marginBottom: 0 }}>
                  <InputNumber min={1} max={100} />
                </Form.Item>
                <Form.Item name="search_mode" label={t('searchPage.mode')} style={{ marginBottom: 0 }}>
                  <Radio.Group optionType="button" buttonStyle="solid">
                    <Radio.Button value="semantic">{t('searchPage.mode_semantic')}</Radio.Button>
                    <Radio.Button value="hierarchical">{t('searchPage.mode_hierarchical')}</Radio.Button>
                  </Radio.Group>
                </Form.Item>
                <Form.Item name="retrieval_strategy" label={t('searchPage.strategy')} style={{ marginBottom: 0 }}>
                  <Select options={strategyOptions} style={{ width: 140 }} />
                </Form.Item>
              </Space>
              <Space wrap size="large" style={{ marginBottom: 16 }}>
                <Form.Item
                  name="use_contextual_retrieval"
                  label={t('searchPage.contextual')}
                  valuePropName="checked"
                  style={{ marginBottom: 0 }}
                >
                  <Switch />
                </Form.Item>
                <Form.Item
                  name="use_rerank"
                  label={t('searchPage.rerank')}
                  valuePropName="checked"
                  style={{ marginBottom: 0 }}
                >
                  <Switch />
                </Form.Item>
                <Form.Item
                  name="use_l1_llm_navigation"
                  label={t('searchPage.l1_llm')}
                  valuePropName="checked"
                  style={{ marginBottom: 0 }}
                >
                  <Switch />
                </Form.Item>
              </Space>
              <Collapse
                items={[
                  {
                    key: 'adv',
                    label: t('searchPage.advanced'),
                    children: (
                      <Space wrap size="large">
                        <Form.Item
                          name="contextual_l0_top_n"
                          label={t('searchPage.l0_top')}
                          style={{ marginBottom: 0 }}
                        >
                          <InputNumber min={5} max={200} />
                        </Form.Item>
                        <Form.Item
                          name="contextual_l1_top_n"
                          label={t('searchPage.l1_top')}
                          style={{ marginBottom: 0 }}
                        >
                          <InputNumber min={5} max={200} />
                        </Form.Item>
                        <Form.Item
                          name="contextual_chunk_fetch_multiplier"
                          label={t('searchPage.chunk_mult')}
                          style={{ marginBottom: 0 }}
                        >
                          <InputNumber min={1} max={20} />
                        </Form.Item>
                      </Space>
                    ),
                  },
                ]}
              />
              <Form.Item style={{ marginTop: 16, marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" icon={<SearchOutlined />} loading={loading}>
                  {t('searchPage.run')}
                </Button>
              </Form.Item>
            </Form>
          </Card>

          {response && (
            <Card
              style={{ marginTop: 24 }}
              title={
                <Space>
                  <span>{t('searchPage.results')}</span>
                  <Tag>
                    {t('searchPage.query_time')}: {response.query_time_ms?.toFixed?.(1) ?? response.query_time_ms}
                    {t('searchPage.ms')}
                  </Tag>
                  <Tag>{response.total ?? response.results?.length ?? 0}</Tag>
                  {response.l1_llm_applied === true && <Tag color="purple">L1 LLM ✓</Tag>}
                  {response.l1_llm_applied === false && response.l1_llm_skip_reason && (
                    <Tag color="warning">
                      L1 LLM ✗ {response.l1_llm_skip_reason === 'no_api_key' ? '(no API key)' :
                        response.l1_llm_skip_reason === 'not_contextual' ? '(not contextual mode)' :
                        response.l1_llm_skip_reason === 'no_openai_package' ? '(openai package missing)' :
                        response.l1_llm_skip_reason === 'no_l1_text' ? '(no L1 data)' :
                        response.l1_llm_skip_reason?.startsWith('llm_call_failed') ? '(LLM call failed)' :
                        `(${response.l1_llm_skip_reason})`}
                    </Tag>
                  )}
                </Space>
              }
            >
              {response.results?.length ? (
                <Table
                  rowKey={(row, i) => `${row.chunk_id ?? row.file_id}-${i}`}
                  columns={columns}
                  dataSource={response.results}
                  pagination={{ pageSize: 10 }}
                  scroll={{ x: 900 }}
                />
              ) : (
                <Empty description={t('searchPage.none')} />
              )}
            </Card>
          )}
        </Content>
      </Layout>

      <ChunkSourcePreviewModal
        open={chunkPreviewOpen}
        file={chunkPreviewFile}
        chunk={chunkPreviewRow}
        onClose={() => {
          setChunkPreviewOpen(false);
          setChunkPreviewFile(null);
          setChunkPreviewRow(null);
        }}
      />
    </Layout>
  );
}
