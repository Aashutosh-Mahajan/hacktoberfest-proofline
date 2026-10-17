# `make dev` starts backend (:8000) + frontend (:5173) on Linux/macOS and on Windows (PowerShell script).
ifeq ($(OS),Windows_NT)
dev:
	powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1
else
dev:
	@trap 'kill 0' INT TERM EXIT; \
	(cd backend && python -m uvicorn mayday.api.app:app --port 8000 --reload) & \
	(cd frontend && npm run dev) & \
	wait
endif

backend:
	cd backend && python -m uvicorn mayday.api.app:app --port 8000 --reload

test:
	cd backend && python -m pytest -q -p no:warnings

eval:
	cd backend && python -m mayday.eval.run

mock:
	cd backend && python -m mayday.api.mock

.PHONY: dev backend test eval mock
