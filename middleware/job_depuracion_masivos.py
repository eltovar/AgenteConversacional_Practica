"""
Pasada periódica que aplica la regla de depuración del embudo "No responde".

QUÉ AUTOMATIZA
    Lo que hasta ahora se corría a mano con `scripts/depurar_no_responde.py`:
    un contacto que recibe 2 o más masivos de reactivación sin contestar se da
    por perdido y pasa a "Cerrado perdido". Se hizo tres veces (18 y 19-ago-2026,
    461 contactos), siempre con alguien delante. Esto lo deja en piloto
    automático para los masivos que vengan.

    LA REGLA NO VIVE AQUÍ. La decide `middleware/depuracion_no_responde.py`, que
    es puro y está cubierto por tests. Este módulo solo lee, evalúa con ese
    motor, escribe y cuenta.

EL INTERRUPTOR
    `DEPURACION_AUTO_ENABLED` viene en `false` a propósito, y `false` NO apaga la
    pasada: la deja calculando y dejando el informe en el log SIN mover a nadie.
    Así el primer lote real se puede leer antes de soltarlo, que es justo lo que
    se pidió — y activarlo es cambiar una variable en Railway, sin desplegar.

SIN ESTADO, A PROPÓSITO
    No hay checkpoint ni marcas de "reclamado". El motor descarta a quien no esté
    ya en el embudo de origen, así que un contacto movido sale por
    `fuera_del_embudo` en la pasada siguiente. Dos motivos: en Railway el disco
    es efímero y un checkpoint en fichero se perdería en cada deploy; y reclamar
    sin poder liberar es exactamente lo que dejó muertas 3 campañas masivas
    durante 33 días.

COSTE MEDIDO (19-ago-2026, producción)
    MongoDB    ~1 s   sobre 55.606 mensajes
    HubSpot    ~15 s  en 20 lotes de 100
    Los masivos son esporádicos —3 días en 4 meses— así que la mayoría de las
    pasadas no encuentran nada que hacer, y eso está bien: cuestan 17 segundos
    a las 5 de la mañana.

⏱️  Todos los datetime salen de MongoDB, que los guarda naive. El motor los
compara entre si y contra `_ahora_utc()`.

Y tienen que ser UTC. `save_message` guarda `datetime.now(TIMEZONE)` en
hora de Bogota, pero PyMongo convierte todo datetime con zona a UTC al
escribirlo en BSON: lo que vuelve de la consulta es naive Y EN UTC.
Comprobado el 20-ago-2026 contra produccion: `timestamp` y `timestamp_utc`
del mismo documento son identicos.

Usar `datetime.now()` aqui haria que la espera de 48h dependiera de la zona
del contenedor: en Railway (UTC) saldria bien por casualidad, y el dia que
alguien ponga TZ=America/Bogota se depurarian contactos 5 horas antes de
tiempo. Se fija en UTC explicitamente.
"""
import asyncio
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from logging_config import logger

from .depuracion_no_responde import (
    MODO_HISTORIAL_COMPLETO,
    MODO_HISTORIAL_SIN_EXCEPCION,
    MOTIVO_DEPURAR,
    Historial,
    Regla,
    evaluar,
)

# Nombre del job en APScheduler. Vive aquí para que app.py no lo invente.
ID_JOB = "depuracion_no_responde"

# Cuántas escrituras a HubSpot por segundo. Conservador: el límite es por portal
# y estas llamadas compiten con las del panel aunque sean las 5 de la mañana.
ESCRITURAS_POR_SEGUNDO = 3

# Corta la pasada si HubSpot empieza a fallar en cadena. Sin esto, un portal
# caído se traduce en 200 intentos inútiles y 200 líneas de error.
FALLOS_SEGUIDOS_PARA_ABORTAR = 5


