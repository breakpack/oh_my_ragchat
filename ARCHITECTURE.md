# chatchat — NAS + Graph RAG + 로컬 LLM 채팅

Mac mini M4 (32GB) 에서 Docker Compose 로 돌아가는 다중 사용자 웹서비스.

## 0. 사용자 격리

사용자마다 **Postgres 스키마 하나(`u<id>`)와 저장소 디렉터리 하나**를 갖는다.

```
public          users, jobs(user_id), schema_migrations
u1 / u2 / …     app_settings, secrets, path_flags, personas, chat_*, documents,
                chunks, entities, relations, chunk_entities, llm_usage
저장소           <NAS_HOST_PATH>/<username>/{nas,trash,tmp}
```

모든 테이블에 `user_id` 를 붙이는 대신 스키마를 나눈 이유는 단순하다. 조건을 붙여야 할
쿼리가 40개가 넘고, 한 군데만 빠뜨려도 남의 문서가 새어 나간다. 스키마를 나누면
`search_path` 하나로 격리되고 기존 쿼리는 손대지 않아도 된다.

요청마다 `deps.current_user` 가 쿠키에서 사용자를 복원해 `ctx` (ContextVar)에 심고,
`db.cursor()` 가 그걸 읽어 `search_path` 를 건다. 워커는 잡의 `user_id` 로 같은 일을 한다.

> `current_user` 는 반드시 `async` 여야 한다. 동기 의존성은 각각 별도 스레드에서
> 컨텍스트 *복사본* 위로 돌기 때문에, 거기서 ContextVar 를 세팅해도 나머지 요청
> 처리에는 보이지 않는다(실제로 이것 때문에 500 이 났다).

첫 사용자는 자동으로 관리자가 되고, 이후 계정은 관리자만 만든다(공개 가입 없음).

## 1. 실행 형태

```
브라우저 ──→ localhost:3040 ──→ [web: nginx]
                                  ├── /            → React 정적 빌드
                                  ├── /api/*       → api:8000  (FastAPI)
                                  └── /api/chat/*  → api:8000  (SSE, 버퍼링 off)

[api]    FastAPI / uvicorn        ─┐
[worker] 폴더 감시 + 인덱싱 잡      ─┼──→ [db] postgres 17 + pgvector
                                    └──→ host.docker.internal:11434 (호스트 Ollama)
```

### Ollama 를 컨테이너에 넣지 않는 이유

macOS 의 Docker 는 Metal GPU 를 컨테이너에 노출하지 못한다. 컨테이너 안 Ollama 는
CPU 추론만 가능해서 27B 급 모델이 실사용 불가 속도가 된다. 따라서 Ollama 는 호스트에
네이티브로 두고(`brew services start ollama`), 컨테이너에서 `host.docker.internal:11434`
로 접속한다. `docker-compose.yml` 에는 리눅스 이전 대비용 ollama 서비스가 주석으로만 들어있다.

## 2. 컨테이너

| 서비스 | 이미지 / 빌드 | 역할 | 포트 |
|---|---|---|---|
| `web` | `nginx:alpine` (frontend 멀티스테이지 빌드) | 정적 서빙 + 리버스 프록시 | **3040** → 80 |
| `api` | `backend/Dockerfile` | REST + SSE | 내부 8000 |
| `worker` | 같은 이미지, `command: python -m app.worker` | 폴더 감시, 인덱싱 잡 소비 | - |
| `db` | `pgvector/pgvector:pg17` | 메타데이터 + 벡터 + 그래프 | 내부 5432 (기본 미노출) |

외부로 열리는 포트는 3040 하나뿐이다.

## 3. 스토리지 레이아웃

호스트 `./data` 를 bind-mount 한다. 실제 파일은 항상 호스트 파일시스템에 평문으로 존재하므로
Time Machine / rsync 로 그대로 백업된다.

