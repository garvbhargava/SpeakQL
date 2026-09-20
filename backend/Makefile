# SpeakQL backend. One command per thing you actually do.
.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help up up-local down logs bootstrap pull-model test test-privileges fmt clean

help:  ## show this
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up:  ## db + api (Gemma hosted)
	$(COMPOSE) up -d --build db api
	@echo "api on http://localhost:8000/health"

up-local:  ## db + api + llm (Gemma in a container)
	$(COMPOSE) --profile local-llm up -d --build
	@echo "api on http://localhost:8000/health · ollama on 11434"

down:  ## stop everything, keep the data
	$(COMPOSE) down

logs:  ## follow the api
	$(COMPOSE) logs -f api

bootstrap:  ## create databases, roles, warehouses and seed
	$(COMPOSE) exec -T api bash scripts/bootstrap.sh

pull-model:  ## fetch Gemma into the llm container (~3 GB, once)
	$(COMPOSE) exec -T llm ollama pull $${LLM_MODEL:-gemma3:4b}

test:  ## the whole suite
	$(COMPOSE) exec -T api pytest

test-privileges:  ## the six privilege assertions (§17) -- the ones that matter
	$(COMPOSE) exec -T api pytest tests/test_privileges.py -v

clean:  ## stop and DELETE the database volume
	$(COMPOSE) down -v
