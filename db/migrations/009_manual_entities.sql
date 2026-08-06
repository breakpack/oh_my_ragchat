-- 옵시디언에서 손으로 만들거나 고친 엔티티 표시.
-- 이런 엔티티는 청크와 연결이 없어서 prune_orphans 에 지워지는데, 사람이 넣은 것을
-- 색인이 치우면 안 되므로 따로 표시해 둔다.
DO $$
DECLARE s text;
BEGIN
    FOR s IN SELECT schema_name FROM users LOOP
        EXECUTE format(
            'ALTER TABLE %I.entities '
            'ADD COLUMN IF NOT EXISTS manual boolean NOT NULL DEFAULT false', s
        );
    END LOOP;
END $$;
