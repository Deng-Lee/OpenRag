# JWT Role Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a role-based access control system where users can be assigned roles with specific workspace permissions, computing effective permissions at runtime while JWTs remain identity-only.

**Architecture:** We will introduce `Role`, `RoleWorkspacePermission`, and `UserRole` models. We will update the `WorkspaceService` to compute effective permissions by unioning `WorkspaceMember` (direct) and `RoleWorkspacePermission` (role-based) grants. Finally, we will expose an API endpoint that aggregates permission sources for the frontend.

**Tech Stack:** Python 3.10+, FastAPI, SQLAlchemy (MySQL), pytest

---

## File Structure
- `openrag/src/openrag/models/role.py`: (New) Define `Role`, `RoleWorkspacePermission`, `UserRole`
- `openrag/src/openrag/models/__init__.py`: (Modify) Export new models
- `openrag/tests/models/test_role.py`: (New) Test role model relationships
- `openrag/src/openrag/services/workspace_service.py`: (Modify) Update `check_permission` and `get_user_workspaces` logic
- `openrag/tests/services/test_workspace_service.py`: (Modify) Add tests for role-based permission inheritance and overrides
- `openrag/src/openrag/api/permissions_api.py`: (Modify) Add endpoint to fetch aggregated permission details
- `openrag/tests/api/test_permissions_api.py`: (Modify) Add tests for the new aggregated endpoint

---

### Task 1: Create Role Data Models

**Files:**
- Create: `openrag/src/openrag/models/role.py`
- Modify: `openrag/src/openrag/models/__init__.py`
- Test: `openrag/tests/models/test_role.py`

- [ ] **Step 1: Write the failing test**

Create `openrag/tests/models/test_role.py`:
```python
import pytest
from sqlalchemy import select
from openrag.models.role import Role, RoleWorkspacePermission, UserRole
from openrag.models.user import User
from openrag.models.workspace import Workspace

def test_role_creation(db_session):
    role = Role(name="Admin", role_code="admin_role", is_active=True)
    db_session.add(role)
    db_session.commit()
    
    saved_role = db_session.scalar(select(Role).where(Role.role_code == "admin_role"))
    assert saved_role is not None
    assert saved_role.name == "Admin"
    assert saved_role.is_active is True

def test_role_workspace_permission(db_session):
    user = User(username="test", email="test@test.com", password_hash="hash", full_name="Test")
    db_session.add(user)
    db_session.commit()
    
    workspace = Workspace(name="Test WS", slug="test-ws", owner_id=user.id)
    role = Role(name="Viewer", role_code="viewer_role")
    db_session.add_all([workspace, role])
    db_session.commit()
    
    perm = RoleWorkspacePermission(role_id=role.id, workspace_id=workspace.id, permission="read")
    db_session.add(perm)
    db_session.commit()
    
    saved_perm = db_session.scalar(select(RoleWorkspacePermission).where(RoleWorkspacePermission.role_id == role.id))
    assert saved_perm.permission == "read"
    assert saved_perm.workspace.name == "Test WS"

def test_user_role_assignment(db_session):
    user = User(username="test2", email="test2@test.com", password_hash="hash", full_name="Test 2")
    role = Role(name="Editor", role_code="editor_role")
    db_session.add_all([user, role])
    db_session.commit()
    
    user_role = UserRole(user_id=user.id, role_id=role.id)
    db_session.add(user_role)
    db_session.commit()
    
    assert len(user.roles) == 1
    assert user.roles[0].role_code == "editor_role"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest openrag/tests/models/test_role.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'openrag.models.role'"

- [ ] **Step 3: Write minimal implementation**

Create `openrag/src/openrag/models/role.py`:
```python
from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from typing import List

from openrag.models.base import Base, TimestampMixin

class Role(Base, TimestampMixin):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="Role display name")
    role_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="System unique code (en_US and underscores)")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)

    workspace_permissions: Mapped[List["RoleWorkspacePermission"]] = relationship(
        "RoleWorkspacePermission", back_populates="role", cascade="all, delete-orphan"
    )
    user_assignments: Mapped[List["UserRole"]] = relationship(
        "UserRole", back_populates="role", cascade="all, delete-orphan"
    )

class RoleWorkspacePermission(Base, TimestampMixin):
    __tablename__ = "role_workspace_permissions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[int] = mapped_column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    permission: Mapped[str] = mapped_column(String(32), nullable=False, comment="read or write")

    role: Mapped["Role"] = relationship("Role", back_populates="workspace_permissions")
    workspace: Mapped["Workspace"] = relationship("Workspace")

    __table_args__ = (
        UniqueConstraint("role_id", "workspace_id", name="uq_role_workspace"),
    )

