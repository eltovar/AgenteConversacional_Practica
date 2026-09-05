import sys
import types

import pytest

from utils.twilio_client import TwilioClient


@pytest.mark.asyncio
async def test_bsuid_no_pasa_por_phone_normalizer(monkeypatch):
    client = TwilioClient()

    fake_phone_module = types.SimpleNamespace()

    def _boom(_value):
        raise AssertionError("PhoneNormalizer no debe ejecutarse para BSUID")

    fake_phone_module.normalize_phone = _boom
    monkeypatch.setitem(sys.modules, "middleware.phone_normalizer", fake_phone_module)

    conv_sid, service_sid = await client._resolve_conv_sid_for_phone(
        "whatsapp:CO.899759302823042"
    )

    assert conv_sid is None
    assert service_sid is None


@pytest.mark.asyncio
async def test_bsuid_alfanumerico_no_pasa_por_lookup_telefonico(monkeypatch):
    client = TwilioClient()

    fake_phone_module = types.SimpleNamespace()

    def _boom(_value):
        raise AssertionError("normalize_phone no debe ejecutarse para BSUID alfanumérico")

    fake_phone_module.normalize_phone = _boom
    monkeypatch.setitem(sys.modules, "middleware.phone_normalizer", fake_phone_module)

    conv_sid, service_sid = await client._resolve_conv_sid_for_phone(
        "whatsapp:US.ABC123XYZ"
    )

    assert conv_sid is None
    assert service_sid is None