```
data/
├── nas/                 → 컨테이너 /data/nas   (NAS 루트, 파일 매니저가 보는 전부)
│   └── documents/       → RAG 감시 대상 기본 폴더
├── trash/               → 삭제 시 이동되는 휴지통
└── tmp/                 → 업로드 임시 파일
pgdata (named volume)    → 컨테이너 /var/lib/postgresql/data
```

경로 규칙: API 가 주고받는 경로는 **항상 NAS 루트 기준 상대 경로**(`documents/a/b.pdf`).
`app/files/paths.py` 의 `resolve()` 가 심볼릭 링크 해석 후 루트 밖으로 나가는 경로를 거부한다.
(`..` 트래버설, 루트 밖을 가리키는 symlink 모두 차단)

## 4. 데이터 모델 (`db/init/001_schema.sql`)

```
schema_migrations(name, applied_at)            -- db/migrations/*.sql 적용 이력
app_settings(key, value jsonb)                 -- 단일 사용자 설정 KV
auth_user(id=1, password_hash, salt)           -- 단일 계정
path_flags(path, hidden bool, lock_hash, lock_salt, note)
                                               -- 7번 숨김 / 8번 파일 잠금. 폴더·파일 공용
personas(id, name, system_prompt, model, temperature, is_default)
chat_sessions(id, title, persona_id, model, rag_enabled, rag_mode, created_at)
chat_messages(id, session_id, role, content, thinking, citations jsonb, created_at)

documents(id, path, mtime, size, sha256, status, chunk_count, error, indexed_at)
chunks(id, document_id, ord, content, token_est, embedding vector(1024))
entities(id, name_norm, name, type, description, embedding vector(1024), degree)
relations(id, src_id, tgt_id, description, keywords, weight)
chunk_entities(chunk_id, entity_id)            -- 청크 ↔ 엔티티 역인덱스
jobs(id, kind, payload jsonb, status, attempts, error, created_at, started_at, done_at)
llm_usage(id, provider, model, prompt_tokens, completion_tokens, cached_tokens, created_at)
secrets(key, value, updated_at)                -- API 키. app_settings 와 분리 (아래 5-5 참고)
```

- 벡터 인덱스: `chunks.embedding`, `entities.embedding` 에 HNSW (`vector_cosine_ops`).
- 그래프는 Neo4j / Apache AGE 없이 `entities` + `relations` 관계 테이블로 두고,
  이웃 확장은 재귀 CTE (`depth <= 2`) 로 처리한다. 단일 사용자 규모에서 충분하고
  pgvector 와 한 이미지에서 공존시킬 수 있다.
- `jobs` 는 `FOR UPDATE SKIP LOCKED` 로 소비하는 DB 큐. Redis/Celery 불필요.
- **스키마 변경**: `001_schema.sql` 은 새 볼륨에서만 도는 initdb 베이스라인이다. 이미 데이터가
  있는 설치본에도 반영하려면 `db/migrations/*.sql` 에 **멱등하게** 쓴다(`ADD COLUMN IF NOT
  EXISTS` 등). api 가 기동할 때 `app/migrate.py` 가 미적용분만 순서대로 실행한다.
- `chunks.content` 에 GIN trigram 인덱스(전문검색), `documents` 에 `ocr` /
  `progress_done` / `progress_total` / `phase`, `chat_messages` 에 `attachments jsonb`.

## 5. Graph RAG 파이프라인 (LightRAG 방식)

### 인덱싱 (worker)

1. **감시** — 워커가 감시 폴더(설정값, 기본 `documents/`)를 주기적으로(기본 30초,
   `RAG_SCAN_SECONDS`) 훑어 `documents` 테이블의 mtime+size 와 대조하고 차이만 큐에 넣는다.
   macOS 호스트의 bind mount 에서는 inotify 이벤트가 컨테이너로 전달되지 않아 `watchdog`
   방식이 동작하지 않으므로 대조 스캔을 쓴다. API 를 통한 업로드·삭제·이동은 스캔을
   기다리지 않고 즉시 잡을 건다.
