# CLAUDE.md

FastAPI sidecar for a Frigate NVR: scrub cache, push notifications (APNs
Live Activities, v2 channel in `push/live_activities.py`), triage/analysis
pages, face pipeline. Server-rendered Jinja + vanilla JS, SQLite sidecar DB.

## Dev & verification (run all before any PR)

- `.venv/bin/ruff check src tests`
- `.venv/bin/python -m mypy src/marcellus`  (CI runs mypy — don't skip)
- `.venv/bin/pytest tests/ -q`
- `node --check` on any touched JS
- Prod is Python 3.10; CI tests 3.10 + 3.12. Don't use 3.11+ syntax.
- `tests/test_guide.py` fails CI if a new page/route or config section lacks
  a guide topic (`guide_content/*.md` frontmatter maps routes/config keys).
- Template JS includes need `?v={{ asset_v }}` cache-busters.

## Deploy (LXC "nvr", deploy-only — dev happens on this Mac)

`ssh nvr "cd /opt/marcellus && git fetch && git reset --hard
origin/main && pip install -q '.[http2,annotation,enrich]' && systemctl
restart marcellus && curl -s localhost:5000/healthz"`
The pip-install step is mandatory (deps/extras drift otherwise).
Ship flow: branch → PR → CI green → squash-merge → deploy → live-verify.

## Architecture notes that bite

- `proxy_routes` registers LAST as a catch-all forwarding unmatched paths to
  Frigate — removed/unknown routes return Frigate's SPA with HTTP 200, never
  404. Curl status codes can't verify route existence; check nav/templates.
- Unauthenticated page requests return a 200 login/SPA shell.
- Face pipeline has two live stages: B2 capture (`faces/crosscam.py`,
  `/faces/captures`) and B3 enrichment (`faces/enrich.py`, `/enrich/clusters`,
  CPU-only). B1 curation was removed 2026-08 — don't reintroduce `face:`
  config or `face_attempts` schema.
- `config.py` Settings uses `extra="ignore"` — old prod YAML keys load fine.
- FastAPI TestClient follows redirects by default.
- systemd timers on nvr run some jobs (face-capture) in separate processes.

## Agent & model routing

Global `~/.claude/CLAUDE.md` policy applies. Repo-specific:
- sonnet: route/template/CSS edits against a spec, pytest additions, running
  the verification bar and reporting only failures.
- haiku: grep sweeps across `src/marcellus`, guide-coverage checks,
  triaging pytest/CI output.
- Main thread only: push-pipeline/APNs payload design, scrub-cache and proxy
  behavior, anything touching prod (deploys, systemd, DB schema).
- CI watching: poll with a background task, never a foreground watch loop.
