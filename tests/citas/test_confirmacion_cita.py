"""
QA Fase 6 — Módulo de citas: etapa automática y confirmación al cliente.

QUÉ SE VALIDA
  Punto 2  Al agendar, el contacto pasa a "Visita agendada" (marketingqualifiedlead)
           sin que la asesora tenga que moverlo a mano.
  Punto 3  La cita lleva dirección obligatoria y teléfono del encargado, y el
           cliente recibe una confirmación por WhatsApp con esos datos.

DATOS REALES SOBRE LOS QUE SE DIMENSIONÓ (medidos el 19-ago-2026 en producción)
  310  contactos con cita en los últimos 90 días
  300  (96.8%) ya pasaban por "Visita agendada"; mediana 12 s ANTES de crear la
       cita → la asesora ya hacía este paso a mano, esto lo automatiza
   10  (3.2%) hicieron la visita sin pasar por "Visita agendada", así que
       `_update_contact_to_visita_realizada` nunca los avanzó: quedaron fuera
       del embudo comercial
   10  (3.2%) venían de una etapa avanzada y la asesora la pisó igual → el
       helper NO lleva guard de etapas protegidas, a propósito
  314  de 326 citas (96.3%) se agendan con la ventana de 24h ABIERTA → la
       confirmación llega como texto libre sin esperar aprobación de Meta
    0  de 326 citas tenían observaciones escritas → un campo opcional aquí
       saldría vacío; por eso la dirección es obligatoria
    6  encargados activos, ninguno con teléfono todavía → la falta de teléfono
       NO puede bloquear el agendamiento

Ejecutar:
    python -m pytest tests/panel/test_confirmacion_cita.py -v
"""
import asyncio
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from middleware.confirmacion_cita import (  # noqa: E402
    CAMPOS,
    IDENTIFICADOR_PLANTILLA,
    MOTIVO_DATOS_INCOMPLETOS,
    MOTIVO_ENVIAR,
    MOTIVO_SIN_CUERPO,
    MOTIVO_SIN_PLANTILLA,
    MOTIVO_SIN_TELEFONO_CLIENTE,
    DatosCita,
    componer,
    valores_de,
)
from middleware.templates.templates import DEFAULT_TEMPLATES  # noqa: E402
from utils.date_parser import (  # noqa: E402
    DIAS_SEMANA,
    DIAS_SEMANA_ABREV,
    DIAS_SEMANA_CAP,
    MESES,
    MESES_ABREV,
    MESES_ABREV_CAP,
    AppointmentDateParser,
    fecha_larga,
    hora_12h,
)

PLANTILLA = DEFAULT_TEMPLATES[IDENTIFICADOR_PLANTILLA]

# Un martes por la tarde, hora de Bogotá.
CUANDO = datetime(2026, 9, 15, 15, 30)


def datos(**cambios):
    base = dict(
        fecha_hora=CUANDO,
        lugar="Calle 10 #43-25, Apto 502, Envigado",
        encargado="Mauricio Restrepo",
        telefono_encargado="+573001234567",
        telefono_cliente="+573009998877",
    )
    base.update(cambios)
    return DatosCita(**base)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Composición del mensaje
# ═══════════════════════════════════════════════════════════════════════════

def test_la_confirmacion_sale_con_los_cinco_datos():
    c = componer(PLANTILLA, datos())
    assert c.enviar is True
    assert c.motivo == MOTIVO_ENVIAR
    assert "martes 15 de septiembre de 2026" in c.cuerpo
    assert "3:30 PM" in c.cuerpo
    assert "Calle 10 #43-25, Apto 502, Envigado" in c.cuerpo
    assert "Mauricio Restrepo" in c.cuerpo
    assert "+573001234567" in c.cuerpo


def test_no_queda_ni_una_llave_sin_reemplazar():
    """Un {lugar} literal en el WhatsApp de un cliente es el peor fallo posible."""
    c = componer(PLANTILLA, datos())
    for variable in PLANTILLA["variables"]:
        assert "{" + variable + "}" not in c.cuerpo


def test_el_texto_sale_de_la_plantilla_no_del_modulo():
    """
    Si una asesora edita la plantilla desde el panel, la confirmación la sigue.
    Se prueba con una plantilla inventada: si el módulo llevara el texto dentro,
    este test devolvería el texto de producción.
    """
    propia = {
        "body": "Cita el {fecha} a las {hora}. Lugar: {lugar}.",
        "variables": ["fecha", "hora", "lugar"],
        "content_sid": None,
        "content_variables_map": ["fecha", "hora", "lugar"],
    }
    c = componer(propia, datos())
    assert c.enviar is True
    assert c.cuerpo.startswith("Cita el martes 15 de septiembre de 2026 a las 3:30 PM.")
    assert "Inmobiliaria Proteger" not in c.cuerpo


