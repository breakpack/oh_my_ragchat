-- 채팅 세션에 외부 검색(논문·웹) 토글 추가. 세션 테이블은 사용자 스키마마다 하나씩 있다.
DO $$
DECLARE s text;
BEGIN
    FOR s IN SELECT schema_name FROM users LOOP
        EXECUTE format(
            'ALTER TABLE %I.chat_sessions '
            'ADD COLUMN IF NOT EXISTS web_enabled boolean NOT NULL DEFAULT false', s
        );
    END LOOP;
END $$;
