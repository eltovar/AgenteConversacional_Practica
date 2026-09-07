from datetime import datetime, timezone
from types import SimpleNamespace

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED

from utils.scheduler_observability import scheduler_event_to_obs


def test_scheduler_event_to_obs_reports_successful_job():
    event = SimpleNamespace(
        code=EVENT_JOB_EXECUTED,
        job_id="check_appointment_reminders",
        scheduled_run_time=datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc),
    )

    level, message = scheduler_event_to_obs(event)

    assert level == "info"
    assert "[OBS] source=scheduler component=apscheduler op=job_executed status=ok" in message
    assert "job=check_appointment_reminders" in message
    assert "scheduled_run_time=2026-09-07T08:00:00_00:00" in message


def test_scheduler_event_to_obs_sanitizes_error_payload():
    event = SimpleNamespace(
        code=EVENT_JOB_ERROR,
        job_id="followup_24h",
        scheduled_run_time=datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc),
        exception=RuntimeError(
            "fallo Redis para +57 313 840 5930 url=https://x.test/a?token=secret"
        ),
    )

    level, message = scheduler_event_to_obs(event)

    assert level == "error"
    assert "op=job_error" in message
    assert "573138405930" not in message
    assert "secret" not in message
    assert "phone:***5930#" not in message
    assert "id:***5930#" in message
    assert "url:https://x.test/" in message


def test_scheduler_event_to_obs_reports_missed_job():
    event = SimpleNamespace(
        code=EVENT_JOB_MISSED,
        job_id="bulk_campaign_tick",
        scheduled_run_time=None,
    )

    level, message = scheduler_event_to_obs(event)

    assert level == "warning"
    assert "op=job_missed" in message
    assert "status=missed" in message
    assert "job=bulk_campaign_tick" in message
    assert "scheduled_run_time=none" in message
