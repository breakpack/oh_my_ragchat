SHELL := /bin/bash
COMPOSE := docker compose
MODELS := bge-m3
PORT := $(shell grep -E '^WEB_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 3040)

.PHONY: help init up down restart build logs logs-api logs-worker ps psql models check clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | column -t -s $$'\t'

init: ## .env 생성 + data 디렉터리 준비
	@test -f .env || { cp .env.example .env; \
	  key=$$(openssl rand -hex 32); \
	  sed -i '' "s|^SECRET_KEY=.*|SECRET_KEY=$$key|" .env; \
	  echo "created .env (SECRET_KEY generated)"; }
	@mkdir -p data/nas/documents data/trash data/tmp
	@echo "ready."

up: init ## 빌드 후 기동
	$(COMPOSE) up -d --build
	@echo "→ http://localhost:$(PORT)"

down: ## 정지
	$(COMPOSE) down

restart: ## api/worker 재시작 (코드 변경 반영)
	$(COMPOSE) up -d --build api worker

build:
	$(COMPOSE) build

logs: ## 전체 로그
	$(COMPOSE) logs -f --tail=100

logs-api:
	$(COMPOSE) logs -f --tail=100 api

logs-worker:
	$(COMPOSE) logs -f --tail=100 worker

ps:
	$(COMPOSE) ps

psql: ## DB 셸
	$(COMPOSE) exec db psql -U chatchat -d chatchat

models: ## 필요한 Ollama 모델 pull (호스트에서 실행)
	@for m in $(MODELS); do echo "pull $$m"; ollama pull $$m; done

check: ## 헬스체크
	@curl -s http://localhost:$(PORT)/api/health | python3 -m json.tool

clean: ## 볼륨까지 삭제 (DB 초기화, NAS 파일은 유지)
	$(COMPOSE) down -v