2. **추출** — 확장자별 텍스트 추출: `.txt .md`(평문, charset 자동감지), `.pdf`(pypdf),
   `.docx`(python-docx). 그 외는 skip 상태로 기록.
   - **OCR**: PDF 는 텍스트 레이어가 `rag_ocr_min_chars`(기본 80자) 미만이면 스캔본으로 보고
     pypdfium2 로 페이지를 렌더링해 tesseract 에 넘긴다. 이미지 파일(`.png .jpg` …)은 바로 OCR.
     tesseract 기본 PSM 3 은 여백이 많은 페이지를 "Empty page" 로 버리는 일이 잦아
     **6(단일 블록) → 4(다단) → 11(성긴 텍스트)** 순으로 재시도하고, 짧은 변이 1400px 미만이면
     확대한 뒤 grayscale+autocontrast 를 건다. `documents.ocr` 에 사용 여부를 남긴다.
3. **청킹** — 문단 경계 우선, 목표 1200자 / 오버랩 150자.
4. **임베딩** — Ollama `/api/embed`, 모델 `bge-m3` (1024차원, 한국어 강함). 배치 16.
5. **엔티티/관계 추출** — 청크마다 LLM 1회 호출. 구조화 출력(JSON)으로
   `entities[{name,type,description}]`, `relations[{src,tgt,description,keywords,weight}]`.
   추출 모델은 채팅 모델과 분리 설정(기본 `gemma4:e4b` 처럼 빠른 모델).
   - **제공자 선택** (`extract_provider`): `local`(Ollama) 또는 `deepseek`(외부 API).
     로컬은 청크당 수 초라 논문 PDF 한 건(수십 청크)에도 수 분이 든다. DeepSeek 은
     OpenAI 호환 `json_object` 모드로 호출하고 **청크를 동시에 N개씩** 보낸다
     (`deepseek_concurrency`, 기본 4 → 실측 4배).
   - **비용 억제**: 입력은 `deepseek_max_input_chars`(4000자)로 자르고, 출력은
     `deepseek_max_output_tokens`(900)과 엔티티/관계 개수 상한으로 묶는다. 응답의
     `usage` 를 `llm_usage` 에 적재하고 `deepseek_token_budget` 누적 상한을 넘으면
     **자동으로 로컬로 되돌아간다**. 개별 호출이 실패해도 그 청크만 로컬로 대체한다.
   - **키 보관**: 웹(설정 화면)에서 입력하면 `secrets` 테이블에 저장한다. `app_settings`
     에 두지 않는 이유는 `GET /api/settings` 가 설정을 통째로 내려주기 때문이다 — 같이
     두면 설정 화면을 여는 것만으로 키가 브라우저까지 나간다. 조회 API 는 마스킹된 값만
     주고 원문 반환 경로는 없다. `DEEPSEEK_API_KEY` 환경변수가 있으면 그쪽이 우선하며
     이때 웹 변경은 400 으로 거부한다.
   - **DB 병합은 항상 메인 스레드에서 순차로** 한다. 추출만 병렬이고, 엔티티 upsert 를
     동시에 돌리면 교착이 날 수 있어서다.
6. **머지** — 엔티티는 정규화된 이름(`lower`+공백정리)으로 upsert, description 을
   누적 후 `name + description` 을 임베딩. 관계는 (src,tgt) 로 weight 누적.

문서 1건 인덱싱은 청크 수 × LLM 1회이므로 로컬 모델 기준 수십 초~분 단위다. UI 는
`documents.status` (`pending → extracting → embedding → graphing → ready | error | skipped`)
로 진행 상황을 폴링해 보여준다.

### 검색 (api, `app/rag/retrieve.py`)

모드는 4가지이며 채팅 세션별로 고를 수 있다.

