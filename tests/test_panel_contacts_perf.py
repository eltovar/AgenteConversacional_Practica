import logging
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from middleware import outbound_panel as panel

PANEL_JS = Path(ROOT) / "middleware" / "PanelAsesores" / "index.js"
PANEL_PY = Path(ROOT) / "middleware" / "outbound_panel.py"
WEBSOCKET_PY = Path(ROOT) / "middleware" / "websocket_manager.py"


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


def test_contact_visibility_blocks_other_advisor_even_when_channel_matches():
    contact = {
        "phone": "+573001112233",
        "owner_id": "89096378",  # Jubeny
        "canal_origen": "finca_raiz",  # canal de Luisa
        "assigned_owner_ids": [],
    }

    assert not panel._contact_visible_for_advisor(
        contact,
        advisor_id="89096380",  # Luisa
        allowed_channels={"finca_raiz", "metrocuadrado"},
        other_advisor_ids={"89096378", "89096379"},
    )


def test_contact_visibility_allows_owner_and_real_collaborator():
    owned_contact = {
        "phone": "+573001112233",
        "owner_id": "89096380",
        "canal_origen": "mercado_libre",
        "assigned_owner_ids": [],
    }
    collaborative_contact = {
        "phone": "+573004445566",
        "owner_id": "89096378",
        "canal_origen": "mercado_libre",
        "assigned_owner_ids": ["89096380"],
    }

    assert panel._contact_visible_for_advisor(
        owned_contact,
        advisor_id="89096380",
        allowed_channels={"finca_raiz", "metrocuadrado"},
        other_advisor_ids={"89096378", "89096379"},
    )
    assert panel._contact_visible_for_advisor(
        collaborative_contact,
        advisor_id="89096380",
        allowed_channels={"finca_raiz", "metrocuadrado"},
        other_advisor_ids={"89096378", "89096379"},
    )


def test_contact_visibility_keeps_orphan_channel_rescue():
    orphan_contact = {
        "phone": "+573007778899",
        "owner_id": None,
        "canal_origen": "finca_raiz",
        "assigned_owner_ids": [],
    }

    assert panel._contact_visible_for_advisor(
        orphan_contact,
        advisor_id="89096380",
        allowed_channels={"finca_raiz", "metrocuadrado"},
        other_advisor_ids={"89096378", "89096379"},
    )


def test_chat_render_uses_message_content_fallbacks():
    source = PANEL_JS.read_text(encoding="utf-8")

    assert "function getMessageDisplayText(msg)" in source
    assert "msg.message, msg.content, msg.body, msg.text" in source
    assert "let _displayMsg = getMessageDisplayText(msg);" in source
    assert "const _rawContent = getMessageDisplayText(msg);" in source


def test_chat_render_no_longer_reads_msg_message_directly_for_body():
    source = PANEL_JS.read_text(encoding="utf-8")
    render_body = source.split("function renderChatBubbles(messages)", 1)[1]
    render_body = render_body.split("function escapeHtml(text)", 1)[0]

    assert "let _displayMsg = msg.message || '';" not in render_body
    assert "const _safeContent = (msg.message || '')" not in render_body


def test_plain_template_without_content_sid_is_blocked_when_window_is_closed():
    closed_window = panel.WindowStatus(
        is_open=False,
        last_message_time=None,
        time_remaining_seconds=None,
        requires_template=True,
        message="No hay registro de mensaje reciente del cliente.",
    )
    open_window = panel.WindowStatus(
        is_open=True,
        last_message_time=None,
        time_remaining_seconds=300,
        requires_template=False,
        message="Ventana abierta.",
    )

    error = panel._plain_template_closed_window_error(None, closed_window)

    assert "ContentSid aprobado" in error
    assert "ventana de 24 horas abierta" in error
    assert panel._plain_template_closed_window_error("HX123", closed_window) is None
    assert panel._plain_template_closed_window_error(None, open_window) is None


def test_closed_window_delivery_failure_is_not_presented_as_invalid_number():
    source = PANEL_JS.read_text(encoding="utf-8")

    assert "data.error_code === '63016'" in source
    assert "Ventana de 24 horas cerrada" in source
    assert "plantilla aprobada de WhatsApp" in source


def test_chat_history_endpoints_validate_advisor_visibility():
    source = PANEL_PY.read_text(encoding="utf-8")

    detail_endpoint = source.split('async def get_contact_detail(', 1)[1]
    detail_endpoint = detail_endpoint.split('@router.get("/contacts/{phone}/hydrate")', 1)[0]
    by_phone_endpoint = source.split('async def get_conversation_history(', 1)[1]
    by_phone_endpoint = by_phone_endpoint.split('@router.get("/history/{contact_id}")', 1)[0]
    by_contact_endpoint = source.split('async def get_history_by_contact_id(', 1)[1]
    by_contact_endpoint = by_contact_endpoint.split('@router.post("/contacts/{phone}/take-control")', 1)[0]

    for endpoint_source in (detail_endpoint, by_phone_endpoint, by_contact_endpoint):
        assert 'advisor_id: Optional[str] = Query(None' in endpoint_source
        assert "_assert_advisor_can_open_chat(" in endpoint_source


