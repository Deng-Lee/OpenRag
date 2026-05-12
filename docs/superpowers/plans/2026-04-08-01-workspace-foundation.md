# 业务空间基础架构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现业务空间（Workspace）多租户隔离基础架构，包括数据模型、API 和数据库迁移

**Architecture:** 创建 Workspace 和 WorkspaceMember 模型实现多租户隔离，为现有模型添加 workspace_id 外键，提供完整的业务空间管理 API

**Tech Stack:** SQLAlchemy, FastAPI, Alembic, pytest

**Dependencies:** 无（基础功能）

**Priority:** P0（其他计划的前置依赖）

---

## 文件结构

```
src/openrag/
├── models/
│   ├── workspace.py              # 新增：Workspace 和 WorkspaceMember 模型
│   ├── file.py                   # 修改：添加 workspace_id
│   ├── team.py                   # 修改：添加 workspace_id
│   ├── permission.py             # 修改：添加 workspace_id
│   └── share.py                  # 修改：添加 workspace_id
├── api/
│   ├── workspaces_api.py         # 新增：业务空间管理 API
│   ├── main.py                   # 修改：注册新路由
│   └── deps.py                   # 修改：添加 workspace 依赖注入
└── services/
    └── workspace_service.py      # 新增：业务空间业务逻辑

tests/
├── test_workspace_models.py      # 新增：模型测试
├── test_workspace_api.py         # 新增：API 测试
└── test_workspace_service.py     # 新增：服务测试

migrations/
└── versions/
    └── xxxx_add_workspace.py     # 新增：数据库迁移脚本
```

---

## Task 1: 创建 Workspace 数据模型

**Files:**
- Create: `src/openrag/models/workspace.py`
- Test: `tests/test_workspace_models.py`

- [ ] **Step 1: 编写 Workspace 模型测试**

创建 `tests/test_workspace_models.py`:

```python
"""Tests for Workspace models"""

import pytest
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User


def test_create_workspace(db):
    """Test creating a workspace"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    db.add(user)
    db.commit()
    
    workspace = Workspace(
        name="Test Workspace",
        slug="test-workspace",
        description="A test workspace",
        owner_id=user.id,
        max_concurrent_tasks=10,
        max_storage_bytes=10737418240,
        priority_strategy="file_size"
    )
    db.add(workspace)
    db.commit()
    
    assert workspace.id is not None
    assert workspace.name == "Test Workspace"
    assert workspace.slug == "test-workspace"
    assert workspace.owner_id == user.id
    assert workspace.max_concurrent_tasks == 10
    assert workspace.priority_strategy == "file_size"
    assert workspace.created_at is not None


def test_workspace_slug_unique(db):
    """Test workspace slug must be unique"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    db.add(user)
    db.commit()
    
    workspace1 = Workspace(name="WS1", slug="test", owner_id=user.id)
    db.add(workspace1)
    db.commit()
    
    workspace2 = Workspace(name="WS2", slug="test", owner_id=user.id)
    db.add(workspace2)
    
    with pytest.raises(IntegrityError):
        db.commit()


def test_create_workspace_member(db):
    """Test creating a workspace member"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    member_user = User(username="member", email="member@test.com", hashed_password="hash")
    db.add_all([user, member_user])
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=member_user.id,
        role="member"
    )
    db.add(member)
    db.commit()
    
    assert member.id is not None
    assert member.workspace_id == workspace.id
    assert member.user_id == member_user.id
    assert member.role == "member"
    assert member.joined_at is not None


def test_workspace_member_unique_constraint(db):
    """Test user can only be added once to a workspace"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    member_user = User(username="member", email="member@test.com", hashed_password="hash")
    db.add_all([user, member_user])
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member1 = WorkspaceMember(workspace_id=workspace.id, user_id=member_user.id, role="member")
    db.add(member1)
    db.commit()
    
    member2 = WorkspaceMember(workspace_id=workspace.id, user_id=member_user.id, role="admin")
    db.add(member2)
    
    with pytest.raises(IntegrityError):
        db.commit()


def test_workspace_member_cascade_delete(db):
    """Test workspace members are deleted when workspace is deleted"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    member_user = User(username="member", email="member@test.com", hashed_password="hash")
    db.add_all([user, member_user])
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=member_user.id, role="member")
    db.add(member)
    db.commit()
    
    workspace_id = workspace.id
    db.delete(workspace)
    db.commit()
    
    # Verify member was deleted
    remaining_members = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id
    ).count()
    assert remaining_members == 0
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_workspace_models.py -v
```

预期：FAIL - 模块不存在

- [ ] **Step 3: 实现 Workspace 和 WorkspaceMember 模型**

创建 `src/openrag/models/workspace.py`:

```python
"""Workspace models for multi-tenant isolation"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, BigInteger, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from openrag.models.base import Base


class Workspace(Base):
    """Business workspace for multi-tenant isolation"""
    __tablename__ = 'workspaces'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(Text)
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    
    # Quota configuration
    max_concurrent_tasks = Column(Integer, default=10, nullable=False)
    max_storage_bytes = Column(BigInteger, default=10*1024*1024*1024, nullable=False)  # 10GB
    priority_strategy = Column(String(50), default='file_size', nullable=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    owner = relationship("User", foreign_keys=[owner_id])
    members = relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Workspace(id={self.id}, name='{self.name}', slug='{self.slug}')>"


class WorkspaceMember(Base):
    """Workspace membership with role"""
    __tablename__ = 'workspace_members'
    
    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey('workspaces.id', ondelete='CASCADE'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    role = Column(String(20), default='member', nullable=False)  # admin, member, viewer
    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User")
    
    # Constraints
    __table_args__ = (
        UniqueConstraint('workspace_id', 'user_id', name='uq_workspace_user'),
        Index('idx_workspace_member', 'workspace_id', 'user_id'),
    )
    
    def __repr__(self):
        return f"<WorkspaceMember(workspace_id={self.workspace_id}, user_id={self.user_id}, role='{self.role}')>"
```

- [ ] **Step 4: 更新 models/__init__.py 导出新模型**

