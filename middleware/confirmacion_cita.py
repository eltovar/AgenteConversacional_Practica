"""
Composición del mensaje de confirmación que recibe el cliente al agendar una cita.

Hasta ahora la confirmación se escribía a mano: la asesora agendaba en el panel y
después redactaba el mensaje. La plantilla `cita_confirmacion` existía en
`templates/templates.py` desde el principio pero **ningún código la usaba**.

QUÉ DECIDE ESTE MÓDULO
    Si hay con qué enviar, y con qué texto exacto. Nada más. El envío, la red y
    el guardado en el historial viven en el llamador (`outbound_panel`).

DE DÓNDE SALE EL TEXTO
    De la plantilla que le pasen, NO de aquí. La plantilla vive en Redis
    (`whatsapp_template:default:cita_confirmacion`, sembrada desde
    `DEFAULT_TEMPLATES`) y una asesora puede tener su propia versión. Si alguien
    edita ese texto desde el panel, la confirmación cambia sin tocar este archivo.
    Por eso aquí no hay ni una frase del mensaje.

QUÉ VARIABLE SE LLENA CON QUÉ
    Lo dice `CAMPOS`, y es lo único que este módulo sabe de la plantilla. Se
    recorren las variables que la plantilla DECLARA, no una lista fija: si mañana
    se le añade una variable nueva y nadie enseña aquí a llenarla, sale como
    faltante y el mensaje NO se envía. Un hueco visible es un fallo; `{lugar}`
    literal en el WhatsApp de un cliente es peor.

DELIBERADAMENTE PURO: ni Redis, ni Mongo, ni Twilio, ni reloj propio. Entra la
plantilla y los datos de la cita, sale una decisión. Así la regla se prueba
entera sin red, y el día que se reenvíe una confirmación desde otro sitio se
reusa tal cual en vez de duplicarla.

⏱️  `fecha_hora` debe llegar en hora de Bogotá. Este módulo no convierte zonas:
el llamador ya tiene el datetime en la zona correcta y hacerlo dos veces es
como se producen las citas con una hora de diferencia.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from utils.date_parser import fecha_larga, hora_12h

# Identificador de la plantilla en Redis / DEFAULT_TEMPLATES. Vive aquí porque
# es lo que une este módulo con el catálogo de plantillas, y estaba en cero
# sitios: la plantilla existía sin que nadie la nombrara.
IDENTIFICADOR_PLANTILLA = "cita_confirmacion"


# ── Motivos ─────────────────────────────────────────────────────────────────
# Cadenas estables: viajan en la respuesta del endpoint y acaban en el log.
MOTIVO_ENVIAR = "enviar"
MOTIVO_SIN_PLANTILLA = "plantilla_no_encontrada"
MOTIVO_SIN_CUERPO = "plantilla_sin_cuerpo"
MOTIVO_SIN_TELEFONO_CLIENTE = "sin_telefono_del_cliente"
MOTIVO_DATOS_INCOMPLETOS = "datos_incompletos"


@dataclass(frozen=True)
class DatosCita:
    """Lo que hace falta saber de una cita para poder confirmarla."""

    fecha_hora: datetime          # en hora de Bogotá
    lugar: str                    # dirección del inmueble — la escribe la asesora
    encargado: str                # quién muestra el inmueble
    telefono_encargado: str       # su teléfono, tomado de su ficha
    telefono_cliente: str         # destinatario del mensaje


@dataclass(frozen=True)
class Confirmacion:
    enviar: bool
    motivo: str
    cuerpo: str
    content_sid: Optional[str]
    content_variables: Optional[Dict[str, str]]
    faltantes: Tuple[str, ...]    # variables que la plantilla pide y no se pudieron llenar
    # Los valores con los que se lleno la plantilla. Se guardan con la cita para
    # poder responder a "¿ha cambiado algo que el cliente deba saber?" comparando
    # datos y no textos: el texto puede cambiar porque alguien edite la plantilla
    # desde el panel, y eso no es motivo para reescribirle al cliente.
    variables: Dict[str, str] = field(default_factory=dict)


# Qué dato alimenta cada variable de la plantilla. Es el único acoplamiento con
# el texto, y está en un solo sitio a propósito.
CAMPOS: Dict[str, Callable[[DatosCita], str]] = {
    "fecha": lambda d: fecha_larga(d.fecha_hora),
    "hora": lambda d: hora_12h(d.fecha_hora),
    "lugar": lambda d: d.lugar,
    "asesor": lambda d: d.encargado,
    "contacto": lambda d: d.telefono_encargado,
}


def _valor(nombre: str, datos: DatosCita) -> str:
    """Valor de una variable, o cadena vacía si no sabemos llenarla."""
    constructor = CAMPOS.get(nombre)
    if constructor is None:
        return ""
    try:
        return (constructor(datos) or "").strip()
    except Exception:
        # Un dato corrupto (fecha inválida, por ejemplo) cuenta como faltante:
        # mejor no enviar que enviar roto.
        return ""


def valores_de(
    variables: Sequence[str],
    datos: DatosCita,
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """
    Llena las variables que la plantilla declara y dice cuáles quedaron vacías.

    Se recorre `variables` —lo que pide la plantilla— y no `CAMPOS`, para que una
    plantilla con menos variables no obligue a tener todos los datos.
    """
    llenos: Dict[str, str] = {}
    faltantes = []
    for nombre in variables:
        valor = _valor(nombre, datos)
        if valor:
            llenos[nombre] = valor
        else:
            faltantes.append(nombre)
    return llenos, tuple(faltantes)


def _content_variables(
    plantilla: Mapping,
    llenos: Mapping[str, str],
) -> Optional[Dict[str, str]]:
    """
    Traduce los valores a la numeración {{1}}, {{2}}... que espera Twilio.

    Misma lógica que ya usa el envío de plantillas del panel: el mapa puede ser
    una lista (numeración secuencial) o un dict (numeración explícita, para
    plantillas que no empiezan en {{1}}).
    """
    mapa = plantilla.get("content_variables_map") or []
    if isinstance(mapa, Mapping):
        return {numero: llenos.get(nombre, "") for numero, nombre in mapa.items()}
    return {str(i + 1): llenos.get(nombre, "") for i, nombre in enumerate(mapa)}


def componer(plantilla: Optional[Mapping], datos: DatosCita) -> Confirmacion:
    """
    Decide si se puede confirmar la cita y con qué texto.

    El orden de las comprobaciones va de lo más excluyente a lo más específico, y
    cada salida deja un motivo distinto: la asesora tiene que poder leer en el
    panel POR QUÉ no salió el mensaje, no solo que no salió.
    """
    def _no(motivo: str, faltantes: Tuple[str, ...] = ()) -> Confirmacion:
        return Confirmacion(False, motivo, "", None, None, faltantes, {})

    if not plantilla:
        return _no(MOTIVO_SIN_PLANTILLA)

    cuerpo_plantilla = (plantilla.get("body") or "").strip()
    if not cuerpo_plantilla:
        return _no(MOTIVO_SIN_CUERPO)

    if not (datos.telefono_cliente or "").strip():
        return _no(MOTIVO_SIN_TELEFONO_CLIENTE)

    variables = plantilla.get("variables") or []
    llenos, faltantes = valores_de(variables, datos)

    if faltantes:
        return _no(MOTIVO_DATOS_INCOMPLETOS, faltantes)

    # `format_map` sobre un dict normal lanzaría KeyError ante una llave del texto
    # que no esté entre las variables declaradas. Se usa una plantilla ya validada,
    # pero un texto editado a mano desde el panel puede traer llaves de más.
    class _Tolerante(dict):
        def __missing__(self, clave):
            return "{" + clave + "}"

    cuerpo = cuerpo_plantilla.format_map(_Tolerante(llenos))

    content_sid = plantilla.get("content_sid") or None
    return Confirmacion(
        enviar=True,
        motivo=MOTIVO_ENVIAR,
        cuerpo=cuerpo,
        content_sid=content_sid,
        content_variables=_content_variables(plantilla, llenos) if content_sid else None,
        faltantes=(),
        variables=dict(llenos),
    )


def hace_falta_reenviar(
    comunicadas: Optional[Mapping[str, str]],
    decision: Confirmacion,
) -> bool:
    """
    Decide si al editar una cita hay que volver a escribirle al cliente.

    LA REGLA: se reenvia cuando cambia algo que el cliente VE. No hace falta una
    lista de campos sensibles; basta comparar los valores con los que se compuso
    el ultimo mensaje que recibio. Si son los mismos, no se ha enterado de nada
    nuevo y no se le molesta — editar una observacion interna no le cuesta un
    WhatsApp.

    Tambien cubre el caso al reves: una cita que nunca pudo confirmarse (el
    encargado no tenia telefono) se confirma en cuanto ese dato aparece, porque
    `comunicadas` esta vacio y cualquier cosa difiere de nada.
    """
    if not decision.enviar:
        return False
    return dict(decision.variables) != dict(comunicadas or {})
