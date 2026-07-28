"""
Endpoint temporal de auditoría Redis.
Agrégalo a app.py en el router para acceder: GET http://localhost:8000/audit/inbox
"""

from fastapi import APIRouter, HTTPException
from middleware.conversation_state import ConversationStateManager
import logging
import json

logger = logging.getLogger('agent_system')

audit_router = APIRouter(prefix="/audit", tags=["audit"])

@audit_router.get("/inbox")
async def audit_inbox():
    """
    Auditoría de Redis inbox — detecta canales no-hardcodeados.
    Acceso: GET /audit/inbox
    """
    try:
        sm = ConversationStateManager()
        advisor_id = "89096380"
        
        # Comando 1: Contenido de advisor_inbox
        inbox_key = f"advisor_inbox:{advisor_id}"
        inbox_data = await sm.redis.zrange(inbox_key, 0, -1, withscores=True)
        
        # Comando 2: Buscar conversaciones del contacto
        contact_tel = "+573192474089"
        active_convs = await sm.redis.zrange("active_conversations_sorted", 0, -1)
        found_convs = [c for c in active_convs if contact_tel in c]
        
        # Análisis
        canales_unicos = set()
        for member, _ in inbox_data:
            if ':' in member:
                canal = member.split(':')[1]
                canales_unicos.add(canal)
        
        # Hardcodeados
        canales_hardcodeados = {
            'whatsapp', 'instagram', 'facebook',
            'finca_raiz', 'metrocuadrado',
            'pagina_web', 'whatsapp_directo'
        }
        
        no_hardcodeados = canales_unicos - canales_hardcodeados
        
        # Verificación específica
        test_member = f"{contact_tel}:ciencuadras"
        is_in = await sm.redis.zscore(inbox_key, test_member)
        
        return {
            "status": "ok",
            "audit_data": {
                "advisor_id": advisor_id,
                "inbox_entries": {
                    "total": len(inbox_data),
                    "entries": [
                        {"member": m, "score": s} for m, s in inbox_data
                    ]
                },
                "canales_analisis": {
                    "unicos_encontrados": sorted(canales_unicos),
                    "hardcodeados": sorted(canales_hardcodeados),
                    "no_hardcodeados": sorted(no_hardcodeados),
                    "es_problemático": len(no_hardcodeados) > 0
                },
                "conversaciones_activas": {
                    "buscadas_para": contact_tel,
                    "encontradas": found_convs
                },
                "verificacion_especifica": {
                    "miembro_buscado": test_member,
                    "encontrado": is_in is not None,
                    "score": is_in
                },
                "diagnostico": {
                    "bug_confirmado": is_in is not None,
                    "razon": "remove_from_advisor_inbox() ignora canales dinámicos",
                    "impacto": "F5 ghosts notifications, badges nunca limpian"
                }
            }
        }
        
    except Exception as e:
        logger.error(f"[Audit] Error: {e}")
        raise HTTPException(status_code=500, detail=f"Error en auditoría: {str(e)}")
