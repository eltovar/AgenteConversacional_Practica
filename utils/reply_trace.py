"""Traza de las citaciones entrantes: separa "no citó" de "citó y lo perdimos".

Why: cuando un cliente responde citando un mensaje en WhatsApp y Twilio no nos
manda una referencia utilizable, la cita desaparece sin dejar rastro — no se
guarda nada en Mongo, no se emite ningún error, y el mensaje queda idéntico a
uno que nunca citó nada. Ese silencio es el problema de fondo: impide medir el
tamaño del fallo y, por tanto, decidir qué arreglar.

La primera versión daba por verdad de terreno la presencia de
`ChannelMetadata.data.context`. Es falsa, y medirlo en producción lo demostró
en una hora: Twilio manda ese campo con la identidad del remitente
(`ProfileName`, `WaId`) en prácticamente todos los entrantes, así que el
clasificador marcaba como pérdida 24 de 27 mensajes. Lo que marca una
respuesta no es que exista el contexto, sino que dentro aparezca una clave de
referencia al mensaje citado.

Estas funciones son puras: no tocan red, disco ni base de datos, y no alteran
ningún flujo. Quien llama decide si registra el resultado y con qué nivel.

PII: la traza nunca incluye teléfonos, nombres ni contenido de mensajes. Sólo
lleva el SID de Twilio, el canal y el diagnóstico — conforme a la regla de
CLAUDE.md de no loguear datos de clientes en producción.
"""

import json
from typing import Any, Optional, Tuple

# ── Diagnósticos ────────────────────────────────────────────────────────────
# Un entrante cae en exactamente uno. Los que empiezan por "perdida:" son los
# que hoy no dejan ninguna huella en el sistema.
SIN_CITA = "sin_cita"
RESUELTA = "resuelta"
PERDIDA_SIN_REFERENCIA = "perdida:sin_referencia"
PERDIDA_SOLO_WAMID = "perdida:solo_wamid"
PERDIDA_NO_EN_MONGO = "perdida:no_esta_en_mongo"

# Claves donde WhatsApp deja el identificador del mensaje citado, dentro de
# ChannelMetadata.data.context. PascalCase es la que usa Twilio de verdad; las
# otras se han visto en payloads antiguos y en la documentación.
_CLAVES_REFERENCIA = ("MessageId", "message_id", "quoted_message_id", "id")

# La presencia de `data.context` NO significa que el mensaje sea una respuesta.
# Medido en producción el 29-ago-2026: Twilio manda
# `context=[ProfileName,WaId]` —la identidad de quien escribe— en
# prácticamente todos los entrantes. Darlo por marca de cita clasificó como
# pérdida 24 de 27 mensajes en una hora, contra una tasa histórica del 2-3%.
# Sólo estas claves marcan una respuesta de verdad.
_CLAVES_DE_RESPUESTA = frozenset(_CLAVES_REFERENCIA)


def _a_dict(bruto: Any) -> Optional[dict]:
    """Normaliza un campo que puede llegar como dict, como JSON o como basura."""
    if not bruto:
        return None
    if isinstance(bruto, dict):
        return bruto
    if isinstance(bruto, (str, bytes, bytearray)):
        try:
            valor = json.loads(bruto)
        except (ValueError, TypeError):
            return None
        return valor if isinstance(valor, dict) else None
    return None


def contexto_del_mensaje(channel_metadata_bruto: Any) -> Optional[dict]:
    """Devuelve `data.context` del ChannelMetadata, o None si no viene.

    Ojo: que exista NO significa que haya cita. Twilio lo manda en casi todos
    los entrantes con la identidad del remitente dentro. Para saber si es una
    respuesta hay que pasar por `es_contexto_de_respuesta`.
    """
    meta = _a_dict(channel_metadata_bruto)
    if meta is None:
        return None
    datos = meta.get("data")
    if not isinstance(datos, dict):
        return None
    contexto = datos.get("context")
    return contexto if isinstance(contexto, dict) else None


def es_contexto_de_respuesta(contexto: Optional[dict]) -> bool:
    """True sólo si el contexto describe una cita, no al remitente.

    Distinguir esto es lo único que separa una medición útil de un contador de
    mensajes: `data.context` viene poblado siempre, y lo que cambia cuando hay
    cita es que aparece una clave de referencia dentro.
    """
    if not isinstance(contexto, dict) or not contexto:
        return False
    return any(clave in contexto for clave in _CLAVES_DE_RESPUESTA)


def referencia_de_contexto(contexto: Optional[dict]) -> Optional[str]:
    """Identificador del mensaje citado que viene dentro del contexto."""
    if not isinstance(contexto, dict):
        return None
    for clave in _CLAVES_REFERENCIA:
        valor = contexto.get(clave)
        if valor:
            return str(valor)
    return None