| 모드 | 동작 |
|---|---|
| `naive` | 질문 임베딩 → `chunks` 코사인 top-k. 가장 빠름 |
| `local` | 질문 임베딩 → `entities` top-k → 그 엔티티의 이웃(1~2홉) + 연결 청크 |
| `global` | 질문 키워드 → `relations` 매칭 → 양끝 엔티티 + 연결 청크 |
| `hybrid` | local ∪ global ∪ naive ∪ **키워드** 를 dedupe 후 랭킹 (기본값) |

**키워드(전문검색) 브랜치** — 임베딩은 고유명사·모델명·숫자처럼 "정확히 그 글자"를 찾는
질문에 약하다. `chunks.content` 의 GIN trigram 인덱스로 `ILIKE '%term%'` 매칭을 병행하고,
점수는 `벡터 유사도 + 적중 키워드 수 × 0.05` 로 매겨 벡터 랭킹을 크게 흔들지 않게 한다.
`rag_use_keyword` 로 끌 수 있으며 hybrid 모드에서만 동작한다(naive 는 순수 벡터로 유지).

컨텍스트는 `-----Entities-----` / `-----Relations-----` / `-----Sources-----`
세 블록의 CSV 유사 형태로 조립하고, 각 소스에 `[S1]` 형태 인용 태그를 붙여
답변에서 근거를 참조하게 한다. 인용은 `chat_messages.citations` 에 저장한다.

## 6. 채팅

- 전송: `POST /api/chat/sessions/{id}/messages` → **SSE** 스트림 응답
  (`fetch` + `ReadableStream` 으로 소비. `EventSource` 는 POST 불가하므로 사용 안 함)
- 이벤트: `meta`(사용 모델/RAG 여부) → `citation`* → `token`* → `done` | `error`
- `thinking` 능력이 있는 모델(qwen3.6, gemma4)은 사고 과정을 별도 이벤트로 분리해
  접이식 UI 에 표시.
- RAG 토글은 세션 단위 기본값 + 메시지 단위 오버라이드 둘 다 지원. 끄면 순수 일반 채팅.
- 페르소나: `system_prompt` + 모델 + temperature 묶음. 세션 생성 시 선택.
- 프롬프트 우선순위: 페르소나 system prompt → RAG 컨텍스트 지시문 → 대화 히스토리.
- 히스토리는 최근 N 턴(설정값, 기본 12턴)만 전송.
- **모델 선택**: 로컬(Ollama)과 외부 API 모델을 같은 목록에서 고른다. 외부 모델은 이름 앞에
  제공자 접두어를 붙여 구분하고(`deepseek/deepseek-chat`, `openai/gpt-…`,
  `anthropic/claude-…`), 라우터가 그 접두어를 보고 스트리밍 경로를 나눈다.
  - `deepseek.py` — DeepSeek. 그래프 추출까지 겸한다. `reasoning_content` 를 thinking
    이벤트로 매핑. vision 이 없어 첨부 이미지는 보내지 않고 OCR 텍스트만 넘긴다.
  - `remote.py` — OpenAI · Claude(채팅 전용). **모델 이름은 상수로 박지 않고 각 제공자의
    모델 API 로 조회**해 5분 캐시한다(하드코딩하면 금방 낡는다). 둘 다 vision 이 되므로
    첨부 이미지를 base64 블록으로 실어 보낸다(확장자를 잃어 base64 앞머리로 종류를 추정).
    Claude 는 공식 `anthropic` SDK 로 스트리밍하고 4.6+ 모델에만 adaptive thinking 을
    붙인다(구형은 `budget_tokens` 방식이라 400 이 난다). 최신 모델이 기본값 외
    `temperature` 를 거부하므로 Claude 에는 아예 안 보내고, OpenAI 는 400 이 나면
    한 번 빼고 재시도한다.
  - 키는 `public.global_secrets` 에 두고 관리자만 바꾼다. 어떤 API 로도 원문을 돌려주지 않는다.
  - Ollama 가 꺼져 있어도 `/api/settings/models` 는 외부 모델만이라도 돌려준다.
