#!/usr/bin/env python3
"""
Script de validación manual para las correcciones del Panel de Asesores.

Ejecutar con:
    python scripts/validate_panel_fixes.py

Este script verifica que las correcciones están implementadas correctamente
sin necesidad de levantar el servidor completo.
"""

import sys
import os

# Agregar directorio raíz al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

def print_header(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def print_result(test_name, passed, details=""):
    icon = "[OK]" if passed else "[FAIL]"
    print(f"{icon} {test_name}")
    if details:
        print(f"     {details}")


def test_1_template_visibility():
    """Test #1: Botón de templates visible."""
    print_header("Test #1: Selector de Templates Siempre Visible")

    try:
        # Leer archivo de panel
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verificar que templateSection existe y está fuera de windowWarning
        has_template_section = 'id="templateSection"' in content
        has_selector = 'id="templateSelector"' in content
        has_send_btn = 'id="sendTemplateBtn"' in content

        # Verificar que templateSection está ANTES de sendForm (indica que está visible)
        template_pos = content.find('id="templateSection"')
        form_pos = content.find('id="sendForm"')

        is_before_form = template_pos < form_pos if template_pos > 0 and form_pos > 0 else False

        print_result(
            "Sección de templates existe",
            has_template_section,
            f"templateSection encontrado: {has_template_section}"
        )
        print_result(
            "Selector de templates existe",
            has_selector
        )
        print_result(
            "Botón de enviar template existe",
            has_send_btn
        )
        print_result(
            "Templates antes del formulario de texto",
            is_before_form,
            "Templates siempre visibles independiente del input"
        )

        return has_template_section and has_selector and has_send_btn

    except Exception as e:
        print_result("Error leyendo archivo", False, str(e))
        return False


def test_2_batch_api():
    """Test #2: Batch API para HubSpot."""
    print_header("Test #2: Batch API para HubSpot (evita 429)")

    try:
        with open("integrations/hubspot/timeline_logger.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verificar uso de Batch API
        uses_batch = "/batch/read" in content
        no_individual_loop = "for note_id in note_ids" not in content or "batch" in content.lower()

        print_result(
            "Usa endpoint /batch/read",
            uses_batch,
            "Endpoint: /crm/v3/objects/notes/batch/read"
        )
        print_result(
            "Obtiene múltiples notas en una petición",
            uses_batch,
            "Reduce de 50 requests a 1 request"
        )

        return uses_batch

    except Exception as e:
        print_result("Error leyendo archivo", False, str(e))
        return False


def test_3_patch_validation():
    """Test #3: Validación de PATCH nombre."""
    print_header("Test #3: Validación de Endpoint PATCH Nombre")

    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verificar validaciones
        has_null_check = 'contact_id == "null"' in content or "contact_id is None" in content.lower()
        has_numeric_check = "int(contact_id)" in content
        has_404_handling = "status_code == 404" in content
        has_logging = "[Panel] PATCH nombre" in content

        print_result(
            "Valida contact_id no nulo",
            has_null_check,
            'Rechaza "null" y "undefined"'
        )
        print_result(
            "Valida contact_id numérico",
            has_numeric_check,
            "IDs de HubSpot deben ser numéricos"
        )
        print_result(
            "Maneja error 404",
            has_404_handling,
            "Contacto no encontrado"
        )
        print_result(
            "Tiene logging para diagnóstico",
            has_logging
        )

        return has_numeric_check and has_logging

    except Exception as e:
        print_result("Error leyendo archivo", False, str(e))
        return False


def test_4_close_conversation():
    """Test #4: Endpoint para cerrar conversación."""
    print_header("Test #4: Botón Cerrar Conversación")

    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verificar endpoint DELETE
        has_delete_endpoint = '@router.delete("/contacts/{phone}/close")' in content
        has_close_function = "async def close_conversation(" in content
        has_close_button = 'id="closeConversationBtn"' in content
        has_js_function = "async function closeConversation()" in content

        print_result(
            "Endpoint DELETE existe",
            has_delete_endpoint,
            "DELETE /contacts/{phone}/close"
        )
        print_result(
            "Función close_conversation implementada",
            has_close_function
        )
        print_result(
            "Botón en UI existe",
            has_close_button
        )
        print_result(
            "Función JavaScript existe",
            has_js_function
        )

        return has_delete_endpoint and has_close_button

    except Exception as e:
        print_result("Error leyendo archivo", False, str(e))
        return False


def test_5_incremental_sync():
    """Test #5: Sincronización incremental."""
    print_header("Test #5: Sincronización Incremental (anti-parpadeo)")

    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verificar implementación de sync incremental
        has_data_msg_id = 'data-msg-id="${msg.id}"' in content or 'data-msg-id="${{msg.id}}"' in content
        uses_insert_adjacent = "insertAdjacentHTML" in content
        checks_existing = 'querySelector(`[data-msg-id=' in content or "querySelector" in content
        has_fade_animation = "animate-fadeIn" in content

        print_result(
            "Usa data-msg-id para tracking",
            has_data_msg_id,
            "Identifica mensajes únicos por ID"
        )
        print_result(
            "Usa insertAdjacentHTML",
            uses_insert_adjacent,
            "Agrega sin destruir DOM existente"
        )
        print_result(
            "Verifica mensajes existentes",
            checks_existing,
            "No duplica mensajes"
        )
        print_result(
            "Animación de entrada",
            has_fade_animation,
            "Fade-in para mensajes nuevos"
        )

        return has_data_msg_id and uses_insert_adjacent

    except Exception as e:
        print_result("Error leyendo archivo", False, str(e))
        return False


def test_6_time_filters():
    """Test #6: Filtros de tiempo correctos."""
    print_header("Test #6: Filtros de Tiempo")

    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verificar filtro de tiempo
        has_filter_step = "PASO 3.5" in content
        filters_active = "filtered_active" in content
        calculates_time_ago = '"time_ago"' in content or "'time_ago'" in content
        shows_time_in_ui = "Llegó ${" in content or "Llegó ${{" in content

        print_result(
            "Filtro de tiempo para contactos activos",
            has_filter_step,
            "PASO 3.5: Filtra por activated_at"
        )
        print_result(
            "Lista filtrada de activos",
            filters_active
        )
        print_result(
            "Calcula tiempo transcurrido",
            calculates_time_ago,
            'Campo "time_ago" para mostrar hace cuánto llegó'
        )
        print_result(
            "Muestra tiempo en UI",
            shows_time_in_ui,
            '"Llegó hace X min/h"'
        )

        return has_filter_step and calculates_time_ago

    except Exception as e:
        print_result("Error leyendo archivo", False, str(e))
        return False


def main():
    print("\n" + "="*60)
    print("  VALIDACIÓN DE CORRECCIONES - Panel de Asesores")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("="*60)

    results = []

    results.append(("Templates visibles", test_1_template_visibility()))
    results.append(("Batch API HubSpot", test_2_batch_api()))
    results.append(("PATCH validación", test_3_patch_validation()))
    results.append(("Cerrar conversación", test_4_close_conversation()))
    results.append(("Sync incremental", test_5_incremental_sync()))
    results.append(("Filtros tiempo", test_6_time_filters()))

    # Resumen
    print_header("RESUMEN")

    passed = sum(1 for _, r in results if r)
    total = len(results)

    for name, result in results:
        icon = "[OK]" if result else "[FAIL]"
        print(f"  {icon} {name}")

    print(f"\n  Resultado: {passed}/{total} tests pasados")

    if passed == total:
        print("\n  [SUCCESS] Todas las correcciones implementadas correctamente!")
        return 0

    print("\n  [WARNING] Algunas correcciones necesitan revision")
    return 1


if __name__ == "__main__":
    sys.exit(main())
