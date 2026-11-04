# Contract fixtures

Real payloads captured from bernardo on 2026-10-15 (commit b90df4c + the MQTT bridge fix),
for consumers that vendor them (Marcellus). The contract they illustrate is
`docs/API.md` (events, manifest, status lifecycle, identity/retention) and `docs/MQTT.md`
(topics). `tests/test_contract_fixtures.py` checks them against the code, so a change that
alters these shapes fails the test suite.

| File | What it is |
|---|---|
| `api_event_animal_complete.json` | `GET /api/events/2685` — a finalized, classified animal event (`data` is a JSON string, as served) |
| `manifest_animal_complete.json` | `captures/events/20261015_210520/manifest.json` for the same event (status `complete`) |
| `api_event_media_pending.json` | `GET /api/events/2686` taken mid-capture: stills captured, clips not built, not yet classified |
| `manifest_media_pending.json` | the same event's manifest at that moment (status `media_pending`; note the full ~260-still burst, later pruned to 12) |
| `mqtt_event_animal.json` | the `bernardo/event` message for event 20261015_210520 (rebuilt with the bridge's `build_event` from the manifest above) |
| `mqtt_event_empty.json` | a `bernardo/event` message captured off the broker (manual test capture 2686, classified `empty`) |
| `mqtt_state.json` | a retained `bernardo/state` message captured off the broker |
| `api_health.json` | `GET /api/health` (= `bernardo/state` with `problems` as objects, plus `mqtt`) |

Event 2686 (`20261015_212835`) was a manual capture made to record these, so its `label` and
`data.sensor` are `manual` and `stream` is `GPIO0`; real triggers say `pir` / `GPIO17`.

To re-capture: copy the manifest from `captures/events/<key>/`, `curl` the endpoints, and
subscribe to `bernardo/#` on the broker; then run `.venv/bin/pytest tests/test_contract_fixtures.py`.
