from __future__ import annotations

from pathlib import Path

import pytest

from marcellus.triage import recorder


def test_record_inserts_label(frigate_db_path: Path, sidecar_db_path: Path) -> None:
    out = recorder.record(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        event_id="e1",
        label="fp",
        note="porch shadow",
    )
    assert out == {"id": "e1", "before": None, "after": "fp"}


def test_record_rejects_missing_event(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    with pytest.raises(recorder.EventNotFoundError):
        recorder.record(
            frigate_db=frigate_db_path,
            sidecar_db=sidecar_db_path,
            event_id="nope",
            label="fp",
        )


def test_record_blocks_overwrite_without_force(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    recorder.record(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
        event_id="e1", label="fp",
    )
    with pytest.raises(recorder.AlreadyLabeledError) as exc_info:
        recorder.record(
            frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
            event_id="e1", label="tp",
        )
    assert exc_info.value.existing == "fp"


def test_record_force_overwrites(frigate_db_path: Path, sidecar_db_path: Path) -> None:
    recorder.record(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
        event_id="e1", label="fp",
    )
    out = recorder.record(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
        event_id="e1", label="tp", force=True,
    )
    assert out == {"id": "e1", "before": "fp", "after": "tp"}


def test_record_rejects_bad_label(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    with pytest.raises(ValueError):
        recorder.record(
            frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
            event_id="e1", label="bogus",  # type: ignore[arg-type]
        )


def test_clear_removes_label(frigate_db_path: Path, sidecar_db_path: Path) -> None:
    recorder.record(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
        event_id="e1", label="fp",
    )
    out = recorder.clear(sidecar_db=sidecar_db_path, event_id="e1")
    assert out == {"id": "e1", "cleared": 1}
    # Re-clearing is a no-op.
    out2 = recorder.clear(sidecar_db=sidecar_db_path, event_id="e1")
    assert out2["cleared"] == 0


def test_stats_counts_by_label(frigate_db_path: Path, sidecar_db_path: Path) -> None:
    for eid, lbl in [("e1", "fp"), ("e2", "fp"), ("e3", "tp"), ("e4", "skip")]:
        recorder.record(
            frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
            event_id=eid, label=lbl,  # type: ignore[arg-type]
        )
    out = recorder.stats(sidecar_db=sidecar_db_path)
    assert out["total"] == 4
    assert out["by_label"] == {"fp": 2, "tp": 1, "skip": 1}
