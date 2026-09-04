import json

import pytest

from middleware.whatsapp_identity import WhatsAppIdentityType, resolve_whatsapp_identity


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


def test_resuelve_bsuid_desde_from():
    identity = resolve_whatsapp_identity({"From": "whatsapp:CO.899759302823042"})

    assert identity.identity_type == WhatsAppIdentityType.BSUID
    assert identity.bsuid == "whatsapp:CO.899759302823042"
    assert identity.phone is None


def test_resuelve_bsuid_desde_external_user_id_si_no_hay_from():
    identity = resolve_whatsapp_identity({
        "From": "",
        "ExternalUserId": "whatsapp:CO.899759302823042",
    })

    assert identity.identity_type == WhatsAppIdentityType.BSUID
    assert identity.bsuid == "whatsapp:CO.899759302823042"
    assert identity.external_user_id == "whatsapp:CO.899759302823042"


@pytest.mark.parametrize(
    ("payload", "expected_username"),
    [
        ({"From": "whatsapp:cliente.usuario"}, "cliente.usuario"),
        ({"From": "", "Username": "cliente_usuario"}, "cliente_usuario"),
    ],
)
def test_resuelve_username_como_identidad_no_telefonica(payload, expected_username):
    identity = resolve_whatsapp_identity(payload)

    assert identity.identity_type == WhatsAppIdentityType.USERNAME
    assert identity.username == expected_username
    assert identity.phone is None


def test_resuelve_bsuid_con_metadata_sin_usar_waid_como_phone():
    channel_metadata = json.dumps({
        "data": {
            "context": {
                "WaId": "573001234567",
                "ProfileName": "Cliente Anonimo",
                "Username": "usuario_anonimo",
            }
        }
    })

    identity = resolve_whatsapp_identity(
        {"From": "whatsapp:CO.899759302823042", "ChannelMetadata": channel_metadata}
    )

    assert identity.identity_type == WhatsAppIdentityType.BSUID
    assert identity.phone is None
    assert identity.wa_id == "573001234567"
    assert identity.username == "usuario_anonimo"
    assert identity.profile_name_present is True


@pytest.mark.parametrize("raw_from", [None, "", "whatsapp:", "texto libre"])
def test_resuelve_unknown(raw_from):
    identity = resolve_whatsapp_identity({"From": raw_from})

    assert identity.identity_type == WhatsAppIdentityType.UNKNOWN
    assert identity.phone is None
    assert identity.bsuid is None
