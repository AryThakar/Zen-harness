"""Run ML tasks and record them in ml_runs.

A run is created as 'running', computed with a read cursor (the slow part runs
outside the write lock), then its results are saved and the run marked
'succeeded' or 'failed' in one transaction.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from app.db.connection import Database

from . import anomalies, forecast, segments
from .base import NotEnoughData, TaskResult

log = logging.getLogger(__name__)

TASKS: dict[str, Callable[..., TaskResult]] = {
    "forecast": forecast.run,
    "segments": segments.run,
    "anomalies": anomalies.run,
}


def start(db: Database, task: str, params: dict) -> tuple[int, bool]:
    """Create a run; returns (run_id, created). An identical run that is still
    going is returned instead of starting a second one (a double click)."""
    if task not in TASKS:
        raise ValueError(f"Unknown task '{task}'")
    encoded = json.dumps(params, sort_keys=True)
    with db.transaction() as cur:
        running = cur.execute(
            "SELECT run_id FROM ml_runs WHERE task = ? AND status = 'running' AND params::VARCHAR = ?",
            [task, encoded],
        ).fetchone()
        if running:
            return running[0], False
        run_id = cur.execute(
            "INSERT INTO ml_runs (run_id, task, status, params) VALUES (nextval('ml_run_id_seq'), ?, 'running', ?) "
            "RETURNING run_id",
            [task, encoded],
        ).fetchone()[0]
        return run_id, True


def execute(db: Database, run_id: int, task: str, params: dict) -> None:
    """Compute and store one run. Never raises: failures are recorded on the run."""
    cur = db.cursor()
    try:
        result = TASKS[task](cur, **params)
    except NotEnoughData as exc:
        _finish(db, run_id, "failed", str(exc))
        return
    except Exception as exc:  # recorded for the user; the traceback goes to the log
        log.exception("ML run %s (%s) failed", run_id, task)
        _finish(db, run_id, "failed", f"Unexpected error: {exc}")
        return
    finally:
        cur.close()
    with db.transaction() as cur:
        result.save(cur, run_id)
        cur.execute(
            "UPDATE ml_runs SET status = 'succeeded', metrics = ?, message = ?, finished_at = current_timestamp "
            "WHERE run_id = ?",
            [json.dumps(result.metrics), result.message, run_id],
        )


def run(db: Database, task: str, params: dict | None = None) -> int:
    """Start and execute a run synchronously; returns the run_id."""
    params = params or {}
    run_id, created = start(db, task, params)
    if created:
        execute(db, run_id, task, params)
    return run_id


def mark_interrupted(db: Database) -> int:
    """Runs still 'running' when the app starts were cut off by a restart."""
    with db.transaction() as cur:
        return cur.execute(
            "UPDATE ml_runs SET status = 'failed', message = 'Interrupted because the server restarted.', "
            "finished_at = current_timestamp WHERE status = 'running'"
        ).fetchone()[0]


def _finish(db: Database, run_id: int, status: str, message: str) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE ml_runs SET status = ?, message = ?, finished_at = current_timestamp WHERE run_id = ?",
            [status, message, run_id],
        )
