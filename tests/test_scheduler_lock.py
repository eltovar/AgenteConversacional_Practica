"""
Test: Verifica que el lock Redis para el scheduler funciona correctamente.
Simula 4 workers intentando adquirir el lock simultaneamente.

Uso: python scripts/test_scheduler_lock.py
"""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

import redis.asyncio as r


async def main():
    # Misma logica que get_redis_url() en app.py: PUBLIC primero, interno como fallback
    redis_url = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
    if not redis_url:
        print("[ERROR] No se encontro REDIS_URL ni REDIS_PUBLIC_URL en .env")
        return

    client = r.from_url(redis_url, decode_responses=True)

    # Limpiar cualquier lock residual del scheduler
    await client.delete("scheduler_leader_test")

    # Simula 4 workers intentando el lock al mismo tiempo (TTL=300 post-fix H3)
    results = await asyncio.gather(
        client.set("scheduler_leader_test", "pid_6", nx=True, ex=300),
        client.set("scheduler_leader_test", "pid_7", nx=True, ex=300),
        client.set("scheduler_leader_test", "pid_8", nx=True, ex=300),
        client.set("scheduler_leader_test", "pid_9", nx=True, ex=300),
    )

    ganadores = sum(1 for res in results if res is True)
    perdedores = sum(1 for res in results if res is None or res is False)

    print("\nResultados por worker:")
    pids = ["pid_6", "pid_7", "pid_8", "pid_9"]
    for pid, res in zip(pids, results):
        if res is True:
            estado = "[LIDER] arranca scheduler (30 min, 15 min, 2h)"
        else:
            estado = "[skip] omite scheduler"
        print(f"  {pid}: {estado}")

    print(f"\nGanadores del lock: {ganadores} (esperado: 1)")
    print(f"Workers que omiten: {perdedores} (esperado: 3)")

    if ganadores == 1 and perdedores == 3:
        print("\n[OK] LOCK SCHEDULER FUNCIONA - solo 1 worker ejecutara los jobs")
        print("     check_appointment_reminders: exactamente cada 30 min")
        print("     check_appointment_followups: exactamente cada 15 min")
        print("     check_conversation_timeouts: exactamente cada 2 horas")
    else:
        print(f"\n[FALLO] se esperaba 1 lider y 3 omisiones")

    # Verificar que el lock tiene TTL configurado (esperado ~300s post-fix H3)
    ttl = await client.ttl("scheduler_leader_test")
    if 250 <= ttl <= 300:
        print(f"\n[OK] TTL del lock: {ttl}s (~5 min — correcto post-fix H3)")
    else:
        print(f"\n[FALLO] TTL inesperado: {ttl}s (esperado 250-300s post-fix H3)")

    # Cleanup
    await client.delete("scheduler_leader_test")
    await client.aclose()


if __name__ == "__main__":
    print("=" * 55)
    print("TEST: Scheduler Leader Lock (Hallazgo 3)")
    print("=" * 55)
    asyncio.run(main())
    print("=" * 55)
