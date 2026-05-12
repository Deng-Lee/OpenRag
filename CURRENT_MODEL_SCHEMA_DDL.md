# OpenRag 当前模型 DDL 快照（PostgreSQL）

**更新时间**: 2026-04-17  
**来源**: `OpenRag/src/openrag/models/*.py`  
**用途**: 与当前 ORM 对齐的 **PostgreSQL** 参考 DDL；生产环境以 **SQLAlchemy / Alembic 生成结果** 为准，勿直接复制执行若与迁移历史冲突。

**说明**:

- 下列 **15 张表** 为当前代码中声明的全部业务表；已从旧版文档中去掉 **与 `UNIQUE` / `PRIMARY KEY` 完全重叠的冗余索引**（不再单独列出）。
- `document_chunks.workspace_id` 在模型中**无**指向 `workspaces` 的外键，仅整型列；与 `files.workspace_id` 逻辑一致。
- **`files.processing_status` 等列使用 PostgreSQL 自定义 `ENUM` 类型**：若只执行 `CREATE TABLE files` 而未先创建类型，会报错 **`type "processing_status" does not exist`**。请**从 §2 脚本开头整段执行**（或先单独跑完枚举再跑建表）。
- **`files.processing_error`**：可空 `TEXT`，记录最近一次流水线失败信息；已有库可执行 `ALTER TABLE files ADD COLUMN IF NOT EXISTS processing_error TEXT;`。

## 1) 表清单（依赖顺序：`team_members` 须在 `teams` 与 `users` 之后创建）

1. `roles`  
2. `users`  
3. `audit_logs`  
4. `user_roles`  
5. `workspaces`  
6. `files`  
7. `role_workspace_permissions`  
8. `service_tokens`  
9. `teams`  
10. `workspace_members`  
11. `document_chunks`  
12. `file_permissions`  
13. `share_links`  
14. `tasks`  
15. `team_members`

## 2) 建表脚本（**请整段执行**：先 `CREATE TYPE`，再 `CREATE TABLE`）

下列脚本**开头**为枚举类型定义（与 ORM 中 Python `Enum` 取值一致），**必须**出现在使用它们的 `CREATE TABLE`（如 `files`、`file_permissions`、`team_members`）之前。若类型已存在，可先 `DROP TYPE IF EXISTS ... CASCADE`（慎用，会波及依赖对象）或跳过四条 `CREATE TYPE`。

```sql
CREATE TYPE processing_status AS ENUM (
    'pending', 'parsing', 'building_hierarchy', 'embedding', 'completed', 'failed'
);

CREATE TYPE entity_type AS ENUM ('user', 'team');

CREATE TYPE acl_permission AS ENUM ('read', 'write', 'admin');

CREATE TYPE team_role AS ENUM ('owner', 'admin', 'member');

CREATE TABLE roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    role_code VARCHAR(64) NOT NULL,
    is_active BOOLEAN NOT NULL,
    description VARCHAR(255),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_roles_role_code UNIQUE (role_code)
);

CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(128) NOT NULL,
    is_active BOOLEAN NOT NULL,
    is_admin BOOLEAN NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_users_username UNIQUE (username),
    CONSTRAINT uq_users_email UNIQUE (email)
);

CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id),
    action VARCHAR(50) NOT NULL,
    resource_type VARCHAR(50) NOT NULL,
    resource_id INTEGER NOT NULL,
    details VARCHAR(500),
    ip_address VARCHAR(45),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_roles (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles (id) ON DELETE CASCADE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_user_role UNIQUE (user_id, role_id)
);

CREATE TABLE workspaces (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    slug VARCHAR(128) NOT NULL,
    description VARCHAR(512),
    owner_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    max_concurrent_tasks INTEGER NOT NULL,
    max_storage_bytes BIGINT NOT NULL,
    priority_strategy VARCHAR(32) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_workspaces_name UNIQUE (name),
    CONSTRAINT uq_workspaces_slug UNIQUE (slug)
);

CREATE TABLE files (
    id SERIAL PRIMARY KEY,
    uri VARCHAR(767) NOT NULL,
    name VARCHAR(255) NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES users (id),
    parent_id INTEGER REFERENCES files (id),
    is_directory BOOLEAN NOT NULL,
    size BIGINT NOT NULL,
    mime_type VARCHAR(128),
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id),
    l0_path VARCHAR(2048),
    l1_path VARCHAR(2048),
    l2_path VARCHAR(2048),
    l0_vector_id VARCHAR(128),
    -- 本列类型为枚举 processing_status，须已由脚本开头的 CREATE TYPE 创建
    processing_status processing_status NOT NULL,
    processing_error TEXT NULL,
    total_chunks INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    parser_type VARCHAR(32),
    child_count INTEGER NOT NULL,
    last_aggregated_at TIMESTAMP NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_files_workspace_uri UNIQUE (workspace_id, uri)
);

CREATE TABLE role_workspace_permissions (
    id SERIAL PRIMARY KEY,
    role_id INTEGER NOT NULL REFERENCES roles (id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    permission VARCHAR(32) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_role_workspace UNIQUE (role_id, workspace_id)
);

CREATE TABLE service_tokens (
    id SERIAL PRIMARY KEY,
    secret VARCHAR(256) NOT NULL,
    name VARCHAR(128),
    created_by_user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    permission VARCHAR(16) NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_service_tokens_secret UNIQUE (secret)
);

CREATE TABLE teams (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    description VARCHAR(512),
    owner_id INTEGER NOT NULL REFERENCES users (id),
    workspace_id INTEGER REFERENCES workspaces (id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_teams_name UNIQUE (name)
);

CREATE TABLE workspace_members (
    id SERIAL PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    "role" VARCHAR(32) NOT NULL,
    joined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_workspace_user UNIQUE (workspace_id, user_id)
);

CREATE TABLE document_chunks (
    id BIGSERIAL PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL,
    chunk_id VARCHAR(64) NOT NULL,
    chunk_index INTEGER NOT NULL,
    object_key VARCHAR(1024) NOT NULL,
    object_url VARCHAR(2048),
    local_chunk_path VARCHAR(2048),
    text_preview TEXT,
    page INTEGER NOT NULL,
    level INTEGER NOT NULL,
    block_type VARCHAR(32) NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    bbox_x0 DOUBLE PRECISION,
    bbox_y0 DOUBLE PRECISION,
    bbox_x1 DOUBLE PRECISION,
    bbox_y1 DOUBLE PRECISION,
    source_block_id VARCHAR(128),
    source_char_start INTEGER,
    source_char_end INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_document_chunks_chunk_id UNIQUE (chunk_id),
    CONSTRAINT uq_document_chunks_file_chunk_index UNIQUE (file_id, chunk_index)
);

CREATE TABLE file_permissions (
    id SERIAL PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files (id),
    workspace_id INTEGER REFERENCES workspaces (id),
    entity_type entity_type NOT NULL,
    entity_id INTEGER NOT NULL,
    permission acl_permission NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_file_entity UNIQUE (file_id, entity_type, entity_id)
);

CREATE TABLE share_links (
    id SERIAL PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files (id),
    workspace_id INTEGER REFERENCES workspaces (id),
    token VARCHAR(64) NOT NULL,
    password_hash VARCHAR(255),
    expires_at TIMESTAMP,
    max_access_count INTEGER,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users (id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_share_links_token UNIQUE (token)
);

CREATE TABLE tasks (
    id SERIAL PRIMARY KEY,
    task_id VARCHAR(64) NOT NULL,
    workspace_id INTEGER NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES files (id) ON DELETE SET NULL,
    task_type VARCHAR(32) NOT NULL,
    queue VARCHAR(32) NOT NULL,
    priority INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    progress INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    max_retries INTEGER NOT NULL,
    assigned_at TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    heartbeat_at TIMESTAMP,
    worker_id VARCHAR(64),
    result JSON,
    error TEXT,
    payload JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_tasks_task_id UNIQUE (task_id)
);

CREATE TABLE team_members (
    id SERIAL PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams (id),
    user_id INTEGER NOT NULL REFERENCES users (id),
    "role" team_role NOT NULL,
    joined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_team_user UNIQUE (team_id, user_id)
);
```

