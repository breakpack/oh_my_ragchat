-- 단일 사용자 → 다중 사용자.
-- public 에는 users / jobs / schema_migrations 만 남기고, 나머지는 사용자 스키마(u<id>)로 옮긴다.

CREATE TABLE IF NOT EXISTS users (
    id            bigserial PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    display_name  text,
    password_hash bytea NOT NULL,
    salt          bytea NOT NULL,
    is_admin      boolean NOT NULL DEFAULT false,
    schema_name   text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- 기존 단일 계정을 첫 사용자(admin)로 승격. 비밀번호는 그대로 쓴다.
INSERT INTO users (id, username, display_name, password_hash, salt, is_admin, schema_name)
SELECT 1, 'admin', '관리자', password_hash, salt, true, 'u1'
  FROM auth_user
 WHERE id = 1 AND password_hash IS NOT NULL
ON CONFLICT (id) DO NOTHING;

SELECT setval(pg_get_serial_sequence('users', 'id'), GREATEST((SELECT COALESCE(max(id), 1) FROM users), 1));

-- 기존 데이터를 u1 로 이동 (계정이 있을 때만)
DO $$
DECLARE t text;
BEGIN
    IF EXISTS (SELECT 1 FROM users WHERE id = 1) THEN
        EXECUTE 'CREATE SCHEMA IF NOT EXISTS u1';
        FOREACH t IN ARRAY ARRAY[
            'app_settings','secrets','path_flags','personas','chat_sessions','chat_messages',
            'documents','chunks','entities','relations','chunk_entities','llm_usage'
        ] LOOP
            IF EXISTS (SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = t) THEN
                EXECUTE format('ALTER TABLE public.%I SET SCHEMA u1', t);
            END IF;
        END LOOP;
    END IF;
END $$;

-- 잡 큐는 워커가 사용자 구분 없이 하나로 돌리므로 public 에 남기고 user_id 를 붙인다.
DROP TABLE IF EXISTS public.jobs;
CREATE TABLE public.jobs (
    id         bigserial PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    status     text NOT NULL DEFAULT 'queued',
    attempts   int NOT NULL DEFAULT 0,
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    done_at    timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON public.jobs (status, id) WHERE status = 'queued';
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe_idx
    ON public.jobs (user_id, kind, (payload->>'path'))
 WHERE status IN ('queued', 'running');

DROP TABLE IF EXISTS public.auth_user;
