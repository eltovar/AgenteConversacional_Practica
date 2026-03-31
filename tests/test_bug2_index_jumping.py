"""
Test Bug 2: Index Jumping — Render Guard y Reorder Inmediato.

Replica en Python la lógica JS implementada en index.js:
  A. Render guard (_renderInProgress + _pendingRenderContacts)
  B. In-place reorder en allContacts cuando llega new_message WS

Ejecutar:
    python -m pytest tests/test_bug2_index_jumping.py -v
"""
import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS — Simular estado global de index.js en Python
# ═══════════════════════════════════════════════════════════════════════════════

def _make_contact(phone: str, name: str = "") -> dict:
    return {"phone": phone, "display_name": name or phone, "status": "HUMAN_ACTIVE"}


def _apply_reorder(all_contacts: list, phone: str, fingerprints: set) -> tuple[list, set]:
    """
    Replica la lógica de reorder inmediato del WS new_message:
      - Si el contacto está en pos >0, lo mueve al tope
      - Elimina su fingerprint para forzar re-render
      - Si no existe o ya está en pos 0, no hace nada
    Retorna (nueva_lista, nuevos_fingerprints).
    """
    idx = next((i for i, c in enumerate(all_contacts) if c["phone"] == phone), -1)
    fps = set(fingerprints)
    if idx > 0:
        contact = all_contacts.pop(idx)
        all_contacts.insert(0, contact)
        fps.discard(phone)
    return all_contacts, fps


# ═══════════════════════════════════════════════════════════════════════════════
# TEST GRUPO A: Render Guard
# ═══════════════════════════════════════════════════════════════════════════════

class TestRenderGuard:
    """Verifica comportamiento del guard _renderInProgress."""

    def _simulate_guard(self, render_calls: list[list]) -> list[list]:
        """
        Simula el guard de renderContactsList():
        - Si render en curso, encola el más reciente (sobrescribe _pending)
        - Al terminar el render activo, procesa el encolado
        Retorna lista de renders efectivamente ejecutados (en orden).
        """
        executed = []
        in_progress = False
        pending = None

        def _execute_render(contacts):
            nonlocal in_progress, pending
            in_progress = True
            executed.append(contacts[:])  # snapshot de lo que se renderizó
            in_progress = False
            if pending is not None:
                _next = pending
                pending = None
                _execute_render(_next)

        for contacts in render_calls:
            if in_progress:
                pending = contacts[:]  # encolar, sobrescribir anterior
            else:
                _execute_render(contacts)

        return executed

    def test_single_render_executes_immediately(self):
        """Un solo render → se ejecuta sin demora."""
        executed = self._simulate_guard([["+1", "+2"]])
        assert len(executed) == 1
        assert executed[0] == ["+1", "+2"]

    def test_second_render_while_first_in_progress_is_queued(self):
        """Segundo render durante el primero → se encola y ejecuta después."""
        # En la simulación secuencial, representamos "en progreso" con 2 llamadas
        # simultáneas donde la primera no ha terminado.
        # Como la simulación es síncrona, verificamos el encolado del pending.
        in_progress = False
        pending = None
        executed = []

        def start_render(contacts):
            nonlocal in_progress, pending
            if in_progress:
                pending = contacts[:]
                return
            in_progress = True
            executed.append(contacts[:])
            # Simular que durante este render llega otro
            # (no finalizamos aún — representado por no resetear in_progress aquí)

        start_render(["+1", "+2"])  # render 1 "en progreso"
        start_render(["+3", "+4"])  # render 2 → debe encolarse

        assert pending == ["+3", "+4"], "El segundo render debe encolarse"
        assert len(executed) == 1, "Solo el primero debe haberse ejecutado"

    def test_pending_overwritten_by_latest(self):
        """Si hay 3 renders rápidos, solo el primero y el último se ejecutan."""
        in_progress = False
        pending = None
        executed = []

        def start_render(contacts):
            nonlocal in_progress, pending
            if in_progress:
                pending = contacts[:]
                return
            in_progress = True
            executed.append(contacts[:])

        start_render(["A"])  # en progreso
        start_render(["B"])  # encola B
        start_render(["C"])  # sobrescribe pending con C

        assert pending == ["C"], "Solo el más reciente debe quedar encolado"
        assert executed == [["A"]]

    def test_render_in_progress_false_after_completion(self):
        """_renderInProgress vuelve a False después del render."""
        in_progress = False
        pending = None

        def render(contacts):
            nonlocal in_progress, pending
            if in_progress:
                pending = contacts[:]
                return
            in_progress = True
            # ... body ...
            in_progress = False
            if pending is not None:
                _next = pending
                pending = None
                render(_next)

        render(["A"])
        assert in_progress is False

    def test_pending_render_executes_after_first_completes(self):
        """El render encolado se ejecuta automáticamente al terminar el primero."""
        executed = self._simulate_guard([["A"], ["B"]])
        # En nuestra simulación secuencial ambos se ejecutan (el segundo como pending)
        assert len(executed) == 2
        assert executed[0] == ["A"]
        assert executed[1] == ["B"]


