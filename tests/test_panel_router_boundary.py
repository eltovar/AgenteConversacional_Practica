def test_panel_boundary_keeps_legacy_prefix_and_core_routes(monkeypatch):
    monkeypatch.setenv("HUBSPOT_API_KEY", "test-token")

    from middleware.panel import get_panel_router

    router = get_panel_router()
    routes = {
        (next(iter(route.methods)), route.path)
        for route in router.routes
        if getattr(route, "methods", None)
    }

    assert ("GET", "/whatsapp/panel/contacts") in routes
    assert ("GET", "/whatsapp/panel/conversations/{phone}") in routes
    assert ("POST", "/whatsapp/panel/send-message") in routes
    assert ("GET", "/whatsapp/panel/templates") in routes
    assert ("GET", "/whatsapp/panel/workers") in routes
    assert ("GET", "/whatsapp/panel/metrics") in routes


def test_panel_boundary_exposes_extracted_route_groups(monkeypatch):
    monkeypatch.setenv("HUBSPOT_API_KEY", "test-token")

    from middleware.panel import get_panel_router

    router = get_panel_router()
    routes = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }

    assert ("GET", "/whatsapp/panel/workers") in routes
    assert ("POST", "/whatsapp/panel/workers") in routes
    assert ("PATCH", "/whatsapp/panel/workers/{worker_id}") in routes
    assert ("DELETE", "/whatsapp/panel/workers/{worker_id}") in routes

    assert ("POST", "/whatsapp/panel/send-template") in routes
    assert ("GET", "/whatsapp/panel/config/template-sids") in routes
    assert ("GET", "/whatsapp/panel/templates") in routes
    assert ("POST", "/whatsapp/panel/templates") in routes
    assert ("PUT", "/whatsapp/panel/templates/{template_id}") in routes
    assert ("DELETE", "/whatsapp/panel/templates/{template_id}") in routes

    assert ("GET", "/whatsapp/panel/contacts/{contact_id}/notes") in routes
    assert ("POST", "/whatsapp/panel/contacts/{contact_id}/notes") in routes
    assert ("PATCH", "/whatsapp/panel/contacts/{contact_id}/notes/{note_id}") in routes
    assert ("DELETE", "/whatsapp/panel/contacts/{contact_id}/notes/{note_id}") in routes

    assert ("POST", "/whatsapp/panel/contacts/{contact_id}/appointments") in routes
    assert ("GET", "/whatsapp/panel/contacts/{contact_id}/appointments") in routes
    assert ("PATCH", "/whatsapp/panel/appointments/{appointment_id}/cancel") in routes
    assert ("PATCH", "/whatsapp/panel/appointments/{appointment_id}") in routes
    assert ("DELETE", "/whatsapp/panel/appointments/{appointment_id}") in routes

    assert ("GET", "/whatsapp/panel/metrics") in routes
    assert ("GET", "/whatsapp/panel/metrics/export") in routes
    assert ("GET", "/whatsapp/panel/metrics/export-excel") in routes
    assert ("GET", "/whatsapp/panel/metrics/appointments") in routes
    assert ("GET", "/whatsapp/panel/metrics/appointments/export-excel") in routes
    assert ("GET", "/whatsapp/panel/metrics/") in routes

    assert ("GET", "/whatsapp/panel/notifications") in routes
    assert ("POST", "/whatsapp/panel/notifications/{notif_id}/read") in routes
    assert ("POST", "/whatsapp/panel/notifications/read-all") in routes

    assert ("GET", "/whatsapp/panel/advisors") in routes
    assert ("PATCH", "/whatsapp/panel/advisors/{advisor_id}") in routes

    assert ("GET", "/whatsapp/panel/stages") in routes

    assert ("POST", "/whatsapp/panel/admin/restore-panel") in routes
    assert ("POST", "/whatsapp/panel/admin/recover-outage") in routes
    assert ("POST", "/whatsapp/panel/admin/cleanup-stale-inbox") in routes
    assert ("POST", "/whatsapp/panel/admin/sync-owners") in routes
    assert ("POST", "/whatsapp/panel/admin/sync-owners-mongo") in routes
    assert ("POST", "/whatsapp/panel/admin/recover-lost-conversations") in routes

    assert ("POST", "/whatsapp/panel/contacts/{contact_id}/scheduled-messages") in routes
    assert ("GET", "/whatsapp/panel/contacts/{contact_id}/scheduled-messages") in routes
    assert ("DELETE", "/whatsapp/panel/scheduled-messages/{message_id}") in routes

    assert ("POST", "/whatsapp/panel/bulk-campaigns/preview") in routes
    assert ("POST", "/whatsapp/panel/bulk-campaigns") in routes
    assert ("GET", "/whatsapp/panel/bulk-campaigns/{campaign_id}") in routes
    assert ("GET", "/whatsapp/panel/bulk-campaigns/last/{stage_id}") in routes

    assert ("GET", "/whatsapp/panel/debug/redis") in routes
    assert ("GET", "/whatsapp/panel/diagnose") in routes
    assert ("GET", "/whatsapp/panel/ws/stats") in routes

    assert ("POST", "/whatsapp/panel/send-message") in routes
    assert ("PATCH", "/whatsapp/panel/messages/{mongo_id}") in routes
    assert ("DELETE", "/whatsapp/panel/messages/{mongo_id}") in routes
    assert ("POST", "/whatsapp/panel/send-message-json") in routes

    assert ("POST", "/whatsapp/panel/contacts/create") in routes
    assert ("PATCH", "/whatsapp/panel/contacts/{contact_id}/name") in routes
    assert ("DELETE", "/whatsapp/panel/contacts/{phone}/close") in routes
    assert ("POST", "/whatsapp/panel/contacts/{phone}/mark-read") in routes
    assert ("PATCH", "/whatsapp/panel/contacts/{contact_id}/stage") in routes
    assert ("PATCH", "/whatsapp/panel/contacts/{phone}/canal") in routes
    assert ("GET", "/whatsapp/panel/contacts/{phone}/detail") in routes
    assert ("GET", "/whatsapp/panel/contacts/{phone}/hydrate") in routes
    assert ("POST", "/whatsapp/panel/contacts/{phone}/take-control") in routes
    assert ("GET", "/whatsapp/panel/contacts/search") in routes
    assert ("GET", "/whatsapp/panel/contacts") in routes

    assert ("POST", "/whatsapp/panel/contacts/{phone}/transfer") in routes
    assert ("POST", "/whatsapp/panel/contacts/{contact_id}/transfer-request") in routes
    assert ("POST", "/whatsapp/panel/contacts/{contact_id}/transfer-accept") in routes
    assert ("POST", "/whatsapp/panel/contacts/{contact_id}/transfer-reject") in routes

    assert ("POST", "/whatsapp/panel/reset-bot/{phone}") in routes
    assert ("GET", "/whatsapp/panel/window-status/{phone}") in routes

    assert ("GET", "/whatsapp/panel/conversations/{phone}") in routes
    assert ("GET", "/whatsapp/panel/history/{contact_id}") in routes

    assert ("GET", "/whatsapp/panel/") in routes


def test_panel_boundary_exposes_realtime_websocket(monkeypatch):
    monkeypatch.setenv("HUBSPOT_API_KEY", "test-token")

    from middleware.panel import get_panel_router

    router = get_panel_router()
    websocket_paths = {
        route.path
        for route in router.routes
        if getattr(route, "path", None)
        and getattr(route, "endpoint", None)
        and not getattr(route, "methods", None)
    }

    assert "/whatsapp/panel/ws/{advisor_id}" in websocket_paths