## 3) 索引（仅保留与 `UNIQUE`/`PRIMARY KEY` 不重复的显式索引）

```sql
-- audit_logs
CREATE INDEX idx_audit_user_id ON audit_logs (user_id);
CREATE INDEX idx_audit_action ON audit_logs (action);
CREATE INDEX idx_audit_resource ON audit_logs (resource_type, resource_id);
CREATE INDEX idx_audit_created_at ON audit_logs (created_at);

-- workspaces：slug、name 已由 UNIQUE 建索引；仅补充 owner
CREATE INDEX idx_workspace_owner ON workspaces (owner_id);

-- files
CREATE INDEX idx_file_owner_id ON files (owner_id);
CREATE INDEX idx_file_parent_id ON files (parent_id);

-- service_tokens（secret 已由 UNIQUE 建索引）
CREATE INDEX idx_service_tokens_workspace ON service_tokens (workspace_id);
CREATE INDEX idx_service_tokens_created_by ON service_tokens (created_by_user_id);

-- teams
CREATE INDEX ix_teams_owner_id ON teams (owner_id);

-- workspace_members：（workspace_id, user_id）已由 UNIQUE 建索引，不再重复定义 idx_workspace_member

-- document_chunks
CREATE INDEX ix_document_chunks_file_id ON document_chunks (file_id);
CREATE INDEX ix_document_chunks_workspace_id ON document_chunks (workspace_id);

-- file_permissions
CREATE INDEX idx_permission_file_id ON file_permissions (file_id);
CREATE INDEX idx_permission_entity ON file_permissions (entity_type, entity_id);

-- share_links（token 已由 UNIQUE 建索引）
CREATE INDEX idx_share_file_id ON share_links (file_id);
CREATE INDEX idx_share_created_by ON share_links (created_by);

-- tasks
CREATE INDEX ix_tasks_workspace_id ON tasks (workspace_id);
CREATE INDEX ix_tasks_user_id ON tasks (user_id);
CREATE INDEX ix_tasks_file_id ON tasks (file_id);
CREATE INDEX ix_tasks_worker_id ON tasks (worker_id);
CREATE INDEX idx_task_workspace_status ON tasks (workspace_id, status);
CREATE INDEX idx_task_user_id ON tasks (user_id);
CREATE INDEX idx_task_running ON tasks (status, started_at);
CREATE INDEX idx_task_worker ON tasks (worker_id, status);
CREATE INDEX idx_task_heartbeat ON tasks (status, heartbeat_at);
```

## 4) 维护建议

模型、约束或索引变更后，请同步：

1. `OpenRag/src/openrag/models/*.py`  
2. Alembic / `OpenRag/scripts/sql/` 等正式迁移  
3. 本文档（若仍作为人工审阅参考）

**可选**：若希望避免 PostgreSQL 原生 `ENUM` 与后续枚举扩值迁移成本，可在 ORM 中将对应列改为 `String` + `CHECK`；本文档按当前模型使用 `ENUM` 类型编写。