def test_una_plantilla_con_menos_variables_no_exige_todos_los_datos():
    """Se recorre lo que la plantilla PIDE, no la lista fija de campos."""
    corta = {"body": "Su cita: {fecha}", "variables": ["fecha"], "content_sid": None}
    c = componer(corta, datos(telefono_encargado="", encargado=""))
    assert c.enviar is True


# ═══════════════════════════════════════════════════════════════════════════
# 2. Cuándo NO se envía — cada negativa con su motivo
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_plantilla_no_se_inventa_un_mensaje():
    assert componer(None, datos()).motivo == MOTIVO_SIN_PLANTILLA


def test_plantilla_vaciada_desde_el_panel_no_manda_un_mensaje_en_blanco():
    vacia = {"body": "   ", "variables": [], "content_sid": None}
    assert componer(vacia, datos()).motivo == MOTIVO_SIN_CUERPO


def test_sin_telefono_del_cliente_no_hay_a_quien_mandarle():
    assert componer(PLANTILLA, datos(telefono_cliente="")).motivo == MOTIVO_SIN_TELEFONO_CLIENTE


def test_encargado_sin_telefono_no_manda_la_cita_a_medias():
    """
    Los 6 encargados en producción no tienen teléfono todavía. Hasta que se les
    ponga, la confirmación se omite — pero diciendo exactamente qué falta.
    """
    c = componer(PLANTILLA, datos(telefono_encargado=""))
    assert c.enviar is False
    assert c.motivo == MOTIVO_DATOS_INCOMPLETOS
    assert c.faltantes == ("contacto",)


def test_el_motivo_nombra_todas_las_variables_que_faltan():
    c = componer(PLANTILLA, datos(telefono_encargado="", encargado="  "))
    assert set(c.faltantes) == {"asesor", "contacto"}


def test_una_variable_que_nadie_sabe_llenar_frena_el_envio():
    """
    Si mañana se le añade {inmueble} a la plantilla y nadie enseña a llenarlo,
    el mensaje NO sale. Fallar es preferible a mandar un hueco.
    """
    ampliada = dict(PLANTILLA)
    ampliada["variables"] = list(PLANTILLA["variables"]) + ["inmueble"]
    c = componer(ampliada, datos())
    assert c.enviar is False
    assert "inmueble" in c.faltantes


def test_un_dato_que_revienta_al_formatear_cuenta_como_faltante():
    c = componer(PLANTILLA, datos(fecha_hora="no soy una fecha"))
    assert c.enviar is False
    assert set(c.faltantes) == {"fecha", "hora"}


def test_cuando_no_se_envia_el_cuerpo_va_vacio():
    """Nadie puede confundirse y mandar el texto de una decisión negativa."""
    for c in (
        componer(None, datos()),
        componer(PLANTILLA, datos(telefono_cliente="")),
        componer(PLANTILLA, datos(telefono_encargado="")),
    ):
        assert c.cuerpo == ""
        assert c.content_sid is None
        assert c.content_variables is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. Plantilla aprobada por Meta (cuando exista el Content SID)
# ═══════════════════════════════════════════════════════════════════════════

def test_hoy_la_plantilla_va_sin_sid_y_por_eso_viaja_como_texto_libre():
    """
    `cita_confirmacion` sigue sin aprobación de Meta. No es un bloqueo: el 96.3%
    de las citas se agendan con la ventana de 24h abierta.
    """
    assert PLANTILLA["content_sid"] is None
    assert componer(PLANTILLA, datos()).content_sid is None


def test_con_sid_se_numeran_las_variables_como_espera_twilio():
    aprobada = dict(PLANTILLA)
    aprobada["content_sid"] = "HXaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    c = componer(aprobada, datos())
    assert c.content_sid == aprobada["content_sid"]
    assert c.content_variables == {
        "1": "martes 15 de septiembre de 2026",
        "2": "3:30 PM",
        "3": "Calle 10 #43-25, Apto 502, Envigado",
        "4": "Mauricio Restrepo",
        "5": "+573001234567",
    }


def test_con_mapa_explicito_se_respeta_la_numeracion_declarada():
    """Plantillas de Meta que no empiezan en {{1}}."""
    rara = {
        "body": "{lugar} a las {hora}",
        "variables": ["lugar", "hora"],
        "content_sid": "HXbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "content_variables_map": {"3": "lugar", "7": "hora"},
    }
    c = componer(rara, datos())
    assert c.content_variables == {"3": "Calle 10 #43-25, Apto 502, Envigado", "7": "3:30 PM"}


