# Encounters — design and slice 1 spec

Status: slice 1 (adjacency, store, linker, live hook + reconciler, inspection
page, /v1 read endpoints). Written 2026-09-19.

## What an encounter is

An **encounter** groups Frigate activity that a person would describe as one
thing: "a raccoon worked its way from the alley to the shed", "two people
and a dog walked past the gate". It is an *overlay*: Frigate's `event` and
`reviewsegment` tables stay the source of truth and are only ever read.

* **Atom** = one Frigate review segment (`reviewsegment` row / `frigate/reviews`
  message). Frigate already bundles concurrent objects on one camera into one
  segment, so "person + dog on the doorbell" is already a single atom. Atom
  id = review id.
* **Encounter** = an ordered chain of atoms across cameras and time gaps.
* Every membership records a `link_reason` and `confidence` so the UI can
  say *why* ("shared zone front_garden, 40 s gap").
* Human decisions (pin an atom into an encounter / split it out) persist and
  the linker honours them on every re-run, so linking is **idempotent**:
  re-ingesting the same atoms yields the same encounters.

Push, reel, related and highlights are untouched in slice 1 (later slices
add `encounter_id` to those and encounter-aware push titles).

## Package layout `src/marcellus/encounters/`

```
__init__.py
adjacency.py   # camera adjacency graph
linker.py      # pure-Python linking logic, no DB, no I/O
store.py       # sidecar sqlite tables + CRUD
service.py     # EncounterService: live hook, reconciler loop, sealing
```
Routes in `routes/encounters.py`, template `templates/encounters.html`,
wire models in `models/wire.py`, guide topic `guide_content/encounters.md`.

## Config (`config.py` → `EncountersSection`, attached as `encounters:`)

```yaml
encounters:
  enabled: false
  reconcile_interval_s: 30.0     # reconciler cadence
  backfill_lookback_s: 86400.0   # first-start backfill window over reviewsegment
  gap_s:                         # max gap (s) between an encounter's last end and a new atom's start
    animal: 180.0
    person: 90.0
    vehicle: 45.0
    default: 60.0
  max_duration_s: 1800.0         # hard cap on one encounter's span
  recent_cameras: 2              # adjacency is checked against the last N distinct cameras
  min_copresence_s: 3.0          # companionship needs this much time overlap
  adjacency: []                  # extra edges, e.g. [["alley-wide","shed"]]
  not_adjacent: []               # edges to remove even if zones are shared
```
All fields must be mentioned in the guide topic (see test_guide.py rules).

## adjacency.py

```python
@dataclass(frozen=True)
class Adjacency:
    edges: frozenset[frozenset[str]]          # undirected camera pairs
    shared_zones: dict[frozenset[str], tuple[str, ...]]  # pair -> zone names
    def adjacent(self, a: str, b: str) -> bool  # True if a == b or edge exists
    def neighbours(self, cam: str) -> set[str]
    def to_json(self) -> dict  # {"cameras": [...], "edges": [{"a","b","zones":[...],"source":"zones|config"}]}

def build_adjacency(zones_by_camera: dict[str, list[dict]], *, extra: list[list[str]], removed: list[list[str]]) -> Adjacency
```
Rule: two cameras are adjacent when they define a zone with the **same
name** (`load_camera_zones` output). Config `adjacency` adds edges,
`not_adjacent` removes them (config wins). Footprint-overlap adjacency
(camera_layout/optics) is a documented follow-up, not slice 1.

## linker.py (pure, fully unit-testable)

