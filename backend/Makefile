# SpeakQL backend. One command per thing you actually do.
.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help up up-local down logs bootstrap pull-model test test-privileges \
        data train-retriever train-generator evaluate models fmt clean

PY := python

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

bootstrap:  ## create databases, roles, warehouses and the demo seed
	@# A one-off container: the only place the owner's credentials exist at
	@# runtime. The long-running api container is never given them.
	$(COMPOSE) run --rm bootstrap

pull-model:  ## fetch Gemma into the llm container (~3 GB, once)
	$(COMPOSE) exec -T llm ollama pull $${LLM_MODEL:-gemma3:4b}

# --- the two models -----------------------------------------------------------
# These run on the HOST, not in a container: training wants every core, and the
# api container has no business holding a training loop. The checkpoints they
# write are mounted into that container read-only.

data:  ## Spider, and the synthetic pairs (the stack must be bootstrapped)
	$(PY) -m ml.spider_prep
	$(PY) -m ml.synth

train-retriever:  ## Model A -- MiniLM schema retrieval (CPU)
	$(PY) -m ml.retriever_train --epochs 1

train-generator:  ## Model B -- CodeT5: Spider, then this warehouse (hours, CPU)
	$(PY) -m ml.generator_train --stage 1 --epochs 1
	$(PY) -m ml.generator_train --stage 2 --epochs 6

evaluate:  ## the recall table and the accuracy table
	$(PY) -m ml.retriever_eval
	$(PY) -m ml.generator_eval

models: data train-retriever train-generator evaluate  ## all of it, in order

demo:  ## ask the running stack the demo questions, end to end
	$(PY) scripts/demo.py

test:  ## the whole suite
	$(COMPOSE) exec -T api pytest

test-privileges:  ## the six privilege assertions (§17) -- the ones that matter
	$(COMPOSE) exec -T api pytest tests/test_privileges.py -v

clean:  ## stop and DELETE the database volume
	$(COMPOSE) down -v
