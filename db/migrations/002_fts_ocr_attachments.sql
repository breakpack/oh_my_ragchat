-- 전문검색 / OCR / 채팅 첨부 / 인덱싱 진행률
-- 마이그레이션은 항상 멱등하게 쓴다 (api 기동 때마다 검사 후 적용).

-- ── 전문검색: 청크 본문 trigram 인덱스 ──
-- 한국어는 기본 tsvector 사전이 없어 to_tsvector 가 사실상 무의미하다.
-- pg_trgm GIN 인덱스는 ILIKE '%…%' 를 가속해주므로 고유명사 정확 매칭에 이쪽이 낫다.
CREATE INDEX IF NOT EXISTS chunks_content_trgm_idx
    ON chunks USING gin (content gin_trgm_ops);

-- ── OCR ──
ALTER TABLE documents ADD COLUMN IF NOT EXISTS ocr boolean NOT NULL DEFAULT false;

-- ── 인덱싱 진행률 (worker 가 갱신, UI 가 실시간 표시) ──
ALTER TABLE documents ADD COLUMN IF NOT EXISTS progress_done  int NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS progress_total int NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS phase text;

-- ── 채팅 첨부 ──
-- [{"kind":"image","name":"a.png","path":"documents/a.png"} , …]
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS attachments jsonb;
