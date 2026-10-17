dev:
	cd backend && python -m uvicorn mayday.api.app:app --port 8000 --reload
test:
	cd backend && python -m pytest -q
eval:
	cd backend && python -m mayday.eval.run
mock:
	cd backend && python -m mayday.api.mock
