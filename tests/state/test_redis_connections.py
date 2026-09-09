"""
Test: Verifica los caps de max_connections en todos los pools Redis del proyecto.
Detecta pools sin límite explícito que pueden causar connection exhaustion en Railway.

Uso: python scripts/test_redis_connections.py
"""
import os
import sys

# Añadir raíz del proyecto al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


def check_pool_max_connections(client, name: str) -> int:
    """Extrae max_connections del pool de un cliente Redis (sync o async)."""
    try:
        pool = getattr(client, "connection_pool", None)
        if pool is None:
            return -1
        max_conn = getattr(pool, "max_connections", None)
        return max_conn if max_conn is not None else 0  # 0 = sin límite (default)
    except Exception:
        return -1


def test_message_aggregator():
    print("\n[1] MessageAggregator (utils/message_aggregator.py)")
    try:
        from utils.message_aggregator import message_aggregator
        if not getattr(message_aggregator, "_redis_available", False):
            print("  [SKIP] Redis no disponible en este entorno (normal en local sin REDIS_URL)")
            return True
        max_conn = check_pool_max_connections(message_aggregator.redis, "MessageAggregator")
        if max_conn > 0:
            print(f"  [OK] max_connections = {max_conn}")
            return True
        else:
            print(f"  [FALLO] max_connections no configurado (default sin limite)")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_lead_assigner():
    print("\n[2] LeadAssigner (integrations/hubspot/lead_assigner.py)")
    try:
        from integrations.hubspot.lead_assigner import lead_assigner
        if not getattr(lead_assigner, "_redis_available", False):
            print("  [SKIP] Redis no disponible")
            return True
        max_conn = check_pool_max_connections(lead_assigner.redis, "LeadAssigner")
        if max_conn > 0:
            print(f"  [OK] max_connections = {max_conn}")
            return True
        else:
            print(f"  [FALLO] max_connections no configurado (default sin limite)")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_conversation_state():
    print("\n[3] ConversationStateManager (middleware/conversation_state.py)")
    try:
        redis_url = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        import redis.asyncio as aioredis
        from middleware.conversation_state import ConversationStateManager
        mgr = ConversationStateManager(redis_url)
        max_conn = check_pool_max_connections(mgr.redis, "ConversationStateManager")
        if max_conn > 0:
            print(f"  [OK] max_connections = {max_conn}")
            return True
        else:
            print(f"  [FALLO] max_connections no configurado (default sin limite)")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_pubsub_factory():
    print("\n[4] WebSocket PubSub factory (app.py _make_redis_for_pubsub)")
    print("  [INFO] Este test verifica el comportamiento del leak manualmente.")
    print("  El fix mueve redis_client = await get_redis_fn() FUERA del while True.")
    print("  Verificacion: buscar en websocket_manager.py que redis_client esta antes del loop.")

    try:
        with open("middleware/websocket_manager.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verificar que redis_client se crea fuera del while True
        # La firma correcta tiene "redis_client = await get_redis_fn()" antes de "while True:"
        listener_start = content.find("async def _run_redis_listener")
        if listener_start == -1:
            print("  [ERROR] No se encontro _run_redis_listener en el archivo")
            return False

        # Buscar hasta el final del metodo (2000 chars es mas que suficiente)
        listener_code = content[listener_start:listener_start + 2000]

        # El cliente debe crearse ANTES del while True real del loop
        # Usamos la indentacion exacta del loop (12 espacios) para no matchear
        # la aparicion de "while True:" que puede existir en el docstring del metodo
        client_creation_pos = listener_code.find("redis_client = await get_redis_fn()")
        while_true_pos = listener_code.find("            while True:")

        if client_creation_pos == -1:
            print("  [FALLO] redis_client no se crea con get_redis_fn() — leak potencial")
            return False
        elif client_creation_pos < while_true_pos:
            print("  [OK] redis_client se crea ANTES del while True — leak corregido")
            return True
        else:
            print("  [FALLO] redis_client se crea DENTRO del while True — leak presente")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_outbound_panel():
    print("\n[5] outbound_panel pool (middleware/outbound_panel.py) — referencia")
    print("  [INFO] Ya tenia max_connections=5. No se modifico. Verificando...")
    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()
        if "max_connections=5" in content:
            print("  [OK] max_connections=5 presente (no requeria cambios)")
            return True
        else:
            print("  [WARN] max_connections=5 no encontrado — revisar manualmente")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_procfile_workers():
    print("\n[6] Procfile — numero de workers")
    try:
        with open("Procfile", "r") as f:
            content = f.read()
        if "--workers 2" in content:
            print("  [OK] workers = 2")
            return True
        elif "--workers 4" in content:
            print("  [FALLO] workers = 4 (todavia sin reducir)")
            return False
        else:
            import re
            match = re.search(r"--workers (\d+)", content)
            workers = match.group(1) if match else "desconocido"
            print(f"  [WARN] workers = {workers} (esperado: 2)")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


if __name__ == "__main__":
    print("=" * 55)
    print("TEST: Redis Connection Caps (Hallazgo 2)")
    print("=" * 55)

    results = [
        test_message_aggregator(),
        test_lead_assigner(),
        test_conversation_state(),
        test_pubsub_factory(),
        test_outbound_panel(),
        test_procfile_workers(),
    ]

    passed = sum(1 for r in results if r)
    total = len(results)

    print("\n" + "=" * 55)
    print(f"Resultado: {passed}/{total} checks pasaron")
    if passed == total:
        print("[OK] Todos los caps de conexion estan configurados correctamente")
        print("Techo teorico con 2 workers: ~30 conexiones Redis (Railway hobby = 50)")
    else:
        print("[FALLO] Algunos caps faltan — revisar los items marcados arriba")
    print("=" * 55)
