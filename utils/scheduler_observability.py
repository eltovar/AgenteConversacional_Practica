"""Observabilidad no invasiva para APScheduler."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Tuple

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)

from utils.safe_logging import obs_event


SCHEDULER_OBS_EVENT_MASK = (
    EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
)


def _safe_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return "none"


def scheduler_event_to_obs(event: Any) -> Tuple[str, str]:
    """Convierte eventos de APScheduler a una linea `[OBS]` sin alterar jobs."""
    code = getattr(event, "code", None)
    job_id = getattr(event, "job_id", "unknown")
    scheduled_run_time = _safe_datetime(getattr(event, "scheduled_run_time", None))

    fields = {
        "job": job_id,
        "scheduled_run_time": scheduled_run_time,
    }

    if code == EVENT_JOB_EXECUTED:
        return "info", obs_event(
            "scheduler",
            "apscheduler",
            "job_executed",
            status="ok",
            **fields,
        )

    if code == EVENT_JOB_ERROR:
        fields["error"] = getattr(event, "exception", None)
        return "error", obs_event(
            "scheduler",
            "apscheduler",
            "job_error",
            status="error",
            **fields,
        )

    if code == EVENT_JOB_MISSED:
        return "warning", obs_event(
            "scheduler",
            "apscheduler",
            "job_missed",
            status="missed",
            **fields,
        )

    if code == EVENT_JOB_MAX_INSTANCES:
        return "warning", obs_event(
            "scheduler",
            "apscheduler",
            "job_max_instances",
            status="skipped",
            **fields,
        )

    return "debug", obs_event(
        "scheduler",
        "apscheduler",
        "job_event",
        status="unknown",
        code=code if code is not None else "none",
        **fields,
    )


def log_scheduler_event(logger: Any, event: Any) -> None:
    level, message = scheduler_event_to_obs(event)
    getattr(logger, level, logger.debug)(message)