- **웹·논문 검색** (`websearch.py`): 세션 토글로 켠다. 모델 제공자가 내장한 web search
  툴을 쓰면 모델마다 되고 안 되고가 갈리므로, 검색을 백엔드에서 직접 하고 결과를
  `[W1]` 블록으로 시스템 프롬프트에 넣는다 — 로컬 Ollama 모델에서도 똑같이 돈다.
  - 소스: OpenAlex · Crossref · arXiv(논문) + DuckDuckGo(웹). 전부 키가 필요 없다.
  - 소스별 결과를 **번갈아** 합쳐(한 소스 독점 방지) URL·정규화 제목으로 중복을 거른다.
  - 한글 질문은 논문 DB 에서 안 잡힌다. 추출 모델로 주제어만 영어로 뽑아 논문 검색에
    쓰고, 일반 웹은 원문 그대로 던진다. 변환이 실패해도 원문으로 계속한다.
  - arXiv 는 `all:a b c` 로 단어를 흩뿌리면 엉뚱한 논문을 준다. 구문(`all:"..."`)으로
    먼저 찾고 0건일 때만 단어 검색으로 물러선다.
  - 웹 결과 상위 N 건은 본문까지 받아 태그를 털어 넣는다(스니펫만으로는 근거가 얇다).
  - RAG 와 동시에 켤 수 있고, 둘은 `asyncio.gather` 로 같이 돈다. 한쪽이 실패해도
    나머지로 답하고 실패는 `meta` 이벤트의 stats 로 알린다.
- **첨부**: NAS 경로 또는 브라우저 업로드(base64) 둘 다 받는다. 이미지는 Ollama 메시지의
  `images` 필드로, 그 외는 추출한 본문을 프롬프트에 인라인한다.
  이미지는 **OCR 결과도 함께** 넘긴다 — `vision` 능력을 광고하면서 실제로는 이미지를
  처리하지 못하는 모델이 있어서다(`qwen3.6:27b-mlx` 에서 확인). `chat_attach_ocr_images` 로 끈다.

### 인덱싱 진행률 실시간화

worker 와 api 는 별도 컨테이너라 메모리를 공유할 수 없다. 브로커를 추가하는 대신
이미 있는 Postgres 의 **LISTEN/NOTIFY** 를 쓴다.

```
worker ──pg_notify('chatchat_events', json)──→ db
                                                │ LISTEN (전용 async 커넥션)
api  ←───────────────────────────────────────────┘
  └── GET /api/rag/events (SSE) ──→ 브라우저 EventSource
```

이벤트는 `document`(상태 전환) / `progress`(단계별 done·total) / `scan`(신규·삭제 수).
페이로드 8000바이트 제한이 있어 요약 정보만 담고, 프론트는 30초 폴링을 안전망으로 함께 둔다.

## 7. 보안 모델

- **로그인**: 아이디+비밀번호. `hashlib.scrypt` (stdlib, 외부 의존성 없음) 로 해시.
  성공 시 `itsdangerous` 로 서명한 HttpOnly 세션 쿠키 발급(기본 30일).
  첫 실행 시 비밀번호 미설정 상태이며 최초 접속에서 설정한다.
- **7번 숨김 폴더**: `path_flags.hidden`. 목록 API 는 기본적으로 숨김 항목을 제외하고,
  `?show_hidden=1` + 세션 인증이 있을 때만 포함한다. 숨김 폴더 내부는 RAG 인덱싱에서도 제외.
- **8번 파일 잠금**: `path_flags.lock_hash`. 잠긴 파일은 목록에는 보이지만
  `GET /api/files/content` 시 `X-File-Password` 헤더 검증을 요구한다. 통과 시 10분짜리
  해제 토큰을 세션에 기록. **디스크상 파일은 평문**이므로 터미널/Finder 직접 접근은 막지
  못한다(단일 사용자 로컬 환경 전제, 실제 암호화는 백업·검색·RAG 를 모두 깨뜨리므로 배제).
  잠긴 파일은 기본적으로 RAG 인덱싱 제외 (설정으로 변경 가능).

