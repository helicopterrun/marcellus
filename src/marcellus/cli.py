"""Top-level CLI for Marcellus."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import typer

from marcellus import __version__
from marcellus.config import load_settings
from marcellus.tables import render_table

app = typer.Typer(
    name="fsc",
    help="Marcellus: triage UI + read-only analysis for Frigate NVR.",
    no_args_is_help=True,
)

triage_app = typer.Typer(help="Sample borderline events and record tp/fp/skip labels.")
analysis_app = typer.Typer(help="Read-only analyses over Frigate's DB and live API.")
face_capture_app = typer.Typer(
    help="High-res cross-camera face capture: grab the ID camera's full-res frame "
    "when a front camera sees a person."
)
scrub_app = typer.Typer(help="Uniform-cadence scrub-cache generation (sprite sheets).")
encounters_app = typer.Typer(help="Encounters: linked chains of Frigate review segments.")
app.add_typer(triage_app, name="triage")
app.add_typer(analysis_app, name="analysis")
app.add_typer(face_capture_app, name="face-capture")
app.add_typer(scrub_app, name="scrub")
app.add_typer(encounters_app, name="encounters")


@app.command()
def serve() -> None:
    """Run the HTTP server."""
    from marcellus.server import run

    run()


@app.command()
def watchdog() -> None:
    """Run the Frigate health watchdog (restarts the container on a hung backend)."""
    from marcellus.watchdog import run_watchdog

    raise typer.Exit(code=run_watchdog(load_settings()))


@app.command()
def init(
    output: str = typer.Option(
        "config/sidecar.yml", "--output", "-o", help="Where to write the config file."
    ),
    frigate_url: str | None = typer.Option(
        None, help="Frigate's unauthenticated API origin, e.g. http://frigate.lan:5000"
    ),
    proxy_url: str | None = typer.Option(
        None, help="Frigate's AUTHENTICATED origin, e.g. http://frigate.lan:8971"
    ),
    frigate_config: str | None = typer.Option(
        None, help="Path to Frigate's config.yml as seen by the sidecar."
    ),
    frigate_db: str | None = typer.Option(
        None, help="Path to Frigate's frigate.db as seen by the sidecar."
    ),
    recordings_path: str | None = typer.Option(
        None, help="Host-side recordings root that replaces frigate.media_path."
    ),
    sidecar_db: str | None = typer.Option(None, help="Path for the sidecar's own SQLite DB."),
    bind_host: str | None = typer.Option(None),
    bind_port: int | None = typer.Option(None),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt; use flags where given and defaults otherwise.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    """Generate a sidecar.yml interactively (or from flags) and sanity-check it.

    Prompts only for the handful of values that differ per deployment; every
    other setting keeps its documented default and can be edited in the
    generated file afterwards (see config/sidecar.example.yml for the full
    annotated reference).
    """
    from pathlib import Path

    import yaml

    from marcellus.config import FrigateSection, Settings, SidecarSection

    frigate_defaults = FrigateSection()
    sidecar_defaults = SidecarSection()

    def ask(label: str, flag: str | int | None, default: str) -> str:
        if flag is not None:
            return str(flag)
        if non_interactive:
            return default
        return str(typer.prompt(label, default=default))

    out_path = Path(output)
    if out_path.exists() and not force:
        typer.echo(f"{out_path} already exists; use --force to overwrite.", err=True)
        raise typer.Exit(code=1)

    cfg: dict[str, Any] = {
        "frigate": {
            "base_url": ask(
                "Frigate API URL (unauthenticated origin)", frigate_url, frigate_defaults.base_url
            ),
            "proxy_base_url": ask(
                "Frigate AUTHENTICATED origin (usually port 8971)",
                proxy_url,
                frigate_defaults.proxy_base_url,
            ),
            "config_path": ask(
                "Path to Frigate's config.yml", frigate_config, str(frigate_defaults.config_path)
            ),
            "db_path": ask(
                "Path to Frigate's frigate.db", frigate_db, str(frigate_defaults.db_path)
            ),
            "recordings_path": ask(
                "Host-side recordings root (replaces media_path in recordings.path)",
                recordings_path,
                str(frigate_defaults.recordings_path),
            ),
        },
        "sidecar": {
            "db_path": ask("Sidecar SQLite DB path", sidecar_db, str(sidecar_defaults.db_path)),
            "bind_host": ask("Bind host", bind_host, sidecar_defaults.bind_host),
            "bind_port": int(ask("Bind port", bind_port, str(sidecar_defaults.bind_port))),
        },
    }

    # Validate through the real settings model so a bad value fails here, not
    # at first server start.
    settings = Settings.model_validate(cfg)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by `fsc init`. All other settings keep their defaults --\n"
        "# see config/sidecar.example.yml for the full annotated reference.\n"
    )
    out_path.write_text(header + yaml.safe_dump(cfg, sort_keys=False))
    typer.echo(f"Wrote {out_path}")

    # Sanity checks: warn, never fail -- the config may be written on a box
    # where Frigate isn't reachable yet (e.g. inside `docker run --rm`).
    import httpx

    try:
        resp = httpx.get(settings.frigate.base_url.rstrip("/") + "/api/version", timeout=5.0)
        resp.raise_for_status()
        typer.echo(f"✓ Frigate reachable at {settings.frigate.base_url} ({resp.text.strip()})")
    except Exception as exc:  # noqa: BLE001 -- diagnostics only
        typer.echo(f"⚠ Could not reach Frigate at {settings.frigate.base_url}: {exc}", err=True)
    db = Path(cfg["frigate"]["db_path"])
    if db.exists():
        typer.echo(f"✓ Frigate DB found at {db}")
    else:
        typer.echo(f"⚠ Frigate DB not found at {db} (check your mount/path)", err=True)
    rec = Path(cfg["frigate"]["recordings_path"])
    if rec.is_dir():
        typer.echo(f"✓ Recordings path exists at {rec}")
    else:
        typer.echo(
            f"⚠ Recordings path {rec} does not exist (scrub cache would find nothing)", err=True
        )


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command()
def backup(
    dest: str = typer.Argument(..., help="Directory to write, or a path ending in .tar.gz."),
) -> None:
    """Back up the sidecar DB, `.session_secret`, and the resolved config file.

    Scrub cache and face-model directories are NOT included -- both are
    regenerable from Frigate's own recordings/DB.
    """
    from pathlib import Path

    from marcellus.backup import BackupError, create_backup

    try:
        manifest = create_backup(load_settings(), Path(dest))
    except BackupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Wrote backup to {dest} ({len(manifest.files)} files, version {manifest.version})")


@app.command()
def restore(
    src: str = typer.Argument(..., help="A backup directory or .tar.gz from `fsc backup`."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Confirm marcellus is stopped and proceed with the restore.",
    ),
) -> None:
    """Restore a backup made by `fsc backup`. Stop marcellus first."""
    from pathlib import Path

    from marcellus.backup import BackupError, restore_backup

    try:
        restore_backup(load_settings(), Path(src), force=force)
    except BackupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Restored from {src}")


# ----- Triage subcommands -----


@triage_app.command("sample")
def triage_sample(
    days: int = typer.Option(14),
    n: int = typer.Option(30),
    camera: str | None = typer.Option(None),
    label: str | None = typer.Option(None),
    seed: int | None = typer.Option(None),
) -> None:
    """Sample borderline events as JSONL on stdout."""
    from marcellus.triage.sampler import sample

    s = load_settings()
    selected = sample(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        api_base_url=s.frigate.base_url,
        days=days,
        n=n,
        camera=camera,
        label=label,
        seed=seed,
    )
    for ev in selected:
        typer.echo(json.dumps(ev))
    typer.echo(f"# selected {len(selected)} of {n} target", err=True)


@triage_app.command("record")
def triage_record(
    event_id: str = typer.Option(..., "--event-id"),
    label: str = typer.Option(..., "--label", help="fp | tp | skip"),
    note: str | None = typer.Option(None),
    session: str | None = typer.Option(None),
    force: bool = typer.Option(False, "--force/--no-force"),
) -> None:
    """Record a triage label for one event."""
    from marcellus.triage.recorder import (
        AlreadyLabeledError,
        EventNotFoundError,
        record,
    )

    s = load_settings()
    try:
        result = record(
            frigate_db=s.frigate.db_path,
            sidecar_db=s.sidecar.db_path,
            event_id=event_id,
            label=label,  # type: ignore[arg-type]
            note=note,
            session=session,
            force=force,
        )
    except EventNotFoundError:
        typer.echo(
            json.dumps({"id": event_id, "ok": False, "error": "event not found in frigate.db"}),
            err=True,
        )
        raise typer.Exit(code=3) from None
    except AlreadyLabeledError as exc:
        typer.echo(
            json.dumps(
                {
                    "id": event_id,
                    "ok": False,
                    "before": exc.existing,
                    "error": f"already labeled '{exc.existing}'; use --force to overwrite",
                }
            ),
            err=True,
        )
        raise typer.Exit(code=4) from None
    typer.echo(json.dumps({**result, "ok": True}))


@triage_app.command("clear")
def triage_clear(event_id: str = typer.Option(..., "--event-id")) -> None:
    """Delete the triage label for one event."""
    from marcellus.triage.recorder import clear

    s = load_settings()
    result = clear(sidecar_db=s.sidecar.db_path, event_id=event_id)
    typer.echo(json.dumps({**result, "ok": True}))


@triage_app.command("stats")
def triage_stats() -> None:
    """Print counts of triage labels."""
    from marcellus.triage.recorder import stats

    s = load_settings()
    typer.echo(json.dumps(stats(sidecar_db=s.sidecar.db_path)))


# ----- Face-capture subcommands -----


@face_capture_app.command("scan")
def face_capture_scan() -> None:
    """Capture the ID camera's full-res frame for every new trigger event."""
    from marcellus.faces import crosscam

    s = load_settings()
    summary = crosscam.scan(s)
    typer.echo(json.dumps(summary))
    if summary.get("problems"):
        raise typer.Exit(code=2)