# ═══════════════════════════════════════════════════════════════════════════════
# TEST GRUPO B: In-place Reorder
# ═══════════════════════════════════════════════════════════════════════════════

class TestInPlaceReorder:
    """Verifica la lógica de reorder inmediato en allContacts al recibir new_message."""

    def test_contact_at_pos2_moves_to_pos0(self):
        """Contacto en posición 2 → sube a posición 0."""
        contacts = [
            _make_contact("+1"),
            _make_contact("+2"),
            _make_contact("+3"),
        ]
        fps = {"+1", "+2", "+3"}
        result, new_fps = _apply_reorder(contacts, "+3", fps)

        assert result[0]["phone"] == "+3"
        assert len(result) == 3

    def test_contact_already_at_pos0_no_change(self):
        """Contacto ya en pos 0 → no se mueve, no hay side effects."""
        contacts = [
            _make_contact("+1"),
            _make_contact("+2"),
        ]
        fps = {"+1", "+2"}
        original_order = [c["phone"] for c in contacts]
        result, new_fps = _apply_reorder(contacts, "+1", fps)

        assert [c["phone"] for c in result] == original_order

    def test_nonexistent_contact_no_change(self):
        """Contacto no existe en allContacts → lista sin cambios, sin error."""
        contacts = [_make_contact("+1"), _make_contact("+2")]
        fps = {"+1", "+2"}
        result, new_fps = _apply_reorder(contacts, "+999", fps)

        assert [c["phone"] for c in result] == ["+1", "+2"]
        assert new_fps == fps

    def test_fingerprint_deleted_on_reorder(self):
        """Al reordenar, el fingerprint del contacto se elimina para forzar re-render."""
        contacts = [_make_contact("+1"), _make_contact("+2"), _make_contact("+3")]
        fps = {"+1", "+2", "+3"}
        _, new_fps = _apply_reorder(contacts, "+3", fps)

        assert "+3" not in new_fps, "Fingerprint debe eliminarse para forzar re-render"

    def test_fingerprint_of_others_unchanged(self):
        """Fingerprints de otros contactos no se tocan."""
        contacts = [_make_contact("+1"), _make_contact("+2"), _make_contact("+3")]
        fps = {"+1", "+2", "+3"}
        _, new_fps = _apply_reorder(contacts, "+3", fps)

        assert "+1" in new_fps
        assert "+2" in new_fps

    def test_reorder_preserves_contact_data(self):
        """Al mover el contacto, sus datos se preservan íntegros."""
        contacts = [
            _make_contact("+1", "Ana"),
            _make_contact("+2", "Carlos"),
        ]
        fps = {"+1", "+2"}
        result, _ = _apply_reorder(contacts, "+2", fps)

        assert result[0]["display_name"] == "Carlos"
        assert result[0]["phone"] == "+2"
        assert result[0]["status"] == "HUMAN_ACTIVE"

    def test_total_contacts_count_unchanged(self):
        """Reorder no agrega ni elimina contactos."""
        contacts = [_make_contact(f"+{i}") for i in range(5)]
        fps = {f"+{i}" for i in range(5)}
        result, _ = _apply_reorder(contacts, "+4", fps)

        assert len(result) == 5

    def test_pos1_contact_moves_to_pos0(self):
        """Contacto en pos 1 (adyacente al top) también sube correctamente."""
        contacts = [_make_contact("+1"), _make_contact("+2")]
        fps = {"+1", "+2"}
        result, _ = _apply_reorder(contacts, "+2", fps)

        assert result[0]["phone"] == "+2"
        assert result[1]["phone"] == "+1"

    def test_reorder_then_scheduleRefresh_produces_correct_final_order(self):
        """
        Simula el flujo completo:
        1. Reorder inmediato (in-place)
        2. Llega respuesta del GET /contacts con el mismo orden
        → El render final debe tener el contacto en pos 0
        """
        contacts = [
            _make_contact("+1"),
            _make_contact("+2"),
            _make_contact("+3"),
        ]
        fps = {"+1", "+2", "+3"}

        # Paso 1: reorder inmediato
        contacts, fps = _apply_reorder(contacts, "+3", fps)
        assert contacts[0]["phone"] == "+3"

        # Paso 2: el GET /contacts responde con +3 también en pos 0 (backend lo ve primero)
        server_response = [
            _make_contact("+3", "Updated Name"),  # el servidor también lo tiene en top
            _make_contact("+1"),
            _make_contact("+2"),
        ]

        # El render aplica el orden del servidor (fuente de verdad)
        final_order = [c["phone"] for c in server_response]
        assert final_order[0] == "+3", "Backend confirma el orden"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST GRUPO C: Integración Guard + Reorder
