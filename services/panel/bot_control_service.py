"""Bot control service for the advisor panel.

Moved verbatim from `middleware.outbound_panel` — no behavior changes.
"""

from typing import Optional

from fastapi import Header, Query, HTTPException

from logging_config import logger
from middleware import outbound_panel as _legacy
from middleware.outbound_panel import (
    _get_state_manager,
    _resolve_panel_target,
    check_24h_window,
)
from utils.safe_logging import safe_error, safe_phone


async def reset_bot_state(
    phone: str,
    canal: Optional[str] = Query(None, description="Canal específico a resetear"),
    force: bool = Query(False, description="Forzar reset incluso si ya está en BOT_ACTIVE"),
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    ENDPOINT DE EMERGENCIA: Fuerza el regreso del estado a BOT_ACTIVE.

    Útil cuando un contacto se queda "trabado" en HUMAN_ACTIVE/IN_CONVERSATION
    y no aparece en el panel para cerrarlo manualmente.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    target = _resolve_panel_target(phone)
    phone_normalized = target.key if not target.error else phone

    try:
        state_manager = _get_state_manager()

        results = []

        if canal:
            # Resetear solo el canal especificado
            canales_to_reset = [canal]
        else:
            # Buscar todos los canales donde existe este teléfono
            # Patrones comunes de canales
            canales_to_check = [
                "whatsapp_directo", "instagram", "facebook",
                "finca_raiz", "metrocuadrado", "pagina_web", "default"
            ]
            canales_to_reset = []

            for c in canales_to_check:
                state = await state_manager.get_conversation_state(phone_normalized, c)
                if state:
                    canales_to_reset.append(c)

            # También verificar con teléfono original
            if phone != phone_normalized:
                for c in canales_to_check:
                    state = await state_manager.get_conversation_state(phone, c)
                    if state and c not in canales_to_reset:
                        canales_to_reset.append(c)

        if not canales_to_reset:
            logger.warning(f"[Panel] Reset: No se encontró estado para {safe_phone(phone_normalized)}")
            return {
                "status": "warning",
                "message": f"No se encontró conversación activa para {phone_normalized}",
                "phone": phone_normalized,
                "results": []
            }

        # Resetear cada canal encontrado
        for c in canales_to_reset:
            try:
                # Obtener estado actual antes de resetear
                current_state = await state_manager.get_conversation_state(phone_normalized, c)
                previous_status = current_state.status.value if current_state else "UNKNOWN"

                # Si ya está en BOT_ACTIVE y no forzamos, skip
                if previous_status == "BOT_ACTIVE" and not force:
                    results.append({
                        "canal": c,
                        "previous_status": previous_status,
                        "new_status": "BOT_ACTIVE",
                        "action": "skipped (already BOT_ACTIVE)"
                    })
                    continue

                # Ejecutar reset
                await state_manager.activate_bot(phone_normalized, canal=c)

                results.append({
                    "canal": c,
                    "previous_status": previous_status,
                    "new_status": "BOT_ACTIVE",
                    "action": "reset successful"
                })

                logger.info(
                    f"[Panel] Reset exitoso: {phone_normalized}:{c} "
                    f"({previous_status} -> BOT_ACTIVE)"
                )

            except Exception as canal_error:
                results.append({
                    "canal": c,
                    "error": str(canal_error),
                    "action": "failed"
                })
                logger.error(f"[Panel] Error reseteando {safe_phone(phone_normalized)}:{c}: {safe_error(canal_error)}")

        success_count = len([r for r in results if r.get("action") == "reset successful"])

        return {
            "status": "success" if success_count > 0 else "warning",
            "message": f"Sofía ha retomado el control de {phone_normalized} en {success_count} canal(es)",
            "phone": phone_normalized,
            "results": results
        }

    except Exception as e:
        logger.error(f"[Panel] Error en reset-bot: {safe_error(e)}")
        raise HTTPException(status_code=500, detail=str(e))
async def get_window_status(
    phone: str,
    x_api_key: str = Header(None, alias="X-API-Key"),
):
    """
    Consulta el estado de la ventana de 24 horas para un número.
    """
    if not _legacy._validate_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="API Key inválida")

    target = _resolve_panel_target(phone)
    if target.error:
        raise HTTPException(status_code=400, detail=target.error)

    window_status = await check_24h_window(target.key)

    return {
        "phone": target.key,
        "window_open": window_status.is_open,
        "last_message_time": window_status.last_message_time.isoformat() if window_status.last_message_time else None,
        "time_remaining_seconds": window_status.time_remaining_seconds,
        "requires_template": window_status.requires_template,
        "message": window_status.message
    }