class UserRole(Base, TimestampMixin):
    __tablename__ = "user_roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role_id: Mapped[int] = mapped_column(Integer, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False)

    role: Mapped["Role"] = relationship("Role", back_populates="user_assignments")
    user: Mapped["User"] = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
    )
```

Modify `openrag/src/openrag/models/user.py` to add the roles relationship:
```python
# Add to the Relationships section:
    roles: Mapped[List["Role"]] = relationship(
        "Role",
        secondary="user_roles",
        viewonly=True
    )
```

Modify `openrag/src/openrag/models/__init__.py`:
```python
# Add:
from .role import Role, RoleWorkspacePermission, UserRole
```

Generate Alembic Migration:
```bash
cd OpenRag && alembic revision --autogenerate -m "Add role models"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest openrag/tests/models/test_role.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/src/openrag/models/role.py openrag/src/openrag/models/__init__.py openrag/src/openrag/models/user.py openrag/tests/models/test_role.py openrag/alembic/versions/
git commit -m "feat(models): add Role, RoleWorkspacePermission, and UserRole models"
```

### Task 2: Implement Effective Permission Calculation

**Files:**
- Modify: `openrag/src/openrag/services/workspace_service.py`
- Modify: `openrag/tests/services/test_workspace_service.py`

- [ ] **Step 1: Write the failing test**

Modify `openrag/tests/services/test_workspace_service.py` (add these tests):
```python
from openrag.models.role import Role, RoleWorkspacePermission, UserRole
from openrag.services.workspace_service import check_workspace_permission

def test_check_permission_direct_grant(db_session, test_user, test_workspace):
    # Setup WorkspaceMember ...
    assert check_workspace_permission(db_session, test_user.id, test_workspace.id) == "read"

def test_check_permission_role_grant(db_session, test_user, test_workspace):
    role = Role(name="Admin", role_code="admin", is_active=True)
    db_session.add(role)
    db_session.commit()
    
    db_session.add(RoleWorkspacePermission(role_id=role.id, workspace_id=test_workspace.id, permission="write"))
    db_session.add(UserRole(user_id=test_user.id, role_id=role.id))
    db_session.commit()
    
    assert check_workspace_permission(db_session, test_user.id, test_workspace.id) == "write"

def test_check_permission_union_highest(db_session, test_user, test_workspace):
    # Direct = read
    from openrag.models.workspace import WorkspaceMember
    db_session.add(WorkspaceMember(user_id=test_user.id, workspace_id=test_workspace.id, role="read"))
    
    # Role = write
    role = Role(name="Admin", role_code="admin", is_active=True)
    db_session.add(role)
    db_session.commit()
    
    db_session.add(RoleWorkspacePermission(role_id=role.id, workspace_id=test_workspace.id, permission="write"))
    db_session.add(UserRole(user_id=test_user.id, role_id=role.id))
    db_session.commit()
    
    # Highest should win
    assert check_workspace_permission(db_session, test_user.id, test_workspace.id) == "write"

def test_check_permission_inactive_role(db_session, test_user, test_workspace):
    role = Role(name="Admin", role_code="admin", is_active=False)  # INACTIVE
    db_session.add(role)
    db_session.commit()
    
    db_session.add(RoleWorkspacePermission(role_id=role.id, workspace_id=test_workspace.id, permission="write"))
    db_session.add(UserRole(user_id=test_user.id, role_id=role.id))
    db_session.commit()
    
    assert check_workspace_permission(db_session, test_user.id, test_workspace.id) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest openrag/tests/services/test_workspace_service.py -k test_check_permission -v`
Expected: FAIL for role-based permission tests because `check_workspace_permission` currently only checks `WorkspaceMember`.

- [ ] **Step 3: Write minimal implementation**

Modify `openrag/src/openrag/services/workspace_service.py` to update the permission check:

```python
from sqlalchemy import select, or_
from openrag.models.workspace import Workspace, WorkspaceMember
from openrag.models.role import Role, RoleWorkspacePermission, UserRole

