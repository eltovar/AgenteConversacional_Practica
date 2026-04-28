#!/usr/bin/env python3
"""Diagnóstico: dump completo de un mensaje en Conversations API.

Uso:
    python scripts/diag_twilio_message.py <CONV_SID> <IM_SID> [CHAT_SERVICE_SID]

Imprime el JSON completo de:
  1. GET /Conversations/{conv}/Messages/{im}
  2. GET /Conversations/{conv}/Messages?Order=desc&PageSize=5
  3. (si es servicio) GET /Services/{svc}/Conversations/{conv}/Messages/{im}

Para identificar dónde guarda Twilio el WAMid del mensaje saliente.
"""
import asyncio
import json
import os
import sys

import httpx

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
BASE        = "https://conversations.twilio.com/v1"


async def dump(client: httpx.AsyncClient, label: str, url: str) -> None:
    print(f"\n=== {label} ===")
    print(f"GET {url}")
    r = await client.get(url, auth=(ACCOUNT_SID, AUTH_TOKEN))
    print(f"Status: {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    except Exception:
        print(r.text)


async def main() -> None:
    if len(sys.argv) < 3:
        print("Uso: diag_twilio_message.py <CONV_SID> <IM_SID> [CHAT_SERVICE_SID]")
        sys.exit(1)
    conv_sid = sys.argv[1]
    im_sid   = sys.argv[2]
    svc_sid  = sys.argv[3] if len(sys.argv) > 3 else None

    if not ACCOUNT_SID or not AUTH_TOKEN:
        print("Faltan TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN en env")
        sys.exit(1)

    async with httpx.AsyncClient(timeout=30.0) as client:
        if svc_sid:
            await dump(
                client,
                "Service-aware GET single message",
                f"{BASE}/Services/{svc_sid}/Conversations/{conv_sid}/Messages/{im_sid}",
            )
            await dump(
                client,
                "Service-aware LIST 5 messages",
                f"{BASE}/Services/{svc_sid}/Conversations/{conv_sid}/Messages?Order=desc&PageSize=5",
            )
        else:
            await dump(
                client,
                "Default GET single message",
                f"{BASE}/Conversations/{conv_sid}/Messages/{im_sid}",
            )
            await dump(
                client,
                "Default LIST 5 messages",
                f"{BASE}/Conversations/{conv_sid}/Messages?Order=desc&PageSize=5",
            )


if __name__ == "__main__":
    asyncio.run(main())