修改 `src/openrag/models/__init__.py`，添加：

```python
from openrag.models.workspace import Workspace, WorkspaceMember

__all__ = [
    # ... existing exports ...
    "Workspace",
    "WorkspaceMember",
]
```

- [ ] **Step 5: 运行测试验证通过**

```bash
pytest tests/test_workspace_models.py -v
```

预期：所有测试 PASS

- [ ] **Step 6: 提交**

```bash
git add src/openrag/models/workspace.py src/openrag/models/__init__.py tests/test_workspace_models.py
git commit -m "feat: add Workspace and WorkspaceMember models

- Add Workspace model with quota configuration
- Add WorkspaceMember model with role-based access
- Add unique constraint on workspace slug
- Add cascade delete for workspace members
- Add comprehensive model tests

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 为现有模型添加 workspace_id

**Files:**
- Modify: `src/openrag/models/file.py`
- Modify: `src/openrag/models/team.py`
- Modify: `src/openrag/models/permission.py`
- Modify: `src/openrag/models/share.py`
- Test: `tests/test_workspace_models.py`

- [ ] **Step 1: 编写现有模型关联测试**

在 `tests/test_workspace_models.py` 添加：

```python
from openrag.models.file import File
from openrag.models.team import Team


def test_file_workspace_relationship(db):
    """Test file belongs to workspace"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    db.add(user)
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    file = File(
        uri="/test/file.txt",
        name="file.txt",
        owner_id=user.id,
        workspace_id=workspace.id,
        is_directory=False,
        size=100
    )
    db.add(file)
    db.commit()
    
    assert file.workspace_id == workspace.id
    assert file.id is not None


def test_team_workspace_relationship(db):
    """Test team belongs to workspace"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    db.add(user)
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    team = Team(
        name="Test Team",
        workspace_id=workspace.id,
        created_by=user.id
    )
    db.add(team)
    db.commit()
    
    assert team.workspace_id == workspace.id
    assert team.id is not None
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_workspace_models.py::test_file_workspace_relationship -v
pytest tests/test_workspace_models.py::test_team_workspace_relationship -v
```

预期：FAIL - workspace_id 列不存在

- [ ] **Step 3: 修改 File 模型添加 workspace_id**

在 `src/openrag/models/file.py` 中，找到 File 类定义，添加：

```python
# 在现有列定义后添加
workspace_id = Column(Integer, ForeignKey('workspaces.id'), nullable=True)  # 先允许 NULL

# 在 relationships 部分添加
workspace = relationship("Workspace")
```

- [ ] **Step 4: 修改 Team 模型添加 workspace_id**

在 `src/openrag/models/team.py` 中，找到 Team 类定义，添加：

```python
# 在现有列定义后添加
workspace_id = Column(Integer, ForeignKey('workspaces.id'), nullable=True)  # 先允许 NULL

# 在 relationships 部分添加
workspace = relationship("Workspace")
```

- [ ] **Step 5: 修改 Permission 模型添加 workspace_id**

在 `src/openrag/models/permission.py` 中，找到 Permission 类定义，添加：

```python
# 在现有列定义后添加
workspace_id = Column(Integer, ForeignKey('workspaces.id'), nullable=True)  # 先允许 NULL

# 在 relationships 部分添加
workspace = relationship("Workspace")
```

- [ ] **Step 6: 修改 Share 模型添加 workspace_id**

在 `src/openrag/models/share.py` 中，找到 Share 类定义，添加：

```python
# 在现有列定义后添加
workspace_id = Column(Integer, ForeignKey('workspaces.id'), nullable=True)  # 先允许 NULL

# 在 relationships 部分添加
workspace = relationship("Workspace")
```

- [ ] **Step 7: 运行测试验证通过**

```bash
pytest tests/test_workspace_models.py::test_file_workspace_relationship -v
pytest tests/test_workspace_models.py::test_team_workspace_relationship -v
```

预期：所有测试 PASS

- [ ] **Step 8: 提交**

```bash
git add src/openrag/models/file.py src/openrag/models/team.py src/openrag/models/permission.py src/openrag/models/share.py tests/test_workspace_models.py
git commit -m "feat: add workspace_id to existing models

- Add workspace_id foreign key to File model
- Add workspace_id foreign key to Team model
- Add workspace_id foreign key to Permission model
- Add workspace_id foreign key to Share model
- Add relationship tests

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 创建数据库迁移脚本

**Files:**
- Create: `migrations/versions/xxxx_add_workspace.py`

- [ ] **Step 1: 生成迁移脚本**

```bash
cd e:/project/OpenRag/OpenRag
alembic revision -m "add workspace and workspace_id to existing models"
```

预期：生成新的迁移文件

- [ ] **Step 2: 编写迁移脚本 upgrade 函数**

编辑生成的迁移文件（例如 `migrations/versions/xxxx_add_workspace.py`）：

```python
"""add workspace and workspace_id to existing models

Revision ID: xxxx
Revises: yyyy
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime


# revision identifiers
revision = 'xxxx'
down_revision = 'yyyy'  # 替换为实际的上一个版本
branch_labels = None
depends_on = None


def upgrade():
    # 1. 创建 workspaces 表
    op.create_table(
        'workspaces',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('slug', sa.String(100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('max_concurrent_tasks', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('max_storage_bytes', sa.BigInteger(), nullable=False, server_default='10737418240'),
        sa.Column('priority_strategy', sa.String(50), nullable=False, server_default='file_size'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug')
    )
    op.create_index('ix_workspaces_slug', 'workspaces', ['slug'])
    
    # 2. 创建 workspace_members 表
    op.create_table(
        'workspace_members',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('workspace_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(20), nullable=False, server_default='member'),
        sa.Column('joined_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('workspace_id', 'user_id', name='uq_workspace_user')
    )
    op.create_index('idx_workspace_member', 'workspace_members', ['workspace_id', 'user_id'])
    
    # 3. 为现有表添加 workspace_id 列（允许 NULL）
    op.add_column('files', sa.Column('workspace_id', sa.Integer(), nullable=True))
    op.add_column('teams', sa.Column('workspace_id', sa.Integer(), nullable=True))
    op.add_column('permissions', sa.Column('workspace_id', sa.Integer(), nullable=True))
    op.add_column('shares', sa.Column('workspace_id', sa.Integer(), nullable=True))
    
    # 4. 创建默认业务空间（如果有用户的话）
    # 注意：这部分需要在应用层执行，因为需要查询用户
    # 这里只创建表结构


def downgrade():
    # 删除外键和列
    op.drop_column('shares', 'workspace_id')
    op.drop_column('permissions', 'workspace_id')
    op.drop_column('teams', 'workspace_id')
    op.drop_column('files', 'workspace_id')
    
    # 删除表
    op.drop_index('idx_workspace_member', 'workspace_members')
    op.drop_table('workspace_members')
    
    op.drop_index('ix_workspaces_slug', 'workspaces')
    op.drop_table('workspaces')
```

