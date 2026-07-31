# 워커 공통 컨텍스트 (P0 스캐폴딩)

전체 설계는 `/ARCHITECTURE.md` 를 먼저 읽을 것. 이 문서는 **이미 코디네이터가 작성해 둔 기반
모듈의 실제 API** 를 정리한 것이다. 이 모듈들은 **수정하지 말고 그대로 호출**한다.
(수정이 꼭 필요하면 `ask` 로 코디네이터에게 물어볼 것)

## 규칙

- Python 3.13, FastAPI, psycopg3(sync, `dict_row`), 외부 ORM 없음. **생 SQL** 사용.
- 주석·에러 메시지·UI 문구는 **한국어**. 주석은 "왜"만 짧게. 자명한 코드에 주석 달지 말 것.
- 타입힌트 사용, `from __future__ import annotations` 로 시작.
- API 응답은 snake_case JSON.
- 인증이 필요한 라우터는 `dependencies=[deps.Auth]` 를 APIRouter 에 건다.
- **자기 담당 파일만 생성/수정한다.** 남의 파일을 건드리면 충돌한다.
- `docker compose` 를 직접 띄우지 말 것 (코디네이터가 마지막에 한 번 검증한다).
  대신 `python -c "import ..."` 수준의 문법/임포트 확인은 해도 좋다. 컨테이너 밖 호스트에는
  의존성이 없으니, 검증은 `python3 -m py_compile <파일>` 로 문법 확인까지만 한다.

## DB 스키마

`db/init/001_schema.sql` 를 읽을 것. 요약:

- `app_settings(key, value jsonb)`, `auth_user(id=1, password_hash, salt)`
- `path_flags(path, is_dir, hidden, lock_hash, lock_salt, note, updated_at)`
- `personas(id, name, system_prompt, model, temperature, is_default, created_at)`
- `chat_sessions(id, title, persona_id, model, rag_enabled, rag_mode, created_at, updated_at)`
- `chat_messages(id, session_id, role, content, thinking, citations jsonb, model, created_at)`
- `documents(id, path, mtime, size, sha256, status, chunk_count, error, indexed_at, created_at)`
- `chunks(id, document_id, ord, content, token_est, embedding vector(1024))`
- `entities(id, name_norm, name, type, description, embedding vector(1024), degree, created_at)`
- `relations(id, src_id, tgt_id, description, keywords, weight, embedding vector(1024))`
- `chunk_entities(chunk_id, entity_id)`
- `jobs(id, kind, payload jsonb, status, attempts, error, created_at, started_at, done_at)`
  - `jobs` 에 `(kind, payload->>'path') WHERE status IN ('queued','running')` 부분 유니크
    인덱스가 있으므로 중복 큐잉은 `ON CONFLICT DO NOTHING` 으로 흡수한다.

## `app/config.py`

```python
from .config import env, DEFAULT_SETTINGS, RAG_MODES, INDEXABLE_EXTS, TEXT_EXTS, PDF_EXTS, DOCX_EXTS

env.nas_root / env.trash_root / env.tmp_root   # pathlib.Path
env.ollama_base_url, env.chat_model, env.extract_model, env.embed_model, env.embed_dim
env.secret_key, env.session_cookie, env.database_url
```

`DEFAULT_SETTINGS` 키 (설정 페이지에서 바꿀 수 있는 전부, 여기 없는 키는 저장 거부됨):

```
ollama_base_url chat_model extract_model embed_model temperature num_ctx
history_turns show_thinking
rag_default_enabled rag_default_mode rag_watch_dirs rag_chunk_size rag_chunk_overlap
rag_top_k_chunks rag_top_k_entities rag_top_k_relations rag_graph_depth
rag_index_locked_files rag_max_file_mb rag_extract_graph
nas_use_trash nas_show_hidden_default nas_preview_exts
session_days file_unlock_minutes
```

## `app/db.py`

```python
from . import db

with db.cursor() as cur:            # 자동 commit
    cur.execute("...", (a, b))
with db.cursor(commit=False) as cur:   # 읽기 전용
    row = cur.fetchone()            # dict | None
    rows = cur.fetchall()           # list[dict]

db.get_settings() -> dict           # 기본값 + DB 저장값 머지
db.get_setting(key) -> Any
db.save_settings(patch: dict) -> dict
db.pool(); db.close_pool()
```

## `app/deps.py`

```python
from . import deps

router = APIRouter(prefix="/api/xxx", tags=["xxx"], dependencies=[deps.Auth])

def handler(cfg: deps.Settings):    # Annotated[dict, Depends(settings)]
    ...
```

