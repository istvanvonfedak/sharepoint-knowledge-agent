-- Applied by sharepoint_rag.db.migrate() (idempotent). Per-generation chunk tables (chunks_g<N>) are created by
-- sharepoint_rag.generations, because their vector column has a fixed size; don't run this file with psql directly.
CREATE EXTENSION IF NOT EXISTS vector;

-- Admin-managed list of sites to index (`python -m sync_worker sites add ...`).
CREATE TABLE IF NOT EXISTS sites (
    id      text PRIMARY KEY,  -- Graph site id
    name    text NOT NULL,
    web_url text NOT NULL,
    enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS drives (
    id      text PRIMARY KEY,
    site_id text NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    name    text NOT NULL,
    enabled boolean NOT NULL DEFAULT true
);

-- One row per SharePoint file we index (and per folder, so a folder delete can remove its subtree).
-- `acl` is the source of truth for who may read the file; chunk tables hold a copy for the search filter.
CREATE TABLE IF NOT EXISTS sources (
    id          bigserial PRIMARY KEY,
    drive_id    text NOT NULL,
    item_id     text NOT NULL,
    site_id     text NOT NULL,
    web_url     text NOT NULL,
    name        text NOT NULL,
    etag        text,
    acl_hash    text,
    modified_at timestamptz,
    indexed_at  timestamptz,
    status      text NOT NULL DEFAULT 'pending',  -- pending | indexed | skipped | error | folder
    status_note text,
    UNIQUE (drive_id, item_id)
);
ALTER TABLE sources ADD COLUMN IF NOT EXISTS parent_id text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS acl text[] NOT NULL DEFAULT '{}';
ALTER TABLE sources ADD COLUMN IF NOT EXISTS acl_status text NOT NULL DEFAULT 'unresolved';  -- resolved | unresolved
ALTER TABLE sources ADD COLUMN IF NOT EXISTS acl_synced_at timestamptz;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS language text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_crawl_id text;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0;
ALTER TABLE sources ADD COLUMN IF NOT EXISTS index_version int;  -- index_store.INDEX_VERSION that wrote the chunks
CREATE INDEX IF NOT EXISTS sources_parent_idx ON sources (drive_id, parent_id);
CREATE INDEX IF NOT EXISTS sources_status_idx ON sources (status);

-- Graph delta cursor per drive. `next_link` is the page to resume a crawl from; `delta_link` the cursor
-- for the next run. `crawl_id` is set while a full crawl (first run or after an expired cursor) is underway.
CREATE TABLE IF NOT EXISTS sync_state (
    drive_id   text PRIMARY KEY,
    delta_link text,
    synced_at  timestamptz
);
ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS next_link text;
ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS last_error text;
ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS crawl_id text;
ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS index_version int;  -- INDEX_VERSION of the drive's last full crawl

-- An index generation = one embedding model. Live = building or active; exactly one is active at a time.
CREATE TABLE IF NOT EXISTS index_generations (
    id         int PRIMARY KEY,
    model      text NOT NULL,  -- e.g. gemini:gemini-embedding-001
    dimensions int NOT NULL,
    status     text NOT NULL CHECK (status IN ('building', 'active', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS index_generations_one_active ON index_generations ((true)) WHERE status = 'active';

-- Which generations hold a file's chunks, and for which version of the file.
CREATE TABLE IF NOT EXISTS source_generations (
    source_id     bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    generation_id int NOT NULL REFERENCES index_generations(id) ON DELETE CASCADE,
    etag          text,
    PRIMARY KEY (source_id, generation_id)
);

-- Postgres job queue, claimed with FOR UPDATE SKIP LOCKED. `dedupe_key` keeps at most one queued job per key,
-- and a job is not claimed while another with its key runs, so jobs for one drive or file never overlap.
-- `locked_until` is the lease a crashed worker's job expires from.
CREATE TABLE IF NOT EXISTS jobs (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}',
    dedupe_key   text,
    run_at       timestamptz NOT NULL DEFAULT now(),
    attempts     int NOT NULL DEFAULT 0,
    state        text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed')),
    locked_by    text,
    locked_until timestamptz,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
-- 0 runs before 1 (bulk: index_item, backfill_generation), so a big crawl can't hold up purges or permission syncs.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority smallint NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs (run_at) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs (priority, run_at, id) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS jobs_lease_idx ON jobs (locked_until) WHERE state = 'running';
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe_queued_idx ON jobs (dedupe_key) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS jobs_dedupe_running_idx ON jobs (dedupe_key) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS jobs_finished_idx ON jobs (finished_at) WHERE state IN ('done', 'failed');

-- Periodic jobs: one row per schedule; an atomic UPDATE decides which replica enqueues the next run.
CREATE TABLE IF NOT EXISTS schedules (
    name        text PRIMARY KEY,
    last_run_at timestamptz NOT NULL
);

-- Graph throttling shared by all worker replicas: when one gets a 429, all back off until `until`.
CREATE TABLE IF NOT EXISTS graph_throttle (
    id    int PRIMARY KEY CHECK (id = 1),
    until timestamptz NOT NULL
);

-- Daily counters for monitoring (graph_429, layer2_checked, layer2_dropped, agent_tokens, ...).
CREATE TABLE IF NOT EXISTS metrics (
    name  text NOT NULL,
    day   date NOT NULL DEFAULT current_date,
    value bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (name, day)
);

-- mcp-server OAuth state. Tokens and codes are stored as SHA-256 hashes, never in the clear.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id     text PRIMARY KEY,
    info          jsonb NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash text PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('code', 'access', 'refresh')),
    client_id  text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    subject    text,
    data       jsonb NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_subject_idx ON oauth_tokens (subject);
CREATE INDEX IF NOT EXISTS oauth_tokens_expiry_idx ON oauth_tokens (expires_at);

-- Entra sign-ins in progress (OAuth state -> encrypted MSAL flow incl. the PKCE verifier).
CREATE TABLE IF NOT EXISTS oauth_pending (
    state_hash text PRIMARY KEY,
    blob       bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Vectors embedded by a job that has not written its file yet (index_item, backfill_generation), so a retry
-- after a rate limit, crash or lost lease starts where the last attempt stopped. Per file, so one job finishing
-- never clears another's progress; rows go when the file's chunks are written (they hold the vectors from then on),
-- when the file or generation is deleted, or a day after a job last read them (index_store.prune_embedding_progress).
CREATE TABLE IF NOT EXISTS embedding_progress (
    generation_id int NOT NULL REFERENCES index_generations (id) ON DELETE CASCADE,
    source_id     bigint NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    digest        bytea NOT NULL,
    embedding     vector NOT NULL,
    used_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (generation_id, source_id, digest)
);
CREATE INDEX IF NOT EXISTS embedding_progress_used_idx ON embedding_progress (used_at);

-- Per-user MSAL token cache (holds Entra refresh tokens), encrypted with a key kept outside the database.
CREATE TABLE IF NOT EXISTS msal_cache (
    user_id    text PRIMARY KEY,
    blob       bytea NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Per-user rate-limit counters, one row per (user, bucket, window).
CREATE TABLE IF NOT EXISTS usage_limits (
    user_id      text NOT NULL,
    bucket       text NOT NULL,
    window_start timestamptz NOT NULL,
    count        int NOT NULL DEFAULT 0,
    tokens       bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, bucket, window_start)
);

-- What users did. Queries are stored as a keyed HMAC, results as source ids: no content.
CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    user_id     text,
    action      text NOT NULL,  -- search | read | ask | login | logout
    query_hmac  text,
    source_ids  bigint[] NOT NULL DEFAULT '{}',
    tokens_used int NOT NULL DEFAULT 0,
    detail      jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_log_user_idx ON audit_log (user_id, ts);
