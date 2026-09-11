"""Panel CRM boundary.

This package is the stable entrypoint for the advisor panel. For now it mounts
the legacy router unchanged; future route groups can move here one by one while
keeping the public `/whatsapp/panel/*` API stable.
"""

from fastapi import APIRouter


def get_panel_router() -> APIRouter:
    from middleware.outbound_panel import router as legacy_router
    from middleware.panel.admin_routes import router as admin_router
    from middleware.panel.advisors_routes import router as advisors_router
    from middleware.panel.appointments_routes import router as appointments_router
    from middleware.panel.bulk_campaigns_routes import router as bulk_campaigns_router
    from middleware.panel.bot_control_routes import router as bot_control_router
    from middleware.panel.contacts_routes import router as contacts_router
    from middleware.panel.conversations_routes import router as conversations_router
    from middleware.panel.diagnostics_routes import router as diagnostics_router
    from middleware.panel.messages_routes import router as messages_router
    from middleware.panel.metrics_routes import router as metrics_router
    from middleware.panel.notes_routes import router as notes_router
    from middleware.panel.notifications_routes import router as notifications_router
    from middleware.panel.reference_routes import router as reference_router
    from middleware.panel.realtime_routes import router as realtime_router
    from middleware.panel.scheduled_messages_routes import router as scheduled_messages_router
    from middleware.panel.templates_routes import router as templates_router
    from middleware.panel.transfer_routes import router as transfer_router
    from middleware.panel.ui_routes import router as ui_router
    from middleware.panel.workers_routes import router as workers_router

    router = APIRouter()
    router.include_router(ui_router)
    router.include_router(realtime_router)
    router.include_router(admin_router)
    router.include_router(diagnostics_router)
    router.include_router(advisors_router)
    router.include_router(reference_router)
    router.include_router(notifications_router)
    router.include_router(scheduled_messages_router)
    router.include_router(bulk_campaigns_router)
    router.include_router(messages_router)
    router.include_router(contacts_router)
    router.include_router(transfer_router)
    router.include_router(bot_control_router)
    router.include_router(conversations_router)
    router.include_router(metrics_router)
    router.include_router(appointments_router)
    router.include_router(notes_router)
    router.include_router(templates_router)
    router.include_router(workers_router)
    router.include_router(legacy_router)
    return router
