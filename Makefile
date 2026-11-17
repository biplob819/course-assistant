# make demo-cache CONFIG=... YES=--yes  (the confirmation is the caller's, never the Makefile's)
YES ?=
# Make targets call the CLI or a script (Tech Spec §4). Targets marked "calls" spend money.

CONFIG ?= sample_course/config.yaml
SPLIT ?= heldout
RUN ?=
# JSON=1 gives one machine-readable object (Tech Spec §4); .env is read only when it exists
JSONFLAG = $(if $(JSON),--json,)
ENVFILE = $(if $(wildcard .env),--env-file .env,)

.PHONY: eval eval-check eval-offline eval-replay demo-cache demo-check

eval-replay:  ## golden replay through ask(), no API calls
	uv run course-assistant eval-replay

demo-cache:  ## calls: build demo/replay.json from the curated public questions (D3.1)
	uv run --env-file .env python scripts/demo_cache.py --config $(CONFIG) $(YES)

demo-check:  ## replay demo/replay.json offline, no API calls
	uv run python scripts/demo_cache.py --check

eval:  ## calls: held-out golden run and the §6.5 release gate; flips the live corpus on exit 0 (E2.4)
	uv run $(ENVFILE) python scripts/release_gate.py eval --config $(CONFIG) --split $(SPLIT) $(YES) $(JSONFLAG)

eval-check:  ## the release gate on an existing run folder (RUN=...), no API calls
	uv run python scripts/release_gate.py check $(RUN) --config $(CONFIG) $(JSONFLAG)

eval-offline:  ## the release gate's offline part (fixture, tuning log, drift, coverage), no API calls
	uv run python scripts/release_gate.py offline $(JSONFLAG)
