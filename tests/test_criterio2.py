# tests/test_criterio2.py
"""
Test de Criterio 2: Actualización Real (Hot-Reload A → B)

Verifica que el sistema RAG puede recargar dinámicamente la base de
conocimiento cuando los archivos cambian.

NOTA: Este test requiere conexión a PostgreSQL (Railway).
      Se skipea automáticamente si se ejecuta localmente.
"""

import pytest
import os
from rag.rag_service import rag_service


@pytest.fixture
def test_file_path():
    """Fixture que proporciona la ruta del archivo de prueba y limpia al final."""
    file_path = 'knowledge_base/test_criterio2.txt'
    yield file_path

    # Cleanup: eliminar archivo si existe
    if os.path.exists(file_path):
        os.remove(file_path)


def test_criterio2_hot_reload(test_file_path):
    """
    Test: Verifica que el sistema puede recargar conocimiento en caliente (A → B).

    Este test valida que:
    1. El sistema carga correctamente un documento con "Respuesta: A"
    2. Después de modificar el archivo a "Respuesta: B", el sistema actualiza
    3. Las búsquedas reflejan el nuevo contenido inmediatamente
    """
    print('=' * 60)
    print('CRITERIO 2: Actualización Real (A -> B)')
    print('=' * 60)

    try:
        # [Paso 1] Crear archivo con respuesta A
        print('\n[Paso 1] Crear archivo con respuesta A')
        with open(test_file_path, 'w', encoding='utf-8') as f:
            f.write('Pregunta: ¿Cuál es la respuesta?\n')
            f.write('Respuesta: A\n')

        # [Paso 2] Recargar y buscar A
        print('[Paso 2] Recargar y buscar A')
        result = rag_service.reload_knowledge_base()
        print(f"  Resultado recarga: {result['message']}")

        result_A = rag_service.search_knowledge(test_file_path, 'respuesta')
        assert 'Respuesta: A' in result_A, f"No se encontró 'Respuesta: A' en: {result_A}"
        print('  ✓ OK: Sistema responde A')

        # [Paso 3] Modificar a respuesta B
        print('\n[Paso 3] Modificar a respuesta B')
        with open(test_file_path, 'w', encoding='utf-8') as f:
            f.write('Pregunta: ¿Cuál es la respuesta?\n')
            f.write('Respuesta: B\n')

        # [Paso 4] Recargar y buscar B
        print('[Paso 4] Recargar y buscar B')
        result = rag_service.reload_knowledge_base()
        print(f"  Resultado recarga: {result['message']}")

        result_B = rag_service.search_knowledge(test_file_path, 'respuesta')
        assert 'Respuesta: B' in result_B, f"No se encontró 'Respuesta: B' en: {result_B}"
        assert 'Respuesta: A' not in result_B, f"Aún aparece 'Respuesta: A' en: {result_B}"
        print('  ✓ OK: Sistema responde B (no A)')

        # [Paso 5] Limpiar
        print('\n[Paso 5] Limpiar')
        os.remove(test_file_path)
        rag_service.reload_knowledge_base()

        print('\n✅ CRITERIO 2 VALIDADO')

    except ConnectionError as e:
        # Si no hay conexión a PostgreSQL, skip el test
        pytest.skip(f"Test requiere PostgreSQL (Railway). Error: {e}")


if __name__ == '__main__':
    # Permite ejecutar el test directamente: python tests/test_criterio2.py
    pytest.main([__file__, '-v', '-s'])