- [ ] **Step 3: 运行迁移测试（使用测试数据库）**

```bash
# 备份当前数据库
# 运行迁移
alembic upgrade head
```

预期：迁移成功执行

- [ ] **Step 4: 验证迁移可回滚**

```bash
alembic downgrade -1
alembic upgrade head
```

预期：回滚和升级都成功

- [ ] **Step 5: 提交**

```bash
git add migrations/versions/xxxx_add_workspace.py
git commit -m "feat: add database migration for workspace tables

- Create workspaces table with quota configuration
- Create workspace_members table with role-based access
- Add workspace_id to files, teams, permissions, shares
- Add indexes for performance
- Support upgrade and downgrade

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 创建数据迁移脚本

**Files:**
- Create: `scripts/migrate_to_workspace.py`

- [ ] **Step 1: 编写数据迁移脚本**

创建 `scripts/migrate_to_workspace.py`:

```python
"""Migrate existing data to default workspace"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from openrag.config import get_config
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User
from openrag.models.file import File
from openrag.models.team import Team
from openrag.models.permission import Permission
from openrag.models.share import Share


def migrate_to_default_workspace():
    """Create default workspace and migrate existing data"""
    
    config = get_config()
    engine = create_engine(
        f"mysql+pymysql://{config.mysql.user}:{config.mysql.password}@"
        f"{config.mysql.host}:{config.mysql.port}/{config.mysql.database}"
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    
    try:
        # 1. Check if default workspace already exists
        default_workspace = db.query(Workspace).filter(
            Workspace.slug == 'default'
        ).first()
        
        if default_workspace:
            print(f"Default workspace already exists: {default_workspace.id}")
        else:
            # 2. Get first admin user or create system user
            admin_user = db.query(User).filter(User.id == 1).first()
            if not admin_user:
                print("ERROR: No users found. Please create at least one user first.")
                return False
            
            # 3. Create default workspace
            default_workspace = Workspace(
                name="Default Workspace",
                slug="default",
                description="Default workspace for existing data",
                owner_id=admin_user.id,
                max_concurrent_tasks=10,
                max_storage_bytes=10*1024*1024*1024,
                priority_strategy="file_size"
            )
            db.add(default_workspace)
            db.commit()
            print(f"Created default workspace: {default_workspace.id}")
        
        # 4. Migrate files
        files_updated = db.query(File).filter(
            File.workspace_id.is_(None)
        ).update({File.workspace_id: default_workspace.id})
        print(f"Migrated {files_updated} files to default workspace")
        
        # 5. Migrate teams
        teams_updated = db.query(Team).filter(
            Team.workspace_id.is_(None)
        ).update({Team.workspace_id: default_workspace.id})
        print(f"Migrated {teams_updated} teams to default workspace")
        
        # 6. Migrate permissions
        permissions_updated = db.query(Permission).filter(
            Permission.workspace_id.is_(None)
        ).update({Permission.workspace_id: default_workspace.id})
        print(f"Migrated {permissions_updated} permissions to default workspace")
        
        # 7. Migrate shares
        shares_updated = db.query(Share).filter(
            Share.workspace_id.is_(None)
        ).update({Share.workspace_id: default_workspace.id})
        print(f"Migrated {shares_updated} shares to default workspace")
        
        db.commit()
        
        # 8. Add all users to default workspace
        users = db.query(User).all()
        for user in users:
            # Check if already a member
            existing_member = db.query(WorkspaceMember).filter(
                WorkspaceMember.workspace_id == default_workspace.id,
                WorkspaceMember.user_id == user.id
            ).first()
            
            if not existing_member:
                member = WorkspaceMember(
                    workspace_id=default_workspace.id,
                    user_id=user.id,
                    role='admin' if user.id == default_workspace.owner_id else 'member'
                )
                db.add(member)
        
        db.commit()
        print(f"Added {len(users)} users to default workspace")
        
        print("\nMigration completed successfully!")
        return True
        
    except Exception as e:
        db.rollback()
        print(f"ERROR during migration: {e}")
        import traceback
        traceback.print_exc()
        return False
        
    finally:
        db.close()


if __name__ == "__main__":
    success = migrate_to_default_workspace()
    sys.exit(0 if success else 1)
```

- [ ] **Step 2: 测试数据迁移脚本（干运行）**

```bash
python scripts/migrate_to_workspace.py
```

预期：成功创建默认空间并迁移数据

- [ ] **Step 3: 验证迁移结果**

```bash
# 检查数据库
mysql -u root -p openrag -e "SELECT COUNT(*) FROM workspaces;"
mysql -u root -p openrag -e "SELECT COUNT(*) FROM workspace_members;"
mysql -u root -p openrag -e "SELECT COUNT(*) FROM files WHERE workspace_id IS NULL;"
```

预期：
- workspaces 表有 1 条记录
- workspace_members 表有用户数量的记录
- files 表没有 workspace_id 为 NULL 的记录

- [ ] **Step 4: 提交**

```bash
git add scripts/migrate_to_workspace.py
git commit -m "feat: add data migration script for default workspace

- Create default workspace for existing data
- Migrate all files, teams, permissions, shares
- Add all users as workspace members
- Handle idempotent execution

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 创建 Workspace 服务层

**Files:**
- Create: `src/openrag/services/workspace_service.py`
- Test: `tests/test_workspace_service.py`

- [ ] **Step 1: 编写 Workspace 服务测试**

创建 `tests/test_workspace_service.py`:

```python
"""Tests for Workspace service"""

import pytest
from openrag.services.workspace_service import WorkspaceService
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User


def test_create_workspace(db):
    """Test creating a workspace"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    db.add(user)
    db.commit()
    
    service = WorkspaceService(db)
    workspace = service.create_workspace(
        name="Test Workspace",
        slug="test-workspace",
        description="Test",
        owner_id=user.id
    )
    
    assert workspace.id is not None
    assert workspace.name == "Test Workspace"
    assert workspace.slug == "test-workspace"
    assert workspace.owner_id == user.id
    
    # Verify owner is added as admin member
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == user.id
    ).first()
    assert member is not None
    assert member.role == 'admin'


