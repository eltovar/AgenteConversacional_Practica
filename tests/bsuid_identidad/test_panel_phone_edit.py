"""
La asesora edita el teléfono desde el panel: PATCH /contacts/{id}/phone.

Gemelo de PATCH /contacts/{id}/name. La diferencia es que cuando la
conversación venía de un BSUID esto NO es un update de HubSpot: dispara la
migración de la clave. Lo que se fija aquí:

- Sin API key no se toca nada.
- Un número inválido se rechaza ANTES de llamar al migrador.
- Con clave BSUID se migra; con teléfono real solo se corrige HubSpot.
- Un lock en curso devuelve 409, no un 500 opaco.
"""
import fakeredis.aioredis
import pytest
from fastapi import HTTPException

import middleware.outbound_panel as panel
from middleware.identity_migration import MigrationResult

CLAVE = "bsuid_aaaabbbbccccddddeeeeffff"
TELEFONO = "+573001234567"
CONTACT_ID = "123456"
API_KEY = "test-dummy-admin-key"


class _RespuestaHubSpot:
    def __init__(self, status_code=200, text="{}"):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


@pytest.fixture
def entorno(monkeypatch):
    """Cablea el endpoint contra dobles y expone lo que se puede observar."""
    redis_falso = fakeredis.aioredis.FakeRedis(decode_responses=True)
    llamadas = {"migraciones": [], "patches": []}

    async def _get_redis():
        return redis_falso

    async def _migrar(identity_key, phone_raw, canal="whatsapp", *, source="sofia"):
        llamadas["migraciones"].append(
            {"identity_key": identity_key, "phone": phone_raw, "canal": canal, "source": source}
        )
        return MigrationResult(
            ok=True, identity_key=identity_key, phone=TELEFONO,
            contact_id=CONTACT_ID, outcome="migrated",
        )

    async def _patch(url, payload, api_key):
        llamadas["patches"].append((url, payload))
        return _RespuestaHubSpot()

    monkeypatch.setattr(panel, "_get_redis_client", _get_redis)
    monkeypatch.setattr(panel, "migrate_identity_to_phone", _migrar)
    monkeypatch.setattr(panel, "_hubspot_patch", _patch)
    monkeypatch.setenv("HUBSPOT_API_KEY", "dummy")

    llamadas["redis"] = redis_falso
    return llamadas


# ═══════════════════════════════════════════════════════════════════════════════
# AUTORIZACIÓN Y VALIDACIÓN
# ═══════════════════════════════════════════════════════════════════════════════


async def test_sin_api_key_no_toca_nada(entorno):
    with pytest.raises(HTTPException) as exc:
        await panel.update_contact_phone(CONTACT_ID, phone=TELEFONO, x_api_key=None)
    assert exc.value.status_code == 401
    assert entorno["migraciones"] == []
    assert entorno["patches"] == []


async def test_contact_id_no_numerico_se_rechaza(entorno):
    with pytest.raises(HTTPException) as exc:
        await panel.update_contact_phone("abc", phone=TELEFONO, x_api_key=API_KEY)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("basura", ["", "300 millones", "12", "no tengo"])