@face_capture_app.command("prune")
def face_capture_prune() -> None:
    """Drop captures past face_capture.retention_days."""
    from marcellus.faces import crosscam

    typer.echo(json.dumps(crosscam.prune(load_settings())))


@encounters_app.command("prune")
def encounters_prune() -> None:
    """Drop sealed encounters (and their members/decisions) past
    encounters.retention_days."""
    import time

    from marcellus import db
    from marcellus.encounters import store

    s = load_settings()
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        result = store.prune(conn, time.time(), s.encounters.retention_days)
    finally:
        conn.close()
    typer.echo(json.dumps(result))


@encounters_app.command("purge-phantoms")
def encounters_purge_phantoms(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be removed only."),
) -> None:
    """Drop phantom encounter members synthesized by the pre-fix push
    backfill (zero-start members with no matching Frigate `reviewsegment`
    row)."""
    import time

    from marcellus import db
    from marcellus.encounters import store

    s = load_settings()
    try:
        frigate_conn = db.open_frigate_ro(s.frigate.db_path)
    except Exception as exc:  # noqa: BLE001 -- never delete anything on a bad Frigate DB
        typer.echo(f"encounters purge-phantoms: could not open Frigate DB: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    sidecar_conn = db.open_sidecar(s.sidecar.db_path)
    try:
        result = store.purge_phantoms(sidecar_conn, frigate_conn, time.time(), dry_run=dry_run)
    finally:
        sidecar_conn.close()
        frigate_conn.close()
    typer.echo(json.dumps(result))


@face_capture_app.command("stats")
def face_capture_stats(days: int = typer.Option(7, min=1)) -> None:
    """Counts by status/review plus the last-run heartbeat."""
    import time

    from marcellus import db
    from marcellus.faces import crosscam

    s = load_settings()
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        since = time.time() - days * 86400
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) n FROM face_captures "
                "WHERE trigger_start_ts >= ? GROUP BY status",
                (since,),
            )
        }
        by_review = {
            r["review"]: r["n"]
            for r in conn.execute(
                "SELECT review, COUNT(*) n FROM face_captures "
                "WHERE trigger_start_ts >= ? GROUP BY review",
                (since,),
            )
        }
        total_bytes = conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) b FROM face_captures WHERE trigger_start_ts >= ?",
            (since,),
        ).fetchone()["b"]
    finally:
        conn.close()
    typer.echo(
        json.dumps(
            {
                "days": days,
                "by_status": by_status,
                "by_review": by_review,
                "bytes": total_bytes,
                "last_run": crosscam.read_last_run(s),
            },
            indent=2,
        )
    )