def test_chat_history_frontend_sends_advisor_id():
    source = PANEL_JS.read_text(encoding="utf-8")

    assert "/detail?limit=${CHAT_PAGE_SIZE}&advisor_id=" in source
    assert "/history/${requestedContactId}?limit=${CHAT_PAGE_SIZE}&advisor_id=" in source
    assert "/conversations/${encodeURIComponent(requestedPhone)}?limit=${CHAT_PAGE_SIZE}&advisor_id=" in source


def test_hubspot_batch_contact_payload_includes_channel_origin_for_visibility():
    source = PANEL_PY.read_text(encoding="utf-8")

    assert '"hubspot_owner_id", "canal_origen"' in source
    assert '"canal_origen": props.get("canal_origen") or ""' in source


def test_transfer_response_is_explicit_for_new_visibility_guard():
    backend_source = PANEL_PY.read_text(encoding="utf-8")
    frontend_source = PANEL_JS.read_text(encoding="utf-8")

    assert '"from_owner_name": from_owner_name' in backend_source
    assert '"to_owner_name": to_owner_name' in backend_source
    assert '"transfer_scope": transfer_scope' in backend_source
    assert "Transferencia {transfer_scope} confirmada" in backend_source

    assert "Transferencia ${scopeText} confirmada" in frontend_source
    assert "La nueva validación permitirá abrirlo en el panel de ${toName}" in frontend_source


def test_targeted_new_message_events_keep_advisor_id_for_notification_guard():
    source = WEBSOCKET_PY.read_text(encoding="utf-8")

    assert "targeted_notification = {" in source
    assert '"advisor_id": str(advisor_id)' in source
    assert "await self.publish_to_advisor(redis_client, advisor_id, targeted_notification)" in source


def test_frontend_does_not_drop_targeted_new_message_when_contact_not_rendered_yet():
    source = PANEL_JS.read_text(encoding="utf-8")

    assert "const _targetedToThisAdvisor = data.advisor_id && ADVISOR_ID" in source
    assert "String(data.advisor_id) === String(ADVISOR_ID)" in source
    assert "if (!_phoneInList && !_targetedToThisAdvisor)" in source
    assert "Evento WS dirigido a esta asesora" in source


def test_browser_notification_checks_api_support_before_permission_read():
    source = PANEL_JS.read_text(encoding="utf-8")
    fn = source.split("function showBrowserNotification(title, body)", 1)[1]
    fn = fn.split("function sendWebSocketPing()", 1)[0]

    assert fn.index("if (!('Notification' in window))") < fn.index("Notification.permission")


def test_panel_visible_changes_invalidate_contacts_cache_and_publish_ws():
    source = PANEL_PY.read_text(encoding="utf-8")

    assert 'scan_iter("contacts_resp:*"' in source
    assert 'await _invalidate_contacts_response_cache("close_conversation")' in source
    assert 'await _invalidate_contacts_response_cache("stage_update")' in source
    assert 'action="closed"' in source
    assert 'action="stage_updated"' in source


def test_frontend_applies_closed_and_stage_ws_without_reload():
    source = PANEL_JS.read_text(encoding="utf-8")

    assert "if (data.action === 'closed' && data.phone)" in source
    assert "_performCloseCleanup(data.phone);" in source
    assert "if (data.action === 'stage_updated' && data.phone && data.current_stage)" in source
    assert "allContacts[si].current_stage = data.current_stage;" in source


def test_panel_contacts_dedupe_by_normalized_phone_and_keeps_unread_copy():
    contacts = [
        {
            "phone": "+57 300 111 2233",
            "display_name": "+57 300 111 2233",
            "has_unread": False,
            "last_activity": "2026-09-11T10:00:00",
        },
        {
            "phone": "+573001112233",
            "display_name": "Cliente Real",
            "has_unread": True,
            "last_activity": "2026-09-11T10:02:00",
        },
    ]

    deduped = panel._dedupe_panel_contacts(contacts)

    assert len(deduped) == 1
    assert deduped[0]["display_name"] == "Cliente Real"
    assert deduped[0]["has_unread"] is True


def test_panel_contacts_dedupe_by_contact_id():
    contacts = [
        {"phone": "+573001112233", "contact_id": "123", "display_name": "Viejo"},
        {"phone": "+573009998888", "id": "123", "display_name": "Nuevo", "pending_reply": True},
    ]

    deduped = panel._dedupe_panel_contacts(contacts)

    assert len(deduped) == 1
    assert deduped[0]["display_name"] == "Nuevo"
    assert deduped[0]["pending_reply"] is True


def test_frontend_dedupes_contacts_before_badges_and_render():
    source = PANEL_JS.read_text(encoding="utf-8")

    assert "function _dedupeContactsForRender(contacts)" in source
    assert "const newContacts = _dedupeContactsForRender(data.contacts || []);" in source
    assert "allContacts = _dedupeContactsForRender(allContacts);" in source


def test_backend_applies_final_dedupe_before_contacts_response():
    source = PANEL_PY.read_text(encoding="utf-8")

    assert "active_contacts = _dedupe_panel_contacts(active_contacts)" in source
    assert "_dynamic_result = _dedupe_panel_contacts(" in source
    assert "_normalize_contact_phone_key(phone)" in source
