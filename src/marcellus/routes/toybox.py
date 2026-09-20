"""Toybox: for-fun mini-games. Currently a 50-states map quiz.

This page is deliberately unrelated to Frigate analysis — it's a couch game.
The only server-side state is an arcade-style high-score board persisted in the
sidecar DB (`toybox_scores`). The game itself is fully client-side
(`static/js/toybox.js` + `static/js/toybox_states.js`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from marcellus.config import Settings
from marcellus.db import open_sidecar
from marcellus.errors import error_detail
from marcellus.routes._deps import settings_of as _settings

router = APIRouter(tags=["toybox"])

# Only one game today, but the board is namespaced by `game` so adding another
# later doesn't require a schema change.
GAME_STATES50: Final = "states50"
_TOP_N = 10
_NAME_MAX = 8  # arcade initials are short; keep the board tidy


def _top_scores(settings: Settings, game: str, limit: int = _TOP_N) -> list[dict[str, Any]]:
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        rows = conn.execute(
            """
            SELECT name, score, played_at
              FROM toybox_scores
             WHERE game = ?
             ORDER BY score DESC, played_at ASC
             LIMIT ?
            """,
            (game, limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"rank": i + 1, "name": r["name"], "score": r["score"], "played_at": r["played_at"]}
        for i, r in enumerate(rows)
    ]


@router.get("/toybox", response_class=HTMLResponse)
def toybox_view(request: Request) -> Any:
    templates = request.app.state.templates
    scores = _top_scores(_settings(request), GAME_STATES50)
    return templates.TemplateResponse(
        request,
        "toybox.html",
        {"scores": scores, "counts": {}},
    )


@router.get("/toybox/scores")
def toybox_scores(request: Request, game: str = GAME_STATES50) -> JSONResponse:
    if game not in KNOWN_GAMES:
        raise HTTPException(
            status_code=404, detail=error_detail("unknown_game", f"unknown game: {game}")
        )
    return JSONResponse({"game": game, "scores": _top_scores(_settings(request), game)})


#: Boards that exist. `game` namespaces the table, so accepting free text let
#: a caller create unbounded leaderboards nobody can see.
KNOWN_GAMES = (GAME_STATES50,)


class ScorePayload(BaseModel):
    name: str = Field(..., min_length=1)
    score: int = Field(..., ge=0, le=50)
    game: Literal["states50"] = GAME_STATES50


@router.post("/toybox/scores")
def toybox_submit(payload: ScorePayload, request: Request) -> JSONResponse:
    # Sanitize the arcade name: uppercase, alnum only, capped length.
    name = "".join(c for c in payload.name.upper() if c.isalnum())[:_NAME_MAX]
    if not name:
        raise HTTPException(
            status_code=400,
            detail=error_detail("invalid_name", "name must contain a letter or digit"),
        )

    settings = _settings(request)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "INSERT INTO toybox_scores (game, name, score, played_at) VALUES (?, ?, ?, ?)",
            (payload.game, name, payload.score, now),
        )
        conn.commit()
    finally:
        conn.close()

    board = _top_scores(settings, payload.game)
    # Rank of this run on the (possibly truncated) board, if it made the cut.
    rank = next(
        (e["rank"] for e in board if e["name"] == name and e["score"] == payload.score),
        None,
    )
    return JSONResponse({"ok": True, "name": name, "rank": rank, "scores": board})
