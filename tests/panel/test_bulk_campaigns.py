"""
Tests para el módulo de mensajes masivos por embudo (bulk campaigns).

Cubre:
- Validaciones de seguridad (stages excluidos, aislamiento per-asesora, advisor_id)
- Validación de template_sid (solo SIDs permitidos en BULK_ALLOWED_TEMPLATES)
- Variables literales escritas por la asesora + auto-fill {nombre} per-contacto
- Worker (procesamiento, dedup, throttle, idempotencia)
- Flujos críticos (NO Timeline HubSpot, NO ZSET, BOT_ACTIVE preservado)
- Auto-promoción "No Responde" → "En conversación"

Ejecutar con:
    python -m pytest tests/panel/test_bulk_campaigns.py -v
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("HUBSPOT_API_KEY", "test_key")
os.environ.setdefault("ADMIN_API_KEY", "test_admin_key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")


# SIDs permitidos para envío masivo (deben coincidir con BULK_ALLOWED_TEMPLATES en outbound_panel.py)
SID_AUN_BUSQUEDA = "HX550a2475d09a5fb3b5410e6d36eadf3f"
SID_SEGUIMIENTO = "HX287a1c005459a6ceaec90a35108330c3"
SID_NOT_ALLOWED = "HXdeadbeef00000000000000000000000"


def _api_key_env():
    return {"ADMIN_API_KEY": "test_admin_key", "PANEL_API_KEY": "test_admin_key"}


def _hubspot_search_result(contacts):
    return {"results": contacts, "next_after": None, "total": len(contacts)}


def _redis_mock_default():
    """Mock Redis con get=None y pipeline vacío — para preview/create tests."""
    rm = AsyncMock()
    rm.get = AsyncMock(return_value=None)
    rm.set = AsyncMock()
    rm.delete = AsyncMock()
    pipe_mock = MagicMock()
    pipe_mock.get = MagicMock()
    pipe_mock.execute = AsyncMock(return_value=[])
    rm.pipeline = MagicMock(return_value=pipe_mock)
    return rm


# ═══════════════════════════════════════════════════════════════════════════
# A) VALIDACIONES DE SEGURIDAD
# ═══════════════════════════════════════════════════════════════════════════

class TestSecurityValidations:

    @pytest.mark.parametrize("excluded_stage", [
        "1326623075",  # En Conversación (stage unificado)
        "evangelist",  # Cerrado perdido
        "1326632628",  # Otros Municipios
        "1326632209",  # Otras Areas
    ])
    def test_excluded_stage_rejected_400(self, excluded_stage):
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        with patch.dict(os.environ, _api_key_env()):
            resp = client.post(
                "/whatsapp/panel/bulk-campaigns",
                json={
                    "stage_id": excluded_stage,
                    "advisor_id": "89096378",
                    "template_sid": SID_AUN_BUSQUEDA,
                },
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code == 400, resp.text
        assert "no permite envío masivo" in resp.json().get("detail", "")

    def test_invalid_stage_rejected(self):
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        with patch.dict(os.environ, _api_key_env()):
            resp = client.post(
                "/whatsapp/panel/bulk-campaigns",
                json={"stage_id": "no_existe", "advisor_id": "89096378", "template_sid": SID_AUN_BUSQUEDA},
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code == 400

    def test_template_sid_not_allowed_rejected(self):
        """SID HX que NO está en BULK_ALLOWED_TEMPLATES debe rechazarse con 400."""
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        with patch.dict(os.environ, _api_key_env()):
            resp = client.post(
                "/whatsapp/panel/bulk-campaigns",
                json={
                    "stage_id": "subscriber",
                    "advisor_id": "89096378",
                    "template_sid": SID_NOT_ALLOWED,
                },
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code == 400
        assert "no permitido para envío masivo" in resp.json().get("detail", "")

    def test_template_sid_missing_rejected(self):
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        with patch.dict(os.environ, _api_key_env()):
            resp = client.post(
                "/whatsapp/panel/bulk-campaigns",
                json={"stage_id": "subscriber", "advisor_id": "89096378", "template_sid": ""},
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code in (400, 422)

    def test_seguimiento_personalizado_requires_message_var(self):
        """seguimiento_personalizado tiene var "2" no auto-fill: debe rechazarse si falta."""
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        with patch.dict(os.environ, _api_key_env()):
            resp = client.post(
                "/whatsapp/panel/bulk-campaigns",
                json={
                    "stage_id": "subscriber",
                    "advisor_id": "89096378",
                    "template_sid": SID_SEGUIMIENTO,
                    # template_variables omitido a propósito
                },
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code == 400
        assert "Faltan valores" in resp.json().get("detail", "")

    def test_aun_busqueda_does_NOT_require_extra_vars(self):
        """aun_en_busqueda solo tiene var "1" auto-fill — no requiere texto del frontend."""
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        contacts_search = _hubspot_search_result([
            {"id": "1", "properties": {"firstname": "Juan", "phone": "+57300", "whatsapp_id": "+57300", "hubspot_owner_id": "89096378"}},
        ])
        with patch.dict(os.environ, _api_key_env()), \
             patch("middleware.outbound_panel._hs_singleton.search_contacts_by_lifecyclestage_and_owner",
                   new=AsyncMock(return_value=contacts_search)), \
             patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=_redis_mock_default())), \
             patch("middleware.outbound_panel._resolve_advisor_name",
                   new=AsyncMock(return_value="Jubeny")), \
             patch("database.mongodb_client.MongoDBManager.create_bulk_campaign",
                   new=AsyncMock(return_value="abc123")):
            resp = client.post(
                "/whatsapp/panel/bulk-campaigns",
                json={
                    "stage_id": "subscriber",
                    "advisor_id": "89096378",
                    "template_sid": SID_AUN_BUSQUEDA,
                    # Sin template_variables: válido porque var "1" es auto_fill
                },
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code == 200, resp.text

    def test_advisor_can_only_see_own_campaign(self):
        """GET /bulk-campaigns/{id} con advisor_id distinto al creador → 403."""
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        fake_doc = {
            "_id": "abc123",
            "creator_advisor_id": "89096378",  # Jubeny
            "status": "in_progress",
            "total_contacts": 10, "sent_count": 3, "failed_count": 0,
            "stage_name": "Reubicados", "template_id": "x",
            "created_at": None, "completed_at": None,
        }
        with patch.dict(os.environ, _api_key_env()), \
             patch("database.mongodb_client.MongoDBManager.get_bulk_campaign",
                   new=AsyncMock(return_value=fake_doc)):
            resp = client.get(
                "/whatsapp/panel/bulk-campaigns/abc123?advisor_id=89096380",  # Luisa
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# B) PREVIEW + CREACIÓN (con template_sid + template_variables)
# ═══════════════════════════════════════════════════════════════════════════

class TestPreviewAndCreate:

    def test_preview_filters_by_owner_id(self):
        """HubSpot search debe llamarse con owner_id=advisor_id."""
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        mock_search = AsyncMock(return_value=_hubspot_search_result([
            {"id": "1", "properties": {"firstname": "Juan", "phone": "+57300", "whatsapp_id": "+57300", "hubspot_owner_id": "89096378"}},
        ]))
        with patch.dict(os.environ, _api_key_env()), \
             patch("middleware.outbound_panel._hs_singleton.search_contacts_by_lifecyclestage_and_owner",
                   new=mock_search), \
             patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=_redis_mock_default())):
            resp = client.post(
                "/whatsapp/panel/bulk-campaigns/preview",
                json={
                    "stage_id": "subscriber",
                    "advisor_id": "89096378",
                    "template_sid": SID_AUN_BUSQUEDA,
                },
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code == 200, resp.text
        call_kwargs = mock_search.call_args.kwargs
        assert call_kwargs.get("owner_id") == "89096378"
        assert call_kwargs.get("stage_id") == "subscriber"

    def test_create_persists_template_sid_and_variables(self):
        """Mongo create_bulk_campaign debe recibir template_sid + literal vars + auto_fill_keys."""
        from fastapi.testclient import TestClient
        from app import app
        client = TestClient(app)
        mock_create = AsyncMock(return_value="abc123")
        contacts_search = _hubspot_search_result([
            {"id": "1", "properties": {"firstname": "Juan", "phone": "+57300", "whatsapp_id": "+57300", "hubspot_owner_id": "89096378"}},
            {"id": "2", "properties": {"firstname": "Maria", "phone": "+57301", "whatsapp_id": "+57301", "hubspot_owner_id": "89096378"}},
        ])
        with patch.dict(os.environ, _api_key_env()), \
             patch("middleware.outbound_panel._hs_singleton.search_contacts_by_lifecyclestage_and_owner",
                   new=AsyncMock(return_value=contacts_search)), \
             patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=_redis_mock_default())), \
             patch("middleware.outbound_panel._resolve_advisor_name",
                   new=AsyncMock(return_value="Jubeny")), \
             patch("database.mongodb_client.MongoDBManager.create_bulk_campaign",
                   new=mock_create):
            resp = client.post(
                "/whatsapp/panel/bulk-campaigns",
                json={
                    "stage_id": "subscriber",
                    "advisor_id": "89096378",
                    "template_sid": SID_SEGUIMIENTO,
                    "template_variables": {"2": "te escribo para preguntar"},
                },
                headers={"X-API-Key": "test_admin_key"},
            )
        assert resp.status_code == 200, resp.text
        # Validar args pasados a Mongo
        ck = mock_create.call_args.kwargs
        assert ck["template_content_sid"] == SID_SEGUIMIENTO
        assert ck["template_id"] == "seguimiento_personalizado"
        assert ck["template_variables"] == {"2": "te escribo para preguntar"}
        assert ck["auto_fill_keys"] == ["1"]


# ═══════════════════════════════════════════════════════════════════════════
# C) RESOLUCIÓN DE VARIABLES (auto-fill + literales mezcladas)
# ═══════════════════════════════════════════════════════════════════════════

class TestTemplateVariableResolution:

    def test_aun_busqueda_only_autofills_nombre(self):
        from middleware.outbound_panel import _resolve_bulk_template_variables, BULK_ALLOWED_TEMPLATES
        meta = BULK_ALLOWED_TEMPLATES[SID_AUN_BUSQUEDA]
        cv = _resolve_bulk_template_variables(
            meta,
            {"firstname": "Juan"},
            {},  # sin literales
        )
        assert cv == {"1": "Juan"}

    @pytest.mark.parametrize("invalid_name", [
        None,                    # sin valor
        "",                      # vacío
        "   ",                   # solo whitespace
        "none",                  # marcador
        "None",                  # capitalizado
        "NONE NONE",             # típico HubSpot mal poblado
        "none none",             # idem case insensitive
        "null",
        "n/a",
        "sin nombre",
        "Sin Nombre",
        "---",
        "+573001234567",         # número de teléfono como nombre
        "3001234567",            # solo dígitos
        "57 300 123 4567",       # número con espacios
        "...",                   # solo puntos
    ])
    def test_autofill_fallback_buen_dia_for_invalid_names(self, invalid_name):
        from middleware.outbound_panel import (
            _resolve_bulk_template_variables, BULK_ALLOWED_TEMPLATES, BULK_NAME_FALLBACK
        )
        meta = BULK_ALLOWED_TEMPLATES[SID_AUN_BUSQUEDA]
        cv = _resolve_bulk_template_variables(
            meta,
            {"firstname": invalid_name},
            {},
        )
        assert cv == {"1": BULK_NAME_FALLBACK}, f"firstname={invalid_name!r} debería dar fallback"

    @pytest.mark.parametrize("valid_name,expected", [
        ("Juan", "Juan"),
        ("MARIA", "MARIA"),                # Mayúsculas se respetan (no normalizamos)
        ("Juan Carlos", "Juan Carlos"),    # Multi-palabra
        ("Áñelo", "Áñelo"),                # Acentos / unicode
        ("María-Fernanda", "María-Fernanda"),
        ("José 2", "José 2"),              # Mezcla letra+número se acepta
        ("  Juan  ", "Juan"),              # Trim de whitespace
    ])
    def test_autofill_keeps_real_names_intact(self, valid_name, expected):
        from middleware.outbound_panel import _resolve_bulk_template_variables, BULK_ALLOWED_TEMPLATES
        meta = BULK_ALLOWED_TEMPLATES[SID_AUN_BUSQUEDA]
        cv = _resolve_bulk_template_variables(
            meta,
            {"firstname": valid_name},
            {},
        )
        assert cv == {"1": expected}

    def test_seguimiento_mixes_autofill_and_literal(self):
        """seguimiento_personalizado: var 1 auto-fill, var 2 texto literal."""
        from middleware.outbound_panel import _resolve_bulk_template_variables, BULK_ALLOWED_TEMPLATES
        meta = BULK_ALLOWED_TEMPLATES[SID_SEGUIMIENTO]
        cv = _resolve_bulk_template_variables(
            meta,
            {"firstname": "Juan"},
            {"2": "te escribo para preguntar"},
        )
        assert cv == {"1": "Juan", "2": "te escribo para preguntar"}

    def test_seguimiento_per_contact_personalization(self):
        """Mismo literal text aplicado a 2 contactos diferentes — var 1 cambia, var 2 igual."""
        from middleware.outbound_panel import _resolve_bulk_template_variables, BULK_ALLOWED_TEMPLATES
        meta = BULK_ALLOWED_TEMPLATES[SID_SEGUIMIENTO]
        literal = {"2": "mensaje personalizado igual para todos"}
        cv_juan = _resolve_bulk_template_variables(meta, {"firstname": "Juan"}, literal)
        cv_maria = _resolve_bulk_template_variables(meta, {"firstname": "Maria"}, literal)
        assert cv_juan["1"] == "Juan" and cv_maria["1"] == "Maria"
        assert cv_juan["2"] == cv_maria["2"] == "mensaje personalizado igual para todos"


# ═══════════════════════════════════════════════════════════════════════════
# D) ENVÍO BULK — flujos críticos (NO Timeline, NO ZSET, NO panel)
# ═══════════════════════════════════════════════════════════════════════════

class TestBulkSendIsolation:

    @pytest.mark.asyncio
    async def test_send_does_NOT_call_timeline_logger(self):
        """Crítico: bulk send NO llama a HubSpot Timeline (evita 429s)."""
        from middleware.outbound_panel import _send_bulk_template_message
        with patch("middleware.outbound_panel.twilio_client.send_whatsapp_message",
                   new=AsyncMock(return_value={"status": "success", "message_sid": "SM1"})), \
             patch("middleware.outbound_panel.get_mongo_manager") as mock_mongo, \
             patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=AsyncMock(set=AsyncMock()))), \
             patch("integrations.hubspot.timeline_logger.TimelineLogger.log_advisor_message",
                   new=AsyncMock()) as mock_timeline:
            mock_mongo.return_value.save_message = AsyncMock(return_value="msg_id")
            await _send_bulk_template_message(
                phone="+573001234567",
                content_sid=SID_AUN_BUSQUEDA,
                content_variables={"1": "Juan"},
                contact_id="123",
                campaign_id="camp1",
                campaign_stage_id="subscriber",
            )
            mock_timeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_does_NOT_call_state_manager_update_activity(self):
        from middleware.outbound_panel import _send_bulk_template_message
        sm_mock = MagicMock()
        sm_mock.update_activity = AsyncMock()
        with patch("middleware.outbound_panel.twilio_client.send_whatsapp_message",
                   new=AsyncMock(return_value={"status": "success", "message_sid": "SM1"})), \
             patch("middleware.outbound_panel.get_mongo_manager") as mock_mongo, \
             patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=AsyncMock(set=AsyncMock()))), \
             patch("middleware.outbound_panel._get_state_manager", return_value=sm_mock):
            mock_mongo.return_value.save_message = AsyncMock(return_value="msg_id")
            await _send_bulk_template_message(
                phone="+573001234567",
                content_sid=SID_AUN_BUSQUEDA,
                content_variables={"1": "Juan"},
                contact_id="123",
                campaign_id="camp1",
                campaign_stage_id="subscriber",
            )
            sm_mock.update_activity.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_to_no_responde_sets_redis_flag(self):
        from middleware.outbound_panel import _send_bulk_template_message, BULK_NO_RESPONDE_FLAG_PREFIX
        redis_mock = AsyncMock()
        redis_mock.set = AsyncMock()
        with patch("middleware.outbound_panel.twilio_client.send_whatsapp_message",
                   new=AsyncMock(return_value={"status": "success", "message_sid": "SM1"})), \
             patch("middleware.outbound_panel.get_mongo_manager") as mock_mongo, \
             patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=redis_mock)):
            mock_mongo.return_value.save_message = AsyncMock(return_value="msg_id")
            await _send_bulk_template_message(
                phone="+573001234567",
                content_sid=SID_AUN_BUSQUEDA,
                content_variables={"1": "Juan"},
                contact_id="123",
                campaign_id="camp1",
                campaign_stage_id="other",
            )
        flag_calls = [c for c in redis_mock.set.await_args_list
                      if c.args and BULK_NO_RESPONDE_FLAG_PREFIX in str(c.args[0])]
        assert len(flag_calls) >= 1

    @pytest.mark.asyncio
    async def test_send_to_other_stages_does_NOT_set_flag(self):
        from middleware.outbound_panel import _send_bulk_template_message, BULK_NO_RESPONDE_FLAG_PREFIX
        redis_mock = AsyncMock()
        redis_mock.set = AsyncMock()
        with patch("middleware.outbound_panel.twilio_client.send_whatsapp_message",
                   new=AsyncMock(return_value={"status": "success", "message_sid": "SM1"})), \
             patch("middleware.outbound_panel.get_mongo_manager") as mock_mongo, \
             patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=redis_mock)):
            mock_mongo.return_value.save_message = AsyncMock(return_value="msg_id")
            await _send_bulk_template_message(
                phone="+573001234567",
                content_sid=SID_AUN_BUSQUEDA,
                content_variables={"1": "Juan"},
                contact_id="123",
                campaign_id="camp1",
                campaign_stage_id="subscriber",
            )
        flag_calls = [c for c in redis_mock.set.await_args_list
                      if c.args and BULK_NO_RESPONDE_FLAG_PREFIX in str(c.args[0])]
        assert len(flag_calls) == 0


# ═══════════════════════════════════════════════════════════════════════════
# E) AUTO-PROMOCIÓN "No Responde" → "En conversación"
# ═══════════════════════════════════════════════════════════════════════════

class TestNoRespondePromotion:

    @pytest.mark.asyncio
    async def test_promote_skips_if_no_flag(self):
        from middleware.outbound_panel import _promote_no_responde_to_en_conversacion
        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value=None)
        with patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=redis_mock)), \
             patch("middleware.outbound_panel._get_contact_lifecyclestage",
                   new=AsyncMock()) as mock_stage:
            ok = await _promote_no_responde_to_en_conversacion("123", "+573001234567")
        assert ok is False
        mock_stage.assert_not_called()

    @pytest.mark.asyncio
    async def test_promote_skips_if_stage_not_other(self):
        from middleware.outbound_panel import _promote_no_responde_to_en_conversacion
        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value="camp1")
        redis_mock.delete = AsyncMock()
        with patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=redis_mock)), \
             patch("middleware.outbound_panel._get_contact_lifecyclestage",
                   new=AsyncMock(return_value="customer")):
            ok = await _promote_no_responde_to_en_conversacion("123", "+573001234567")
        assert ok is False
        redis_mock.delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_promote_succeeds_if_other_stage_and_flag(self):
        from middleware.outbound_panel import _promote_no_responde_to_en_conversacion
        redis_mock = AsyncMock()
        redis_mock.get = AsyncMock(return_value="camp1")
        redis_mock.delete = AsyncMock()
        client_mock = MagicMock()
        client_mock.patch = AsyncMock(return_value=MagicMock(status_code=200, text="{}"))
        with patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=redis_mock)), \
             patch("middleware.outbound_panel._get_contact_lifecyclestage",
                   new=AsyncMock(return_value="other")), \
             patch("middleware.outbound_panel.get_httpx_client", return_value=client_mock), \
             patch("middleware.outbound_panel._invalidate_contact_stage_cache",
                   new=AsyncMock()):
            ok = await _promote_no_responde_to_en_conversacion("123", "+573001234567")
        assert ok is True
        client_mock.patch.assert_awaited_once()
        call_args = client_mock.patch.await_args
        assert call_args.kwargs["json"]["properties"]["lifecyclestage"] == "1326623075"
        redis_mock.delete.assert_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# F) WORKER — Idempotencia + lock
# ═══════════════════════════════════════════════════════════════════════════

class TestWorkerProcessor:

    @pytest.mark.asyncio
    async def test_processor_skips_if_lock_taken(self):
        from middleware.outbound_panel import _process_bulk_campaign_tick
        redis_mock = AsyncMock()
        redis_mock.set = AsyncMock(return_value=False)
        redis_mock.delete = AsyncMock()
        with patch("middleware.outbound_panel._get_redis_client",
                   new=AsyncMock(return_value=redis_mock)), \
             patch("middleware.outbound_panel.get_mongo_manager") as mock_mongo:
            mock_mongo.return_value.get_active_bulk_campaigns = AsyncMock(return_value=[])
            await _process_bulk_campaign_tick()
            mock_mongo.return_value.get_active_bulk_campaigns.assert_not_called()
