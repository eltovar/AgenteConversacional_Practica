# tests/middleware/test_conversation_state.py
"""
Tests para el gestor de estado de conversaciones.

Ejecutar con: pytest tests/middleware/test_conversation_state.py -v -s
"""

import pytest
import sys
import os

# Agregar el directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from dotenv import load_dotenv
load_dotenv()

# Obtener URL de Redis (priorizar PUBLIC para desarrollo local)
REDIS_URL = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")

skip_no_redis = pytest.mark.skipif(
    not REDIS_URL,
    reason="REDIS_URL/REDIS_PUBLIC_URL no configurada"
)


class TestConversationState:
    """Tests para el gestor de estado de conversaciones."""

    @pytest.fixture
    def state_manager(self):
        """Instancia del gestor de estado."""
        from middleware.conversation_state import ConversationStateManager
        return ConversationStateManager(REDIS_URL)

    @pytest.fixture
    def test_phone(self):
        """Número de teléfono para tests."""
        return "+573009999999_test"

    @skip_no_redis
    @pytest.mark.asyncio
    async def test_default_state_is_bot_active(self, state_manager, test_phone):
        """El estado por defecto debe ser BOT_ACTIVE."""
        from middleware.conversation_state import ConversationStatus

        # Limpiar estado previo
        await state_manager.delete_conversation(test_phone)

        status = await state_manager.get_status(test_phone)
        assert status == ConversationStatus.BOT_ACTIVE

    @skip_no_redis
    @pytest.mark.asyncio
    async def test_can_activate_human(self, state_manager, test_phone):
        """Puede cambiar a HUMAN_ACTIVE."""
        from middleware.conversation_state import ConversationStatus

        await state_manager.activate_human(test_phone, owner_id="test_owner")

        status = await state_manager.get_status(test_phone)
        assert status == ConversationStatus.HUMAN_ACTIVE

        # Verificar metadata
        meta = await state_manager.get_meta(test_phone)
        assert meta is not None
        assert meta.assigned_owner_id == "test_owner"

        # Limpiar
        await state_manager.delete_conversation(test_phone)

    @skip_no_redis
    @pytest.mark.asyncio
    async def test_can_reactivate_bot(self, state_manager, test_phone):
        """Puede volver a BOT_ACTIVE después de HUMAN_ACTIVE."""
        from middleware.conversation_state import ConversationStatus

        # Activar humano
        await state_manager.activate_human(test_phone)
        assert await state_manager.is_human_active(test_phone)

        # Reactivar bot
        await state_manager.activate_bot(test_phone)
        assert await state_manager.is_bot_active(test_phone)

        # Limpiar
        await state_manager.delete_conversation(test_phone)

    @skip_no_redis
    @pytest.mark.asyncio
    async def test_handoff_request(self, state_manager, test_phone):
        """Puede solicitar handoff."""
        from middleware.conversation_state import ConversationStatus

        await state_manager.request_handoff(test_phone, reason="Test handoff")

        status = await state_manager.get_status(test_phone)
        assert status == ConversationStatus.PENDING_HANDOFF

        meta = await state_manager.get_meta(test_phone)
        assert meta.handoff_reason == "Test handoff"

        # Limpiar
        await state_manager.delete_conversation(test_phone)

    @skip_no_redis
    @pytest.mark.asyncio
    async def test_update_activity(self, state_manager, test_phone):
        """Puede actualizar la actividad."""
        # Limpiar estado previo
        await state_manager.delete_conversation(test_phone)

        # Actualizar actividad
        await state_manager.update_activity(test_phone)

        # Verificar que se creó metadata
        meta = await state_manager.get_meta(test_phone)
        assert meta is not None
        assert meta.message_count == 1
        assert meta.last_activity is not None

        # Actualizar de nuevo
        await state_manager.update_activity(test_phone)
        meta = await state_manager.get_meta(test_phone)
        assert meta.message_count == 2

        # Limpiar
        await state_manager.delete_conversation(test_phone)


# Ejecutar tests si se ejecuta directamente
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])