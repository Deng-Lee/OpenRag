# 计划 5：API 和前端

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现完整的 RESTful API 和 Web UI，提供用户友好的界面

**Architecture:** FastAPI 后端 + React 前端 + Docker 部署

**Tech Stack:** FastAPI, React 18, TypeScript, Ant Design, Docker Compose, Nginx

**Dependencies:** 
- 计划 1（环境搭建与基础集成）必须完成
- 计划 2（权限管理系统）必须完成
- 计划 3（文档处理系统）必须完成
- 计划 4（检索服务）必须完成

**Priority:** P2（用户界面）

---

## 文件结构

```
src/openrag/
├── api/
│   ├── __init__.py
│   ├── main.py                     # FastAPI 主应用
│   ├── deps.py                     # 依赖注入
│   ├── auth.py                     # 认证中间件
│   ├── files_api.py                # 文件管理 API
│   ├── users_api.py                # 用户管理 API
│   ├── teams_api.py                # 团队管理 API
│   ├── permissions_api.py          # 权限管理 API
│   ├── share_api.py                # 共享管理 API
│   └── search_api.py               # 搜索 API

web/
├── package.json
├── src/
│   ├── App.tsx                     # 主应用
│   ├── pages/
│   │   ├── Login.tsx               # 登录页
│   │   ├── Files.tsx               # 文件管理页
│   │   ├── Search.tsx              # 搜索页
│   │   └── Settings.tsx            # 设置页
│   ├── components/
│   │   ├── FileList.tsx            # 文件列表组件
│   │   ├── FileUpload.tsx          # 文件上传组件
│   │   ├── SearchBar.tsx           # 搜索栏组件
│   │   └── PermissionManager.tsx   # 权限管理组件
│   └── services/
│       └── api.ts                  # API 客户端

docker/
├── docker-compose.prod.yml         # 生产环境配置
├── nginx/
│   └── nginx.conf                  # Nginx 配置
└── Dockerfile.api                  # API Dockerfile
```

---

### Task 1: FastAPI 主应用

**Files:**
- Create: `src/openrag/api/main.py`
- Create: `src/openrag/api/deps.py`
- Create: `src/openrag/api/auth.py`

**主要步骤：**
- 创建 FastAPI 应用
- 配置 CORS
- 实现依赖注入（数据库会话）
- 实现 JWT 认证中间件
- 实现错误处理
- 编写 API 测试

---

### Task 2: 文件管理 API

**Files:**
- Create: `src/openrag/api/files_api.py`
- Create: `tests/test_files_api.py`

**主要步骤：**
- 实现文件上传 API
- 实现文件列表 API
- 实现文件删除 API
- 实现文件移动 API
- 实现目录创建 API
- 权限检查集成
- 编写 API 测试

---

### Task 3: 用户和团队管理 API

**Files:**
- Create: `src/openrag/api/users_api.py`
- Create: `src/openrag/api/teams_api.py`
- Create: `tests/test_users_api.py`

**主要步骤：**
- 实现用户注册/登录 API
- 实现用户信息管理 API
- 实现团队 CRUD API
- 实现团队成员管理 API
- 编写 API 测试

---

### Task 4: 权限和共享 API

**Files:**
- Create: `src/openrag/api/permissions_api.py`
- Create: `src/openrag/api/share_api.py`
- Create: `tests/test_permissions_api.py`

**主要步骤：**
- 实现权限查询 API
- 实现权限授予/撤销 API
- 实现共享链接创建 API
- 实现共享链接访问 API
- 编写 API 测试

---

### Task 5: React 前端应用

**Files:**
- Create: `web/package.json`
- Create: `web/src/App.tsx`
- Create: `web/src/pages/*.tsx`
- Create: `web/src/components/*.tsx`

**主要步骤：**
- 初始化 React 项目
- 实现登录页面
- 实现文件管理页面
- 实现搜索页面
- 实现权限管理页面
- 实现 API 客户端
- 编写前端测试

---

### Task 6: Docker 部署配置

**Files:**
- Create: `docker/docker-compose.prod.yml`
- Create: `docker/Dockerfile.api`
- Create: `docker/nginx/nginx.conf`

**主要步骤：**
- 创建 API Dockerfile
- 创建生产环境 Docker Compose
- 配置 Nginx 反向代理
- 配置静态文件服务
- 测试部署流程

---

### Task 7: 端到端测试

**Files:**
- Create: `tests/test_e2e.py`

**主要步骤：**
- 测试完整用户流程
- 测试文件上传和搜索
- 测试权限控制
- 测试共享链接
- 性能测试

---

## 验收标准

完成此计划后，应该具备：

- ✅ 完整的 RESTful API
- ✅ 用户友好的 Web UI
- ✅ 文件管理功能完整
- ✅ 搜索功能正常
- ✅ 权限管理功能正常
- ✅ Docker 部署配置完整
- ✅ 所有测试通过
- ✅ 系统可以投入使用

## 项目完成

完成此计划后，OpenRag 系统开发完成！

## 后续优化（可选）

- 基于层级结构的 Rerank 优化
- 更多文档格式支持
- 性能优化
- 监控和日志
- 多语言支持