def test_sin_sid_no_se_mandan_content_variables():
    assert componer(PLANTILLA, datos()).content_variables is None


# ═══════════════════════════════════════════════════════════════════════════
# 4. Formato de fecha y hora — los bordes que se rompen solos
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("hora,minuto,esperado", [
    (0, 0, "12:00 AM"),      # medianoche no es "0:00"
    (0, 5, "12:05 AM"),
    (11, 59, "11:59 AM"),
    (12, 0, "12:00 PM"),     # mediodía no es "0:00 PM"
    (13, 5, "1:05 PM"),
    (23, 30, "11:30 PM"),
])
def test_las_horas_frontera_se_escriben_bien(hora, minuto, esperado):
    assert hora_12h(datetime(2026, 9, 15, hora, minuto)) == esperado


def test_la_variante_con_cero_es_la_que_ya_usaban_las_notas():
    assert hora_12h(datetime(2026, 9, 15, 9, 5), con_cero=True) == "09:05 AM"


def test_los_doce_meses_y_los_siete_dias_estan_completos():
    assert len(MESES) == 12
    assert len(DIAS_SEMANA) == 7
    assert fecha_larga(datetime(2026, 1, 4)) == "domingo 4 de enero de 2026"
    assert fecha_larga(datetime(2026, 12, 31)) == "jueves 31 de diciembre de 2026"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Regresión del refactor: las cuatro copias de nombres de fecha
# ═══════════════════════════════════════════════════════════════════════════
# Estaban duplicadas en date_parser (x2) y outbound_panel (x2). Ahora se derivan
# de dos tablas. Estos tests fijan los valores literales que había en cada copia:
# si alguien toca la derivación, aquí salta.

def test_las_abreviaturas_derivadas_son_las_que_habia():
    assert list(DIAS_SEMANA_ABREV) == ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    assert list(MESES_ABREV) == [
        "ene", "feb", "mar", "abr", "may", "jun",
        "jul", "ago", "sep", "oct", "nov", "dic",
    ]


def test_las_capitalizadas_derivadas_son_las_que_habia():
    assert list(DIAS_SEMANA_CAP) == [
        "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo",
    ]
    assert list(MESES_ABREV_CAP) == [
        "Ene", "Feb", "Mar", "Abr", "May", "Jun",
        "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
    ]


def test_el_mensaje_del_recordatorio_no_cambio_de_formato():
    """`format_appointment_for_message` la usa el scheduler de recordatorios."""
    d = datetime(2026, 9, 14, 15, 5)
    assert AppointmentDateParser.format_appointment_for_message(d) == (
        "lunes 14 de septiembre a las 3:05 PM"
    )
    assert AppointmentDateParser.format_appointment_for_message(d, include_day_name=False) == (
        "14 de septiembre a las 3:05 PM"
    )


def test_la_nota_de_hubspot_conserva_su_formato_exacto():
    """Reproduce la línea que arma create_appointment, que antes usaba strftime."""
    d = datetime(2026, 9, 14, 15, 5)
    linea = (
        f"{DIAS_SEMANA_CAP[d.weekday()]}, "
        f"{d.day} {MESES_ABREV_CAP[d.month - 1]} {d.year} | "
        f"{hora_12h(d, con_cero=True)} (Hora Colombia)"
    )
    assert linea == "Lunes, 14 Sep 2026 | 03:05 PM (Hora Colombia)"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Contrato del módulo
# ═══════════════════════════════════════════════════════════════════════════

def test_la_plantilla_de_produccion_declara_lo_que_el_modulo_sabe_llenar():
    """
    Si alguien añade una variable a `cita_confirmacion` sin enseñar a llenarla,
    las confirmaciones dejan de salir en silencio. Este test lo dice antes.
    """
    assert set(PLANTILLA["variables"]) <= set(CAMPOS)


def test_el_mapa_de_twilio_coincide_con_las_variables_declaradas():
    assert list(PLANTILLA["content_variables_map"]) == list(PLANTILLA["variables"])


def test_valores_de_no_mete_las_vacias_entre_los_llenos():
    llenos, faltantes = valores_de(
        ["fecha", "contacto"], datos(telefono_encargado="")
    )
    assert "contacto" not in llenos
    assert faltantes == ("contacto",)


def test_el_modulo_es_puro_de_verdad():
    """Ni red, ni reloj, ni base de datos: si aparece un import de esos, se rompe."""
    import middleware.confirmacion_cita as modulo

    fuente = open(modulo.__file__, encoding="utf-8").read()
    for prohibido in ("import httpx", "import redis", "twilio", "motor", "datetime.now"):
        assert prohibido not in fuente, f"el motor dejó de ser puro: {prohibido}"
