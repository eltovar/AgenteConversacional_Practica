"""
Test: Verifica que el lock Redis para RAG funciona correctamente.
Simula 4 workers intentando adquirir el lock simultáneamente.

Uso: python scripts/test_rag_lock.py
"""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

import redis.asyncio as r


async def main():
    # Misma lógica que get_redis_url() en app.py: PUBLIC primero, interno como fallback
    redis_url = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL", "redis://localhost:6379")
    if not redis_url:
        print("❌ No se encontró REDIS_URL ni REDIS_PUBLIC_URL en .env")
        return

    client = r.from_url(redis_url, decode_responses=True)

    # Limpiar cualquier lock residual
    await client.delete("rag_kb_lock_test")

    # Simula 4 workers intentando el lock al mismo tiempo
    results = await asyncio.gather(
        client.set("rag_kb_lock_test", "pid_6", nx=True, ex=10),
        client.set("rag_kb_lock_test", "pid_7", nx=True, ex=10),
        client.set("rag_kb_lock_test", "pid_8", nx=True, ex=10),
        client.set("rag_kb_lock_test", "pid_9", nx=True, ex=10),
    )

    ganadores = sum(1 for res in results if res is True)
    perdedores = sum(1 for res in results if res is None or res is False)

    print("\nResultados por worker:")
    pids = ["pid_6", "pid_7", "pid_8", "pid_9"]
    for pid, res in zip(pids, results):
        estado = "[LIDER] indexa KB" if res is True else "[skip] omite indexacion"
        print(f"  {pid}: {estado}")

    print(f"\nGanadores del lock: {ganadores} (esperado: 1)")
    print(f"Workers que omiten: {perdedores} (esperado: 3)")

    if ganadores == 1 and perdedores == 3:
        print("\n[OK] LOCK FUNCIONA CORRECTAMENTE - solo 1 worker indexara en Railway")
    else:
        print(f"\n[FALLO] se esperaba 1 lider y 3 omisiones")

    # Cleanup
    await client.delete("rag_kb_lock_test")
    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
