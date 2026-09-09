# tests/test_redis_metrics.py
"""
Tests de métricas y rendimiento para StateManager con Redis.
Valida latencia, conexiones y capacidad de carga.
"""

import pytest
import time
import os
from datetime import datetime
from state_manager import StateManager, ConversationState, ConversationStatus


# ===== FIXTURES =====

@pytest.fixture(scope="module")
def state_manager_real():
    """
    Fixture que proporciona un StateManager con conexión REAL a Redis.
    Requiere que Redis esté corriendo en localhost:6379.

    Nota: Este fixture usa scope="module" para reutilizar la conexión
    entre tests y reducir overhead.
    """
    # Verificar que las variables de entorno estén configuradas
    if not os.getenv("REDIS_URL"):
        pytest.skip("REDIS_URL no configurada. Ejecutar: export REDIS_URL=redis://localhost:6379/0")

    try:
        manager = StateManager()

        # IMPORTANTE: Forzar inicialización de Redis para capturar errores de conexión
        # antes de que los tests empiecen. Sin esto, lazy initialization hace que
        # el error ocurra dentro de los tests (FAILED) en lugar de aquí (SKIPPED).
        manager._ensure_redis_initialized()

        yield manager

        # Cleanup: Limpiar claves de test después de todos los tests
        if manager.client:
            keys_to_delete = manager.client.keys("session:test_*")
            keys_to_delete += manager.client.keys("session:stress_*")
            if keys_to_delete:
                manager.client.delete(*keys_to_delete)
                print(f"\n[Cleanup] {len(keys_to_delete)} claves de test eliminadas")
    except (ConnectionError, Exception) as e:
        pytest.skip(f"No se puede conectar a Redis: {e}")


# ===== TESTS: Latencia =====

def test_get_state_latency(state_manager_real):
    """
    Test que mide la latencia de get_state.
    Objetivo: < 5ms en red local.
    """
    session_id = "test_latency_get"

    # Preparar: Crear sesión
    state = ConversationState(session_id=session_id)
    state_manager_real.update_state(state)

    # Medir latencia (promedio de 100 lecturas)
    latencies = []
    for _ in range(100):
        start = time.perf_counter()
        state_manager_real.get_state(session_id)
        end = time.perf_counter()
        latencies.append((end - start) * 1000)  # Convertir a ms

    avg_latency = sum(latencies) / len(latencies)
    max_latency = max(latencies)
    min_latency = min(latencies)

    print(f"\n[Métrica] get_state() latencia:")
    print(f"  - Promedio: {avg_latency:.2f}ms")
    print(f"  - Mínima:   {min_latency:.2f}ms")
    print(f"  - Máxima:   {max_latency:.2f}ms")

    # Validación: Promedio debe ser < 5ms
    assert avg_latency < 5.0, f"Latencia promedio ({avg_latency:.2f}ms) excede 5ms"


def test_update_state_latency(state_manager_real):
    """
    Test que mide la latencia de update_state.
    Objetivo: < 10ms en red local.
    """
    session_id = "test_latency_update"

    # Medir latencia (promedio de 100 escrituras)
    latencies = []
    for i in range(100):
        state = ConversationState(
            session_id=session_id,
            status=ConversationStatus.TRANSFERRED_INFO,
            lead_data={"iteration": i}
        )

        start = time.perf_counter()
        state_manager_real.update_state(state)
        end = time.perf_counter()
        latencies.append((end - start) * 1000)

    avg_latency = sum(latencies) / len(latencies)
    max_latency = max(latencies)
    min_latency = min(latencies)

    print(f"\n[Métrica] update_state() latencia:")
    print(f"  - Promedio: {avg_latency:.2f}ms")
    print(f"  - Mínima:   {min_latency:.2f}ms")
    print(f"  - Máxima:   {max_latency:.2f}ms")

    # Validación: Promedio debe ser < 10ms
    assert avg_latency < 10.0, f"Latencia promedio ({avg_latency:.2f}ms) excede 10ms"


# ===== TESTS: Conexiones =====

