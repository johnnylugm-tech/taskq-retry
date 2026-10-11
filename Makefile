# NFR-12: end-to-end system verification (SPEC.md section 4, NFR-12).
PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
VERIFY_DB := .verify-system.db
VERIFY_PORT ?= 8765

.PHONY: verify-system
verify-system:
	@rm -f $(VERIFY_DB)
	$(PYTHON) -m alembic -c alembic.ini upgrade head
	$(PYTHON) -m pytest 03-development/tests -q
	@set -e; \
	TASKQ_DB_URL=sqlite:///$(CURDIR)/$(VERIFY_DB) PYTHONPATH=03-development/src \
	  $(PYTHON) -m uvicorn taskq_api.app:create_app --factory --port $(VERIFY_PORT) --log-level warning & \
	PID=$$!; trap 'kill $$PID 2>/dev/null' EXIT; \
	for i in $$(seq 1 50); do curl -sf http://127.0.0.1:$(VERIFY_PORT)/healthz >/dev/null && break; sleep 0.2; done; \
	curl -sf http://127.0.0.1:$(VERIFY_PORT)/healthz; echo; \
	curl -sf http://127.0.0.1:$(VERIFY_PORT)/readyz; echo; \
	kill $$PID; wait $$PID 2>/dev/null || true; trap - EXIT
	$(PYTHON) -m alembic -c alembic.ini downgrade base
	$(PYTHON) -m alembic -c alembic.ini upgrade head
	@rm -f $(VERIFY_DB)
	@echo "verify-system: PASS"