### 옵시디언 내보내기 (`rag/obsidian.py`)

옵시디언 그래프 뷰는 노트 사이의 `[[링크]]`를 간선으로 그린다. 그래서 별도 포맷을
만들 필요 없이 **엔티티 1개 = 노트 1개**, 관계는 그 노트 안의 위키링크로 적으면 끝이다.

- `엔티티/<이름>.md` — frontmatter(type·degree·tags) + 설명 + `## 관계`(양방향으로 적어
  어느 쪽에서 열어도 이웃이 보인다) + `## 나온 문서`
- `문서/<제목>.md` — 경로·청크 수 + `## 등장 엔티티`. 업로드 파일 이름이 UUID 인 경우가
  많아 첫 청크 앞머리에서 제목을 뽑는다(글자 비율이 낮은 줄은 깨진 PDF 로 보고 버린다)
- 파일 이름은 `\ / : * ? " < > | # ^ [ ]` 를 치환하고, 겹치면 번호를 붙인다.
  링크가 파일 이름을 가리키므로 **쓰기 전에 이름을 전부 확정**한 뒤 본문을 만든다
- 대상 폴더에 `.chatchat-vault` 표시 파일을 남긴다. 표시가 없는 비어 있지 않은 폴더는
  409 로 거부하고, 재실행 시에는 우리가 만든 `엔티티/`·`문서/` 만 갈아끼운다
- 감시 폴더 안으로 내보내면 방금 만든 노트를 다시 색인하게 되므로 경고를 함께 돌려준다
- **내보낼 위치는 NAS 안 / 외부 마운트 둘 중 하나.** 외부는 `OBSIDIAN_HOST_PATH` 를
  `/data/obsidian` 으로 bind mount 한 것이고, iCloud Drive 의 옵시디언 폴더를 걸면
  아이폰·아이패드에서도 같은 볼트가 열린다. 호스트 경로는 컨테이너가 알 수 없어
  `OBSIDIAN_HOST_LABEL` 로 따로 받아 화면에만 쓴다.
- 외부 마운트는 서버 주인의 폴더라, **관리자는 마운트 바로 아래**에 볼트를 만들고
  (iOS 옵시디언은 iCloud 컨테이너의 최상위 폴더만 볼트로 인식한다) 다른 사용자는
  `<username>/` 하위에 쓴다. NAS 쪽은 기존대로 항상 사용자별로 격리된다.

**역가져오기** (`import_vault`): 볼트에서 고친 설명·관계를 DB 로 되돌린다. DB 가 원본이라
여기서 반영해 두면 다음 내보내기에 그대로 다시 나간다 — 재내보내기가 수정을 지우지 않는다.

- 엔티티는 frontmatter `aliases` 의 원래 이름으로 찾는다(파일 이름을 바꿔도 매칭된다).
- `## 관계` 의 **목록 줄**에서만 관계 설명을 읽는다. 본문에 흘려 쓴 `[[링크]]`도 간선으로
  잡되 설명은 비운다 — 링크 뒤 문장이 기존 설명을 덮어쓰면 안 되기 때문.
- **추가·수정만 하고 삭제하지 않는다.** 관계는 양쪽 노트에 다 적히므로, 한쪽만 지웠을 때
  간선이 사라지는 사고를 막으려고 일부러 뺐다.
- 바뀐 엔티티만 UPDATE 하고 그때 임베딩도 다시 만든다(설명이 바뀌면 벡터가 낡는다).
  Ollama 가 죽어 있으면 임베딩만 건너뛰고 그래프는 반영한 뒤 경고를 돌려준다.
- 손댄 엔티티에는 `entities.manual` 을 세운다. 사람이 만든 엔티티는 청크 연결이 없어서
  `prune_orphans` 에 지워지는데, 그 대상에서 빼기 위해서다(마이그레이션 009).