# ----- Analysis subcommands -----


@analysis_app.command("score-histogram")
def analysis_score_histogram(
    days: int = typer.Option(14),
    camera: str | None = typer.Option(None),
    label: str | None = typer.Option(None),
    min_samples: int = typer.Option(30),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Score distribution + min_score / threshold suggestions per (camera,label)."""
    from marcellus.analysis import score_histogram

    s = load_settings()
    result = score_histogram.analyze(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        days=days,
        camera=camera,
        label=label,
        min_samples=min_samples,
    )
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    headers = [
        "camera",
        "label",
        "n",
        "n_tp",
        "n_fp",
        "median_score",
        "p10_top",
        "p25_top",
        "p50_top",
        "p75_top",
        "suggested_min_score",
        "suggested_threshold",
        "confidence",
    ]
    typer.echo(render_table(headers, result["rows"]))


@analysis_app.command("motion-rate")
def analysis_motion_rate(
    days: int = typer.Option(14),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Per-camera event rate + spikiness + suggestions."""
    from marcellus.analysis import motion_rate

    s = load_settings()
    rows = motion_rate.analyze(frigate_db=s.frigate.db_path, days=days)
    if output_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    headers = [
        "camera",
        "events_total",
        "events_per_hr_avg",
        "events_per_hr_p95",
        "peak_hour_count",
        "spikiness",
        "night_ratio",
        "suggestion",
    ]
    typer.echo(render_table(headers, rows))


@analysis_app.command("fps-budget")
def analysis_fps_budget(output_json: bool = typer.Option(False, "--json")) -> None:
    """Detector inference budget vs configured demand."""
    from marcellus.analysis import fps_budget

    s = load_settings()
    result = fps_budget.analyze(frigate_base_url=s.frigate.base_url)
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo("## Detectors")
    typer.echo(
        render_table(
            ["name", "inference_ms", "implied_fps_per_detector", "thermal_flag"],
            result["detectors"],
        )
    )
    typer.echo("\n## Per-camera demand")
    typer.echo(
        render_table(
            [
                "camera",
                "configured_detect_fps",
                "observed_detection_fps",
                "observed_skipped_fps",
                "gap_pct",
            ],
            result["cameras"],
        )
    )
    typer.echo(
        f"\n**Budget:** {result['total_budget_fps']} fps  "
        f"**Demand:** {result['total_demand_fps']} fps  "
        f"**Util:** {result['utilization_pct']}%  "
        f"**Headroom:** {result['headroom_fps']} fps"
    )
    for rec in result["recommendations"]:
        typer.echo(f"- {rec}")


@analysis_app.command("motion-active")
def analysis_motion_active(
    days: int = typer.Option(14),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Per-camera raw motion activity and yield."""
    from marcellus.analysis import motion_active

    s = load_settings()
    result = motion_active.analyze(frigate_base_url=s.frigate.base_url, days=days)
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    headers = [
        "camera",
        "class",
        "mu_per_hr",
        "events_per_hr",
        "yield_per_kmu",
        "obs_det_fps",
        "cfg_det_fps",
        "motion_threshold",
        "hours_with_data",
    ]
    typer.echo(render_table(headers, result["rows"]))


@analysis_app.command("motion-compare")
def analysis_motion_compare(
    baseline: str = typer.Option(..., "--baseline"),
    target: str = typer.Option(..., "--target"),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """A/B motion comparison across two date ranges."""
    from marcellus.analysis import motion_compare

    s = load_settings()
    result = motion_compare.analyze(
        frigate_base_url=s.frigate.base_url, baseline=baseline, target=target
    )
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"# baseline {result['baseline']} vs target {result['target']}\n")
    headers = [
        "camera",
        "class",
        "base_mu_per_hr",
        "tgt_mu_per_hr",
        "ratio",
        "base_yield_per_kmu",
        "tgt_yield_per_kmu",
        "motion_threshold",
        "suggestion",
    ]
    typer.echo(render_table(headers, result["rows"]))


@analysis_app.command("zone-hits")
def analysis_zone_hits(
    days: int = typer.Option(30),
    camera: str | None = typer.Option(None),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Per-camera zone hit-map + mask candidates."""
    from marcellus.analysis import zone_hits

    s = load_settings()
    result = zone_hits.analyze(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        days=days,
        camera=camera,
    )
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"## Zone hits (last {result['days']} days)\n")
    typer.echo(render_table(["camera", "zone", "label", "n", "fp_in_triage"], result["hits"]))
    typer.echo("\n## Possible mask candidates\n")
    if not result["mask_candidates"]:
        typer.echo("_none_")
    else:
        typer.echo(
            render_table(
                [
                    "camera",
                    "label",
                    "cluster_size",
                    "centroid_x",
                    "centroid_y",
                    "sample_event_id",
                    "reason",
                ],
                result["mask_candidates"],
            )
        )


@analysis_app.command("pull-events")
def analysis_pull_events(
    days: int = typer.Option(14),
    camera: str | None = typer.Option(None),
    label: str | None = typer.Option(None),
) -> None:
    """Dump events as JSONL on stdout."""
    from marcellus.analysis import pull_events

    s = load_settings()
    n = 0
    for ev in pull_events.pull(frigate_db=s.frigate.db_path, days=days, camera=camera, label=label):
        typer.echo(json.dumps(ev))
        n += 1
    typer.echo(f"# wrote {n} events", err=True)


@analysis_app.command("annotation-offset")
def analysis_annotation_offset(
    days: int = typer.Option(7),
    camera: str | None = typer.Option(None),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Measured detect.annotation_offset (ms) per camera via template matching.

    Requires the `[annotation]` extra: `pip install "marcellus[annotation]"`.
    """
    from marcellus.analysis import annotation_offset

    s = load_settings()
    try:
        result = annotation_offset.analyze(
            frigate_db=s.frigate.db_path,
            frigate_base_url=s.frigate.base_url,
            days=days,
            camera=camera,
        )
    except annotation_offset.AnnotationOffsetUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    if output_json:
        typer.echo(json.dumps(result, indent=2))
        return
    headers = [
        "camera",
        "n_contributing_events",
        "n_qualifying_events",
        "p25_ms",
        "median_offset_ms",
        "p75_ms",
        "iqr_ms",
        "suggested_offset_ms",
        "confidence",
    ]
    typer.echo(render_table(headers, result))


# ----- Scrub subcommands (docs/scrub-cache-and-proxy-spec.md §5.7) -----

# Backfill runs the same generator in a loop; this bounds a run that never
# converges (e.g. the live edge outrunning it) instead of looping forever.
_BACKFILL_MAX_CYCLES = 500


@contextlib.contextmanager
def _scrub_cache_lock(settings: Any) -> Iterator[None]:
    """Acquire the scrub cache single-writer lock for a CLI command, exiting 2
    with the holder's identity on stderr if another writer already has it.
    CLI commands never wait for it (`timeout_s=0`) -- the service loop is the
    only holder expected to be slow to release, and it isn't the CLI's job to
    wait it out."""
    from marcellus.scrub.lock import ScrubCacheLock, ScrubLockHeld

    lock = ScrubCacheLock(settings.scrub.cache_dir, timeout_s=0.0)
    try:
        lock.acquire()
    except ScrubLockHeld as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    try:
        yield
    finally:
        lock.release()


@scrub_app.command("generate")
def scrub_generate(
    camera: str | None = typer.Option(None, "--camera"),
    output_json: bool = typer.Option(False, "--json"),
) -> None:
    """Run one generation cycle now (forward edge). Mirrors what the in-process
    ~60s loop does, for a systemd-timer deployment (§5.4 option (b)) or manual
    backfill kickstart."""
    import asyncio

    from marcellus.scrub.generator import generate_cycle

    s = load_settings()
    if camera:
        s = s.model_copy(update={"scrub": s.scrub.model_copy(update={"cameras": [camera]})})
    with _scrub_cache_lock(s):
        results = asyncio.run(generate_cycle(s))
    if output_json:
        typer.echo(json.dumps(results))
        return
    for r in results:
        typer.echo(json.dumps(r))


@scrub_app.command("backfill")
def scrub_backfill(
    camera: str = typer.Option(..., "--camera"),
    days: int = typer.Option(4, help="Capped at scrub.retention_days regardless of this value."),
) -> None:
    """One-time history fill for `camera`, repeatedly cycling until the
    generator catches up to `now` (§5.7). Cadence is still the same
    interval-verified generator -- this just loops it instead of waiting on
    the 60s timer."""
    import asyncio
    import time as _time

    from marcellus.scrub.generator import generate_cycle

    s = load_settings()
    # `--days` bounds the window the generator actually walks: it is the
    # retention horizon for this run, capped by the configured one. Without
    # this the option was inert and every backfill covered full retention.
    days = max(1, min(days, s.scrub.retention_days))
    s = s.model_copy(
        update={"scrub": s.scrub.model_copy(update={"cameras": [camera], "retention_days": days})}
    )
    start = _time.time()
    cutoff = start - days * 86400
    total_frames = 0
    # Safety stop: the live edge keeps producing frames, so "no new frames"
    # is the intended exit but must not be the only one.
    with _scrub_cache_lock(s):
        for _ in range(_BACKFILL_MAX_CYCLES):
            results = asyncio.run(generate_cycle(s))
            new_frames = sum(r.get("new_frames", 0) for r in results)
            total_frames += new_frames
            if new_frames == 0:
                break
        else:
            typer.echo(
                f"# stopped after {_BACKFILL_MAX_CYCLES} cycles; re-run to continue", err=True
            )
    typer.echo(
        json.dumps({"camera": camera, "days": days, "since": cutoff, "new_frames": total_frames})
    )


@scrub_app.command("verify")
def scrub_verify(
    camera: str | None = typer.Option(None, "--camera"),
    interval: float | None = typer.Option(None, "--interval"),
    repair: bool = typer.Option(False, "--repair", help="Republish over-claiming sheets."),
) -> None:
    """Find sheets whose index entry claims more cells than they render.

    Publication can no longer produce one, but sheets written before that fix
    keep their inflated count -- their span has passed, so nothing republishes
    them. `--repair` republishes each at its true count (same pixels, honest
    name) and retires the over-claiming version.
    """
    from marcellus.scrub.repair import verify_and_repair

    s = load_settings()
    if repair:
        with _scrub_cache_lock(s):
            result = verify_and_repair(s, camera=camera, interval=interval, repair=repair)
    else:
        # Read-only: does not take the lock.
        result = verify_and_repair(s, camera=camera, interval=interval, repair=repair)
    typer.echo(json.dumps(result, default=str))


@scrub_app.command("prune")
def scrub_prune(
    camera: str | None = typer.Option(None, "--camera"),
    drop_interval: list[float] = typer.Option(  # noqa: B008 -- typer's list-option idiom
        [],
        "--drop-interval",
        help="Unconditionally delete every bucket/sheet at exactly this interval_s, "
        "regardless of retention -- e.g. migrating off an old coarse_intervals_s "
        "default (10.0/60.0) that has no successor in scrub.derived_intervals_s. "
        "Repeatable. Runs in addition to the normal retention-based prune below.",
    ),
) -> None:
    """Drop sheets/buckets past scrub.retention_days, oldest-first."""
    from marcellus.scrub.generator import drop_intervals, prune

    s = load_settings()
    result: dict[str, object] = {}
    with _scrub_cache_lock(s):
        if drop_interval:
            result["dropped_intervals"] = drop_intervals(s, drop_interval, camera=camera)
        result.update(prune(s, camera=camera))
    typer.echo(json.dumps(result))


@scrub_app.command("coverage")
def scrub_coverage_cmd(camera: str = typer.Option(..., "--camera")) -> None:
    """Print what's generated for `camera` (debug)."""
    import time as _time

    from marcellus import db

    s = load_settings()
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        buckets = db.list_scrub_buckets(conn, camera, 0, _time.time())
        generated_through = db.latest_generated_through(conn, camera)
    finally:
        conn.close()
    typer.echo(
        json.dumps(
            {"camera": camera, "buckets": buckets, "generated_through": generated_through},
            default=str,
        )
    )


if __name__ == "__main__":
    app()
