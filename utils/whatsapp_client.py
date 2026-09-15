"""Selector del proveedor WhatsApp activo.

El codigo de negocio debe importar `whatsapp_client` desde aqui, no desde un
proveedor concreto. Para la migracion a Meta el default es Meta Cloud API.
"""

from __future__ import annotations

import os

from logging_config import logger
from utils.meta_whatsapp_client import meta_whatsapp_client


def _provider_name() -> str:
    return (os.getenv("WHATSAPP_PROVIDER", "meta") or "meta").strip().lower()


def _resolve_client():
    provider = _provider_name()
    if provider == "meta":
        return meta_whatsapp_client
    if provider == "twilio":
        logger.error("[WhatsAppClient] WHATSAPP_PROVIDER=twilio ya no esta soportado en el camino Meta")
        return meta_whatsapp_client
    logger.error("[WhatsAppClient] Proveedor desconocido: %s; usando Meta", provider)
    return meta_whatsapp_client


whatsapp_client = _resolve_client()
