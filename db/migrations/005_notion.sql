-- Notion 페이지도 documents/chunks/entities 를 그대로 쓴다.
-- 다만 파일 스캔이 "디스크에 없는 문서"를 지우므로 출처를 구분해야 한다.

ALTER TABLE documents ADD COLUMN IF NOT EXISTS source    text NOT NULL DEFAULT 'nas';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS notion_id text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS url       text;

CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source);
CREATE UNIQUE INDEX IF NOT EXISTS documents_notion_id_idx
    ON documents (notion_id) WHERE notion_id IS NOT NULL;
