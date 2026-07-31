-- 외부 LLM(DeepSeek) 토큰 사용량. 예산 상한과 사용량 표시에 쓴다.

CREATE TABLE IF NOT EXISTS llm_usage (
    id                bigserial PRIMARY KEY,
    provider          text NOT NULL,
    model             text NOT NULL,
    prompt_tokens     int NOT NULL DEFAULT 0,
    completion_tokens int NOT NULL DEFAULT 0,
    cached_tokens     int NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_usage_created_idx ON llm_usage (created_at DESC);
CREATE INDEX IF NOT EXISTS llm_usage_provider_idx ON llm_usage (provider, created_at DESC);