def check_workspace_permission(db, user_id: int, workspace_id: int) -> str | None:
    """
    Returns 'write', 'read', or None.
    Uses union of highest permission from direct WorkspaceMember and active Roles.
    """
    # 1. Check if user is owner
    workspace = db.scalar(select(Workspace).where(Workspace.id == workspace_id))
    if not workspace:
        return None
    if workspace.owner_id == user_id:
        return "write"
        
    permissions = []
    
    # 2. Check direct grant
    member = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id
        )
    )
    if member:
        permissions.append(member.role)
        
    # 3. Check role grants
    role_perms = db.scalars(
        select(RoleWorkspacePermission.permission)
        .join(Role, RoleWorkspacePermission.role_id == Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user_id,
            RoleWorkspacePermission.workspace_id == workspace_id,
            Role.is_active == True
        )
    ).all()
    
    permissions.extend(role_perms)
    
    if not permissions:
        return None
        
    if "write" in permissions:
        return "write"
    return "read"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest openrag/tests/services/test_workspace_service.py -k test_check_permission -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/src/openrag/services/workspace_service.py openrag/tests/services/test_workspace_service.py
git commit -m "feat(services): compute workspace permissions by unioning direct and role grants"
```

### Task 3: Expose Aggregated Permission API

**Files:**
- Modify: `openrag/src/openrag/api/permissions_api.py`
- Modify: `openrag/tests/api/test_permissions_api.py`

- [ ] **Step 1: Write the failing test**

Modify `openrag/tests/api/test_permissions_api.py`:
```python
def test_get_user_permission_details(client, auth_headers, db_session, test_user, test_workspace):
    # Setup data
    from openrag.models.workspace import WorkspaceMember
    from openrag.models.role import Role, RoleWorkspacePermission, UserRole
    
    db_session.add(WorkspaceMember(user_id=test_user.id, workspace_id=test_workspace.id, role="read"))
    
    role = Role(name="Admin", role_code="admin", is_active=True)
    db_session.add(role)
    db_session.commit()
    
    db_session.add(RoleWorkspacePermission(role_id=role.id, workspace_id=test_workspace.id, permission="write"))
    db_session.add(UserRole(user_id=test_user.id, role_id=role.id))
    db_session.commit()

    response = client.get(f"/api/v1/users/{test_user.id}/permissions/details", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    
    assert "roles" in data
    assert len(data["roles"]) == 1
    assert data["roles"][0]["role_code"] == "admin"
    
    assert "workspace_permissions" in data
    # Should list both sources separately for the UI
    perms = data["workspace_permissions"]
    assert len(perms) == 2
    
    sources = [p["source"] for p in perms]
    assert "direct" in sources
    assert "role:Admin" in sources
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest openrag/tests/api/test_permissions_api.py -k test_get_user_permission_details -v`
Expected: FAIL with 404 Not Found

- [ ] **Step 3: Write minimal implementation**

Modify `openrag/src/openrag/api/permissions_api.py`:
```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import select

from openrag.api.deps import get_db, get_current_user
from openrag.models.user import User
from openrag.models.workspace import WorkspaceMember, Workspace
from openrag.models.role import Role, RoleWorkspacePermission, UserRole

router = APIRouter(prefix="/users", tags=["Permissions"])

@router.get("/{user_id}/permissions/details")
def get_user_permission_details(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.id != user_id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Not authorized to view these permissions")
        
    # 1. Get user roles
    roles = db.scalars(
        select(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    ).all()
    
    # 2. Get direct permissions
    direct_perms = db.scalars(
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == user_id)
    ).all()
    
    # 3. Get role permissions
    role_perms = db.execute(
        select(RoleWorkspacePermission, Role.name, Workspace.name)
        .join(Role, RoleWorkspacePermission.role_id == Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .join(Workspace, RoleWorkspacePermission.workspace_id == Workspace.id)
        .where(UserRole.user_id == user_id, Role.is_active == True)
    ).all()
    
    workspace_permissions = []
    
    # Add direct
    for dp in direct_perms:
        ws = db.scalar(select(Workspace).where(Workspace.id == dp.workspace_id))
        workspace_permissions.append({
            "workspace_id": dp.workspace_id,
            "workspace_name": ws.name if ws else "Unknown",
            "permission": dp.role,
            "source": "direct",
            "source_details": None
        })
        
    # Add role
    for rp, role_name, ws_name in role_perms:
        workspace_permissions.append({
            "workspace_id": rp.workspace_id,
            "workspace_name": ws_name,
            "permission": rp.permission,
            "source": f"role:{role_name}",
            "source_details": {
                "role_id": rp.role_id,
                "role_name": role_name
            }
        })
        
    return {
        "user_id": user_id,
        "roles": [{"id": r.id, "name": r.name, "role_code": r.role_code, "is_active": r.is_active} for r in roles],
        "workspace_permissions": workspace_permissions
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest openrag/tests/api/test_permissions_api.py -k test_get_user_permission_details -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add openrag/src/openrag/api/permissions_api.py openrag/tests/api/test_permissions_api.py
git commit -m "feat(api): add endpoint to fetch detailed aggregated permissions for UI"
```