```python
LABEL_FAMILIES = {"person": {"person"},
                  "vehicle": {"car","truck","bus","motorcycle","bicycle"},
                  "animal": {"dog","cat","raccoon","bird","squirrel","fox","deer","skunk","opossum","rabbit","bear"},
                  "package": {"package"}}
def family_of(label: str) -> str   # "default" if unknown

@dataclass(frozen=True)
class Atom:
    atom_id: str; camera: str; start_time: float; end_time: float | None
    labels: tuple[str, ...]; zones: tuple[str, ...]; event_ids: tuple[str, ...]
    sub_labels: tuple[str, ...]; severity: str   # "alert" | "detection"

@dataclass
class OpenEncounter:            # in-memory view of an unsealed encounter
    encounter_id: str; start_time: float; last_end: float
    cameras: list[str]          # ordered distinct, in order of first appearance
    labels: set[str]; identities: set[str]; zones: set[str]
    atom_ids: list[str]; peak_severity: str

@dataclass(frozen=True)
class LinkDecision:
    encounter_id: str | None    # None => start a new encounter
    reason: str                 # "pinned" | "identity" | "same_camera" | "shared_zone" | "adjacent" | "companion" | "new"
    confidence: float           # 0..1

@dataclass(frozen=True)
class LinkerConfig: gap_s: dict[str, float]; max_duration_s: float; recent_cameras: int; min_copresence_s: float

def decide(atom: Atom, open_encounters: Sequence[OpenEncounter], adjacency: Adjacency, cfg: LinkerConfig,
           *, pinned_to: str | None = None, split_from: frozenset[str] = frozenset()) -> LinkDecision
```

`decide` rules, evaluated per candidate encounter (skip any in `split_from`;
if `pinned_to` is given and that encounter is open, return it with reason
"pinned", confidence 1.0):

1. **Hard rejects**: atom.start_time - enc.start_time > max_duration_s;
   atom.sub_labels and enc.identities both non-empty and disjoint
   (contradictory identity).
2. **Gap**: `gap = atom.start_time - enc.last_end` (negative = overlap).
   Allowed gap = max over the atom's label families of `gap_s[family]`
   (fallback `gap_s["default"]`). Identity match (sub_labels ∩ identities)
   allows 3× that gap.
3. **Continuity** (needs a shared label family between atom.labels and
   enc.labels, and gap within allowance). Spatial test against the last
   `recent_cameras` distinct cameras of the encounter:
   * identity match → reason "identity", conf 0.95 (spatial test skipped)
   * same camera → "same_camera", 0.9
   * shared zone name (atom.zones ∩ enc.zones) → "shared_zone", 0.8
   * adjacency edge → "adjacent", 0.6
4. **Companionship** (no shared family required): the atom's time span
   overlaps the encounter's span by ≥ min_copresence_s AND atom.camera is
   the same as, or adjacent to, one of the recent cameras → "companion",
   0.7.
5. Pick the highest confidence; tie-break by smallest |gap|. No candidate →
   `LinkDecision(None, "new", 1.0)`.

Also provide `apply(enc: OpenEncounter, atom: Atom) -> None` that mutates
the open encounter (extend last_end, append camera if new, union labels /
identities / zones, bump peak_severity where alert > detection) and
`fold(atoms: Iterable[Atom], ...) -> list[OpenEncounter]` that runs decide +
apply over atoms sorted by start_time (used by tests and the reconciler).

## store.py (tables go in `db.SIDECAR_SCHEMA`; new columns later must also go in `_ADDED_COLUMNS`)

