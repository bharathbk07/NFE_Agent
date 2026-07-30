.PHONY: heartbeat

heartbeat:
	.venv/bin/python -m src.agents.runtime.heartbeat --once