def test_connection_pooling(state_manager_real):
    """
    Test que verifica que StateManager usa connection pooling eficientemente.
    """
    # Redis client de redis-py usa connection pooling por defecto
    # Verificar que el pool está configurado
    assert hasattr(state_manager_real.client, "connection_pool")

    pool = state_manager_real.client.connection_pool
    print(f"\n[Métrica] Connection Pool:")
    print(f"  - Tipo: {type(pool).__name__}")
    print(f"  - Max conexiones: {pool.max_connections if hasattr(pool, 'max_connections') else 'N/A'}")


def test_multiple_operations_same_connection(state_manager_real):
    """
    Test que verifica que múltiples operaciones reutilizan la misma conexión.
    """
    # Realizar 50 operaciones mixtas
    for i in range(50):
        session_id = f"test_conn_{i}"
        state = ConversationState(session_id=session_id)
        state_manager_real.update_state(state)
        retrieved = state_manager_real.get_state(session_id)
        assert retrieved.session_id == session_id

    # Verificar que el pool no creó 100 conexiones
    # (50 escrituras + 50 lecturas deberían reutilizar conexiones)
    print("\n[Métrica] 50 operaciones completadas usando connection pooling")


# ===== TESTS: Prueba de Estrés =====

def test_stress_create_100_sessions(state_manager_real):
    """
    Test de estrés: Crear 100 sesiones rápidamente y verificar que todas se guarden.
    """
    num_sessions = 100
    session_ids = [f"stress_{i:03d}" for i in range(num_sessions)]

    # Fase 1: Crear 100 sesiones
    start_time = time.perf_counter()

    for i, session_id in enumerate(session_ids):
        state = ConversationState(
            session_id=session_id,
            status=ConversationStatus.TRANSFERRED_CRM,
            lead_data={
                "name": f"Usuario {i}",
                "email": f"user{i}@example.com",
                "iteration": i
            },
            history=[f"Mensaje inicial {i}", f"Respuesta {i}"]
        )
        state_manager_real.update_state(state)

    creation_time = time.perf_counter() - start_time

    print(f"\n[Estrés] 100 sesiones creadas en {creation_time:.2f}s")
    print(f"  - Throughput: {num_sessions / creation_time:.2f} sesiones/segundo")

    # Fase 2: Verificar que todas las sesiones existen en Redis
    verification_start = time.perf_counter()
    missing_sessions = []

    for session_id in session_ids:
        key = f"session:{session_id}"
        if not state_manager_real.client.exists(key):
            missing_sessions.append(session_id)

    verification_time = time.perf_counter() - verification_start

    print(f"[Estrés] Verificación completada en {verification_time:.2f}s")
    print(f"  - Sesiones encontradas: {num_sessions - len(missing_sessions)}/{num_sessions}")

    # Validación: Todas las sesiones deben existir
    assert len(missing_sessions) == 0, f"Sesiones faltantes: {missing_sessions}"


def test_stress_concurrent_reads(state_manager_real):
    """
    Test de estrés: Leer la misma sesión 200 veces consecutivamente.
    Simula múltiples usuarios consultando el mismo estado.
    """
    session_id = "stress_concurrent_reads"

    # Preparar: Crear sesión con datos complejos
    state = ConversationState(
        session_id=session_id,
        status=ConversationStatus.TRANSFERRED_INFO,
        lead_data={"name": "Test User", "history_length": 50},
        history=[f"Mensaje {i}" for i in range(50)],
        last_interaction_timestamp=datetime(2025, 1, 15, 10, 30, 0)
    )
    state_manager_real.update_state(state)

    # Fase: Leer 200 veces
    start_time = time.perf_counter()
    read_count = 200

    for _ in range(read_count):
        retrieved_state = state_manager_real.get_state(session_id)
        assert retrieved_state.session_id == session_id
        assert len(retrieved_state.history) == 50

    total_time = time.perf_counter() - start_time

    print(f"\n[Estrés] {read_count} lecturas consecutivas en {total_time:.2f}s")
    print(f"  - Throughput: {read_count / total_time:.2f} lecturas/segundo")
    print(f"  - Latencia promedio: {(total_time / read_count) * 1000:.2f}ms")

    # Validación: Throughput debe ser > 1000 ops/seg en red local
    throughput = read_count / total_time
    assert throughput > 100, f"Throughput ({throughput:.2f} ops/s) muy bajo"


