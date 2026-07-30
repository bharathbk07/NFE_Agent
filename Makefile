.PHONY: heartbeat studio-clean

heartbeat:
	.venv/bin/python -m src.agents.runtime.heartbeat --once

# Kill stuck Studio workers + wipe persisted hung runs, then start clean.
studio-clean:
	-@lsof -tiTCP:2024 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
	-@pkill -9 -f 'langgraph dev' 2>/dev/null || true
	rm -rf .langgraph_api
	mkdir -p .langgraph_api
	.venv/bin/langgraph dev --allow-blocking --port 2024