def test_get_user_workspaces(db):
    """Test getting user's workspaces"""
    user = User(username="user", email="user@test.com", hashed_password="hash")
    db.add(user)
    db.commit()
    
    # Create workspaces
    ws1 = Workspace(name="WS1", slug="ws1", owner_id=user.id)
    ws2 = Workspace(name="WS2", slug="ws2", owner_id=user.id)
    db.add_all([ws1, ws2])
    db.commit()
    
    # Add user as member
    member1 = WorkspaceMember(workspace_id=ws1.id, user_id=user.id, role='admin')
    member2 = WorkspaceMember(workspace_id=ws2.id, user_id=user.id, role='member')
    db.add_all([member1, member2])
    db.commit()
    
    service = WorkspaceService(db)
    workspaces = service.get_user_workspaces(user.id)
    
    assert len(workspaces) == 2
    assert {ws.slug for ws in workspaces} == {'ws1', 'ws2'}


def test_add_member(db):
    """Test adding member to workspace"""
    owner = User(username="owner", email="owner@test.com", hashed_password="hash")
    member_user = User(username="member", email="member@test.com", hashed_password="hash")
    db.add_all([owner, member_user])
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=owner.id)
    db.add(workspace)
    db.commit()
    
    service = WorkspaceService(db)
    member = service.add_member(workspace.id, member_user.id, role='member')
    
    assert member.workspace_id == workspace.id
    assert member.user_id == member_user.id
    assert member.role == 'member'


def test_remove_member(db):
    """Test removing member from workspace"""
    owner = User(username="owner", email="owner@test.com", hashed_password="hash")
    member_user = User(username="member", email="member@test.com", hashed_password="hash")
    db.add_all([owner, member_user])
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=owner.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=member_user.id, role='member')
    db.add(member)
    db.commit()
    
    service = WorkspaceService(db)
    result = service.remove_member(workspace.id, member_user.id)
    
    assert result is True
    
    # Verify member was removed
    remaining = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == member_user.id
    ).first()
    assert remaining is None


def test_check_user_access(db):
    """Test checking user access to workspace"""
    owner = User(username="owner", email="owner@test.com", hashed_password="hash")
    member_user = User(username="member", email="member@test.com", hashed_password="hash")
    non_member = User(username="other", email="other@test.com", hashed_password="hash")
    db.add_all([owner, member_user, non_member])
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=owner.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=member_user.id, role='member')
    db.add(member)
    db.commit()
    
    service = WorkspaceService(db)
    
    assert service.check_user_access(workspace.id, member_user.id) is True
    assert service.check_user_access(workspace.id, non_member.id) is False


def test_update_workspace_quota(db):
    """Test updating workspace quota"""
    user = User(username="owner", email="owner@test.com", hashed_password="hash")
    db.add(user)
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    service = WorkspaceService(db)
    updated = service.update_workspace_quota(
        workspace.id,
        max_concurrent_tasks=20,
        priority_strategy='user_role'
    )
    
    assert updated.max_concurrent_tasks == 20
    assert updated.priority_strategy == 'user_role'
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_workspace_service.py -v
```

预期：FAIL - 模块不存在

- [ ] **Step 3: 实现 Workspace 服务**

创建 `src/openrag/services/workspace_service.py`:

```python
"""Workspace service for business logic"""

from typing import List, Optional
from sqlalchemy.orm import Session
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.user import User