## `app/security.py`

```python
from . import security

security.is_configured() -> bool
security.set_password(pw); security.check_password(pw) -> bool
security.issue_session() -> str; security.valid_session(token, max_age_days) -> bool

security.set_file_lock(path, password_or_None, is_dir=False)
security.is_locked(path) -> bool
security.try_unlock(path, password, minutes) -> bool
security.is_unlocked(path) -> bool
security.forget_unlock(path)
```

## `app/paths.py` (NAS 루트 샌드박싱)

```python
from . import paths

paths.root() -> Path                      # 해석된 NAS 루트
paths.normalize(rel) -> str               # "" == 루트, 트래버설 거부
paths.resolve(rel, must_exist=True) -> Path   # 루트 밖이면 400, 없으면 404
paths.to_rel(abs_path) -> str
paths.check_name(name) -> str
paths.join(parent, name) -> str
paths.parent_of(rel) -> str
paths.is_under(rel, ancestor) -> bool
paths.ensure_dirs()
paths.PathError(detail)                   # HTTPException 400
```

## `app/flags.py` (숨김 / 잠금 상태)

```python
from . import flags

flags.row_for(path) -> dict | None         # path, is_dir, hidden, locked, note
flags.flags_for([paths]) -> dict[str, dict]
flags.hidden_paths() -> list[str]
flags.locked_paths() -> list[str]
flags.is_hidden_inherited(rel, hidden=None) -> bool   # 조상 폴더 숨김 상속
flags.set_hidden(path, hidden, is_dir)
flags.set_note(path, note, is_dir)
flags.move(old, new)      # 이동 시 하위 플래그까지 경로 갱신
flags.drop(path)          # 삭제 시 하위 플래그까지 제거
flags.list_all()
```

## `app/ollama.py`

```python
from . import ollama

# async (FastAPI)
await ollama.list_models() -> list[dict]
await ollama.ping() -> {"ok": bool, ...}
async for chunk in ollama.chat_stream(model, messages, temperature=, num_ctx=, think=None):
    chunk["content"], chunk["thinking"], chunk["done"]
await ollama.embed(texts, model=None) -> list[list[float]]

# sync (worker)
ollama.embed_sync(texts, model, client=None) -> list[list[float]]
ollama.generate_sync(model, prompt, system=None, fmt=<json schema|"json">,
                     temperature=0.0, num_ctx=8192, client=None) -> str
ollama.OllamaError
```

임베딩 차원은 1024 (`bge-m3`). 벡터 컬럼은 pgvector 이고 `db.py` 가 커넥션마다
`register_vector` 를 걸어두었으므로, 파이썬 `list[float]` 를 그대로 파라미터로 넘기면 된다.
단 명시적 캐스팅이 필요한 비교식에서는 `%s::vector` 를 쓸 것.

## 라우터 등록

코디네이터가 `app/main.py` 에서 아래 이름으로 임포트한다. **정확히 이 경로/변수명**으로 만들 것.

```python
from .routers.auth     import router as auth_router        # 코디네이터 담당
from .routers.settings import router as settings_router     # 코디네이터 담당
from .routers.files    import router as files_router        # W1
from .routers.sessions import router as sessions_router     # W2
from .routers.personas import router as personas_router     # W2
from .routers.chat     import router as chat_router         # W2
from .routers.rag      import router as rag_router          # W3
```

각 라우터 모듈은 `router = APIRouter(prefix="/api/...", ...)` 를 노출한다.
`app/routers/__init__.py` 는 코디네이터가 이미 만들어 둔다(빈 파일).

## W2 ↔ W3 인터페이스 계약 (RAG 검색)

채팅(W2)은 RAG 검색을 아래 시그니처로만 호출한다. W3 는 이 시그니처를 정확히 구현한다.

```python
# app/rag/retrieve.py
async def retrieve(query: str, mode: str, cfg: dict) -> RagContext

@dataclass
class RagContext:
    prompt_block: str          # 시스템 프롬프트에 덧붙일 완성된 컨텍스트 텍스트
    citations: list[dict]      # [{"tag":"S1","path":"documents/a.pdf","document_id":1,
                               #   "chunk_id":9,"excerpt":"...","score":0.83}, ...]
    stats: dict                # {"chunks":6,"entities":12,"relations":8,"mode":"hybrid","ms":123}
    empty: bool                # 근거를 하나도 못 찾았으면 True
```

W2 는 `empty` 가 True 면 "관련 문서를 찾지 못했다"는 지시문만 덧붙이고 일반 응답을 진행한다.
