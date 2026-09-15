import pytest

from middleware.whatsapp_identity import is_bsuid_identity_key
from middleware.outbound_panel import PanelTarget, _resolve_panel_target


def test_bsuid_identity_key_reconoce_solo_llave_interna():
    assert is_bsuid_identity_key("bsuid_93e2111acf24633590779924") is True
    assert is_bsuid_identity_key("whatsapp:CO.899759302823042") is False
    assert is_bsuid_identity_key("+573001234567") is False


def test_panel_target_bsuid_no_pasa_por_phone_normalizer(monkeypatch):
    def _boom(_value):
        raise AssertionError("PhoneNormalizer no debe ejecutarse para una llave BSUID")

    monkeypatch.setattr("middleware.outbound_panel.PhoneNormalizer", _boom)

    target = _resolve_panel_target("bsuid_93e2111acf24633590779924")

    assert target == PanelTarget(
        key="bsuid_93e2111acf24633590779924",
        is_bsuid=True,
        error=None,
    )


def test_panel_target_telefono_mantiene_flujo_actual():
    target = _resolve_panel_target("3001234567")

    assert target.key == "+573001234567"
    assert target.is_bsuid is False
    assert target.error is None


@pytest.mark.asyncio
async def test_panel_outbound_bsuid_resuelve_routing_address(monkeypatch):
    from middleware.outbound_panel import _resolve_outbound_address

    class FakeStateManager:
        async def get_meta(self, phone, canal):
            assert phone == "bsuid_93e2111acf24633590779924"
            assert canal == "whatsapp"
            return type("Meta", (), {"routing_address": "whatsapp:CO.899759302823042"})()

    monkeypatch.setattr("middleware.outbound_panel._get_state_manager", lambda: FakeStateManager())

    outbound_to, error = await _resolve_outbound_address(
        PanelTarget(key="bsuid_93e2111acf24633590779924", is_bsuid=True),
        "whatsapp",
    )

    assert outbound_to == "whatsapp:CO.899759302823042"
    assert error is None


@pytest.mark.asyncio
async def test_panel_outbound_bsuid_falla_cerrado_sin_routing(monkeypatch):
    from middleware.outbound_panel import _resolve_outbound_address

    class FakeStateManager:
        async def get_meta(self, phone, canal):
            return type("Meta", (), {"routing_address": None})()

    monkeypatch.setattr("middleware.outbound_panel._get_state_manager", lambda: FakeStateManager())

    outbound_to, error = await _resolve_outbound_address(
        PanelTarget(key="bsuid_93e2111acf24633590779924", is_bsuid=True),
        "whatsapp",
    )

    assert outbound_to is None
    assert "routing" in error