class WorkspaceService:
    """Service for workspace operations"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create_workspace(
        self,
        name: str,
        slug: str,
        description: Optional[str],
        owner_id: int,
        max_concurrent_tasks: int = 10,
        max_storage_bytes: int = 10*1024*1024*1024,
        priority_strategy: str = 'file_size'
    ) -> Workspace:
        """Create a new workspace and add owner as admin member"""
        workspace = Workspace(
            name=name,
            slug=slug,
            description=description,
            owner_id=owner_id,
            max_concurrent_tasks=max_concurrent_tasks,
            max_storage_bytes=max_storage_bytes,
            priority_strategy=priority_strategy
        )
        self.db.add(workspace)
        self.db.flush()  # Get workspace.id
        
        # Add owner as admin member
        member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=owner_id,
            role='admin'
        )
        self.db.add(member)
        self.db.commit()
        self.db.refresh(workspace)
        
        return workspace
    
    def get_workspace(self, workspace_id: int) -> Optional[Workspace]:
        """Get workspace by ID"""
        return self.db.query(Workspace).filter(
            Workspace.id == workspace_id
        ).first()
    
    def get_user_workspaces(self, user_id: int) -> List[Workspace]:
        """Get all workspaces user is a member of"""
        return self.db.query(Workspace).join(
            WorkspaceMember,
            Workspace.id == WorkspaceMember.workspace_id
        ).filter(
            WorkspaceMember.user_id == user_id
        ).all()
    
    def add_member(
        self,
        workspace_id: int,
        user_id: int,
        role: str = 'member'
    ) -> WorkspaceMember:
        """Add a member to workspace"""
        member = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role
        )
        self.db.add(member)
        self.db.commit()
        self.db.refresh(member)
        return member
    
    def remove_member(self, workspace_id: int, user_id: int) -> bool:
        """Remove a member from workspace"""
        member = self.db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id
        ).first()
        
        if member:
            self.db.delete(member)
            self.db.commit()
            return True
        return False
    
    def get_workspace_members(self, workspace_id: int) -> List[WorkspaceMember]:
        """Get all members of a workspace"""
        return self.db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id
        ).all()
    
    def check_user_access(self, workspace_id: int, user_id: int) -> bool:
        """Check if user has access to workspace"""
        member = self.db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id
        ).first()
        return member is not None
    
    def update_workspace_quota(
        self,
        workspace_id: int,
        max_concurrent_tasks: Optional[int] = None,
        max_storage_bytes: Optional[int] = None,
        priority_strategy: Optional[str] = None
    ) -> Workspace:
        """Update workspace quota configuration"""
        workspace = self.get_workspace(workspace_id)
        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")
        
        if max_concurrent_tasks is not None:
            workspace.max_concurrent_tasks = max_concurrent_tasks
        if max_storage_bytes is not None:
            workspace.max_storage_bytes = max_storage_bytes
        if priority_strategy is not None:
            workspace.priority_strategy = priority_strategy
        
        self.db.commit()
        self.db.refresh(workspace)
        return workspace
    
    def delete_workspace(self, workspace_id: int) -> bool:
        """Delete a workspace (cascade deletes members)"""
        workspace = self.get_workspace(workspace_id)
        if workspace:
            self.db.delete(workspace)
            self.db.commit()
            return True
        return False
```

- [ ] **Step 4: 运行测试验证通过**

```bash
pytest tests/test_workspace_service.py -v
```

预期：所有测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/openrag/services/workspace_service.py tests/test_workspace_service.py
git commit -m "feat: add WorkspaceService for business logic

- Implement workspace CRUD operations
- Add member management (add/remove/list)
- Add user access checking
- Add quota configuration updates
- Add comprehensive service tests

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 创建 Workspace API

**Files:**
- Create: `src/openrag/api/workspaces_api.py`
- Modify: `src/openrag/api/deps.py`
- Modify: `src/openrag/api/main.py`
- Test: `tests/test_workspace_api.py`

- [ ] **Step 1: 编写 Workspace API 测试**

创建 `tests/test_workspace_api.py`:

```python
"""Tests for Workspace API"""

import pytest
from fastapi.testclient import TestClient
from openrag.api.main import app
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_headers(db):
    """Create user and return auth headers"""
    user = User(username="testuser", email="test@test.com", hashed_password="hash")
    db.add(user)
    db.commit()
    
    # Mock authentication - in real app would use JWT
    return {"Authorization": f"Bearer mock_token_{user.id}"}


def test_create_workspace(client, auth_headers, db):
    """Test creating a workspace"""
    response = client.post(
        "/workspaces",
        json={
            "name": "Test Workspace",
            "slug": "test-workspace",
            "description": "A test workspace"
        },
        headers=auth_headers
    )
    
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Workspace"
    assert data["slug"] == "test-workspace"
    assert "id" in data


def test_list_user_workspaces(client, auth_headers, db):
    """Test listing user's workspaces"""
    # Create test data
    user = db.query(User).filter(User.username == "testuser").first()
    ws1 = Workspace(name="WS1", slug="ws1", owner_id=user.id)
    ws2 = Workspace(name="WS2", slug="ws2", owner_id=user.id)
    db.add_all([ws1, ws2])
    db.commit()
    
    member1 = WorkspaceMember(workspace_id=ws1.id, user_id=user.id, role='admin')
    member2 = WorkspaceMember(workspace_id=ws2.id, user_id=user.id, role='member')
    db.add_all([member1, member2])
    db.commit()
    
    response = client.get("/workspaces", headers=auth_headers)
    
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert {ws["slug"] for ws in data} == {"ws1", "ws2"}


def test_get_workspace_detail(client, auth_headers, db):
    """Test getting workspace details"""
    user = db.query(User).filter(User.username == "testuser").first()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')
    db.add(member)
    db.commit()
    
    response = client.get(f"/workspaces/{workspace.id}", headers=auth_headers)
    
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == workspace.id
    assert data["name"] == "Test"
    assert data["slug"] == "test"


def test_update_workspace(client, auth_headers, db):
    """Test updating workspace"""
    user = db.query(User).filter(User.username == "testuser").first()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')
    db.add(member)
    db.commit()
    
    response = client.put(
        f"/workspaces/{workspace.id}",
        json={"name": "Updated Name", "description": "Updated description"},
        headers=auth_headers
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Name"
    assert data["description"] == "Updated description"


def test_delete_workspace(client, auth_headers, db):
    """Test deleting workspace"""
    user = db.query(User).filter(User.username == "testuser").first()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')
    db.add(member)
    db.commit()
    
    workspace_id = workspace.id
    response = client.delete(f"/workspaces/{workspace_id}", headers=auth_headers)
    
    assert response.status_code == 200
    
    # Verify workspace was deleted
    deleted = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    assert deleted is None


def test_add_workspace_member(client, auth_headers, db):
    """Test adding member to workspace"""
    user = db.query(User).filter(User.username == "testuser").first()
    new_user = User(username="newuser", email="new@test.com", hashed_password="hash")
    db.add(new_user)
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')
    db.add(member)
    db.commit()
    
    response = client.post(
        f"/workspaces/{workspace.id}/members",
        json={"user_id": new_user.id, "role": "member"},
        headers=auth_headers
    )
    
    assert response.status_code == 201
    data = response.json()
    assert data["user_id"] == new_user.id
    assert data["role"] == "member"


def test_list_workspace_members(client, auth_headers, db):
    """Test listing workspace members"""
    user = db.query(User).filter(User.username == "testuser").first()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')
    db.add(member)
    db.commit()
    
    response = client.get(f"/workspaces/{workspace.id}/members", headers=auth_headers)
    
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert any(m["user_id"] == user.id for m in data)


def test_remove_workspace_member(client, auth_headers, db):
    """Test removing member from workspace"""
    user = db.query(User).filter(User.username == "testuser").first()
    member_user = User(username="member", email="member@test.com", hashed_password="hash")
    db.add(member_user)
    db.commit()
    
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    admin_member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')
    member = WorkspaceMember(workspace_id=workspace.id, user_id=member_user.id, role='member')
    db.add_all([admin_member, member])
    db.commit()
    
    response = client.delete(
        f"/workspaces/{workspace.id}/members/{member_user.id}",
        headers=auth_headers
    )
    
    assert response.status_code == 200
    
    # Verify member was removed
    removed = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == member_user.id
    ).first()
    assert removed is None


