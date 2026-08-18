"""
Motor de decisión de la depuración del embudo "No responde".

Un contacto que recibe varios masivos de reactivación y no contesta a ninguno
está perdido de hecho. Hoy las asesoras lo detectan a ojo y lo mueven a mano a
"Cerrado perdido" — 58 de ellos ya movidos así a 18-ago-2026. Este módulo
decide lo mismo, con la misma regla, de forma reproducible.

DELIBERADAMENTE PURO: ni Redis, ni Mongo, ni HubSpot, ni reloj propio. Solo
datos de entrada y una decisión de salida. Eso permite dos cosas:

  1. Probar el 100% de las reglas sin red, incluidos los bordes.
  2. Reusarlo tal cual desde el script de depuración histórica y, más adelante,
     desde el job periódico — sin duplicar la regla en dos sitios, que es como
     acaban desincronizándose.

Los identificadores de embudo NO viven aquí: llegan en `Regla` desde
outbound_panel (HUBSPOT_STAGE_NO_RESPONDE / HUBSPOT_STAGE_CERRADO_PERDIDO), que
es su fuente única.

⏱️  Todos los datetime deben ser naive y en hora de Bogotá — es como los guarda
MongoDB en `messages.timestamp`. Mezclar naive y aware revienta al comparar; el
llamador es responsable de normalizar.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple

# Cuántos masivos seguidos sin respuesta hacen falta para dar el lead por perdido.
# El usuario lo fijó en "2 o más" el 18-ago-2026.
UMBRAL_MASIVOS_SIN_RESPUESTA = 2

# Cuánto se espera tras el último masivo antes de decidir. Sin esta espera se
# cerraría a quien todavía no ha tenido ocasión de contestar.
ESPERA_MINIMA_HORAS = 48


# ── Motivos ─────────────────────────────────────────────────────────────────
# Cadenas estables: se escriben en el informe y en el log de auditoría, así que
# renombrarlas rompe la trazabilidad de depuraciones ya ejecutadas.
MOTIVO_DEPURAR = "depurar"
MOTIVO_SIN_MASIVOS = "sin_masivos"
MOTIVO_FUERA_DEL_EMBUDO = "fuera_del_embudo"
MOTIVO_RESPONDIO = "respondio_tras_el_masivo"
MOTIVO_RACHA_CORTA = "racha_insuficiente"
MOTIVO_ESPERA_NO_CUMPLIDA = "espera_no_cumplida"
MOTIVO_RACHA_EXCLUIDA = "racha_iniciada_en_periodo_excluido"


@dataclass(frozen=True)
class Regla:
    """Parámetros de la depuración. Los embudos llegan de fuera a propósito."""

    etapa_origen: str
    etapa_destino: str
    umbral: int = UMBRAL_MASIVOS_SIN_RESPUESTA
    espera_horas: int = ESPERA_MINIMA_HORAS
    # Si la racha ARRANCA en esta fecha o después, no se depura. Sirve para
    # dejar fuera campañas demasiado recientes: un contacto cuyo primer masivo
    # sin responder es de hace pocas semanas merece otro intento, no el cierre.
    excluir_racha_desde: Optional[datetime] = None


@dataclass(frozen=True)
class Historial:
    """Todo lo que se sabe de un contacto para decidir sobre él."""

    contact_id: str
    envios_masivos: Sequence[datetime]      # masivos al embudo origen
    ultima_respuesta: Optional[datetime]    # último mensaje entrante del cliente
    etapa_actual: Optional[str]             # lifecyclestage vigente en HubSpot


@dataclass(frozen=True)
class Decision:
    depurar: bool
    motivo: str
    racha: int                              # masivos seguidos sin respuesta
    primer_masivo_racha: Optional[datetime]
    ultimo_masivo: Optional[datetime]


def racha_sin_respuesta(
    envios_masivos: Sequence[datetime],
    ultima_respuesta: Optional[datetime],
) -> Tuple[datetime, ...]:
    """
    Masivos posteriores a la última respuesta del cliente, en orden.

    La racha se cuenta DESDE la última respuesta, no desde el principio de los
    tiempos: si alguien contestó hace seis meses y luego encajó dos masivos sin
    responder, su racha es 2. Y si contestó entre el primer y el segundo
    masivo, su racha vuelve a 1 — la respuesta reinicia la cuenta.
    """
    if ultima_respuesta is None:
        return tuple(sorted(envios_masivos))
    return tuple(sorted(t for t in envios_masivos if t > ultima_respuesta))


def evaluar(historial: Historial, regla: Regla, ahora: datetime) -> Decision:
    """
    Decide si un contacto debe pasar al embudo terminal.

    El orden de las comprobaciones importa: va de lo más barato y excluyente a
    lo más específico, y cada salida deja un motivo distinto para que el informe
    explique por qué NO se tocó a alguien, no solo por qué sí.
    """
    envios = tuple(sorted(historial.envios_masivos or ()))

    if not envios:
        return Decision(False, MOTIVO_SIN_MASIVOS, 0, None, None)

    # La etapa manda: si la asesora ya lo movió, la depuración no lo revierte.
    if historial.etapa_actual != regla.etapa_origen:
        return Decision(False, MOTIVO_FUERA_DEL_EMBUDO, 0, None, envios[-1])

    racha = racha_sin_respuesta(envios, historial.ultima_respuesta)

    if not racha:
        return Decision(False, MOTIVO_RESPONDIO, 0, None, envios[-1])

    primero, ultimo = racha[0], racha[-1]

    if len(racha) < regla.umbral:
        return Decision(False, MOTIVO_RACHA_CORTA, len(racha), primero, ultimo)

    if ahora - ultimo < timedelta(hours=regla.espera_horas):
        return Decision(False, MOTIVO_ESPERA_NO_CUMPLIDA, len(racha), primero, ultimo)

    if regla.excluir_racha_desde is not None and primero >= regla.excluir_racha_desde:
        return Decision(False, MOTIVO_RACHA_EXCLUIDA, len(racha), primero, ultimo)

    return Decision(True, MOTIVO_DEPURAR, len(racha), primero, ultimo)
