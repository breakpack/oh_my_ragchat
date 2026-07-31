-- API 키 같은 비밀값. app_settings 와 분리해 둔다.
-- GET /api/settings 는 app_settings 를 통째로 내려주므로, 여기에 섞이면
-- 설정 화면을 여는 것만으로 키가 브라우저까지 나간다. 그래서 테이블을 나눈다.

CREATE TABLE IF NOT EXISTS secrets (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
