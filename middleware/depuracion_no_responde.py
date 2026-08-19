"""
Motor de decisión de la depuración del embudo "No responde".

Un contacto que recibe varios masivos de reactivación y no contesta a ninguno
está perdido de hecho. Hoy las asesoras lo detectan a ojo y lo mueven a mano a
"Cerrado perdido" — 58 de ellos ya movidos así a 18-ago-2026. Este módulo
decide lo mismo, con la misma regla, de forma reproducible.

DOS FORMAS DE CONTAR (ver MODO_*)
    La primera corrida (18-ago-2026) contó solo los masivos SEGUIDOS, es decir
    los posteriores a la última respuesta del cliente. Eso dejaba fuera un caso
    real y frecuente: quien contestó al primer masivo con un "no gracias",
    recibió otro meses después y ya no volvió a hablar. Son 2 masivos y 0
    interés, pero la racha valía 1.

    De ahí `MODO_HISTORIAL_COMPLETO`, que cuenta todos los masivos del historial
    estén seguidos o no. Medido el 19-ago-2026: 378 contactos con la primera
    forma, 458 con la segunda, y NINGUNO que saliera antes deja de salir — la
    segunda amplía a la primera, no la contradice.

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
from typing import Callable, Dict, Optional, Sequence, Tuple

# Cuántos masivos sin respuesta hacen falta para dar el lead por perdido.
# El usuario lo fijó en "2 o más" el 18-ago-2026.
UMBRAL_MASIVOS_SIN_RESPUESTA = 2

# Cuánto se espera tras el último masivo antes de decidir. Sin esta espera se
# cerraría a quien todavía no ha tenido ocasión de contestar.
ESPERA_MINIMA_HORAS = 48


# ── Modos de conteo ─────────────────────────────────────────────────────────
# Solo cambian QUÉ masivos se cuentan. El resto de la regla (umbral, espera,
# exclusión por fecha, embudo de origen) es idéntico en los dos.
MODO_RACHA_SEGUIDA = "racha_seguida"
MODO_HISTORIAL_COMPLETO = "historial_completo"


# ── Motivos ─────────────────────────────────────────────────────────────────
# Cadenas estables: se escriben en el informe y en el log de auditoría, así que
# renombrarlas rompe la trazabilidad de depuraciones ya ejecutadas. Por eso
# siguen diciendo "racha" aunque ahora exista un modo que cuenta el historial
# entero: el valor es un identificador, no una descripción.
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
    # Si el primer masivo contado cae en esta fecha o después, no se depura.
    # Sirve para dejar fuera campañas demasiado recientes: un contacto cuyo
    # primer masivo sin responder es de hace pocas semanas merece otro intento,
    # no el cierre.
    excluir_racha_desde: Optional[datetime] = None
    # Por defecto, la forma de contar de la primera corrida. Se deja así a
    # propósito: cambiar el defecto alteraría en silencio a cualquier llamador
    # que ya exista —incluido el futuro job periódico—, y ampliar la regla debe
    # ser una decisión explícita de quien la pide.
    modo: str = MODO_RACHA_SEGUIDA


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
    masivos_contados: int                   # los que la regla tuvo en cuenta
    primer_masivo: Optional[datetime]       # el primero de los contados
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


def masivos_del_historial(
    envios_masivos: Sequence[datetime],
    ultima_respuesta: Optional[datetime],
) -> Tuple[datetime, ...]:
    """
    Todos los masivos recibidos, estén seguidos o no.

    Una respuesta intercalada NO reinicia la cuenta: contestar "no gracias" a un
    masivo y callar ante el siguiente sigue siendo un lead perdido.

    El único caso que vacía la cuenta es que el cliente hablara DESPUÉS del
    último masivo. Ahí no aplica el embudo: esa persona respondió. En el modo de
    racha ese candado salía gratis; aquí hay que ponerlo a mano, y es
    imprescindible — el 19-ago-2026 había 106 contactos con 2+ masivos que sí
    contestaron al último, 39 de ellos todavía dentro del embudo.
    """
    envios = tuple(sorted(envios_masivos))
    if not envios:
        return ()
    if ultima_respuesta is not None and ultima_respuesta > envios[-1]:
        return ()
    return envios


BaseDeConteo = Callable[[Sequence[datetime], Optional[datetime]], Tuple[datetime, ...]]

BASES_DE_CONTEO: Dict[str, BaseDeConteo] = {
    MODO_RACHA_SEGUIDA: racha_sin_respuesta,
    MODO_HISTORIAL_COMPLETO: masivos_del_historial,
}


def base_de_conteo(modo: str) -> BaseDeConteo:
    """
    Traduce el modo a la función que cuenta. Falla ruidosamente si no existe:
    caer en un modo por defecto ante un nombre mal escrito depuraría contactos
    con una regla distinta de la pedida.
    """
    try:
        return BASES_DE_CONTEO[modo]
    except KeyError:
        conocidos = ", ".join(sorted(BASES_DE_CONTEO))
        raise ValueError(
            f"modo de conteo desconocido: {modo!r}. Conocidos: {conocidos}"
        )


def evaluar(historial: Historial, regla: Regla, ahora: datetime) -> Decision:
    """
    Decide si un contacto debe pasar al embudo terminal.

    El orden de las comprobaciones importa: va de lo más barato y excluyente a
    lo más específico, y cada salida deja un motivo distinto para que el informe
    explique por qué NO se tocó a alguien, no solo por qué sí.
    """
    contar = base_de_conteo(regla.modo)
    envios = tuple(sorted(historial.envios_masivos or ()))

    if not envios:
        return Decision(False, MOTIVO_SIN_MASIVOS, 0, None, None)

    # La etapa manda: si la asesora ya lo movió, la depuración no lo revierte.
    if historial.etapa_actual != regla.etapa_origen:
        return Decision(False, MOTIVO_FUERA_DEL_EMBUDO, 0, None, envios[-1])

    contados = contar(envios, historial.ultima_respuesta)

    if not contados:
        return Decision(False, MOTIVO_RESPONDIO, 0, None, envios[-1])

    primero, ultimo = contados[0], contados[-1]

    if len(contados) < regla.umbral:
        return Decision(False, MOTIVO_RACHA_CORTA, len(contados), primero, ultimo)

    if ahora - ultimo < timedelta(hours=regla.espera_horas):
        return Decision(False, MOTIVO_ESPERA_NO_CUMPLIDA, len(contados), primero, ultimo)

    if regla.excluir_racha_desde is not None and primero >= regla.excluir_racha_desde:
        return Decision(False, MOTIVO_RACHA_EXCLUIDA, len(contados), primero, ultimo)

    return Decision(True, MOTIVO_DEPURAR, len(contados), primero, ultimo)
