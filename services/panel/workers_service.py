"""Workers service for the advisor panel (equipo de campo para citas).

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    get_mongo_manager,
)
from middleware.phone_normalizer import PhoneNormalizer
from utils.safe_logging import safe_id


async def list_workers(x_api_key: str = Header(None, alias="X-API-Key")):
    """Lista todos los workers activos del equipo de campo."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    workers = await mongo_mgr.get_workers()
    return {"workers": workers}


class WorkerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    # Opcional a propósito: los 6 encargados que ya existían se crearon sin él y
    # se puede dar de alta a alguien antes de saber su número. Sin teléfono la
    # cita se agenda igual, sólo no sale la confirmación al cliente.
    phone: str = Field("", max_length=25, description="Teléfono que verá el cliente")


class WorkerUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    # None = no tocar el teléfono. Cadena vacía = borrarlo. Renombrar a alguien no
    # puede apagarle las confirmaciones sin querer.
    phone: Optional[str] = Field(None, max_length=25)


def _normalizar_telefono_encargado(crudo: Optional[str]) -> Optional[str]:
    """
    Valida y normaliza a E.164 el teléfono de un encargado.

    None entra y None sale (= no tocar). Cadena vacía sale vacía (= borrar). Un
    número inválido corta con 422: guardar basura aquí significa mandársela al
    cliente en la confirmación de su cita.
    """
    if crudo is None:
        return None
    crudo = crudo.strip()
    if not crudo:
        return ""
    resultado = PhoneNormalizer().normalize(crudo)
    if not resultado.is_valid:
        raise HTTPException(
            status_code=422,
            detail=f"Teléfono del encargado inválido: {resultado.error_message}",
        )
    return resultado.normalized


async def create_worker(
    body: WorkerCreateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Crea un nuevo worker (encargado de mostrar inmuebles)."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    telefono = _normalizar_telefono_encargado(body.phone) or ""
    mongo_mgr = get_mongo_manager()
    worker_id = await mongo_mgr.create_worker(body.name, telefono)
    if not worker_id:
        raise HTTPException(status_code=409, detail="Ya existe un worker con ese nombre")
    logger.info(f"[Panel] Worker creado: {safe_id(body.name, 'name')} ({safe_id(worker_id, 'worker')})")
    return {"worker_id": worker_id, "name": body.name, "phone": telefono}


async def update_worker(
    worker_id: str,
    body: WorkerUpdateRequest,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Actualiza el nombre de un worker."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    telefono = _normalizar_telefono_encargado(body.phone)
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.update_worker(worker_id, body.name, telefono)
    if not ok:
        raise HTTPException(status_code=404, detail="Worker no encontrado")
    return {"ok": True, "name": body.name, "phone": telefono}


async def delete_worker(
    worker_id: str,
    x_api_key: str = Header(None, alias="X-API-Key")
):
    """Elimina (soft-delete) un worker. Sus citas históricas se preservan."""
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")
    mongo_mgr = get_mongo_manager()
    ok = await mongo_mgr.delete_worker(worker_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Worker no encontrado")
    return {"ok": True}
