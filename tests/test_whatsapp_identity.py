import json

import pytest

from middleware.whatsapp_identity import (
    WhatsAppIdentityType,
    is_whatsapp_bsuid,
    resolve_whatsapp_identity,
)


@pytest.mark.parametrize(
    "raw_from",
    [
        "whatsapp:+573001234567",
        "whatsapp:+14155552671",
        "+14155552671",
        "+573001234567",
        "573001234567",
        "3001234567",
    ],
)
def test_resuelve_telefono(raw_from):
    identity = resolve_whatsapp_identity({"From": raw_from})

    assert identity.identity_type == WhatsAppIdentityType.PHONE
    assert identity.phone == raw_from
    assert identity.bsuid is None


@pytest.mark.parametrize(
    "bsuid",
    [
        "whatsapp:CO.899759302823042",
        "whatsapp:US.ABC123XYZ",
        "whatsapp:BR.1A2B3C4D5E6F7G8H9I0J",
        "whatsapp:pe.a1b2c3d4",
    ],
)
def test_resuelve_bsuid_oficial_twilio(bsuid):
    identity = resolve_whatsapp_identity({"From": bsuid})

    assert identity.identity_type == WhatsAppIdentityType.BSUID
    assert identity.bsuid == bsuid
    assert identity.phone is None
    assert is_whatsapp_bsuid(bsuid) is True


def test_resuelve_bsuid_desde_external_user_id_si_no_hay_from():
    identity = resolve_whatsapp_identity(
        {
            "From": "",
            "ExternalUserId": "whatsapp:CO.899759302823042",
        }
    )

    assert identity.identity_type == WhatsAppIdentityType.BSUID
    assert identity.bsuid == "whatsapp:CO.899759302823042"
    assert identity.external_user_id == "whatsapp:CO.899759302823042"


def test_external_user_id_bsuid_no_queda_oculto_por_from_desconocido():
    identity = resolve_whatsapp_identity(
        {
            "From": "identidad-no-telefonica",
            "ExternalUserId": "whatsapp:US.ABC123XYZ",
        }
    )

    assert identity.identity_type == WhatsAppIdentityType.BSUID
    assert identity.bsuid == "whatsapp:US.ABC123XYZ"
    assert identity.phone is None


def test_telefono_en_from_tiene_prioridad_de_routing_sobre_external_user_id_bsuid():
    identity = resolve_whatsapp_identity(
        {
            "From": "whatsapp:+573001234567",
            "ExternalUserId": "whatsapp:CO.899759302823042",
            "Username": "juan_rodriguez_18",
        }
    )

    assert identity.identity_type == WhatsAppIdentityType.PHONE
    assert identity.phone == "whatsapp:+573001234567"
    assert identity.external_user_id == "whatsapp:CO.899759302823042"


@pytest.mark.parametrize(
    ("payload", "expected_username"),
    [
        ({"From": "whatsapp:cliente.usuario", "Username": "cliente.usuario"}, "cliente.usuario"),
        ({"From": "", "Username": "cliente_usuario"}, "cliente_usuario"),
        ({"From": "", "Username": "@juan_rodriguez_18"}, "@juan_rodriguez_18"),
    ],
)
def test_username_es_informativo_no_routing(payload, expected_username):
    identity = resolve_whatsapp_identity(payload)

    assert identity.identity_type == WhatsAppIdentityType.USERNAME
    assert identity.username == expected_username
    assert identity.phone is None
    assert identity.bsuid is None


def test_raw_from_con_forma_username_sin_campo_username_es_unknown():
    identity = resolve_whatsapp_identity({"From": "whatsapp:cliente.usuario"})

    assert identity.identity_type == WhatsAppIdentityType.UNKNOWN
    assert identity.phone is None
    assert identity.bsuid is None


def test_resuelve_bsuid_con_metadata_sin_usar_waid_como_phone():
    channel_metadata = json.dumps(
        {
            "data": {
                "context": {
                    "WaId": "573001234567",
                    "ProfileName": "Cliente Anonimo",
                    "Username": "usuario_anonimo",
                }
            }
        }
    )

    identity = resolve_whatsapp_identity(
        {"From": "whatsapp:CO.899759302823042", "ChannelMetadata": channel_metadata}
    )

    assert identity.identity_type == WhatsAppIdentityType.BSUID
    assert identity.phone is None
    assert identity.wa_id == "573001234567"
    assert identity.username == "usuario_anonimo"
    assert identity.profile_name_present is True


@pytest.mark.parametrize(
    "raw_from",
    [
        None,
        "",
        "whatsapp:",
        "texto libre",
        "whatsapp:C.12345678",
        "whatsapp:COL.12345678",
        "whatsapp:CO.ABC-123",
    ],
)
def test_resuelve_unknown(raw_from):
    identity = resolve_whatsapp_identity({"From": raw_from})

    assert identity.identity_type == WhatsAppIdentityType.UNKNOWN
    assert identity.phone is None
    assert identity.bsuid is None