```sql
CREATE TABLE IF NOT EXISTS encounters (
    id               TEXT PRIMARY KEY,           -- uuid4 hex
    start_time       REAL NOT NULL,
    end_time         REAL,                       -- max member end, NULL while any member open
    sealed_at        REAL,                       -- NULL = open, candidate for linking
    cameras_json     TEXT NOT NULL DEFAULT '[]', -- ordered distinct
    labels_json      TEXT NOT NULL DEFAULT '[]',
    identities_json  TEXT NOT NULL DEFAULT '[]', -- sub_labels seen
    zones_json       TEXT NOT NULL DEFAULT '[]',
    primary_event_id TEXT,                       -- first alert-severity member's first event id, else first member's
    peak_severity    TEXT NOT NULL DEFAULT 'detection',
    atom_count       INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_encounters_start ON encounters(start_time);
CREATE INDEX IF NOT EXISTS idx_encounters_open ON encounters(sealed_at) WHERE sealed_at IS NULL;
CREATE TABLE IF NOT EXISTS encounter_members (
    atom_id         TEXT PRIMARY KEY,            -- Frigate review id
    encounter_id    TEXT NOT NULL,
    camera          TEXT NOT NULL,
    start_time      REAL NOT NULL,
    end_time        REAL,
    severity        TEXT NOT NULL,
    labels_json     TEXT NOT NULL DEFAULT '[]',
    zones_json      TEXT NOT NULL DEFAULT '[]',
    event_ids_json  TEXT NOT NULL DEFAULT '[]',
    sub_labels_json TEXT NOT NULL DEFAULT '[]',
    link_reason     TEXT NOT NULL,
    confidence      REAL NOT NULL,
    joined_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_encounter_members_enc ON encounter_members(encounter_id, start_time);
CREATE TABLE IF NOT EXISTS encounter_decisions (
    atom_id      TEXT NOT NULL,
    action       TEXT NOT NULL CHECK(action IN ('pin','split')),
    encounter_id TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    note         TEXT,
    PRIMARY KEY (atom_id, action, encounter_id)
);
CREATE TABLE IF NOT EXISTS encounter_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- 'watermark' = last reconciled reviewsegment start_time
```

