import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from middleware import outbound_panel as panel


def test_contacts_perf_trace_masks_advisor_and_reports_slow_step(caplog, monkeypatch):
    monkeypatch.setattr(panel, "CONTACTS_PERF_TOTAL_SLOW_MS", 0.0)
    monkeypatch.setattr(panel, "CONTACTS_PERF_STEP_SLOW_MS", 0.0)

    trace = panel._ContactsPerfTrace(
        advisor="89096380",
        filter_time="24h",
        page=1,
        limit=30,
    )
    trace.mark("load_active")

    with caplog.at_level(logging.WARNING):
        trace.emit(status="ok", contact_count=12)

    assert "[OBS] source=panel component=contacts op=perf_trace" in caplog.text
    assert "advisor:***6380#" in caplog.text
    assert "89096380" not in caplog.text
    assert "load_active=" in caplog.text
