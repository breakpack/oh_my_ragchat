-- 모델·외부 API 처럼 서버 전체에 적용되는 설정은 관리자만 바꾸고 모두가 공유한다.
-- 나머지(감시 폴더, RAG 파라미터, 화면 취향)는 사용자 스키마의 app_settings 에 남는다.

CREATE TABLE IF NOT EXISTS public.global_settings (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS public.global_secrets (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 첫 관리자(u1)가 이미 저장해 둔 값이 있으면 전역으로 승격
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'u1' AND table_name = 'app_settings') THEN
        INSERT INTO public.global_settings (key, value)
        SELECT key, value FROM u1.app_settings
         WHERE key IN ('ollama_base_url','chat_model','extract_model','embed_model','num_ctx',
                       'extract_provider','extract_max_entities','extract_max_relations',
                       'deepseek_model','deepseek_base_url','deepseek_concurrency',
                       'deepseek_max_input_chars','deepseek_max_output_tokens',
                       'deepseek_token_budget','rag_ocr_enabled','rag_ocr_langs','rag_ocr_min_chars')
        ON CONFLICT (key) DO NOTHING;

        DELETE FROM u1.app_settings
         WHERE key IN (SELECT key FROM public.global_settings);
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'u1' AND table_name = 'secrets') THEN
        INSERT INTO public.global_secrets (key, value)
        SELECT key, value FROM u1.secrets WHERE key = 'deepseek_api_key'
        ON CONFLICT (key) DO NOTHING;
        DELETE FROM u1.secrets WHERE key = 'deepseek_api_key';
    END IF;
END $$;
