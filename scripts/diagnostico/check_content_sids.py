"""
Verifica contra Twilio que cada ContentSid del catálogo existe y sigue aprobado.

Why: un SID caducado no rompe el arranque ni el despliegue. Rompe el primer envío
real, con un HTTP 400 que solo se ve en los logs de Railway. Así se cayó el script
de campañas: quedó apuntando a un SID de la cuenta anterior a la migración del
4-ago-2026 y nadie lo supo hasta revisarlo a mano.

Es de solo lectura: un GET a content.twilio.com. No manda ningún mensaje.

Uso:
    python -m scripts.diagnostico.check_content_sids

Salida: 0 si todos los SIDs existen y están `approved`; 1 si falta alguno.
"""
import os
import sys

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Después de load_dotenv a propósito: content_sids lee las env vars al importarse.
from middleware.templates import content_sids as sids  # noqa: E402

API = "https://content.twilio.com/v1/ContentAndApprovals"


def _catalogo_twilio(account_sid: str, auth_token: str) -> dict:
    """{sid: (friendly_name, estado_de_aprobacion)} de toda la cuenta, paginado."""
    encontrados = {}
    url = f"{API}?PageSize=100"
    with httpx.Client(timeout=30) as client:
        while url:
            resp = client.get(url, auth=(account_sid, auth_token))
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("contents", []):
                aprobacion = item.get("approval_requests") or {}
                encontrados[item["sid"]] = (
                    item.get("friendly_name", "?"),
                    aprobacion.get("status", "sin_solicitud"),
                )
            url = (data.get("meta") or {}).get("next_page_url")
    return encontrados


def main() -> int:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not account_sid or not auth_token:
        print("✗ Faltan TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
        return 1

    en_twilio = _catalogo_twilio(account_sid, auth_token)
    nuestros = sids.all_sids()

    print(f"\nCuenta Twilio: {account_sid[:8]}…  —  {len(en_twilio)} plantillas\n")
    print(f"  {'constante':24} {'ContentSid':36} estado")
    print("  " + "─" * 78)

    fallos = 0
    for nombre in sorted(nuestros):
        sid = nuestros[nombre]
        if not sid:
            print(f"  {nombre:24} {'(vacío)':36} ⚠  sin SID configurado")
            continue
        if sid not in en_twilio:
            print(f"  {nombre:24} {sid:36} ✗  NO EXISTE en esta cuenta")
            fallos += 1
            continue
        friendly, estado = en_twilio[sid]
        marca = "✓" if estado == "approved" else "✗"
        if estado != "approved":
            fallos += 1
        print(f"  {nombre:24} {sid:36} {marca}  {estado}  ({friendly})")

    huerfanas = set(en_twilio) - {s for s in nuestros.values() if s}
    if huerfanas:
        print(f"\n  Aprobadas en Meta que el código no consume ({len(huerfanas)}):")
        for sid in sorted(huerfanas):
            friendly, estado = en_twilio[sid]
            print(f"    · {friendly:28} {sid}  [{estado}]")

    print()
    if fallos:
        print(f"✗ {fallos} SID(s) sin uso posible: no existen o no están aprobados.")
        return 1
    print("✓ Todos los ContentSid del catálogo existen y están aprobados.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