Functions (all take `conn: sqlite3.Connection`, sync): `load_open(conn) ->
list[OpenEncounter]`, `upsert_atom(conn, atom, decision, now) -> str`
(insert or, if the atom already exists, update its mutable fields —
end_time, labels, zones, event_ids, sub_labels, severity — and refresh its
encounter's aggregates; membership never changes on update unless the
atom's current encounter is sealed and a decision forces otherwise),
`recompute(conn, encounter_id)` (aggregates from members), `seal_stale(conn,
now, cfg) -> int` (seal when open and `now - last_end > 1.5 × largest gap_s`
or span ≥ max_duration_s), `list_recent(conn, *, since, limit, camera=None)`,
`get(conn, encounter_id)`, `decisions_for(conn, atom_id)`,
`get_watermark/set_watermark`.

## service.py

```python
class EncounterService:
    def __init__(self, settings: Settings, *, adjacency: Adjacency, now: Callable[[], float] = time.time)
    def observe_review(self, ev: ReviewEvent) -> None        # LIVE hook; never raises (log + swallow)
    def reconcile(self) -> ReconcileStats                   # BACKFILL/repair; sync, run via asyncio.to_thread
    def status(self) -> dict                                 # for /healthz
```
* `observe_review`: build an Atom from the `ReviewEvent` (`review_id`,
  `camera`, `labels`, `zones`, `track_ids` → event_ids, `sub_labels`,
  `severity`, `start_time`, end_time None; an "end" message sets end_time to
  now). `parse_review_message` currently keeps only new/update — extend it
  to pass through Frigate's `type: "end"` as `msg_type="end"` **without**
  changing PushEngine behaviour (engine must ignore "end" exactly as it does
  today; add a test proving that). Then open sidecar conn → `decide` against
  `load_open` → `upsert_atom` → close.
* `reconcile`: open Frigate RO + sidecar; read `reviewsegment` rows with
  `start_time >= watermark - max_duration_s` (first run: `now -
  backfill_lookback_s`), apply the annotation clock offset per camera the
  same way `routes/scrub.py::_event_clock_offset_s` does (factor that helper
  out so both share it), sort by start_time, run decide/upsert for each
  (existing members get their end_time/objects refreshed), then
  `seal_stale`, advance the watermark to the max start_time seen, return
  stats (rows, new, updated, sealed). This is the "belt": anything the
  MQTT path missed or saw only partially is repaired here.
* Wiring in `server.py` lifespan, mirroring `_face_enrich_loop`: when
  `settings.encounters.enabled`, build adjacency from
  `load_camera_zones(settings.frigate.config_path)` + config, construct the
  service, store on `app.state.encounters`, start
  `_encounters_loop(app)` (reconcile every `reconcile_interval_s`, record
  last stats on app.state). Pass the service to `PushEngine` via a new
  optional ctor kwarg `on_review: Callable[[ReviewEvent], None] | None =
  None`; `PushEngine.handle_event` calls it first inside try/except (an
  encounters failure must never affect push). `MqttReviewSubscriber`'s
  ordered consumer already serialises calls, so no extra locking.
* `/healthz`: add `encounters: ok|disabled|error` plus last reconcile stats.

## Routes (`routes/encounters.py`)

HTML (same middleware auth as other admin pages, `base.html`, triage.css,
util.js helpers; phone-first):
* `GET /encounters` — last 48 h, newest first: one row per encounter with
  time span, camera path (friendly names via existing helpers, e.g.
  "alley-wide → shed"), label chips, identities, atom count, open/sealed
  badge; expandable member list showing each atom's camera, span, labels,
  zones, `link_reason` and confidence. Filters: `?camera=`, `?since=`.
* `GET /encounters/{id}` — same detail for one encounter, plus a "Related
  events" list linking to existing event detail/clip URLs already used by
  the triage pages.
* No pin/split UI yet (slice 2); decisions table exists so the linker logic
  is complete.

JSON (`/v1`, wire models with `extra="forbid"` in `models/wire.py`):
* `GET /v1/encounters?since=<epoch>&limit=<int≤500>&camera=` →
  `EncountersResponse{t: float, encounters: list[EncounterSummary]}`
* `GET /v1/encounters/{id}` → `EncounterResponse{encounter: EncounterSummary, members: list[EncounterMember]}`
* `GET /v1/encounters/adjacency` → `Adjacency.to_json()` (registered before
  the `{id}` route).

```python
class EncounterMember(_Wire):
    atom_id: str; camera: str; start: float; end: float | None; severity: str
    labels: list[str]; zones: list[str]; event_ids: list[str]; sub_labels: list[str]
    link_reason: str; confidence: float
class EncounterSummary(_Wire):
    id: str; start: float; end: float | None; sealed: bool
    cameras: list[str]; labels: list[str]; identities: list[str]
    primary_event_id: str | None; peak_severity: str; atom_count: int
```
If `test_golden_contract_fixtures.py` requires fixtures for every wire
model, add `v1_encounters.json` / `v1_encounter.json` via
`CONTRACT_GOLDEN_REGEN=1` and update MANIFEST.json.

## Tests (pytest, no new deps, Python 3.10-compatible)

* `tests/test_encounters_linker.py`: raccoon chain alley-wide → shed →
  stairway-wide within gaps joins one encounter; same raccoon after 10 min
  on a non-adjacent camera starts a new one; person on a non-adjacent
  camera with no shared zone does not join; identity match joins despite
  no adjacency; contradictory sub_labels split; person + dog co-present on
  one camera then dog alone on an adjacent camera 5 s later → one
  encounter with reason "companion"; sealed encounters are not candidates;
  pin/split decisions honoured; `fold` is idempotent (running twice over
  the same atoms yields identical grouping); max_duration cap.
* `tests/test_encounters_store.py`: schema applies, upsert twice updates
  not duplicates, recompute aggregates, seal_stale, watermark.
* `tests/test_encounters_service.py`: reconcile over conftest Frigate DB
  with inserted `reviewsegment` rows (including one with NULL end_time and a
  camera with a clock offset); live `observe_review` then reconcile agrees
  (no duplicate atoms, end_time filled in); replay
  `tests/fixtures/capture-charger-loiter.jsonl` reviews through
  `observe_review` and assert a sane grouping; an exception inside
  observe_review does not propagate through `PushEngine.handle_event`.
* `tests/test_encounters_routes.py`: HTML pages 200, JSON shapes validate
  against the wire models, adjacency endpoint.
* `tests/test_guide.py` must pass: add `guide_content/encounters.md`
  (frontmatter `routes: ["/encounters", "/encounters/{id}"]`, `config:
  ["encounters"]`; body explains the feature and mentions every config
  field in backticks).

## Verification bar

`ruff check`, `ruff format --check`, `mypy`, `pytest` all green. Do not
touch push ladder/card logic beyond the `on_review` hook.
