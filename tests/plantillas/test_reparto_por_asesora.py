"""
Cada asesora ve en el picker del chat solo las plantillas que le tocan.

El reparto vive en `utils/advisors_registry.py` (campo `panel_templates`) y lo
aplica `outbound_panel._picker_visible`. Dos cosas hay que blindar aquí:

    · un id mal escrito en el reparto deja a la asesora sin esa plantilla y NADIE
      se entera: no hay error, simplemente no aparece en la lista;
    · las plantillas que las asesoras crean a mano desde el modal (Horarios,
      Requisitos) viven solo en Redis y no pueden verse afectadas por el reparto.

Son de análisis estático: no hay red, ni Redis, ni Twilio.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from middleware.outbound_panel import _picker_visible  # noqa: E402
from middleware.templates.templates import (  # noqa: E402
    DEFAULT_TEMPLATES,
    SOLO_AUTOMATICAS,
)
from utils.advisors_registry import ADVISORS, get_panel_templates  # noqa: E402

JUBENY = "89096378"
LUISA = "89096380"
MONICA = "89096379"


def _visibles(advisor_id: str) -> set:
    """Ids predefinidos que acabarían en el picker de esta asesora."""
    permitidas = get_panel_templates(advisor_id)
    return {
        tid for tid, tpl in DEFAULT_TEMPLATES.items()
        if _picker_visible({**tpl, "id": tid}, permitidas)
    }


# ── El reparto declarado ─────────────────────────────────────────────────────

def test_reparto_de_jubeny():
    assert get_panel_templates(JUBENY) == [
        "saludo_reactivador_inmueble",
        "cita_confirmacion",
        "seguimiento_personalizado",
    ]


def test_reparto_de_luisa():
    assert get_panel_templates(LUISA) == [
        "cita_confirmacion",
        "seguimiento_personalizado",
    ]


def test_sin_reparto_ve_el_catalogo_entero():
    assert get_panel_templates(MONICA) is None
    # Una asesora que ni siquiera esté en el registro tampoco se queda sin nada:
    # mejor que vea de más a que no pueda reabrir una ventana de 24h cerrada.
    assert get_panel_templates("00000000") is None


@pytest.mark.parametrize("advisor_id", sorted(ADVISORS))
def test_todo_id_del_reparto_existe_en_el_catalogo(advisor_id):
    """Un typo aquí no lanza: deja a la asesora sin la plantilla, en silencio."""
    permitidas = ADVISORS[advisor_id].get("panel_templates")
    if permitidas is None:
        return
    desconocidos = [tid for tid in permitidas if tid not in DEFAULT_TEMPLATES]
    assert not desconocidos, f"{advisor_id} tiene ids que no existen: {desconocidos}"


def test_ningun_reparto_asigna_una_automatica():
    """Asignar una del scheduler no la haría visible, así que sería letra muerta."""
    for advisor_id, cfg in ADVISORS.items():
        permitidas = cfg.get("panel_templates") or []
        assert not set(permitidas) & set(SOLO_AUTOMATICAS), advisor_id


# ── Lo que acaba en cada picker ──────────────────────────────────────────────

def test_jubeny_ve_tres_predefinidas():
    assert _visibles(JUBENY) == {
        "saludo_reactivador_inmueble",
        "cita_confirmacion",
        "seguimiento_personalizado",
    }


def test_luisa_ve_dos_predefinidas():
    assert _visibles(LUISA) == {"cita_confirmacion", "seguimiento_personalizado"}


def test_monica_ve_el_catalogo_menos_las_automaticas():
    assert _visibles(MONICA) == set(DEFAULT_TEMPLATES) - set(SOLO_AUTOMATICAS)


def test_las_del_scheduler_no_le_salen_a_nadie():
    for tid in SOLO_AUTOMATICAS:
        for advisor_id in (JUBENY, LUISA, MONICA):
            assert tid not in _visibles(advisor_id), f"{tid} visible para {advisor_id}"


def test_reactivacion_link_queda_fuera_del_reparto():
    """No está en ninguna de las dos listas: desaparece de sus pickers."""
    assert "reactivacion_inmueble_link" not in _visibles(JUBENY)
    assert "reactivacion_inmueble_link" not in _visibles(LUISA)
    # Pero sigue en el catálogo y la ve quien no tiene reparto.
    assert "reactivacion_inmueble_link" in _visibles(MONICA)


# ── Las plantillas que crean las asesoras ────────────────────────────────────

@pytest.mark.parametrize("advisor_id", [JUBENY, LUISA, MONICA])
def test_las_creadas_a_mano_nunca_se_filtran(advisor_id):
    """Horarios y Requisitos las crearon las asesoras: el reparto no las toca.

    Why: viven solo en `whatsapp_template:{advisor_id}:*` en Redis y no están en
    DEFAULT_TEMPLATES. Filtrarlas por el reparto las haría desaparecer del panel
    sin que nadie pueda recuperarlas desde el código.
    """
    personal = {"id": "requisitos", "name": "Requisitos", "is_default": False}
    assert _picker_visible(personal, get_panel_templates(advisor_id)) is True


def test_una_personal_no_se_confunde_por_el_flag_is_default():
    """Editar una predefinida la copia al namespace personal y `is_default` miente.

    Why: `PUT /templates/{id}` (outbound_panel.py:3297) lee con fallback al
    default, así que la copia personal hereda el flag. Por eso `_picker_visible`
    decide por pertenencia a DEFAULT_TEMPLATES, no por el flag.
    """
    # Predefinida que Luisa NO tiene asignada, disfrazada de personal.
    disfrazada = {"id": "reactivacion_inmueble_link", "is_default": False}
    assert _picker_visible(disfrazada, get_panel_templates(LUISA)) is False

    # Y al revés: una creada a mano con el flag puesto sigue siendo suya.
    inventada = {"id": "horarios", "is_default": True}
    assert _picker_visible(inventada, get_panel_templates(LUISA)) is True
