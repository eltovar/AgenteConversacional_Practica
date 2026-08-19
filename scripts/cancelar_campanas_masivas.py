#!/usr/bin/env python
"""
Cancela campañas masivas encoladas sin enviar lo que quede pendiente.

POR QUÉ EXISTE
    Una campaña puede quedarse encolada semanas — por un atasco del procesador
    o porque simplemente ya no interesa. Enviarla entonces significa que el
    cliente recibe un mensaje comercial fuera de contexto. Esto la cierra sin
    enviar y libera la cola.

    Los contactos sin enviar quedan como "cancelled", no "failed": la
    diferencia es si se intentó escribirles o no.

MODO DE EMPLEO
    Ver qué campañas hay encoladas (no escribe nada):
        python scripts/cancelar_campanas_masivas.py

    Cancelar por id (pide confirmación):
        python scripts/cancelar_campanas_masivas.py <id> [<id> ...] --motivo "..."
"""
import argparse
import asyncio
import os
import sys
from collections import Counter

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(RAIZ, ".env"))


async def listar(mongo) -> list:
    activas = await mongo.get_active_bulk_campaigns(limit=50)
    if not activas:
        print("  No hay campañas encoladas.")
        return []
    print(f"  {len(activas)} campaña(s) encolada(s), de la más antigua a la más nueva:\n")
    for c in activas:
        estados = Counter(x.get("status") for x in c.get("contacts", []))
        sin_enviar = estados.get("pending", 0) + estados.get("in_progress", 0)
        print(f"    id      : {c['_id']}")
        print(f"    creada  : {str(c.get('created_at'))[:16]}   status: {c.get('status')}")
        print(f"    embudo  : {c.get('stage_id')}   plantilla: {c.get('template_id')}")
        print(f"    estados : {dict(estados)}")
        print(f"    -> se cancelarian {sin_enviar} envios pendientes\n")
    return activas


async def principal(ids: list, motivo: str) -> int:
    from database.mongodb_client import get_mongo_manager

    mongo = get_mongo_manager()

    print("\n" + "=" * 70)
    print("  CANCELACION DE CAMPANAS MASIVAS")
    print("=" * 70 + "\n")

    activas = await listar(mongo)

    if not ids:
        print("  Modo consulta: no se ha cancelado nada.")
        print("  Para cancelar:  python scripts/cancelar_campanas_masivas.py <id> --motivo \"...\"\n")
        return 0

    conocidas = {str(c["_id"]) for c in activas}
    desconocidas = [i for i in ids if i not in conocidas]
    if desconocidas:
        print(f"  AVISO: estos ids no estan entre las campanas encoladas: {desconocidas}")
        print("  (puede que ya estuvieran cerradas)\n")

    print(f"  Se van a cancelar {len(ids)} campana(s).")
    print(f"  Motivo: {motivo}")
    if input("  Escribe CANCELAR para continuar: ").strip() != "CANCELAR":
        print("  Cancelado. No se ha tocado nada.\n")
        return 1

    for campaign_id in ids:
        r = await mongo.cancel_bulk_campaign(campaign_id, motivo)
        if r["ya_estaba_cerrada"]:
            print(f"    {campaign_id}: ya estaba cerrada, sin cambios")
        else:
            print(f"    {campaign_id}: cerrada — {r['cancelados']} envios cancelados")

    print("\n  Estado despues de cancelar:\n")
    await listar(mongo)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="*", help="ids de campana a cancelar")
    parser.add_argument("--motivo", default="Cancelada por quedar obsoleta en la cola",
                        help="queda registrado en la campana")
    args = parser.parse_args()
    sys.exit(asyncio.run(principal(args.ids, args.motivo)))
