ARGS ?=
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: install uninstall publish-static lint test

install:
	bash deploy/install.sh $(ARGS)

uninstall:
	bash deploy/uninstall.sh $(ARGS)

publish-static:
	price-bot mirror-artifacts $(ARGS)

lint:
	$(PYTHON) -m ruff check .

# DB-backed tests run only when DATABASE_URL is exported (never read from .env); point it at a
# throwaway database, never the shared service database.
test: lint
	$(PYTHON) -m pytest -q $(ARGS)
