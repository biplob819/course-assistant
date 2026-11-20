# make demo-cache CONFIG=... YES=--yes  (the confirmation is the caller's, never the Makefile's)
YES ?=
# Make targets call the CLI or a script (Tech Spec §4). Targets marked "calls" spend money.

CONFIG ?= sample_course/config.yaml
SPLIT ?= heldout
RUN ?=
# JSON=1 gives one machine-readable object (Tech Spec §4); .env is read only when it exists
# Exit codes: scripts/release_gate.py exits 0 pass, 1 gate failed, 2 error, 3 cost cap, 4 judging
# pending. GNU make reports any failing recipe as 2, so `make eval*` exits 0 or 2; CI and scripts
# that need the 0-4 code call scripts/release_gate.py directly (also in gate.json; D-59).
JSONFLAG = $(if $(JSON),--json,)
ENVFILE = $(if $(wildcard .env),--env-file .env,)

APP_DIR ?= runs/app
PROOF ?= teardown-proof.json
PILOT_END ?=

.PHONY: eval eval-check eval-offline eval-replay demo-cache demo-check purge

# make purge PILOT_END=2027-03-31 YES=--yes  (deletes; the confirmation is the caller's)
purge:  ## deletes: retention purge of the app store and retry of open trace deletions
	uv run course-assistant purge --app-dir $(APP_DIR) --pilot-end $(PILOT_END) $(YES)

teardown:  ## deletes: end a pilot (vector store, its files, app data) with a proof (P4.3)
	uv run --env-file .env course-assistant teardown --config $(CONFIG) --app-dir $(APP_DIR) --proof $(PROOF) $(YES)

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