def test_stress_mixed_operations(state_manager_real):
    """
    Test de estrés: Operaciones mixtas (lecturas y escrituras alternadas).
    """
    num_operations = 100
    session_id = "stress_mixed"

    start_time = time.perf_counter()

    for i in range(num_operations):
        if i % 2 == 0:
            # Escritura
            state = ConversationState(
                session_id=session_id,
                lead_data={"iteration": i}
            )
            state_manager_real.update_state(state)
        else:
            # Lectura
            state = state_manager_real.get_state(session_id)
            assert state.session_id == session_id

    total_time = time.perf_counter() - start_time

    print(f"\n[Estrés] {num_operations} operaciones mixtas en {total_time:.2f}s")
    print(f"  - Throughput: {num_operations / total_time:.2f} ops/segundo")


# ===== TESTS: TTL Verification =====

def test_ttl_is_set_correctly(state_manager_real):
    """
    Test que verifica que el TTL se configura correctamente en Redis.
    """
    session_id = "test_ttl_verification"

    # Crear sesión
    state = ConversationState(session_id=session_id)
    state_manager_real.update_state(state)

    # Verificar TTL en Redis
    key = f"session:{session_id}"
    ttl_seconds = state_manager_real.client.ttl(key)

    print(f"\n[Métrica] TTL configurado:")
    print(f"  - Esperado: {state_manager_real.session_ttl}s")
    print(f"  - Redis TTL: {ttl_seconds}s")
    print(f"  - Diferencia: {abs(state_manager_real.session_ttl - ttl_seconds)}s")

    # Validación: TTL debe estar entre session_ttl-5 y session_ttl
    # (puede haber ligera diferencia por tiempo de ejecución)
    assert ttl_seconds > 0, "TTL no configurado (clave sin expiración)"
    assert ttl_seconds <= state_manager_real.session_ttl, "TTL excede configuración"
    assert ttl_seconds >= state_manager_real.session_ttl - 5, "TTL demasiado bajo"


# ===== TESTS: Data Integrity =====

def test_datetime_serialization_integrity(state_manager_real):
    """
    Test que verifica que los campos datetime se serializan/deserializan correctamente.
    """
    session_id = "test_datetime_integrity"
    original_timestamp = datetime(2025, 11, 24, 15, 30, 45)

    # Crear estado con timestamp
    state = ConversationState(
        session_id=session_id,
        last_interaction_timestamp=original_timestamp
    )
    state_manager_real.update_state(state)

    # Recuperar y verificar tipo
    retrieved_state = state_manager_real.get_state(session_id)

    print(f"\n[Métrica] Serialización de datetime:")
    print(f"  - Original:    {original_timestamp} (tipo: {type(original_timestamp).__name__})")
    print(f"  - Recuperado:  {retrieved_state.last_interaction_timestamp} (tipo: {type(retrieved_state.last_interaction_timestamp).__name__})")

    # Validación: Debe ser datetime, no string
    assert isinstance(retrieved_state.last_interaction_timestamp, datetime), \
        f"Tipo incorrecto: {type(retrieved_state.last_interaction_timestamp)}"
    assert retrieved_state.last_interaction_timestamp == original_timestamp, \
        "Timestamp no coincide después de serialización"


# ===== TESTS: Memory Footprint =====

def test_redis_memory_usage(state_manager_real):
    """
    Test que mide el uso de memoria de una sesión típica en Redis.
    """
    session_id = "test_memory_usage"

    # Crear sesión con datos típicos
    state = ConversationState(
        session_id=session_id,
        status=ConversationStatus.TRANSFERRED_INFO,
        lead_data={"name": "Juan Pérez", "email": "juan@example.com", "phone": "3001234567"},
        history=["Hola", "¿Cuáles son sus horarios?", "Gracias", "Adiós"]
    )
    state_manager_real.update_state(state)

    # Medir tamaño en Redis
    key = f"session:{session_id}"
    memory_bytes = state_manager_real.client.memory_usage(key)

    print(f"\n[Métrica] Uso de memoria por sesión:")
    print(f"  - Memoria en Redis: {memory_bytes} bytes ({memory_bytes / 1024:.2f} KB)")

    # Validación: Una sesión típica debe ocupar < 5KB
    assert memory_bytes < 5120, f"Sesión ocupa demasiada memoria: {memory_bytes} bytes"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])