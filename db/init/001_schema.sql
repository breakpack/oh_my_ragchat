CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ─────────────────────────────── 설정 / 인증 ───────────────────────────────

CREATE TABLE app_settings (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);

CREATE TABLE auth_user (
    id            int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    password_hash bytea,
    salt          bytea,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
INSERT INTO auth_user (id) VALUES (1);

-- ───────────────────────── NAS 경로 플래그 (숨김 / 잠금) ─────────────────────────
-- path 는 NAS 루트 기준 상대경로. 폴더와 파일 공용.

CREATE TABLE path_flags (
    path      text PRIMARY KEY,
    is_dir    boolean NOT NULL DEFAULT false,
    hidden    boolean NOT NULL DEFAULT false,
    lock_hash bytea,
    lock_salt bytea,
    note      text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX path_flags_hidden_idx ON path_flags (hidden) WHERE hidden;
CREATE INDEX path_flags_locked_idx ON path_flags (path) WHERE lock_hash IS NOT NULL;

-- ─────────────────────────────── 채팅 ───────────────────────────────

CREATE TABLE personas (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL,
    system_prompt text NOT NULL DEFAULT '',
    model         text,
    temperature   real,
    is_default    boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX personas_one_default ON personas (is_default) WHERE is_default;

CREATE TABLE chat_sessions (
    id          bigserial PRIMARY KEY,
    title       text NOT NULL DEFAULT '새 대화',
    persona_id  bigint REFERENCES personas(id) ON DELETE SET NULL,
    model       text,
    rag_enabled boolean NOT NULL DEFAULT false,
    rag_mode    text NOT NULL DEFAULT 'hybrid',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chat_sessions_updated_idx ON chat_sessions (updated_at DESC);

CREATE TABLE chat_messages (
    id         bigserial PRIMARY KEY,
    session_id bigint NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role       text NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content    text NOT NULL DEFAULT '',
    thinking   text,
    citations  jsonb,
    model      text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chat_messages_session_idx ON chat_messages (session_id, id);

-- ─────────────────────────────── RAG ───────────────────────────────
-- status: pending | extracting | embedding | graphing | ready | error | skipped

CREATE TABLE documents (
    id          bigserial PRIMARY KEY,
    path        text NOT NULL UNIQUE,
    mtime       double precision,
    size        bigint,
    sha256      text,
    status      text NOT NULL DEFAULT 'pending',
    chunk_count int NOT NULL DEFAULT 0,
    error       text,
    indexed_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX documents_status_idx ON documents (status);

CREATE TABLE chunks (
    id          bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord         int NOT NULL,
    content     text NOT NULL,
    token_est   int NOT NULL DEFAULT 0,
    embedding   vector(1024),
    UNIQUE (document_id, ord)
);
CREATE INDEX chunks_embedding_idx ON chunks
    USING hnsw (embedding vector_cosine_ops);

CREATE TABLE entities (
    id          bigserial PRIMARY KEY,
    name_norm   text NOT NULL UNIQUE,
    name        text NOT NULL,
    type        text NOT NULL DEFAULT 'unknown',
    description text NOT NULL DEFAULT '',
    embedding   vector(1024),
    degree      int NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entities_embedding_idx ON entities
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX entities_name_trgm_idx ON entities USING gin (name gin_trgm_ops);

CREATE TABLE relations (
    id          bigserial PRIMARY KEY,
    src_id      bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    tgt_id      bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    description text NOT NULL DEFAULT '',
    keywords    text NOT NULL DEFAULT '',
    weight      real NOT NULL DEFAULT 1.0,
    embedding   vector(1024),
    UNIQUE (src_id, tgt_id)
);
CREATE INDEX relations_src_idx ON relations (src_id);
CREATE INDEX relations_tgt_idx ON relations (tgt_id);
CREATE INDEX relations_embedding_idx ON relations
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX relations_keywords_trgm_idx ON relations USING gin (keywords gin_trgm_ops);

CREATE TABLE chunk_entities (
    chunk_id  bigint NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    entity_id bigint NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (chunk_id, entity_id)
);
CREATE INDEX chunk_entities_entity_idx ON chunk_entities (entity_id);

-- ───────────────────────── 잡 큐 (SKIP LOCKED 소비) ─────────────────────────
-- kind: index_document | delete_document | reindex_all

CREATE TABLE jobs (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    status     text NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    attempts   int NOT NULL DEFAULT 0,
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    done_at    timestamptz
);
CREATE INDEX jobs_queue_idx ON jobs (status, id) WHERE status = 'queued';
CREATE UNIQUE INDEX jobs_dedupe_idx ON jobs (kind, (payload->>'path'))
    WHERE status IN ('queued', 'running');

-- ─────────────────────────────── 시드 ───────────────────────────────

INSERT INTO personas (name, system_prompt, is_default) VALUES
    ('기본',
     '당신은 사용자의 개인 지식 베이스를 돕는 유능한 한국어 어시스턴트입니다. 간결하고 정확하게 답하세요. 모르는 것은 모른다고 말하세요.',
     true),
    ('문서 분석가',
     '당신은 문서 분석 전문가입니다. 제공된 자료에 근거해서만 답하고, 각 주장 끝에 [S1] 형태로 출처를 표기하세요. 자료에 없는 내용은 "제공된 문서에서 확인할 수 없습니다"라고 답하세요.',
     false),
    ('코드 리뷰어',
     '당신은 시니어 소프트웨어 엔지니어입니다. 코드의 버그, 경계 조건, 성능 문제를 우선 지적하고 구체적인 수정 코드를 제시하세요.',
     false);
