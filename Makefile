# Care Connect Makefile

.PHONY: install playground run test clean

install:
	uv sync

playground:
	uv run adk web app --host 127.0.0.1 --port 18081 --reload_agents

run:
	uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port 8080 --reload

test:
	uv run pytest tests/

clean:
	rm -rf .venv/ __pycache__/ *.egg-info/ dist/ build/ .adk/ clinical_notes.db.txt
