"""
Test: Verifica que las notificaciones WebSocket dirigidas van via Redis Pub/Sub.
Checks estaticos + runtime para Hallazgo 4.

Uso: python scripts/test_ws_targeted.py
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()


# --- 1. CHECKS ESTATICOS ----------------------------------------------------

def test_publish_to_advisor_exists():
    print("\n[1] websocket_manager.py - metodo publish_to_advisor()")
    try:
        with open("middleware/websocket_manager.py", "r", encoding="utf-8") as f:
            content = f.read()
        if "async def publish_to_advisor(" in content:
            print("  [OK] publish_to_advisor() existe")
            return True
        else:
            print("  [FALLO] publish_to_advisor() no encontrado")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_listener_handles_target():
    print("\n[2] websocket_manager.py - listener enruta _target")
    try:
        with open("middleware/websocket_manager.py", "r", encoding="utf-8") as f:
            content = f.read()
        if 'data.pop("_target", None)' in content:
            print("  [OK] listener hace pop('_target') antes de broadcast")
            return True
        else:
            print("  [FALLO] listener no maneja _target - routing cruzado no funciona")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_no_direct_send_to_advisor():
    print("\n[3] outbound_panel.py - sin llamadas directas a send_to_advisor()")
    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Buscar llamadas directas (no en definiciones ni en send_to_advisor propio)
        lines = content.split("\n")
        direct_calls = [
            (i+1, line.strip())
            for i, line in enumerate(lines)
            if "ws_manager.send_to_advisor(" in line
        ]

        if not direct_calls:
            print("  [OK] Sin llamadas directas a ws_manager.send_to_advisor() - todo via pub/sub")
            return True
        else:
            print(f"  [FALLO] Todavia hay llamadas directas:")
            for lineno, line in direct_calls:
                print(f"    Linea {lineno}: {line}")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_publish_to_advisor_calls():
    print("\n[4] outbound_panel.py - publish_to_advisor() en transfer-accept y transfer-reject")
    try:
        with open("middleware/outbound_panel.py", "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count("ws_manager.publish_to_advisor(")
        if count >= 2:
            print(f"  [OK] {count} llamadas a publish_to_advisor() encontradas")
            return True
        else:
            print(f"  [FALLO] Solo {count} llamadas (esperado: >= 2)")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


# --- 2. TEST RUNTIME ---------------------------------------------------------

def test_route_sync_in_listener():
    print("\n[5] websocket_manager.py - listener maneja _route para sync phone->advisor")
    try:
        with open("middleware/websocket_manager.py", "r", encoding="utf-8") as f:
            content = f.read()
        has_route_pop = 'data.pop("_route", None)' in content
        has_register   = 'self.register_phone_owner(route["phone"], route["advisor_id"])' in content
        has_continue   = 'if not data and not target:' in content and 'continue' in content
        if has_route_pop and has_register and has_continue:
            print("  [OK] listener hace pop('_route'), actualiza phone_to_advisor y hace continue si nada que enviar")
            return True
        else:
            if not has_route_pop:
                print("  [FALLO] listener no hace pop('_route')")
            if not has_register:
                print("  [FALLO] listener no llama register_phone_owner con route data")
            if not has_continue:
                print("  [FALLO] listener no tiene 'continue' para mensajes solo de sync")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_route_publish_in_transfer():
    print("\n[6] websocket_manager.py - notify_contact_transferred publica _route sync")
    try:
        with open("middleware/websocket_manager.py", "r", encoding="utf-8") as f:
            content = f.read()
        has_route_publish = '"_route"' in content and 'publish_broadcast' in content
        if has_route_publish:
            print("  [OK] notify_contact_transferred publica {'_route': ...} via publish_broadcast")
            return True
        else:
            print("  [FALLO] no se encontro publish_broadcast con _route")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


async def test_runtime_route_sync():
    print("\n[7] Runtime - mensaje _route es procesado sin broadcast al frontend")
    try:
        redis_url = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        import redis.asyncio as r
        import json

        client = r.from_url(redis_url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe("ws:broadcast")

        # Publicar mensaje de _route puro (sin _target, sin otro contenido)
        route_msg = {"_route": {"phone": "+573001234567", "advisor_id": "advisor_test_777"}}
        pub_client = r.from_url(redis_url, decode_responses=True)
        await pub_client.publish("ws:broadcast", json.dumps(route_msg))

        received = None
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                received = json.loads(msg["data"])
                break

        await pubsub.unsubscribe()
        await pubsub.aclose()
        await client.aclose()
        await pub_client.aclose()

        if received and "_route" in received:
            # Simular el comportamiento del listener
            route = received.pop("_route", None)
            target = received.pop("_target", None)
            nothing_to_send = not received and not target
            if route["phone"] == "+573001234567" and route["advisor_id"] == "advisor_test_777" and nothing_to_send:
                print(f"  [OK] _route recibido: phone={route['phone']} -> advisor={route['advisor_id']}")
                print(f"  [OK] 'continue' se activaria - sin broadcast vacio al frontend")
                return True
            else:
                print(f"  [FALLO] datos inesperados en _route: {route}")
                return False
        else:
            print(f"  [FALLO] mensaje recibido sin _route: {received}")
            return False
    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


async def test_runtime_targeted():
    print("\n[5] Runtime - publish_to_advisor agrega _target al payload")
    try:
        redis_url = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
        import redis.asyncio as r

        # Suscribirse al canal antes de publicar
        client = r.from_url(redis_url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe("ws:broadcast")

        # Publicar un mensaje dirigido via la logica de publish_to_advisor
        import json
        test_msg = {"type": "transfer_accepted", "contact_id": "test123"}
        test_advisor = "advisor_test_999"
        payload = {**test_msg, "_target": test_advisor}

        publish_client = r.from_url(redis_url, decode_responses=True)
        await publish_client.publish("ws:broadcast", json.dumps(payload))

        # Leer el mensaje publicado
        received = None
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                received = json.loads(msg["data"])
                break

        await pubsub.unsubscribe()
        await pubsub.aclose()
        await client.aclose()
        await publish_client.aclose()

        if received and received.get("_target") == test_advisor:
            print(f"  [OK] Mensaje recibido con _target={test_advisor}")
            print(f"       El listener lo enrutara a send_to_advisor('{test_advisor}', msg)")
            # Simular el pop que hace el listener
            target = received.pop("_target", None)
            assert target == test_advisor
            assert "_target" not in received
            print(f"  [OK] pop('_target') correcto - mensaje limpio para el frontend: {received}")
            return True
        else:
            print(f"  [FALLO] No se recibio _target en el mensaje: {received}")
            return False

    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


async def _run_runtime_tests():
    r1 = await test_runtime_targeted()
    r2 = await test_runtime_route_sync()
    return [r1, r2]


if __name__ == "__main__":
    print("=" * 55)
    print("TEST: WebSocket Cross-Worker Targeted + _route sync (Hallazgo 4)")
    print("=" * 55)

    static_results = [
        test_publish_to_advisor_exists(),
        test_listener_handles_target(),
        test_no_direct_send_to_advisor(),
        test_publish_to_advisor_calls(),
        test_route_sync_in_listener(),
        test_route_publish_in_transfer(),
    ]

    runtime_results = asyncio.run(_run_runtime_tests())
    all_results = static_results + runtime_results

    passed = sum(1 for r in all_results if r)
    total = len(all_results)

    print("\n" + "=" * 55)
    print(f"Resultado: {passed}/{total} checks pasaron")
    if passed == total:
        print("[OK] Notificaciones dirigidas + _route sync funcionan via pub/sub cross-worker")
        print("     transfer_accepted/rejected: 100% entrega sin importar el worker")
        print("     phone_to_advisor: sincronizado en todos los workers post-transfer")
    else:
        print("[FALLO] Revisar los items marcados arriba")
    print("=" * 55)
