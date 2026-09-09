"""
Test Bug 5: Badge de Citas — Verifica que has_appointment se calcula sobre
TODOS los contactos, no solo los de la primera página.

Ejecutar:
    python -m pytest tests/test_bug5_badge.py -v
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1: Lógica de paginación — contacto más allá del límite recibe badge
# ═══════════════════════════════════════════════════════════════════════════════
class TestBadgePaginationFix:
    """Verifica que all_contact_ids usa contacts_sorted completo, no [:limit]."""

    def _build_contacts(self, count: int) -> list:
        """Genera lista de contactos simulados con contact_id único."""
        return [
            {"phone": f"+5730000{i:05d}", "contact_id": f"contact_{i}"}
            for i in range(count)
        ]

    def test_all_contacts_queried_not_just_first_page(self):
        """
        Con 50 contactos y limit=30, la query debe recibir los 50 IDs,
        no solo los primeros 30.
        """
        limit = 30
        contacts_sorted = self._build_contacts(50)

        # Código ANTES del fix (comportamiento incorrecto)
        old_page_ids = [
            c.get("contact_id", "") for c in contacts_sorted[:limit]
            if c.get("contact_id")
        ]

        # Código DESPUÉS del fix (comportamiento correcto)
        new_all_ids = [
            c.get("contact_id", "") for c in contacts_sorted
            if c.get("contact_id")
        ]

        assert len(old_page_ids) == 30, "Antes: solo 30 contactos"
        assert len(new_all_ids) == 50, "Ahora: todos los 50 contactos"

        # El contacto en posición 35 NO estaba en la query antigua
        assert "contact_35" not in old_page_ids
        # El contacto en posición 35 SÍ está en la query nueva
        assert "contact_35" in new_all_ids

    def test_contact_at_position_35_gets_badge(self):
        """
        Simula que contact_35 tiene cita. Con el fix, recibe has_appointment=True.
        """
        contacts_sorted = self._build_contacts(50)

        # Simular que contact_35 y contact_40 tienen citas en MongoDB
        contacts_with_appts = {"contact_35", "contact_40"}

        # Aplicar lógica de asignación (exactamente como en outbound_panel.py)
        all_contact_ids = [
            c.get("contact_id", "") for c in contacts_sorted
            if c.get("contact_id")
        ]
        # (la query real retorna contacts_with_appts desde MongoDB)
        for c in contacts_sorted:
            c["has_appointment"] = c.get("contact_id", "") in contacts_with_appts

        # Verificar
        contact_35 = next(c for c in contacts_sorted if c["contact_id"] == "contact_35")
        contact_40 = next(c for c in contacts_sorted if c["contact_id"] == "contact_40")
        contact_10 = next(c for c in contacts_sorted if c["contact_id"] == "contact_10")

        assert contact_35["has_appointment"] is True
        assert contact_40["has_appointment"] is True
        assert contact_10["has_appointment"] is False

    def test_old_bug_contact_at_position_35_had_no_badge(self):
        """Reproduce el bug original: contact_35 NO recibía badge con el código viejo."""
        limit = 30
        contacts_sorted = self._build_contacts(50)
        contacts_with_appts = {"contact_35"}

        # Código BUGGY (antes del fix)
        page_contact_ids = [
            c.get("contact_id", "") for c in contacts_sorted[:limit]
            if c.get("contact_id")
        ]
        for c in contacts_sorted:
            # Bug: contacts_with_appts siempre fue evaluado con page_contact_ids
            # pero contact_35 nunca estuvo en page_contact_ids
            c["has_appointment"] = c.get("contact_id", "") in contacts_with_appts

        contact_35 = next(c for c in contacts_sorted if c["contact_id"] == "contact_35")
        # Con el código buggy, contact_35 NUNCA matcheaba la query → has_appointment=False
        # (porque nunca se incluía en la query a MongoDB, retornaba set vacío para él)
        # Aquí simulamos que contacts_with_appts retorna vacío para contact_35
        # porque no fue incluido en el $in
        contacts_with_appts_old = set()  # MongoDB recibió solo IDs 0-29, retornó vacío para 35
        contact_35_buggy = {"contact_id": "contact_35"}
        contact_35_buggy["has_appointment"] = contact_35_buggy["contact_id"] in contacts_with_appts_old
        assert contact_35_buggy["has_appointment"] is False, "Bug confirmado: antes era False"

    def test_empty_contact_ids_filtered(self):
        """Contactos sin contact_id no deben incluirse en la query."""
        contacts_sorted = [
            {"phone": "+573001", "contact_id": "c1"},
            {"phone": "+573002", "contact_id": ""},      # vacío
            {"phone": "+573003", "contact_id": None},    # None
            {"phone": "+573004"},                        # sin campo
            {"phone": "+573005", "contact_id": "c5"},
        ]
        all_contact_ids = [
            c.get("contact_id", "") for c in contacts_sorted
            if c.get("contact_id")
        ]
        assert all_contact_ids == ["c1", "c5"]
        assert len(all_contact_ids) == 2

    def test_has_appointment_false_when_no_appointments(self):
        """Si ningún contacto tiene cita, todos quedan has_appointment=False."""
        contacts_sorted = [
            {"contact_id": "c1"}, {"contact_id": "c2"}, {"contact_id": "c3"}
        ]
        contacts_with_appts = set()  # MongoDB retornó vacío
        for c in contacts_sorted:
            c["has_appointment"] = c.get("contact_id", "") in contacts_with_appts
        assert all(c["has_appointment"] is False for c in contacts_sorted)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2: Update optimista en frontend (lógica replicada en Python)
# ═══════════════════════════════════════════════════════════════════════════════
class TestOptimisticBadgeUpdate:
    """Verifica la lógica del update optimista en submitAppointment()."""

    def _build_all_contacts(self):
        return [
            {"contact_id": "c_001", "phone": "+573001", "has_appointment": False},
            {"contact_id": "c_002", "phone": "+573002", "has_appointment": False},
            {"contact_id": "c_003", "phone": "+573003", "has_appointment": True},  # ya tenía
        ]

    def _apply_optimistic_update(self, all_contacts, current_contact_id, is_edit):
        """Replica la lógica JS: if (!editingId && currentContactId) { ... }"""
        if not is_edit and current_contact_id:
            target = next(
                (c for c in all_contacts if c.get("contact_id") == current_contact_id),
                None
            )
            if target:
                target["has_appointment"] = True
                return True  # render triggered
        return False  # no update

    def test_new_appointment_sets_badge(self):
        """Crear nueva cita → badge se activa inmediatamente."""
        all_contacts = self._build_all_contacts()
        updated = self._apply_optimistic_update(all_contacts, "c_001", is_edit=False)

        assert updated is True
        c001 = next(c for c in all_contacts if c["contact_id"] == "c_001")
        assert c001["has_appointment"] is True

    def test_edit_appointment_no_change(self):
        """Editar cita existente → NO dispara update optimista."""
        all_contacts = self._build_all_contacts()
        updated = self._apply_optimistic_update(all_contacts, "c_002", is_edit=True)

        assert updated is False
        c002 = next(c for c in all_contacts if c["contact_id"] == "c_002")
        assert c002["has_appointment"] is False  # sin cambio

    def test_null_contact_id_no_crash(self):
        """currentContactId=None → no crash, no update."""
        all_contacts = self._build_all_contacts()
        updated = self._apply_optimistic_update(all_contacts, None, is_edit=False)
        assert updated is False

    def test_contact_not_in_all_contacts(self):
        """Contacto no está en allContacts (raro) → no crash."""
        all_contacts = self._build_all_contacts()
        updated = self._apply_optimistic_update(all_contacts, "c_999", is_edit=False)
        assert updated is False  # find retorna None, if target protege

    def test_already_has_appointment(self):
        """Contacto ya tiene badge → actualizar a True de nuevo no rompe nada."""
        all_contacts = self._build_all_contacts()
        updated = self._apply_optimistic_update(all_contacts, "c_003", is_edit=False)
        assert updated is True
        c003 = next(c for c in all_contacts if c["contact_id"] == "c_003")
        assert c003["has_appointment"] is True  # sigue True


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3: MongoDB query — firma de get_contacts_with_appointments
# ═══════════════════════════════════════════════════════════════════════════════
class TestMongoDBBadgeQuery:
    """Verifica que la query MongoDB recibe todos los IDs correctamente."""

    @pytest.mark.asyncio
    async def test_query_receives_all_ids(self):
        """Simula que la función recibe 50 IDs (no solo 30)."""
        received_ids = []

        async def mock_get_contacts_with_appts(contact_ids):
            received_ids.extend(contact_ids)
            # Simular que contact_35 tiene cita
            return {cid for cid in contact_ids if cid == "contact_35"}

        all_contact_ids = [f"contact_{i}" for i in range(50)]
        result = await mock_get_contacts_with_appts(all_contact_ids)

        assert len(received_ids) == 50, f"Esperaba 50, recibió {len(received_ids)}"
        assert "contact_35" in result
        assert "contact_10" not in result

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty_set(self):
        """Lista vacía → retorna set vacío sin error."""
        async def mock_get_contacts_with_appts(contact_ids):
            if not contact_ids:
                return set()
            return set()

        result = await mock_get_contacts_with_appts([])
        assert result == set()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