async def test_un_numero_invalido_se_rechaza_antes_de_migrar(entorno, basura):
    """
    La validación va ANTES del migrador a propósito: un 400 aquí es un mensaje
    para la asesora; dejarlo pasar sería una clave de conversación corrupta.
    """
    with pytest.raises(HTTPException) as exc:
        await panel.update_contact_phone(CONTACT_ID, phone=basura, x_api_key=API_KEY)
    assert exc.value.status_code == 400
    assert entorno["migraciones"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSACIÓN BSUID → MIGRACIÓN
# ═══════════════════════════════════════════════════════════════════════════════


async def test_una_clave_bsuid_dispara_la_migracion(entorno):
    await entorno["redis"].set(f"phone_cache:{CONTACT_ID}", CLAVE)

    res = await panel.update_contact_phone(
        CONTACT_ID, phone="3001234567", canal="whatsapp", x_api_key=API_KEY
    )

    assert res["status"] == "success"
    assert res["migrated"] is True
    assert res["phone"] == TELEFONO
    assert res["old_phone"] == CLAVE

    assert len(entorno["migraciones"]) == 1
    llamada = entorno["migraciones"][0]
    assert llamada["identity_key"] == CLAVE
    assert llamada["source"] == "panel"   # distingue lo que puso la asesora


async def test_el_panel_no_parchea_hubspot_por_su_cuenta_al_migrar(entorno):
    """HubSpot lo escribe el migrador, que además decide si adopta un duplicado."""
    await entorno["redis"].set(f"phone_cache:{CONTACT_ID}", CLAVE)

    await panel.update_contact_phone(CONTACT_ID, phone=TELEFONO, x_api_key=API_KEY)

    assert entorno["patches"] == []


async def test_una_migracion_en_curso_devuelve_409(entorno, monkeypatch):
    async def _bloqueado(identity_key, phone_raw, canal="whatsapp", *, source="sofia"):
        return MigrationResult(ok=False, identity_key=identity_key, outcome="locked")

    monkeypatch.setattr(panel, "migrate_identity_to_phone", _bloqueado)
    await entorno["redis"].set(f"phone_cache:{CONTACT_ID}", CLAVE)

    with pytest.raises(HTTPException) as exc:
        await panel.update_contact_phone(CONTACT_ID, phone=TELEFONO, x_api_key=API_KEY)

    assert exc.value.status_code == 409


async def test_un_fallo_del_migrador_no_miente_a_la_asesora(entorno, monkeypatch):
    async def _falla(identity_key, phone_raw, canal="whatsapp", *, source="sofia"):
        return MigrationResult(ok=False, identity_key=identity_key, outcome="error")

    monkeypatch.setattr(panel, "migrate_identity_to_phone", _falla)
    await entorno["redis"].set(f"phone_cache:{CONTACT_ID}", CLAVE)

    with pytest.raises(HTTPException) as exc:
        await panel.update_contact_phone(CONTACT_ID, phone=TELEFONO, x_api_key=API_KEY)

    assert exc.value.status_code == 500


# ═══════════════════════════════════════════════════════════════════════════════
# CONTACTO TELEFÓNICO → SOLO HUBSPOT
# ═══════════════════════════════════════════════════════════════════════════════


async def test_un_contacto_con_telefono_no_migra_claves(entorno):
    """
    Corregir el número de un contacto que YA tenía uno es otra operación. Migrar
    aquí movería una conversación telefónica sana sin que nadie lo pidiera.
    """
    await entorno["redis"].set(f"phone_cache:{CONTACT_ID}", "+573009998877")

    res = await panel.update_contact_phone(CONTACT_ID, phone=TELEFONO, x_api_key=API_KEY)

    assert res["migrated"] is False
    assert res["phone"] == TELEFONO
    assert entorno["migraciones"] == []
    assert len(entorno["patches"]) == 1
    _, payload = entorno["patches"][0]
    assert payload == {"properties": {"phone": TELEFONO}}


async def test_sin_conversacion_en_redis_solo_actualiza_hubspot(entorno):
    res = await panel.update_contact_phone(CONTACT_ID, phone=TELEFONO, x_api_key=API_KEY)
    assert res["migrated"] is False
    assert entorno["migraciones"] == []


async def test_un_404_de_hubspot_se_propaga(entorno, monkeypatch):
    async def _no_existe(url, payload, api_key):
        return _RespuestaHubSpot(status_code=404, text="not found")

    monkeypatch.setattr(panel, "_hubspot_patch", _no_existe)

    with pytest.raises(HTTPException) as exc:
        await panel.update_contact_phone(CONTACT_ID, phone=TELEFONO, x_api_key=API_KEY)

    assert exc.value.status_code == 404


async def test_el_telefono_se_normaliza_antes_de_guardarlo(entorno):
    """Que el panel no meta en HubSpot el formato crudo que escribió la asesora."""
    res = await panel.update_contact_phone(
        CONTACT_ID, phone="300 123 4567", x_api_key=API_KEY
    )
    assert res["phone"] == TELEFONO
    _, payload = entorno["patches"][0]
    assert payload["properties"]["phone"] == TELEFONO
