"""
Tests robustos — POST /contacts/{phone}/mark-read

Valida la solución permanente al 422 + badge persistente:
  1. Con advisor_id provisto → lo usa directamente, no hace get_meta
  2. Sin advisor_id + meta existe → infiere assigned_owner_id del meta
  3. Sin advisor_id + sin meta → degradación graceful (200 OK, no crash, no remove)
  4. Sin advisor_id → nunca 422 (parámetro ahora es Optional)
  5. Sin API Key → 401
  6. canal se propaga correctamente a remove_from_advisor_inbox

Run:
    python -m pytest tests/test_mark_read_robust.py -v
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import status as http_status

PHONE = "+573195652633"
ADVISOR_ID = "88251457"
CANAL = "whatsapp"


def _make_mock_sm(owner_id=None, has_meta=True):
    """
    Construye un mock de ConversationStateManager con comportamiento configurable.
    - owner_id: valor de meta.assigned_owner_id (None = meta sin owner)
    - has_meta: False → get_meta retorna None (contacto sin meta)
    """
    mock_sm = AsyncMock()

    if has_meta:
        mock_meta = MagicMock()
        mock_meta.assigned_owner_id = owner_id
        mock_sm.get_meta = AsyncMock(return_value=mock_meta)
    else:
        mock_sm.get_meta = AsyncMock(return_value=None)

    mock_sm.remove_from_advisor_inbox = AsyncMock()
    return mock_sm


# ============================================================================
# Grupo A — Comportamiento del endpoint
# ============================================================================

class TestMarkReadEndpoint:

    def test_con_advisor_id_retorna_200(self, api_client, api_headers):
        """advisor_id en query → 200 OK con status='ok'."""
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            MockSM.return_value = _make_mock_sm(owner_id=ADVISOR_ID)
            response = api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                params={"advisor_id": ADVISOR_ID, "canal": CANAL},
                headers=api_headers,
            )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["status"] == "ok"

    def test_sin_advisor_id_no_retorna_422(self, api_client, api_headers):
        """Sin advisor_id → 200 OK. Nunca más 422 por parámetro faltante."""
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            MockSM.return_value = _make_mock_sm(has_meta=False)
            response = api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                headers=api_headers,  # sin advisor_id intencionalmente
            )
        assert response.status_code != 422, "El endpoint NO debe retornar 422 cuando advisor_id no se provee"
        assert response.status_code == http_status.HTTP_200_OK

    def test_sin_api_key_retorna_401(self, api_client):
        """Sin X-API-Key → 401 Unauthorized."""
        response = api_client.post(
            f"/whatsapp/panel/contacts/{PHONE}/mark-read",
            params={"advisor_id": ADVISOR_ID},
        )
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED


# ============================================================================
# Grupo B — Lógica de inferencia de advisor_id
# ============================================================================

class TestMarkReadAdvisorInference:

    def test_con_advisor_id_no_llama_get_meta(self, api_client, api_headers):
        """Con advisor_id → get_meta NO se llama (evita round-trip Redis innecesario)."""
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            mock_sm = _make_mock_sm(owner_id=ADVISOR_ID)
            MockSM.return_value = mock_sm
            api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                params={"advisor_id": ADVISOR_ID},
                headers=api_headers,
            )
        mock_sm.get_meta.assert_not_called()

    def test_con_advisor_id_usa_el_provisto_en_remove(self, api_client, api_headers):
        """Con advisor_id → remove_from_advisor_inbox recibe exactamente ese advisor_id."""
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            mock_sm = _make_mock_sm()
            MockSM.return_value = mock_sm
            api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                params={"advisor_id": ADVISOR_ID},
                headers=api_headers,
            )
        mock_sm.remove_from_advisor_inbox.assert_called_once()
        args = mock_sm.remove_from_advisor_inbox.call_args[0]
        assert args[0] == ADVISOR_ID

    def test_sin_advisor_id_infiere_del_meta(self, api_client, api_headers):
        """Sin advisor_id + meta con owner → infiere y llama remove con owner del meta."""
        inferred_owner = "99887766"
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            mock_sm = _make_mock_sm(owner_id=inferred_owner, has_meta=True)
            MockSM.return_value = mock_sm
            api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                params={"canal": CANAL},
                headers=api_headers,
            )
        mock_sm.get_meta.assert_called_once()
        mock_sm.remove_from_advisor_inbox.assert_called_once()
        args = mock_sm.remove_from_advisor_inbox.call_args[0]
        assert args[0] == inferred_owner

    def test_sin_advisor_id_ni_meta_no_llama_remove(self, api_client, api_headers):
        """Sin advisor_id y sin meta → remove_from_advisor_inbox NO se llama (no hay a quién limpiar)."""
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            mock_sm = _make_mock_sm(has_meta=False)
            MockSM.return_value = mock_sm
            response = api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                headers=api_headers,
            )
        assert response.status_code == http_status.HTTP_200_OK
        mock_sm.remove_from_advisor_inbox.assert_not_called()

    def test_sin_advisor_id_meta_sin_owner_no_llama_remove(self, api_client, api_headers):
        """Sin advisor_id + meta existe pero sin assigned_owner_id → remove NO se llama."""
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            mock_sm = _make_mock_sm(owner_id=None, has_meta=True)  # meta existe pero sin owner
            MockSM.return_value = mock_sm
            response = api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                headers=api_headers,
            )
        assert response.status_code == http_status.HTTP_200_OK
        mock_sm.remove_from_advisor_inbox.assert_not_called()


# ============================================================================
# Grupo C — Propagación de canal
# ============================================================================

class TestMarkReadCanal:

    def test_canal_se_propaga_a_remove(self, api_client, api_headers):
        """canal en query → se pasa como tercer arg a remove_from_advisor_inbox."""
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            mock_sm = _make_mock_sm()
            MockSM.return_value = mock_sm
            api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                params={"advisor_id": ADVISOR_ID, "canal": "instagram"},
                headers=api_headers,
            )
        args = mock_sm.remove_from_advisor_inbox.call_args[0]
        assert args[2] == "instagram"

    def test_sin_canal_pasa_none_a_remove(self, api_client, api_headers):
        """Sin canal → remove_from_advisor_inbox recibe canal=None (limpia todos los canales)."""
        with patch("middleware.outbound_panel.ConversationStateManager") as MockSM:
            mock_sm = _make_mock_sm()
            MockSM.return_value = mock_sm
            api_client.post(
                f"/whatsapp/panel/contacts/{PHONE}/mark-read",
                params={"advisor_id": ADVISOR_ID},
                headers=api_headers,
            )
        args = mock_sm.remove_from_advisor_inbox.call_args[0]
        assert args[2] is None


if __name__ == "__main__":
    import pytest as _pt
    _pt.main([__file__, "-v", "--tb=short"])
