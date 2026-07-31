# oh_my_ragchat

개인용 **NAS + Graph RAG + 로컬 LLM 채팅**. Mac mini M4(32GB)에서 Docker Compose 로 돌리는
단일 사용자 웹서비스다. 외부로 열리는 포트는 **3040** 하나뿐이다.

문서를 폴더에 넣어두면 워커가 알아서 청킹·임베딩하고 엔티티/관계 그래프까지 만들어서,
채팅에서 RAG 토글을 켜면 내 문서를 근거로 출처(`[S1]`)를 달아 답한다. 끄면 평범한 로컬 LLM 채팅이다.

## 기능

- **NAS** — 업로드/이동/이름변경/삭제(휴지통·복원), 경로 샌드박싱
- **인라인 미리보기** — 이미지·PDF·마크다운·텍스트를 파일 목록 옆 패널에서 바로 확인
- **Graph RAG** — 감시 폴더의 `.md .txt .pdf .docx` 등을 자동 색인.
  naive / local / global / hybrid 4가지 검색 모드 + **본문 키워드 검색(pg_trgm) 병행**
- **OCR** — 이미지 파일과 텍스트 레이어 없는 스캔 PDF 를 tesseract(kor+eng)로 읽어 색인
- **그래프 추출 가속(선택)** — 색인이 느리면 DeepSeek API 로 전환. 동시 요청 + 입력/출력
  토큰 상한 + 누적 예산으로 비용을 묶고, 예산을 넘기면 자동으로 로컬 모델로 되돌아간다
- **실시간 인덱싱 진행률** — 워커가 Postgres NOTIFY 로 밀어주는 진행률을 SSE 로 화면에 표시
- **채팅** — SSE 스트리밍, 페르소나(시스템 프롬프트+모델+temperature), 세션별 RAG 토글,
  thinking 모델 사고 과정 표시, **이미지·문서 첨부**.
  모델은 로컬(Ollama)과 **DeepSeek API**(`deepseek-chat` / `deepseek-reasoner`) 중에 고른다
- **숨김 폴더** — 목록에서 제외되고 하위로 상속. RAG 색인에서도 빠진다
- **파일 잠금** — 열람 시 비밀번호 요구(`423`). 해제는 N분간 유지
- **설정 페이지** — 연결·모델·RAG·NAS·보안·페르소나 6개 탭

## 요구사항