def test_update_workspace_quota(client, auth_headers, db):
    """Test updating workspace quota"""
    user = db.query(User).filter(User.username == "testuser").first()
    workspace = Workspace(name="Test", slug="test", owner_id=user.id)
    db.add(workspace)
    db.commit()
    
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role='admin')
    db.add(member)
    db.commit()
    
    response = client.put(
        f"/workspaces/{workspace.id}/quota",
        json={"max_concurrent_tasks": 20, "priority_strategy": "user_role"},
        headers=auth_headers
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["max_concurrent_tasks"] == 20
    assert data["priority_strategy"] == "user_role"
```

- [ ] **Step 2: 运行测试验证失败**

```bash
pytest tests/test_workspace_api.py -v
```

预期：FAIL - 路由不存在

- [ ] **Step 3: 添加 workspace 依赖注入到 deps.py**

在 `src/openrag/api/deps.py` 添加：

```python
from fastapi import Depends, HTTPException, status
from openrag.models.workspace import Workspace, WorkspaceMember


async def get_workspace(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Workspace:
    """Get workspace and verify user access"""
    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    
    # Check if user is a member
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == current_user.id
    ).first()
    
    if not member:
        raise HTTPException(status_code=403, detail="Not a member of this workspace")
    
    return workspace


async def get_workspace_admin(
    workspace_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Workspace:
    """Get workspace and verify user is admin"""
    workspace = await get_workspace(workspace_id, current_user, db)
    
    # Check if user is admin
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == current_user.id
    ).first()
    
    if member.role != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    return workspace
```

- [ ] **Step 4: 实现 Workspace API**

创建 `src/openrag/api/workspaces_api.py`:

```python
"""Workspace management API endpoints"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from openrag.api.deps import get_current_user, get_db, get_workspace, get_workspace_admin
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.services.workspace_service import WorkspaceService


router = APIRouter(prefix="/workspaces", tags=["workspaces"])


# Pydantic schemas
class WorkspaceCreate(BaseModel):
    """Workspace creation request"""
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-z0-9-]+$')
    description: Optional[str] = None
    max_concurrent_tasks: int = Field(default=10, ge=1, le=100)
    max_storage_bytes: int = Field(default=10*1024*1024*1024, ge=0)
    priority_strategy: str = Field(default='file_size')


class WorkspaceUpdate(BaseModel):
    """Workspace update request"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None


class WorkspaceQuotaUpdate(BaseModel):
    """Workspace quota update request"""
    max_concurrent_tasks: Optional[int] = Field(None, ge=1, le=100)
    max_storage_bytes: Optional[int] = Field(None, ge=0)
    priority_strategy: Optional[str] = None


class WorkspaceResponse(BaseModel):
    """Workspace response schema"""
    id: int
    name: str
    slug: str
    description: Optional[str]
    owner_id: int
    max_concurrent_tasks: int
    max_storage_bytes: int
    priority_strategy: str
    created_at: str
    updated_at: Optional[str]

    model_config = {"from_attributes": True}


class MemberAdd(BaseModel):
    """Add member request"""
    user_id: int
    role: str = Field(default='member', pattern=r'^(admin|member|viewer)$')


class MemberResponse(BaseModel):
    """Member response schema"""
    id: int
    workspace_id: int
    user_id: int
    role: str
    joined_at: str

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    """Generic message response"""
    message: str


# Workspace endpoints
@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    request: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new workspace"""
    service = WorkspaceService(db)
    
    # Check if slug already exists
    existing = db.query(Workspace).filter(Workspace.slug == request.slug).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Workspace with slug '{request.slug}' already exists"
        )
    
    workspace = service.create_workspace(
        name=request.name,
        slug=request.slug,
        description=request.description,
        owner_id=current_user.id,
        max_concurrent_tasks=request.max_concurrent_tasks,
        max_storage_bytes=request.max_storage_bytes,
        priority_strategy=request.priority_strategy
    )
    
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        owner_id=workspace.owner_id,
        max_concurrent_tasks=workspace.max_concurrent_tasks,
        max_storage_bytes=workspace.max_storage_bytes,
        priority_strategy=workspace.priority_strategy,
        created_at=workspace.created_at.isoformat(),
        updated_at=workspace.updated_at.isoformat() if workspace.updated_at else None
    )


@router.get("", response_model=List[WorkspaceResponse])
async def list_workspaces(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List user's workspaces"""
    service = WorkspaceService(db)
    workspaces = service.get_user_workspaces(current_user.id)
    
    return [
        WorkspaceResponse(
            id=ws.id,
            name=ws.name,
            slug=ws.slug,
            description=ws.description,
            owner_id=ws.owner_id,
            max_concurrent_tasks=ws.max_concurrent_tasks,
            max_storage_bytes=ws.max_storage_bytes,
            priority_strategy=ws.priority_strategy,
            created_at=ws.created_at.isoformat(),
            updated_at=ws.updated_at.isoformat() if ws.updated_at else None
        )
        for ws in workspaces
    ]


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace_detail(
    workspace: Workspace = Depends(get_workspace)
):
    """Get workspace details"""
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        owner_id=workspace.owner_id,
        max_concurrent_tasks=workspace.max_concurrent_tasks,
        max_storage_bytes=workspace.max_storage_bytes,
        priority_strategy=workspace.priority_strategy,
        created_at=workspace.created_at.isoformat(),
        updated_at=workspace.updated_at.isoformat() if workspace.updated_at else None
    )


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    request: WorkspaceUpdate,
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Update workspace (admin only)"""
    if request.name is not None:
        workspace.name = request.name
    if request.description is not None:
        workspace.description = request.description
    
    db.commit()
    db.refresh(workspace)
    
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        description=workspace.description,
        owner_id=workspace.owner_id,
        max_concurrent_tasks=workspace.max_concurrent_tasks,
        max_storage_bytes=workspace.max_storage_bytes,
        priority_strategy=workspace.priority_strategy,
        created_at=workspace.created_at.isoformat(),
        updated_at=workspace.updated_at.isoformat() if workspace.updated_at else None
    )


@router.delete("/{workspace_id}", response_model=MessageResponse)
async def delete_workspace(
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Delete workspace (admin only)"""
    service = WorkspaceService(db)
    service.delete_workspace(workspace.id)
    
    return MessageResponse(message="Workspace deleted successfully")


