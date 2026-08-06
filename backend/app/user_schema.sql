-- 사용자 한 명의 데이터 전부. 새 사용자를 만들 때 스키마 u<id> 안에 이대로 만든다.
-- {schema} 는 코드에서 치환한다.
--
-- 왜 스키마를 나누나: 모든 테이블에 user_id 를 붙이고 40여 개 쿼리에 조건을 추가하면
-- 한 군데만 빠뜨려도 남의 문서가 새어 나온다. search_path 로 나누면 기존 쿼리를
-- 한 줄도 안 고치고 격리된다.

CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.app_settings (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS {schema}.secrets (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS {schema}.path_flags (
    path       text PRIMARY KEY,
    is_dir     boolean NOT NULL DEFAULT false,
    hidden     boolean NOT NULL DEFAULT false,
    lock_hash  bytea,
    lock_salt  bytea,
    note       text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS path_flags_hidden_idx ON {schema}.path_flags (hidden) WHERE hidden;
CREATE INDEX IF NOT EXISTS path_flags_locked_idx ON {schema}.path_flags (path) WHERE lock_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS {schema}.personas (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL,
    system_prompt text NOT NULL DEFAULT '',
    model         text,
    temperature   real,
    is_default    boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS personas_one_default ON {schema}.personas (is_default) WHERE is_default;

CREATE TABLE IF NOT EXISTS {schema}.chat_sessions (
    id          bigserial PRIMARY KEY,
    title       text NOT NULL DEFAULT '새 대화',
    persona_id  bigint REFERENCES {schema}.personas(id) ON DELETE SET NULL,
    model       text,
    rag_enabled boolean NOT NULL DEFAULT false,
    rag_mode    text NOT NULL DEFAULT 'hybrid',
    web_enabled boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_sessions_updated_idx ON {schema}.chat_sessions (updated_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.chat_messages (
    id          bigserial PRIMARY KEY,
    session_id  bigint NOT NULL REFERENCES {schema}.chat_sessions(id) ON DELETE CASCADE,
    role        text NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content     text NOT NULL DEFAULT '',
    thinking    text,
    citations   jsonb,
    model       text,
    attachments jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session_idx ON {schema}.chat_messages (session_id, id);

CREATE TABLE IF NOT EXISTS {schema}.documents (
    id             bigserial PRIMARY KEY,
    path           text NOT NULL UNIQUE,
    mtime          double precision,
    size           bigint,
    sha256         text,
    status         text NOT NULL DEFAULT 'pending',
    chunk_count    int NOT NULL DEFAULT 0,
    error          text,
    indexed_at     timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    ocr            boolean NOT NULL DEFAULT false,
    progress_done  int NOT NULL DEFAULT 0,
    progress_total int NOT NULL DEFAULT 0,
    phase          text,
    source         text NOT NULL DEFAULT 'nas',
    notion_id      text,
    url            text
);
CREATE INDEX IF NOT EXISTS documents_status_idx ON {schema}.documents (status);
CREATE INDEX IF NOT EXISTS documents_source_idx ON {schema}.documents (source);
CREATE UNIQUE INDEX IF NOT EXISTS documents_notion_id_idx
    ON {schema}.documents (notion_id) WHERE notion_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS {schema}.chunks (
    id          bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES {schema}.documents(id) ON DELETE CASCADE,
    ord         int NOT NULL,
    content     text NOT NULL,
    token_est   int NOT NULL DEFAULT 0,
    embedding   public.vector(1024),
    UNIQUE (document_id, ord)
);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON {schema}.chunks
    USING hnsw (embedding public.vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_content_trgm_idx ON {schema}.chunks
    USING gin (content public.gin_trgm_ops);

CREATE TABLE IF NOT EXISTS {schema}.entities (
    id          bigserial PRIMARY KEY,
    name_norm   text NOT NULL UNIQUE,
    name        text NOT NULL,
    type        text NOT NULL DEFAULT 'unknown',
    description text NOT NULL DEFAULT '',
    embedding   public.vector(1024),
    degree      int NOT NULL DEFAULT 0,
    -- 사람이 옵시디언에서 만들거나 고친 것. 청크 연결이 없어도 정리 대상에서 뺀다.
    manual      boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entities_embedding_idx ON {schema}.entities
    USING hnsw (embedding public.vector_cosine_ops);
CREATE INDEX IF NOT EXISTS entities_name_trgm_idx ON {schema}.entities
    USING gin (name public.gin_trgm_ops);

CREATE TABLE IF NOT EXISTS {schema}.relations (
    id          bigserial PRIMARY KEY,
    src_id      bigint NOT NULL REFERENCES {schema}.entities(id) ON DELETE CASCADE,
    tgt_id      bigint NOT NULL REFERENCES {schema}.entities(id) ON DELETE CASCADE,
    description text NOT NULL DEFAULT '',
    keywords    text NOT NULL DEFAULT '',
    weight      real NOT NULL DEFAULT 1.0,
    embedding   public.vector(1024),
    UNIQUE (src_id, tgt_id)
);
CREATE INDEX IF NOT EXISTS relations_src_idx ON {schema}.relations (src_id);
CREATE INDEX IF NOT EXISTS relations_tgt_idx ON {schema}.relations (tgt_id);
CREATE INDEX IF NOT EXISTS relations_embedding_idx ON {schema}.relations
    USING hnsw (embedding public.vector_cosine_ops);
CREATE INDEX IF NOT EXISTS relations_keywords_trgm_idx ON {schema}.relations
    USING gin (keywords public.gin_trgm_ops);

CREATE TABLE IF NOT EXISTS {schema}.chunk_entities (
    chunk_id  bigint NOT NULL REFERENCES {schema}.chunks(id) ON DELETE CASCADE,
    entity_id bigint NOT NULL REFERENCES {schema}.entities(id) ON DELETE CASCADE,
    PRIMARY KEY (chunk_id, entity_id)
);
CREATE INDEX IF NOT EXISTS chunk_entities_entity_idx ON {schema}.chunk_entities (entity_id);

CREATE TABLE IF NOT EXISTS {schema}.llm_usage (
    id                bigserial PRIMARY KEY,
    provider          text NOT NULL,
    model             text NOT NULL,
    prompt_tokens     int NOT NULL DEFAULT 0,
    completion_tokens int NOT NULL DEFAULT 0,
    cached_tokens     int NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_usage_created_idx ON {schema}.llm_usage (created_at DESC);
CREATE INDEX IF NOT EXISTS llm_usage_provider_idx ON {schema}.llm_usage (provider, created_at DESC);

INSERT INTO {schema}.personas (name, system_prompt, is_default)
SELECT '기본',
       '당신은 사용자의 개인 지식 베이스를 돕는 유능한 한국어 어시스턴트입니다. 간결하고 정확하게 답하세요. 모르는 것은 모른다고 말하세요.',
       true
WHERE NOT EXISTS (SELECT 1 FROM {schema}.personas);