def _ahora_utc() -> datetime:
    """
    El reloj con el que se mide la espera, en el mismo marco que MongoDB.

    Naive y en UTC: es como vuelven los `timestamp` de `messages`, y
    compararlos con `datetime.now()` desplazaria la ventana de 48h tantas
    horas como diga la zona del contenedor.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _activado() -> bool:
    """
    `false` (defecto) NO apaga la pasada: la deja en seco. La comprobación se
    hace en cada ejecución y no al importar, para que cambiar la variable en
    Railway surta efecto en el reinicio sin tocar código.
    """
    return (os.getenv("DEPURACION_AUTO_ENABLED", "false") or "").strip().lower() == "true"


def _modo() -> str:
    """
    Modo de conteo. El defecto es `historial_completo`, la regla que el usuario
    aprobó el 19-ago-2026 para las corridas manuales.

    `historial_sin_excepcion` cierra conversaciones vivas —cierra también a quien
    contestó— y solo se usó una vez, con las 37 conversaciones afectadas
    delante. No puede ser el defecto de un job que corre solo.
    """
    return (os.getenv("DEPURACION_AUTO_MODO") or MODO_HISTORIAL_COMPLETO).strip()


def _tope() -> int:
    """
    Máximo de contactos a mover en una pasada. Es una red contra una campaña
    anómala, no un límite operativo: con 166 previstos no muerde, y si alguna vez
    lo hace, la pasada del día siguiente recoge el resto.
    """
    try:
        return max(1, int(os.getenv("DEPURACION_AUTO_TOPE", "200")))
    except ValueError:
        return 200


# ── Lectura ─────────────────────────────────────────────────────────────────

async def leer_historial_masivos(db, etapa_origen: str) -> Tuple[
    Dict[str, List[datetime]], Dict[str, datetime], Dict[str, str]
]:
    """
    Por teléfono: masivos enviados al embudo, última respuesta y contact_id.

    Recibe la base de datos en vez de abrirla: la regla 1 de CLAUDE.md prohíbe
    crear conexiones fuera de los singletons, y así el script y el job comparten
    esta consulta sin compartir cliente. Es también lo que hace esta función
    probable sin red.

    Se piden solo los campos necesarios: el contenido de 55.000 mensajes no cabe
    en memoria y no hace falta para contar.
    """
    masivos: Dict[str, List[datetime]] = {}
    contact_ids: Dict[str, str] = {}
    async for m in db.messages.find(
        {"metadata.is_bulk_send": True, "metadata.stage_id": etapa_origen},
        {"phone": 1, "timestamp": 1, "hubspot_contact_id": 1, "_id": 0},
    ):
        telefono = m.get("phone")
        marca = m.get("timestamp")
        if not telefono or not marca:
            continue
        masivos.setdefault(telefono, []).append(marca)
        if m.get("hubspot_contact_id"):
            contact_ids.setdefault(telefono, str(m["hubspot_contact_id"]))

    ultima_respuesta: Dict[str, datetime] = {}
    async for m in db.messages.find(
        {"sender": "client"}, {"phone": 1, "timestamp": 1, "_id": 0}
    ):
        telefono, marca = m.get("phone"), m.get("timestamp")
        if not telefono or not marca:
            continue
        if telefono not in ultima_respuesta or marca > ultima_respuesta[telefono]:
            ultima_respuesta[telefono] = marca

    return masivos, ultima_respuesta, contact_ids


async def _leer_etapas(contact_ids: List[str]) -> Dict[str, str]:
    """
    Etapa vigente de cada contacto, en lotes de 100 (el máximo del batch read).

    Deliberadamente SIN caché, al contrario que `_get_contacts_batch_hubspot`:
    una etapa de hace diez minutos puede ser la de un contacto que una asesora
    acaba de rescatar del embudo, y moverlo sería deshacer su trabajo.
    """
    from .outbound_panel import HUBSPOT_API_KEY, _hubspot_post, get_httpx_client

    url = "https://api.hubapi.com/crm/v3/objects/contacts/batch/read"
    cliente = get_httpx_client()
    etapas: Dict[str, str] = {}

    for i in range(0, len(contact_ids), 100):
        lote = contact_ids[i:i + 100]
        payload = {
            "properties": ["lifecyclestage"],
            "inputs": [{"id": c} for c in lote],
        }
        try:
            r = await _hubspot_post(cliente, url, payload, HUBSPOT_API_KEY, max_retries=2)
            if r.status_code not in (200, 207):
                logger.warning(
                    "[Depuracion] Lote de etapas devolvio %s — se omiten %d contactos",
                    r.status_code, len(lote),
                )
                continue
            for contacto in r.json().get("results", []):
                etapas[contacto["id"]] = (
                    contacto.get("properties", {}).get("lifecyclestage") or ""
                )
        except Exception as e:
            logger.warning("[Depuracion] Error leyendo un lote de etapas: %s", e)
        await asyncio.sleep(1 / ESCRITURAS_POR_SEGUNDO)

    return etapas


# ── Escritura ───────────────────────────────────────────────────────────────

async def _mover_al_embudo_terminal(
    contact_id: str, etapa_previa: str, etapa_destino: str
) -> Optional[str]:
    """
    Mueve un contacto al embudo terminal. Devuelve None si fue bien, o el motivo.

    Dos pasos obligatorios: `lifecyclestage` en HubSpot es unidireccional y solo
    deja avanzar, así que traer a alguien desde otra etapa exige limpiar primero.
    Entre los dos pasos el contacto NO TIENE ETAPA: no lo ve el panel y no lo
    alcanza ninguna regla de embudo. Pasó de verdad el 18-ago-2026 y dejó un
    contacto atrapado hasta que se rescató a mano, así que si el segundo paso
    falla se deshace.
    """
    from .outbound_panel import (
        HUBSPOT_API_KEY,
        _deshacer_etapa,
        _hubspot_patch,
        _invalidate_contact_stage_cache,
    )

    url = f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
    try:
        limpiado = await _hubspot_patch(
            url, {"properties": {"lifecyclestage": ""}}, HUBSPOT_API_KEY
        )
        if limpiado.status_code != 200:
            # No se llego a tocar nada: el contacto sigue en su etapa.
            return f"limpiar_HTTP_{limpiado.status_code}"

        fijado = await _hubspot_patch(
            url, {"properties": {"lifecyclestage": etapa_destino}}, HUBSPOT_API_KEY
        )
        if fijado.status_code == 200:
            await _invalidate_contact_stage_cache(contact_id, etapa_destino)
            return None

        return await _deshacer_etapa(
            contact_id, etapa_previa, f"fijar_HTTP_{fijado.status_code}"
        )
    except Exception as e:
        # Un corte de red entre los dos pasos abre el mismo hueco que un HTTP malo.
        return await _deshacer_etapa(contact_id, etapa_previa, f"error_de_red: {e}")


async def _cerrar_en_panel(telefonos: List[str]) -> int:
    """
    Saca del panel a los que se acaban de dar por perdidos.

    Reutiliza el cierre del panel en vez de replicarlo, y NO emite WebSocket: una
    notificación por contacto provocaría un refresco de lista por cada asesora
    conectada, decenas de veces seguidas.
    """
    if not telefonos:
        return 0

    from .outbound_panel import _close_conversation_internal, _get_redis_client

    try:
        r = await _get_redis_client()
        miembros = await r.zrange("active_conversations_sorted", 0, -1)
    except Exception as e:
        logger.warning("[Depuracion] No se pudo leer el indice del panel: %s", e)
        return 0

    # El indice se lee UNA vez: preguntarlo por contacto serian 166 viajes.
    abiertos: Dict[str, List[str]] = {}
    for miembro in miembros:
        telefono, _, canal = str(miembro).rpartition(":")
        if telefono:
            abiertos.setdefault(telefono, []).append(canal)

    cerradas = 0
    for telefono in telefonos:
        for canal in abiertos.get(telefono, []):
            try:
                await _close_conversation_internal(telefono, canal)
                cerradas += 1
            except Exception as e:
                logger.warning(
                    "[Depuracion] No se pudo cerrar ...%s:%s (%s)", telefono[-4:], canal, e
                )
    return cerradas


# ── La pasada ───────────────────────────────────────────────────────────────

async def ejecutar_pasada(aplicar: Optional[bool] = None) -> Dict[str, object]:
    """
    Una pasada completa. Devuelve el resumen contable.

    `aplicar=None` consulta la variable de entorno; se puede forzar desde un test
    o desde una llamada manual sin depender del entorno.
    """
    from .outbound_panel import (
        HUBSPOT_STAGE_CERRADO_PERDIDO,
        HUBSPOT_STAGE_NO_RESPONDE,
        get_mongo_manager,
    )

    aplicar = _activado() if aplicar is None else aplicar
    modo = _modo()
    regla = Regla(
        etapa_origen=HUBSPOT_STAGE_NO_RESPONDE,
        etapa_destino=HUBSPOT_STAGE_CERRADO_PERDIDO,
        modo=modo,
    )
    resumen: Dict[str, object] = {
        "modo": modo, "aplicado": aplicar, "evaluados": 0, "candidatos": 0,
        "movidos": 0, "fallidos": 0, "cerrados": 0, "topado": False,
    }

    mongo = get_mongo_manager()
    if not await mongo.connect():
        logger.error("[Depuracion] Sin MongoDB — se aborta la pasada")
        return resumen

    masivos, respuestas, contact_ids = await leer_historial_masivos(
        mongo.db, regla.etapa_origen
    )
    resumen["evaluados"] = len(masivos)
    if not masivos:
        logger.info("[Depuracion] Ningun masivo al embudo — nada que evaluar")
        return resumen

    identificadores = sorted({c for c in contact_ids.values() if c})
    etapas = await _leer_etapas(identificadores)

    ahora = _ahora_utc()
    candidatos = []
    for telefono, envios in masivos.items():
        contact_id = contact_ids.get(telefono)
        if not contact_id:
            continue
        decision = evaluar(
            Historial(contact_id, envios, respuestas.get(telefono), etapas.get(contact_id)),
            regla,
            ahora,
        )
        if decision.motivo == MOTIVO_DEPURAR:
            candidatos.append((decision.ultimo_masivo, telefono, contact_id, decision))

    # Los mas antiguos primero: si el tope muerde, se van los que llevan mas
    # tiempo esperando, no los que acaban de entrar.
    candidatos.sort(key=lambda c: (c[0], c[1]))
    resumen["candidatos"] = len(candidatos)

    tope = _tope()
    if len(candidatos) > tope:
        resumen["topado"] = True
        logger.warning(
            "[Depuracion] %d candidatos superan el tope de %d — se mueven los mas "
            "antiguos y el resto queda para la pasada siguiente",
            len(candidatos), tope,
        )
        candidatos = candidatos[:tope]

    if not aplicar:
        logger.info(
            "[Depuracion] EN SECO (DEPURACION_AUTO_ENABLED=false) — modo=%s, "
            "se moverian %d de %d evaluados",
            modo, len(candidatos), resumen["evaluados"],
        )
        for ultimo, telefono, contact_id, decision in candidatos[:50]:
            logger.info(
                "[Depuracion]   ...%s contact=%s masivos=%d ultimo=%s",
                telefono[-4:], contact_id, decision.masivos_contados,
                ultimo.strftime("%Y-%m-%d"),
            )
        if len(candidatos) > 50:
            logger.info("[Depuracion]   ... y %d mas", len(candidatos) - 50)
        return resumen

    movidos_tel: List[str] = []
    fallos_seguidos = 0
    for _ultimo, telefono, contact_id, _decision in candidatos:
        error = await _mover_al_embudo_terminal(
            contact_id, regla.etapa_origen, regla.etapa_destino
        )
        if error:
            resumen["fallidos"] = int(resumen["fallidos"]) + 1
            fallos_seguidos += 1
            logger.warning("[Depuracion] contact=%s no se movio: %s", contact_id, error)
            if fallos_seguidos >= FALLOS_SEGUIDOS_PARA_ABORTAR:
                logger.error(
                    "[Depuracion] %d fallos seguidos — se corta la pasada",
                    fallos_seguidos,
                )
                break
        else:
            fallos_seguidos = 0
            resumen["movidos"] = int(resumen["movidos"]) + 1
            movidos_tel.append(telefono)
        await asyncio.sleep(1 / ESCRITURAS_POR_SEGUNDO)

    resumen["cerrados"] = await _cerrar_en_panel(movidos_tel)
    logger.info(
        "[Depuracion] Pasada completada — modo=%s candidatos=%d movidos=%d "
        "fallidos=%d cerrados=%d",
        modo, resumen["candidatos"], resumen["movidos"],
        resumen["fallidos"], resumen["cerrados"],
    )
    return resumen


async def check_depuracion_masivos() -> None:
    """
    Punto de entrada del scheduler. Nunca lanza: una excepción aquí mataría el
    job para el resto de la vida del worker.
    """
    try:
        if _modo() == MODO_HISTORIAL_SIN_EXCEPCION:
            # Cierra tambien a quien contesto. Se usó una vez, supervisado. Que
            # corra solo cada madrugada es otra cosa muy distinta.
            logger.warning(
                "[Depuracion] DEPURACION_AUTO_MODO=%s cierra conversaciones vivas; "
                "revisa que sea deliberado",
                MODO_HISTORIAL_SIN_EXCEPCION,
            )
        await ejecutar_pasada()
    except Exception as e:
        logger.error("[Depuracion] Pasada abortada por error: %s", e, exc_info=True)