- 볼트가 오래됐으면 그 사이 정리된 엔티티가 되살아난다. 내보내기 → 편집 → 가져오기
  순서로 짧게 도는 걸 전제로 한다.

## 8. API 표면

```
POST   /api/auth/setup            최초 비밀번호 설정
POST   /api/auth/login            로그인 (쿠키 발급)
POST   /api/auth/logout
GET    /api/auth/me               로그인 상태 + 초기 설정 필요 여부

GET    /api/files?path=&show_hidden=   목록 (name, size, mtime, is_dir, hidden, locked)
POST   /api/files/upload               multipart 업로드
POST   /api/files/mkdir
POST   /api/files/rename
POST   /api/files/move
DELETE /api/files                      휴지통으로 이동
GET    /api/files/content?path=        다운로드/미리보기 (잠긴 파일은 비번 헤더 필요)
GET    /api/files/preview?path=        인라인 미리보기 메타(+텍스트 계열은 본문까지)
POST   /api/files/unlock               파일 비밀번호 확인
PUT    /api/files/flags                숨김 토글 / 잠금 설정·해제

GET    /api/personas                   CRUD
GET    /api/sessions                   목록·생성·삭제·설정변경
GET    /api/sessions/{id}/messages
POST   /api/chat/sessions/{id}/messages   → SSE
POST   /api/chat/stop/{id}

GET    /api/rag/documents              인덱싱 상태 목록
POST   /api/rag/reindex                전체/개별 재인덱싱
DELETE /api/rag/documents/{id}
GET    /api/rag/search?q=              검색 결과 프리뷰 (디버깅용)
GET    /api/rag/graph?entity=          그래프 이웃 조회
GET    /api/rag/events                 인덱싱 진행률 SSE (EventSource)
POST   /api/rag/export/obsidian        지식 그래프를 옵시디언 볼트로 내보내기
POST   /api/rag/import/obsidian        볼트에서 고친 설명·관계를 되가져오기
POST   /api/rag/scan                   주기 스캔을 기다리지 않고 즉시 대조

GET/PUT /api/settings                  설정 전체 조회/부분 저장
GET    /api/settings/models            모델 목록 (Ollama + 외부 API)
GET    /api/settings/providers         제공자 상태 + 토큰 사용량
POST   /api/settings/providers/deepseek/test   키 동작 확인 (최소 토큰)
PUT    /api/settings/providers/{name}/key      OpenAI/Claude 키 저장·삭제 (관리자)
POST   /api/settings/providers/{name}/test     키 동작 확인 (관리자)
GET    /api/health                     db / ollama 헬스체크
```

## 9. 설정 페이지 항목

**관리자/일반 분리** — `ADMIN_SETTINGS`(모델·외부 API·OCR)는 `public.global_settings` 에
서버 전체 값으로 저장되고 관리자만 바꾼다(비관리자가 PUT 하면 403). 나머지는 사용자
스키마의 `app_settings` 에 개인 값으로 들어간다. `GET /api/settings` 는 기본값 ← 전역 ←
개인 순으로 합쳐 돌려주고 `is_admin` 을 같이 실어, 프론트는 그걸로 탭과 카드를 숨긴다.

- 연결: Ollama base URL·모델 목록(관리자), Notion 토큰(개인), 헬스 상태
- 모델: 채팅/추출/임베딩 모델·num_ctx·외부 채팅 API 키(관리자),
  temperature·히스토리 턴 수·첨부 최대 크기·이미지 첨부 OCR·사고 과정 표시(개인)
- RAG: 감시 폴더 목록, 청크 크기/오버랩, top-k(청크·엔티티·관계·키워드), 기본 검색 모드,
  기본 RAG 토글, 잠긴 파일 인덱싱 여부, 키워드 검색 병행, OCR(사용 여부·언어·스캔본 판정 기준),
  전체 재인덱싱 버튼
