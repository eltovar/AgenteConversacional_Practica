"""Contact note service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    get_mongo_manager,
)


class NoteCreateBody(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)
    advisor_id: str = ""
    advisor_name: str = ""
    phone: str = ""

class NoteUpdateBody(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000)


async def get_contact_notes(
    contact_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Lista notas internas activas de un contacto."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    notes = await mongo_mgr.get_notes(contact_id)
    return {"notes": notes, "total": len(notes)}


async def create_contact_note(
    contact_id: str,
    body: NoteCreateBody,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Crea una nota interna para un contacto."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    note_id = await mongo_mgr.create_note(
        contact_id=contact_id,
        content=body.content,
        advisor_id=body.advisor_id,
        advisor_name=body.advisor_name,
        phone=body.phone,
    )
    if not note_id:
        raise HTTPException(status_code=500, detail="Error al crear la nota")
    notes = await mongo_mgr.get_notes(contact_id)
    return {"ok": True, "note_id": note_id, "notes": notes}


async def update_contact_note(
    contact_id: str,
    note_id: str,
    body: NoteUpdateBody,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Actualiza el contenido de una nota interna."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.update_note(note_id, body.content)
    if not ok:
        raise HTTPException(status_code=404, detail="Nota no encontrada")
    notes = await mongo_mgr.get_notes(contact_id)
    return {"ok": True, "notes": notes}


async def delete_contact_note(
    contact_id: str,
    note_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Elimina (soft delete) una nota interna."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.soft_delete_note(note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Nota no encontrada")
    notes = await mongo_mgr.get_notes(contact_id)
    return {"ok": True, "notes": notes}