def forma_de_referencia(referencia: Optional[str]) -> str:
    """Clasifica la referencia por su forma, que determina si es resoluble.

    Importa porque cada forma se busca en un campo distinto de Mongo, y hoy no
    todos esos campos están poblados.
    """
    if not referencia:
        return "ausente"
    ref = str(referencia)
    if ref.startswith("wamid."):
        return "wamid"
    if ref.startswith("IM"):
        return "im_sid"
    if ref.startswith(("SM", "MM")):
        return "sm_sid"
    return "otra"


def diagnosticar(
    referencia: Optional[str],
    hubo_contexto: bool,
    resuelto: bool,
) -> str:
    """Clasifica el destino de una posible cita entrante.

    Args:
        referencia: identificador del mensaje citado que el pipeline extrajo.
        hubo_contexto: WhatsApp marcó el entrante como respuesta.
        resuelto: la búsqueda en Mongo encontró el mensaje citado.
    """
    if resuelto:
        return RESUELTA
    if not referencia:
        # Sin referencia y sin contexto no hubo cita ninguna. Con contexto, el
        # cliente sí citó y nos quedamos sin identificador con el que buscar.
        return PERDIDA_SIN_REFERENCIA if hubo_contexto else SIN_CITA
    if forma_de_referencia(referencia) == "wamid":
        # El WAMid sólo casa contra el campo `wamid`, que se puebla en una rama
        # del webhook que hoy no recibe eventos. Buscar por él no puede acertar.
        return PERDIDA_SOLO_WAMID
    return PERDIDA_NO_EN_MONGO


def es_perdida(diagnostico: str) -> bool:
    """True si el diagnóstico representa una cita que el cliente hizo y se perdió."""
    return diagnostico.startswith("perdida:")


# ── DIAGNÓSTICO TEMPORAL — retirar cuando el clasificador esté calibrado ─────
#
# Why: la primera hora en producción marcó el 89% de los entrantes como cita
# perdida. La suposición de que `data.context` sólo aparece en respuestas era
# falsa: Twilio lo manda en casi todos los mensajes. Para endurecer el
# clasificador hace falta saber QUÉ trae ese contexto, y eso no se puede
# deducir desde aquí — hay que mirarlo en el payload real.
#
# Registra nombres de clave, nunca valores, igual que el log [Webhook][RAW].
# Los nombres son del esquema fijo de Twilio, no datos del cliente.

_FORMAS_VISTAS: set = set()
_MAX_FORMAS = 200  # cota dura: el watchdog de memoria no perdona sets sin techo


def forma_del_payload(channel_metadata_bruto: Any) -> Optional[str]:
    """Firma estructural del ChannelMetadata: qué claves trae, en qué nivel.

    Devuelve None si no hay metadata que describir.
    """
    meta = _a_dict(channel_metadata_bruto)
    if meta is None:
        return None

    datos = meta.get("data")
    datos = datos if isinstance(datos, dict) else {}
    contexto = datos.get("context")

    partes = [
        "meta=[" + ",".join(sorted(meta.keys())) + "]",
        "data=[" + ",".join(sorted(datos.keys())) + "]",
    ]
    if isinstance(contexto, dict):
        partes.append("context=[" + ",".join(sorted(contexto.keys())) + "]")
    elif contexto is not None:
        partes.append(f"context=<{type(contexto).__name__}>")
    return " ".join(partes)


def forma_es_nueva(firma: Optional[str]) -> bool:
    """True la primera vez que se ve esta estructura en este proceso.

    Acota el volumen: la forma habitual se registra una vez, y una respuesta
    real —que traerá claves distintas— salta a la vista de inmediato.
    """
    if not firma or firma in _FORMAS_VISTAS:
        return False
    if len(_FORMAS_VISTAS) >= _MAX_FORMAS:
        return False
    _FORMAS_VISTAS.add(firma)
    return True


def traza(
    message_sid: Optional[str],
    canal: Optional[str],
    referencia: Optional[str],
    channel_metadata_bruto: Any = None,
    resuelto: bool = False,
    etapa: str = "entrada",
) -> Tuple[str, str]:
    """Arma la línea de traza y su diagnóstico.

    Devuelve `(linea, diagnostico)`. Quien llama elige el nivel de log según
    `es_perdida(diagnostico)`.
    """
    contexto = contexto_del_mensaje(channel_metadata_bruto)
    hubo_contexto = es_contexto_de_respuesta(contexto)
    if referencia is None:
        referencia = referencia_de_contexto(contexto)

    diagnostico = diagnosticar(referencia, hubo_contexto, resuelto)
    linea = (
        f"[CitaTrace][{etapa}] sid={message_sid or 'N/A'} "
        f"canal={canal or 'N/A'} contexto_whatsapp={'si' if hubo_contexto else 'no'} "
        f"forma_ref={forma_de_referencia(referencia)} diagnostico={diagnostico}"
    )
    return linea, diagnostico
