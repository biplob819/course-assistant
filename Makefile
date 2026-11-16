# Make targets call the CLI or a script (Tech Spec §4). Targets marked "calls" spend money.

CONFIG ?= sample_course/config.yaml

.PHONY: eval-replay demo-cache demo-check

eval-replay:  ## golden replay through ask(), no API calls
	uv run course-assistant eval-replay

demo-cache:  ## calls: build demo/replay.json from the curated public questions (D3.1)
	uv run --env-file .env python scripts/demo_cache.py --config $(CONFIG) --yes

demo-check:  ## replay demo/replay.json offline, no API calls
	uv run python scripts/demo_cache.py --check