- NAS: 업로드 최대 크기, 휴지통 사용 여부, 미리보기 허용 확장자
- 보안: 비밀번호 변경, 세션 만료일, 숨김 항목 표시 기본값
- 페르소나 관리
- 유지보수: 인덱싱 큐 상태, 실패 잡 재시도, 통계(문서/청크/엔티티/관계 수)

## 9-1. 프론트엔드 디자인 시스템

UI 는 `/DESIGN.md` (여기어때 YDS 6.0 레퍼런스)를 따른다. 토큰은 `frontend/src/styles.css`
최상단 `:root` 에 모여 있고, 주석으로 **[공식]** 과 **[파생]** 을 구분해 둔다.

- **[공식]** — YDS foundation 에 명시된 값. 임의로 바꾸지 않는다.
  - 색: primary `#1D8BFF`, canvas `#FFFFFF`, foreground `#222222`, border `#E6E6E6`,
    red `#F94239`, red-tint `#FFEDEA`, blue-tint `#E3F0FF`, yellow `#FFC83B`, slate `#49627A`
  - 서체: Pretendard (npm `pretendard` 동적 서브셋을 번들 → 오프라인에서도 동작)
  - 타이포 스케일: display 32/24, title 18, body 14, caption 12
  - 간격 2·4·8·10·12·16·20·24·32·48·64, 라운드 2·4·8·12·20·full, 화면 좌우 margin 20px
  - 그림자 Flat / Raised(dialog) / Sheet
  - 컴포넌트: Button(radius 8), Input·Search bar(radius 12), Card(radius 12 + Flat),
    Badge·Filter chip(radius full, 1.5px border), Tabs, Dialog(radius 20 + Raised)
- **[파생]** — 문서에 없어 이 앱에서 만든 중립 계열(`--n-page`, `--n-fill`, `--n-600`, `--n-700`).
  캡션·비활성·앱 크롬 배경에만 쓰고 공식 토큰처럼 취급하지 않는다.

DESIGN.md 의 제약을 따른 결정 두 가지:

- **motion 토큰을 만들지 않았다** (§9). duration/easing 은 공개 근거가 없어 transition 을
  최소로만 두고 스케일을 정의하지 않는다.
- **아이콘 세트를 만들지 않았다** (§7). 내비게이션은 텍스트 label 이고, 아이콘형 버튼에는
  전부 `aria-label` 을 붙였다. 파일 확장자 표시에만 이모지를 쓴다(콘텐츠 힌트).

상태는 색만으로 구분하지 않고 텍스트를 함께 둔다(§14). 예: 잠긴 파일은 색이 아니라
"잠김" 배지, RAG 토글은 "RAG 켜짐 · 하이브리드" 문구를 같이 노출한다.

## 10. 개발 로드맵

- ~~**P0 (스캐폴딩)** — compose 부팅, 스키마, 인증, 파일 매니저, Ollama 채팅 SSE, 설정 골격~~ ✅
- ~~**P1 (RAG)** — 추출·청킹·임베딩·엔티티 추출 워커, hybrid 검색, 인용 UI~~ ✅
- ~~**P2 (다듬기)** — 숨김/잠금 UX, 그래프 뷰어, 휴지통, 작업 큐 모니터~~ ✅
- ~~**P3** — 전문검색(pg_trgm), 이미지·스캔 PDF OCR, 채팅 첨부,
  문서 인라인 미리보기, 인덱싱 진행률 SSE 실시간화~~ ✅
- **P4 (선택)** — WebDAV 마운트, 표/레이아웃 보존 PDF 파싱, 첨부에서 NAS 파일 직접 고르기,
  검색 결과 하이라이트

## 11. 개발 명령

```bash
make up        # 빌드 + 기동 (http://localhost:3040)
make down
make logs
make models    # 필요한 Ollama 모델 pull (bge-m3 등)
make psql
make fmt
```