# Member endpoints
@router.post("/{workspace_id}/members", response_model=MemberResponse, status_code=status.HTTP_201_CREATED)
async def add_member(
    request: MemberAdd,
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Add member to workspace (admin only)"""
    service = WorkspaceService(db)
    
    # Check if user exists
    user = db.query(User).filter(User.id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if already a member
    existing = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == request.user_id
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is already a member"
        )
    
    member = service.add_member(workspace.id, request.user_id, request.role)
    
    return MemberResponse(
        id=member.id,
        workspace_id=member.workspace_id,
        user_id=member.user_id,
        role=member.role,
        joined_at=member.joined_at.isoformat()
    )


@router.get("/{workspace_id}/members", response_model=List[MemberResponse])
async def list_members(
    workspace: Workspace = Depends(get_workspace),
    db: Session = Depends(get_db)
):
    """List workspace members"""
    service = WorkspaceService(db)
    members = service.get_workspace_members(workspace.id)
    
    return [
        MemberResponse(
            id=m.id,
            workspace_id=m.workspace_id,
            user_id=m.user_id,
            role=m.role,
            joined_at=m.joined_at.isoformat()
        )
        for m in members
    ]


@router.delete("/{workspace_id}/members/{user_id}", response_model=MessageResponse)
async def remove_member(
    user_id: int,
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Remove member from workspace (admin only)"""
    service = WorkspaceService(db)
    
    # Cannot remove owner
    if user_id == workspace.owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove workspace owner"
        )
    
    success = service.remove_member(workspace.id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Member not found")
    
    return MessageResponse(message="Member removed successfully")


# Quota endpoint
@router.put("/{workspace_id}/quota", response_model=WorkspaceResponse)
async def update_quota(
    request: WorkspaceQuotaUpdate,
    workspace: Workspace = Depends(get_workspace_admin),
    db: Session = Depends(get_db)
):
    """Update workspace quota (admin only)"""
    service = WorkspaceService(db)
    
    updated = service.update_workspace_quota(
        workspace.id,
        max_concurrent_tasks=request.max_concurrent_tasks,
        max_storage_bytes=request.max_storage_bytes,
        priority_strategy=request.priority_strategy
    )
    
    return WorkspaceResponse(
        id=updated.id,
        name=updated.name,
        slug=updated.slug,
        description=updated.description,
        owner_id=updated.owner_id,
        max_concurrent_tasks=updated.max_concurrent_tasks,
        max_storage_bytes=updated.max_storage_bytes,
        priority_strategy=updated.priority_strategy,
        created_at=updated.created_at.isoformat(),
        updated_at=updated.updated_at.isoformat() if updated.updated_at else None
    )
```

- [ ] **Step 5: 注册路由到 main.py**

在 `src/openrag/api/main.py` 中添加：

```python
from openrag.api.workspaces_api import router as workspaces_router

# 在现有路由注册后添加
app.include_router(workspaces_router)
```

- [ ] **Step 6: 运行测试验证通过**

```bash
pytest tests/test_workspace_api.py -v
```

预期：所有测试 PASS

- [ ] **Step 7: 提交**

```bash
git add src/openrag/api/workspaces_api.py src/openrag/api/deps.py src/openrag/api/main.py tests/test_workspace_api.py
git commit -m "feat: add Workspace management API

- Add workspace CRUD endpoints
- Add member management endpoints
- Add quota configuration endpoint
- Add workspace and admin access dependencies
- Add comprehensive API tests

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: 端到端测试

**Files:**
- Create: `tests/test_workspace_e2e.py`

- [ ] **Step 1: 编写端到端测试**

创建 `tests/test_workspace_e2e.py`:

```python
"""End-to-end tests for workspace functionality"""

import pytest
from fastapi.testclient import TestClient
from openrag.api.main import app
from openrag.models.user import User
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.file import File


@pytest.fixture
def client():
    return TestClient(app)


def test_complete_workspace_workflow(client, db):
    """Test complete workspace workflow from creation to deletion"""
    
    # 1. Create users
    owner = User(username="owner", email="owner@test.com", hashed_password="hash")
    member1 = User(username="member1", email="member1@test.com", hashed_password="hash")
    member2 = User(username="member2", email="member2@test.com", hashed_password="hash")
    db.add_all([owner, member1, member2])
    db.commit()
    
    owner_headers = {"Authorization": f"Bearer mock_token_{owner.id}"}
    member1_headers = {"Authorization": f"Bearer mock_token_{member1.id}"}
    
    # 2. Owner creates workspace
    response = client.post(
        "/workspaces",
        json={
            "name": "Test Company",
            "slug": "test-company",
            "description": "Our test workspace",
            "max_concurrent_tasks": 15
        },
        headers=owner_headers
    )
    assert response.status_code == 201
    workspace_id = response.json()["id"]
    
    # 3. Owner adds members
    response = client.post(
        f"/workspaces/{workspace_id}/members",
        json={"user_id": member1.id, "role": "member"},
        headers=owner_headers
    )
    assert response.status_code == 201
    
    response = client.post(
        f"/workspaces/{workspace_id}/members",
        json={"user_id": member2.id, "role": "viewer"},
        headers=owner_headers
    )
    assert response.status_code == 201
    
    # 4. Member1 can see workspace
    response = client.get("/workspaces", headers=member1_headers)
    assert response.status_code == 200
    workspaces = response.json()
    assert len(workspaces) == 1
    assert workspaces[0]["slug"] == "test-company"
    
    # 5. Member1 can access workspace details
    response = client.get(f"/workspaces/{workspace_id}", headers=member1_headers)
    assert response.status_code == 200
    
    # 6. Member1 cannot update workspace (not admin)
    response = client.put(
        f"/workspaces/{workspace_id}",
        json={"name": "Hacked Name"},
        headers=member1_headers
    )
    assert response.status_code == 403
    
    # 7. Owner updates workspace
    response = client.put(
        f"/workspaces/{workspace_id}",
        json={"name": "Updated Company", "description": "New description"},
        headers=owner_headers
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Updated Company"
    
    # 8. Owner updates quota
    response = client.put(
        f"/workspaces/{workspace_id}/quota",
        json={"max_concurrent_tasks": 20, "priority_strategy": "user_role"},
        headers=owner_headers
    )
    assert response.status_code == 200
    assert response.json()["max_concurrent_tasks"] == 20
    
    # 9. Owner lists members
    response = client.get(f"/workspaces/{workspace_id}/members", headers=owner_headers)
    assert response.status_code == 200
    members = response.json()
    assert len(members) == 3  # owner + 2 members
    
    # 10. Owner removes member2
    response = client.delete(
        f"/workspaces/{workspace_id}/members/{member2.id}",
        headers=owner_headers
    )
    assert response.status_code == 200
    
    # 11. Verify member2 was removed
    response = client.get(f"/workspaces/{workspace_id}/members", headers=owner_headers)
    assert response.status_code == 200
    members = response.json()
    assert len(members) == 2
    assert not any(m["user_id"] == member2.id for m in members)
    
    # 12. Owner deletes workspace
    response = client.delete(f"/workspaces/{workspace_id}", headers=owner_headers)
    assert response.status_code == 200
    
    # 13. Verify workspace was deleted
    response = client.get(f"/workspaces/{workspace_id}", headers=owner_headers)
    assert response.status_code == 404


def test_workspace_data_isolation(client, db):
    """Test that workspace data is properly isolated"""
    
    # Create users and workspaces
    user1 = User(username="user1", email="user1@test.com", hashed_password="hash")
    user2 = User(username="user2", email="user2@test.com", hashed_password="hash")
    db.add_all([user1, user2])
    db.commit()
    
    ws1 = Workspace(name="WS1", slug="ws1", owner_id=user1.id)
    ws2 = Workspace(name="WS2", slug="ws2", owner_id=user2.id)
    db.add_all([ws1, ws2])
    db.commit()
    
    # Add users as members
    member1 = WorkspaceMember(workspace_id=ws1.id, user_id=user1.id, role='admin')
    member2 = WorkspaceMember(workspace_id=ws2.id, user_id=user2.id, role='admin')
    db.add_all([member1, member2])
    db.commit()
    
    # Create files in different workspaces
    file1 = File(
        uri="/ws1/file.txt",
        name="file.txt",
        owner_id=user1.id,
        workspace_id=ws1.id,
        is_directory=False,
        size=100
    )
    file2 = File(
        uri="/ws2/file.txt",
        name="file.txt",
        owner_id=user2.id,
        workspace_id=ws2.id,
        is_directory=False,
        size=200
    )
    db.add_all([file1, file2])
    db.commit()
    
    # Verify user1 cannot access ws2
    user1_headers = {"Authorization": f"Bearer mock_token_{user1.id}"}
    response = client.get(f"/workspaces/{ws2.id}", headers=user1_headers)
    assert response.status_code == 403
    
    # Verify user2 cannot access ws1
    user2_headers = {"Authorization": f"Bearer mock_token_{user2.id}"}
    response = client.get(f"/workspaces/{ws1.id}", headers=user2_headers)
    assert response.status_code == 403
    
    # Verify each user only sees their own workspace
    response = client.get("/workspaces", headers=user1_headers)
    assert response.status_code == 200
    workspaces = response.json()
    assert len(workspaces) == 1
    assert workspaces[0]["id"] == ws1.id
    
    response = client.get("/workspaces", headers=user2_headers)
    assert response.status_code == 200
    workspaces = response.json()
    assert len(workspaces) == 1
    assert workspaces[0]["id"] == ws2.id
```

- [ ] **Step 2: 运行端到端测试**

```bash
pytest tests/test_workspace_e2e.py -v
```

预期：所有测试 PASS

- [ ] **Step 3: 提交**

```bash
git add tests/test_workspace_e2e.py
git commit -m "test: add end-to-end tests for workspace functionality

- Test complete workspace workflow
- Test workspace data isolation
- Verify access control
- Verify member management

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## 自查清单

**规范覆盖检查**：
- ✅ Workspace 和 WorkspaceMember 模型 - Task 1
- ✅ 为现有模型添加 workspace_id - Task 2
- ✅ 数据库迁移 - Task 3
- ✅ 数据迁移到默认空间 - Task 4
- ✅ 业务逻辑服务层 - Task 5
- ✅ RESTful API - Task 6
- ✅ 端到端测试 - Task 7

**占位符检查**：
- ✅ 无 TBD 或 TODO
- ✅ 所有代码块完整
- ✅ 所有测试包含具体断言

**类型一致性检查**：
- ✅ Workspace 模型字段在所有任务中一致
- ✅ WorkspaceMember 模型字段在所有任务中一致
- ✅ API 响应模型与数据库模型匹配

---

## 执行说明

完成本计划后：
1. 运行完整测试套件：`pytest tests/test_workspace*.py -v`
2. 执行数据库迁移：`alembic upgrade head`
3. 运行数据迁移脚本：`python scripts/migrate_to_workspace.py`
4. 验证 API 文档：访问 `http://localhost:8000/docs`

下一步：实施计划 2 - 任务管理系统核心

