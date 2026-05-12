-- Step 1: Create the junction table
CREATE TABLE IF NOT EXISTS service_token_workspaces (
    id SERIAL PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES service_tokens(id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    permission VARCHAR(16) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_stw_token_workspace UNIQUE (token_id, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_stw_token ON service_token_workspaces(token_id);
CREATE INDEX IF NOT EXISTS idx_stw_workspace ON service_token_workspaces(workspace_id);

-- Step 2: Migrate existing bindings (active tokens)
INSERT INTO service_token_workspaces (token_id, workspace_id, permission, created_at)
SELECT id, workspace_id, permission, created_at
FROM service_tokens
WHERE workspace_id IS NOT NULL AND revoked_at IS NULL;

-- Step 3: Migrate revoked tokens (preserve audit history)
INSERT INTO service_token_workspaces (token_id, workspace_id, permission, created_at)
SELECT id, workspace_id, permission, created_at
FROM service_tokens
WHERE workspace_id IS NOT NULL AND revoked_at IS NOT NULL;

-- Step 4: Drop old columns
ALTER TABLE service_tokens DROP COLUMN IF EXISTS workspace_id;
ALTER TABLE service_tokens DROP COLUMN IF EXISTS permission;

-- Step 5: Drop old index
DROP INDEX IF EXISTS idx_service_tokens_workspace;