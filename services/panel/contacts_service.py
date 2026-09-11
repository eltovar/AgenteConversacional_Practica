"""Contact service facade."""

from middleware.outbound_panel import (
    close_conversation,
    create_manual_contact,
    get_active_contacts,
    get_contact_detail,
    hydrate_contact_endpoint,
    mark_contact_read,
    search_contacts_by_keyword,
    take_control_of_conversation,
    update_contact_canal_display,
    update_contact_name,
    update_contact_stage,
)