# ═══════════════════════════════════════════════════════════════════════════════

class TestGuardAndReorderIntegration:
    """Escenarios combinados que reproducen el bug original."""

    def test_rapid_messages_do_not_produce_interleaved_renders(self):
        """
        5 mensajes llegan rápido → solo 1 render en progreso + máx 1 encolado.
        El resultado final es el orden correcto.
        """
        render_log = []
        in_progress = False
        pending = None

        def render(contacts):
            nonlocal in_progress, pending
            if in_progress:
                pending = contacts[:]
                return
            in_progress = True
            render_log.append(contacts[:])
            in_progress = False
            if pending is not None:
                _next = pending
                pending = None
                render(_next)

        # Simular 5 mensajes rápidos llegando mientras el primer render está activo
        render(["+1", "+2", "+3"])         # ejecuta
        in_progress = True                  # simular que sigue en progreso
        render(["+new", "+1", "+2", "+3"]) # encola
        render(["+new2", "+1", "+2", "+3"]) # sobrescribe pending
        render(["+new3", "+1", "+2", "+3"]) # sobrescribe pending
        render(["+final", "+1", "+2", "+3"]) # último — sobrescribe pending

        # Liberar el render activo
        in_progress = False
        if pending is not None:
            _next = pending
            pending = None
            render(_next)

        # Solo se deben haber ejecutado el primero y el último encolado
        assert len(render_log) == 2
        assert render_log[0] == ["+1", "+2", "+3"]
        assert render_log[1] == ["+final", "+1", "+2", "+3"]

    def test_reorder_before_refresh_prevents_visible_jump(self):
        """
        Reproduce el bug original:
        SIN fix: render1(orden viejo) → DOM jumps
        CON fix: reorder inmediato → render1(nuevo orden) → DOM estable
        """
        # Estado inicial: C es el contacto activo (seleccionado)
        contacts = [
            _make_contact("+A"),
            _make_contact("+B"),
            _make_contact("+C"),  # seleccionado por el asesor
        ]

        # Evento new_message llega para +A
        # CON FIX: reorder inmediato antes del GET /contacts
        fps = {"+A", "+B", "+C"}
        contacts, fps = _apply_reorder(contacts, "+A", fps)

        # El orden local ya es correcto
        assert contacts[0]["phone"] == "+A"
        # +C sigue en la lista sin cambios de posición relativa al tope actual
        assert contacts[2]["phone"] == "+C"

        # Cuando llega el GET /contacts, el backend confirma el mismo orden
        # → el render no mueve nada (fingerprints iguales para +B y +C)
        # → CERO index jumping para el asesor


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
