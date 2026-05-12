# 计划 2：权限管理系统

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现完整的企业级权限管理系统，包括用户/团队管理、ACL 权限控制、共享链接、审计日志

**Architecture:** 基于 MySQL 的权限数据存储，使用 SQLAlchemy ORM，实现分层权限检查（文件级 > 目录级 > 团队级）

**Tech Stack:** SQLAlchemy, Alembic, MySQL, Pydantic, bcrypt, python-jose

**Dependencies:** 计划 1（环境搭建与基础集成）必须完成

**Priority:** P1（核心功能，必须在文档处理之前完成）

---

## 文件结构

```
src/openrag/
├── models/
│   ├── __init__.py
│   ├── base.py                     # SQLAlchemy Base
│   ├── user.py                     # 用户模型
│   ├── team.py                     # 团队模型
│   ├── file.py                     # 文件元数据模型
│   ├── permission.py               # 权限模型
│   └── share.py                    # 共享链接模型
├── services/
│   ├── __init__.py
│   ├── user_manager.py             # 用户管理服务
│   ├── team_manager.py             # 团队管理服务
│   ├── permission_manager.py       # 权限管理服务
│   ├── share_manager.py            # 共享管理服务
│   └── audit_logger.py             # 审计日志服务
├── database.py                     # 数据库连接
└── security.py                     # 安全工具（密码哈希、JWT）

tests/
├── test_models.py                  # 模型测试
├── test_user_manager.py            # 用户管理测试
├── test_permission_manager.py      # 权限管理测试
└── test_share_manager.py           # 共享管理测试
```

---

**注意：** 由于计划内容较长，完整的任务步骤请参考计划 1 的格式。此计划包含以下主要任务：

### Task 1: 数据库模型设计
- 创建 User, Team, TeamMember 模型
- 创建 File, FilePermission 模型
- 创建 ShareLink 模型
- 编写模型测试

### Task 2: 安全工具
- 实现密码哈希（bcrypt）
- 实现 JWT 令牌生成和验证
- 编写安全测试

### Task 3: 用户管理服务
- 实现用户 CRUD 操作
- 实现用户认证
- 编写用户管理测试

### Task 4: 团队管理服务
- 实现团队 CRUD 操作
- 实现团队成员管理
- 编写团队管理测试

### Task 5: 权限管理服务
- 实现 ACL 权限检查
- 实现分层权限继承
- 编写权限管理测试

### Task 6: 共享链接服务
- 实现共享链接生成
- 实现访问控制（密码、过期、次数限制）
- 编写共享管理测试

### Task 7: 审计日志服务
- 实现操作日志记录
- 实现日志查询
- 编写审计日志测试

---

## 验收标准

完成此计划后，应该具备：

- ✅ 完整的用户和团队管理功能
- ✅ 企业级 ACL 权限控制
- ✅ 共享链接功能（密码、过期、访问控制）
- ✅ 审计日志记录
- ✅ 所有测试通过（覆盖率 > 80%）
- ✅ 可以开始文档处理系统开发

## 下一步

完成此计划后，继续执行：
- **计划 3：文档处理系统**
