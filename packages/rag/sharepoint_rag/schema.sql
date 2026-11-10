-- {{DIMENSIONS}} is substituted from EMBEDDING_DIMENSIONS by sharepoint_rag.db.migrate(); don't run this file with psql directly.
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per SharePoint file we index.
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
    status      text NOT NULL DEFAULT 'pending',  -- pending | indexed | skipped | error
    status_note text,
    UNIQUE (drive_id, item_id)
);

-- One row per text chunk. `acl` holds user/group object ids allowed to read the source file.
CREATE TABLE IF NOT EXISTS chunks (
    id           bigserial PRIMARY KEY,
    source_id    bigint NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ordinal      int NOT NULL,
    text         text NOT NULL,
    heading_path text,
    embedding    vector({{DIMENSIONS}}) NOT NULL,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    acl          text[] NOT NULL DEFAULT '{}',
    metadata     jsonb NOT NULL DEFAULT '{}',
    UNIQUE (source_id, ordinal)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_acl_idx ON chunks USING gin (acl);

-- Graph delta cursor per drive, for incremental sync.
CREATE TABLE IF NOT EXISTS sync_state (
    drive_id   text PRIMARY KEY,
    delta_link text,
    synced_at  timestamptz
);