- Docker Desktop
- 호스트에 네이티브 [Ollama](https://ollama.com) (macOS 컨테이너는 Metal GPU 를 못 쓴다)

```bash
brew services start ollama
ollama pull bge-m3          # 임베딩 (1024차원, 필수)
ollama pull gemma4:e4b      # 그래프 추출용 — 빠른 모델 권장
ollama pull qwen3.6:27b-mlx # 채팅용 — 원하는 모델로 대체 가능
```

## 실행

```bash
make up      # .env 생성(SECRET_KEY 자동 생성) + 빌드 + 기동
             # → http://localhost:3040
```

첫 접속에서 비밀번호를 정하면 시작된다. 문서는 `data/nas/documents/` 에 넣으면 30초 안에 색인된다.

```bash
make down      # 정지
make logs      # 전체 로그
make restart   # api/worker 재시작 (코드 변경 반영)
make check     # 헬스체크
make psql      # DB 셸
make clean     # 볼륨까지 삭제 (DB 초기화, NAS 파일은 유지)
```

기본 채팅/추출 모델은 `.env` 또는 설정 페이지에서 바꾸면 된다. NAS 루트를 외장 디스크로 옮기려면
`.env` 의 `NAS_HOST_PATH` 를 절대경로로 지정한다.

## 구성

```
브라우저 ──→ :3040 ──→ [web: nginx] ─┬─ /        → React 정적 빌드
                                     └─ /api/*   → api:8000 (FastAPI, SSE)

[api] FastAPI   ─┐
[worker] 색인    ─┼──→ [db] postgres 17 + pgvector
                  └──→ host.docker.internal:11434 (호스트 Ollama)
```

- 설계 전반: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- UI 디자인 시스템: [`DESIGN.md`](DESIGN.md) (여기어때 YDS 6.0) — 적용 내역은 ARCHITECTURE.md §9-1

## 알아둘 점

- **파일은 디스크에 평문으로 있다.** 파일 잠금은 웹 UI 열람 게이트이고, Finder/터미널 직접 접근은
  막지 못한다. 실제 암호화는 백업·검색·RAG 를 전부 깨뜨려서 뺐다(단일 사용자 로컬 전제).
- **그래프 추출이 병목이다.** 청크 1개당 LLM 1회 호출이라 문서가 많으면 오래 걸린다.
  논문 PDF 한 건이 70청크를 넘기도 한다. 빠르게 하려면 순서대로:
  1. 설정 → RAG 에서 **추출 제공자를 DeepSeek 으로** (아래 참고) — 동시 요청까지 붙어 가장 빠르다
  2. 추출 모델을 더 작은 걸로 (`gemma4:e2b` 등)
  3. **"그래프 추출" 자체를 끄기** — 벡터 전용 RAG 가 되어 수십 배 빨라진다. 엔티티/관계
     그래프는 안 생기지만 문서 검색과 인용은 그대로 동작한다.

### DeepSeek 로 추출 가속하기 (선택)

설정 → RAG → **그래프 추출 제공자** 에서 API 키를 붙여넣고 저장한 뒤,
제공자를 `DeepSeek API` 로 바꾸면 끝이다. 재시작도 파일 편집도 필요 없다.

키 보관 방식:

- `app_settings` 가 아니라 별도 `secrets` 테이블에 넣는다. `GET /api/settings` 는
  설정을 통째로 내려주므로, 같이 두면 설정 화면을 여는 것만으로 키가 브라우저까지 나간다.
- 조회 API 는 마스킹된 값(`sk-ab…7890`)만 준다. 원문을 되돌려주는 경로는 없다.
- DB 에 **평문**으로 저장되므로 DB 백업을 공유할 때는 주의한다.
- 더 엄격하게 두고 싶으면 `.env` 에 `DEEPSEEK_API_KEY` 를 넣으면 된다.
  환경변수가 있으면 그쪽이 우선하고, 웹에서의 변경은 거부된다.

토큰을 아끼려고 기본값이 이미 보수적으로 잡혀 있다 — 청크 입력 4000자, 출력 900토큰,
엔티티/관계 각 12개, 누적 예산 300만 토큰. **예산을 넘으면 자동으로 로컬 모델로 되돌아가고**,
호출이 실패한 청크도 로컬로 대체 처리한다. 누적/오늘 사용량과 예산 잔여는 같은 화면에서 볼 수 있다.
- **리버스 프록시를 앞에 둔다면 SSE 설정이 필요하다.** nginx 는 기본값이 업스트림 HTTP/1.0 +
  응답 버퍼링이라 스트리밍이 막힌다. 채팅 응답이 한 번에 나오거나 지식 페이지가
  "30초 갱신"에서 안 돌아오면 이것이다. nginx-proxy-manager 라면 해당 프록시 호스트의
  **Advanced** 탭에 아래를 넣는다.

  ```nginx
  location / {
      proxy_pass       $forward_scheme://$server:$port;
      proxy_http_version 1.1;
      proxy_set_header Connection '';
      proxy_set_header Host $host;
      proxy_buffering  off;
      proxy_cache      off;
      proxy_read_timeout 1h;
  }
  ```

- **파일 감시는 폴링이다.** macOS bind mount 로는 inotify 가 컨테이너까지 오지 않아
  30초 주기 대조 스캔을 쓴다(`RAG_SCAN_SECONDS`). API 를 통한 업로드/삭제는 즉시 반영된다.
- **채팅 이미지 첨부는 OCR 을 함께 넘긴다.** Ollama 가 `vision` 능력을 광고해도 실제로는
  이미지를 처리하지 못하는 모델이 있다(확인: `qwen3.6:27b-mlx`). 그래서 이미지는 base64 로
  모델에 넘기는 동시에 OCR 결과도 프롬프트에 넣는다. 설정에서 끌 수 있다.
- **OCR 은 완벽하지 않다.** 기본 PSM 3 이 여백 많은 페이지를 통째로 버리는 문제가 있어
  단일 블록(6) → 다단(4) → 성긴 텍스트(11) 순으로 재시도한다. 그래도 영문·숫자가 섞인
  모델명 등은 틀릴 수 있다.
