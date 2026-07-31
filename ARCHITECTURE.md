# chatchat — 개인용 NAS + Graph RAG + 로컬 LLM 채팅

Mac mini M4 (32GB) 에서 Docker Compose 로 돌아가는 단일 사용자 웹서비스.

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
```

- 벡터 인덱스: `chunks.embedding`, `entities.embedding` 에 HNSW (`vector_cosine_ops`).
- 그래프는 Neo4j / Apache AGE 없이 `entities` + `relations` 관계 테이블로 두고,
  이웃 확장은 재귀 CTE (`depth <= 2`) 로 처리한다. 단일 사용자 규모에서 충분하고
  pgvector 와 한 이미지에서 공존시킬 수 있다.
- `jobs` 는 `FOR UPDATE SKIP LOCKED` 로 소비하는 DB 큐. Redis/Celery 불필요.

## 5. Graph RAG 파이프라인 (LightRAG 방식)

### 인덱싱 (worker)

1. **감시** — 워커가 감시 폴더(설정값, 기본 `documents/`)를 주기적으로(기본 30초,
   `RAG_SCAN_SECONDS`) 훑어 `documents` 테이블의 mtime+size 와 대조하고 차이만 큐에 넣는다.
   macOS 호스트의 bind mount 에서는 inotify 이벤트가 컨테이너로 전달되지 않아 `watchdog`
   방식이 동작하지 않으므로 대조 스캔을 쓴다. API 를 통한 업로드·삭제·이동은 스캔을
   기다리지 않고 즉시 잡을 건다.
2. **추출** — 확장자별 텍스트 추출: `.txt .md`(평문, charset 자동감지), `.pdf`(pypdf),
   `.docx`(python-docx). 그 외는 skip 상태로 기록.
3. **청킹** — 문단 경계 우선, 목표 1200자 / 오버랩 150자.
4. **임베딩** — Ollama `/api/embed`, 모델 `bge-m3` (1024차원, 한국어 강함). 배치 16.
5. **엔티티/관계 추출** — 청크마다 로컬 LLM 1회 호출. 구조화 출력(JSON)으로
   `entities[{name,type,description}]`, `relations[{src,tgt,description,keywords,weight}]`.
   추출 모델은 채팅 모델과 분리 설정(기본 `gemma4:e4b` 처럼 빠른 모델).
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
| `hybrid` | local ∪ global ∪ naive 를 dedupe 후 랭킹 (기본값) |

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

## 7. 보안 모델 (단일 사용자)

- **로그인**: 비밀번호 1개. `hashlib.scrypt` (stdlib, 외부 의존성 없음) 로 해시.
  성공 시 `itsdangerous` 로 서명한 HttpOnly 세션 쿠키 발급(기본 30일).
  첫 실행 시 비밀번호 미설정 상태이며 최초 접속에서 설정한다.
- **7번 숨김 폴더**: `path_flags.hidden`. 목록 API 는 기본적으로 숨김 항목을 제외하고,
  `?show_hidden=1` + 세션 인증이 있을 때만 포함한다. 숨김 폴더 내부는 RAG 인덱싱에서도 제외.
- **8번 파일 잠금**: `path_flags.lock_hash`. 잠긴 파일은 목록에는 보이지만
  `GET /api/files/content` 시 `X-File-Password` 헤더 검증을 요구한다. 통과 시 10분짜리
  해제 토큰을 세션에 기록. **디스크상 파일은 평문**이므로 터미널/Finder 직접 접근은 막지
  못한다(단일 사용자 로컬 환경 전제, 실제 암호화는 백업·검색·RAG 를 모두 깨뜨리므로 배제).
  잠긴 파일은 기본적으로 RAG 인덱싱 제외 (설정으로 변경 가능).

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

GET/PUT /api/settings                  설정 전체 조회/부분 저장
GET    /api/settings/models            Ollama 모델 목록 프록시
GET    /api/health                     db / ollama 헬스체크
```

## 9. 설정 페이지 항목

- 연결: Ollama base URL, 헬스 상태, 모델 목록
- 모델: 채팅 모델, 추출 모델, 임베딩 모델, temperature, num_ctx, 히스토리 턴 수
- RAG: 감시 폴더 목록, 청크 크기/오버랩, top-k, 기본 검색 모드, 기본 RAG 토글,
  잠긴 파일 인덱싱 여부, 전체 재인덱싱 버튼
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
- **P3 (선택)** — WebDAV 마운트, 전문검색(pg_trgm), 이미지 OCR, 멀티모달 첨부,
  문서 인라인 미리보기, 인덱싱 진행률 SSE 실시간화

## 11. 개발 명령

```bash
make up        # 빌드 + 기동 (http://localhost:3040)
make down
make logs
make models    # 필요한 Ollama 모델 pull (bge-m3 등)
make psql
make fmt
```
