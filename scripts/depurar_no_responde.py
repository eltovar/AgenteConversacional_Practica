#!/usr/bin/env python
"""
Depuración histórica del embudo "No responde" → "Cerrado perdido".

QUÉ HACE
    Busca contactos que recibieron 2 o más masivos de reactivación sin
    contestar, y los pasa al embudo terminal. Es la tarea que hoy las asesoras
    hacen a ojo, una por una.

    La regla NO vive aquí: la decide middleware/depuracion_no_responde.py, que
    es puro y está cubierto por tests. Este fichero solo lee, pagina, escribe y
    va contando. Cuando exista el job periódico usará el mismo motor.

DOS FORMAS DE CONTAR
    racha_seguida      (por defecto) solo los masivos posteriores a la última
                       respuesta del cliente. Es lo que se corrió el 18-ago.
    historial_completo todos los masivos del historial, estén seguidos o no.
                       Recoge a quien contestó "no gracias" a uno y calló ante
                       el siguiente. Amplía al anterior: nadie que saliera con
                       racha_seguida deja de salir aquí.

MODO DE EMPLEO
    Informe, no escribe nada (por defecto):
        python scripts/depurar_no_responde.py

    Informe con la regla ampliada:
        python scripts/depurar_no_responde.py --modo historial_completo

    Aplicar de verdad (pide confirmación por teclado):
        python scripts/depurar_no_responde.py --modo historial_completo --aplicar

    Prueba corta sobre los N primeros:
        python scripts/depurar_no_responde.py --aplicar --limite 5

CUÁNDO EJECUTARLO
    Fuera de horario laboral. El límite de HubSpot es por portal, no por
    proceso: estas llamadas compiten con las del panel lo mismo se lancen desde
    Railway o desde un portátil.

SEGURIDAD
  · Sin --aplicar no escribe nada en ningún sitio.
  · Ritmo limitado y corte automático si HubSpot empieza a devolver 429.
  · Reanudable: el estado se deriva de HubSpot en cada pasada, nunca de una
    marca propia. Si el proceso muere, se relanza y recalcula — no deja
    registros "reclamados" que puedan bloquear nada, que es exactamente lo que
    dejó muertas las campañas masivas 33 días.
  · El checkpoint solo evita repetir trabajo; no es la fuente de verdad.
"""
import argparse
import asyncio
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(RAIZ, ".env"))

from middleware.depuracion_no_responde import (  # noqa: E402
    BASES_DE_CONTEO,
    MODO_RACHA_SEGUIDA,
    Historial,
    Regla,
    evaluar,
)

# ── Parámetros de la corrida histórica ──────────────────────────────────────
# Los embudos se importan más abajo desde outbound_panel: son su fuente única.

# Decisión del 18-ago-2026: no depurar a quien su racha arranca en la campaña
# de julio. Solo había recibido un intento de reactivación cuando empezó.
EXCLUIR_RACHA_DESDE = datetime(2026, 6, 1)

BASE_HUBSPOT = "https://api.hubapi.com/crm/v3/objects/contacts"
LOTE_LECTURA = 100          # máximo que admite el batch read de HubSpot
PETICIONES_POR_SEGUNDO = 3  # conservador: el panel comparte el presupuesto
ERRORES_SEGUIDOS_PARA_ABORTAR = 5

CARPETA_SALIDA = os.path.join(RAIZ, "scripts", "salida")

# El informe lleva el modo en el nombre: cada corrida deja su propia evidencia
# en vez de pisar la de la anterior.
def _ruta_informe(modo: str) -> str:
    return os.path.join(CARPETA_SALIDA, f"depuracion_no_responde_informe_{modo}.csv")


# El checkpoint, en cambio, es UNO solo a propósito: es el registro de "a este
# ya se le movió", y eso no depende de con qué regla se decidió moverlo.
CHECKPOINT = os.path.join(CARPETA_SALIDA, "depuracion_no_responde_checkpoint.jsonl")


def _clave_hubspot() -> str:
    clave = os.getenv("HUBSPOT_API_KEY")
    if not clave:
        sys.exit("Falta HUBSPOT_API_KEY. Revisa el .env.")
    return clave


def _cabeceras() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_clave_hubspot()}",
        "Content-Type": "application/json",
    }


class Ritmo:
    """Espaciador simple: como mucho N peticiones por segundo."""

    def __init__(self, por_segundo: int):
        self._intervalo = 1.0 / max(por_segundo, 1)
        self._ultima = 0.0

    def esperar(self) -> None:
        falta = self._intervalo - (time.monotonic() - self._ultima)
        if falta > 0:
            time.sleep(falta)
        self._ultima = time.monotonic()


# ── Lectura ─────────────────────────────────────────────────────────────────

async def leer_historial(etapa_origen: str):
    """
    Devuelve, por teléfono: masivos al embudo, última respuesta y contact_id.
    Dos consultas a Mongo, sin traer el contenido de los mensajes.
    """
    from motor.motor_asyncio import AsyncIOMotorClient

    uri = os.getenv("MONGO_PUBLIC_URL") or os.getenv("MONGO_URL")
    if not uri:
        sys.exit("Falta MONGO_URL / MONGO_PUBLIC_URL. Revisa el .env.")

    cliente = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=30000)
    db = cliente.get_database("inmobiliaria_chat")

    masivos: Dict[str, List[datetime]] = defaultdict(list)
    contact_ids: Dict[str, str] = {}
    async for m in db.messages.find(
        {"metadata.is_bulk_send": True, "metadata.stage_id": etapa_origen},
        {"phone": 1, "timestamp": 1, "hubspot_contact_id": 1, "_id": 0},
    ):
        tel = m.get("phone")
        if not tel:
            continue
        masivos[tel].append(m["timestamp"])
        if m.get("hubspot_contact_id"):
            contact_ids.setdefault(tel, str(m["hubspot_contact_id"]))

    ultima_respuesta: Dict[str, datetime] = {}
    async for m in db.messages.find({"sender": "client"}, {"phone": 1, "timestamp": 1, "_id": 0}):
        tel, ts = m.get("phone"), m.get("timestamp")
        if not tel or not ts:
            continue
        if tel not in ultima_respuesta or ts > ultima_respuesta[tel]:
            ultima_respuesta[tel] = ts

    cliente.close()
    return masivos, ultima_respuesta, contact_ids


def leer_etapas(contact_ids: List[str], ritmo: Ritmo) -> Dict[str, Dict[str, str]]:
    """Lee lifecyclestage y nombre en lotes de 100."""
    salida: Dict[str, Dict[str, str]] = {}
    for i in range(0, len(contact_ids), LOTE_LECTURA):
        trozo = contact_ids[i:i + LOTE_LECTURA]
        cuerpo = json.dumps({
            "properties": ["lifecyclestage", "firstname", "lastname", "phone"],
            "inputs": [{"id": c} for c in trozo],
        }).encode()
        peticion = urllib.request.Request(
            f"{BASE_HUBSPOT}/batch/read", data=cuerpo, headers=_cabeceras()
        )
        for intento in range(3):
            ritmo.esperar()
            try:
                with urllib.request.urlopen(peticion, timeout=60) as resp:
                    for r in json.load(resp).get("results", []):
                        salida[r["id"]] = r.get("properties") or {}
                break
            except Exception as e:
                if intento == 2:
                    print(f"    aviso: lote {i // LOTE_LECTURA + 1} sin leer ({e})")
                else:
                    time.sleep(2 * (intento + 1))
        print(f"    leidos {min(i + LOTE_LECTURA, len(contact_ids))}/{len(contact_ids)}", end="\r")
    print()
    return salida


# ── Escritura ───────────────────────────────────────────────────────────────

def mover_etapa(contact_id: str, etapa_destino: str, ritmo: Ritmo) -> Optional[str]:
    """
    Cambia lifecyclestage en dos pasos: limpiar y fijar.

    HubSpot solo deja avanzar el lifecyclestage; sin el paso de limpieza un
    movimiento "hacia atrás" devuelve 200 y no cambia nada. Es el mismo
    procedimiento que usa el panel en PATCH /contacts/{id}/stage.
    """
    for valor in ("", etapa_destino):
        cuerpo = json.dumps({"properties": {"lifecyclestage": valor}}).encode()
        peticion = urllib.request.Request(
            f"{BASE_HUBSPOT}/{contact_id}", data=cuerpo,
            headers=_cabeceras(), method="PATCH",
        )
        ritmo.esperar()
        try:
            with urllib.request.urlopen(peticion, timeout=60) as resp:
                if resp.status != 200:
                    return f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code}"
        except Exception as e:
            return str(e)
    return None


async def cerrar_conversaciones(telefonos_visibles: Dict[str, List[str]]) -> int:
    """
    Cierra en el panel solo a quien siga visible. Reutiliza la función del panel
    en vez de replicarla, y NO emite WebSocket: 378 notificaciones seguidas
    provocarían un refresco de lista por contacto en cada asesora conectada.
    """
    if not telefonos_visibles:
        return 0
    from middleware.outbound_panel import _close_conversation_internal

    cerradas = 0
    for telefono, canales in telefonos_visibles.items():
        for canal in canales:
            try:
                await _close_conversation_internal(telefono, canal)
                cerradas += 1
            except Exception as e:
                print(f"    aviso: no se pudo cerrar {telefono[-4:]}:{canal} ({e})")
    return cerradas


async def visibles_en_panel(telefonos) -> Dict[str, List[str]]:
    """Lee el índice del panel UNA vez y devuelve los canales abiertos por teléfono."""
    import redis.asyncio as redis

    url = os.getenv("REDIS_PUBLIC_URL") or os.getenv("REDIS_URL")
    if not url:
        return {}
    r = redis.from_url(url, decode_responses=True, max_connections=5)
    try:
        miembros = await r.zrange("active_conversations_sorted", 0, -1)
    finally:
        await r.aclose()

    por_telefono: Dict[str, List[str]] = defaultdict(list)
    for m in miembros:
        tel, _, canal = m.rpartition(":")
        por_telefono[tel].append(canal)
    return {t: por_telefono[t] for t in telefonos if t in por_telefono}


# ── Checkpoint ──────────────────────────────────────────────────────────────

def leer_checkpoint() -> set:
    if not os.path.exists(CHECKPOINT):
        return set()
    hechos = set()
    with open(CHECKPOINT, "r", encoding="utf-8") as f:
        for linea in f:
            try:
                hechos.add(json.loads(linea)["contact_id"])
            except Exception:
                continue
    return hechos


def anotar_checkpoint(contact_id: str) -> None:
    """
    Anota SOLO los movimientos que salieron bien.

    Si se anotaran también los fallidos, el `continue` del relanzamiento los
    daría por hechos y quedarían sin depurar para siempre — justo lo contrario
    de para lo que sirve un checkpoint.
    """
    os.makedirs(CARPETA_SALIDA, exist_ok=True)
    with open(CHECKPOINT, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "contact_id": contact_id,
            "cuando": datetime.now().isoformat(timespec="seconds"),
        }) + "\n")


# ── Principal ───────────────────────────────────────────────────────────────

async def principal(aplicar: bool, limite: Optional[int], modo: str) -> int:
    from middleware.outbound_panel import (
        HUBSPOT_STAGE_CERRADO_PERDIDO,
        HUBSPOT_STAGE_NO_RESPONDE,
    )

    regla = Regla(
        etapa_origen=HUBSPOT_STAGE_NO_RESPONDE,
        etapa_destino=HUBSPOT_STAGE_CERRADO_PERDIDO,
        excluir_racha_desde=EXCLUIR_RACHA_DESDE,
        modo=modo,
    )
    ahora = datetime.now()
    ritmo = Ritmo(PETICIONES_POR_SEGUNDO)
    informe = _ruta_informe(regla.modo)

    print("\n" + "=" * 70)
    print(f"  DEPURACION  {regla.etapa_origen}  ->  {regla.etapa_destino}")
    print(f"  conteo: {regla.modo}")
    print(f"  modo: {'APLICAR (escribe en HubSpot)' if aplicar else 'INFORME (no escribe nada)'}")
    print("=" * 70)

    print("\n[1/5] Leyendo historial de masivos y respuestas en MongoDB...")
    masivos, respuestas, contact_ids = await leer_historial(regla.etapa_origen)
    print(f"      {len(masivos)} telefonos con al menos un masivo al embudo")

    # Preselección sin etapa: descarta el grueso antes de preguntar a HubSpot.
    preseleccion = []
    for telefono, envios in masivos.items():
        cid = contact_ids.get(telefono)
        if not cid:
            continue
        previa = evaluar(
            Historial(cid, envios, respuestas.get(telefono), regla.etapa_origen),
            regla, ahora,
        )
        if previa.depurar:
            preseleccion.append((telefono, cid, envios))
    print(f"      {len(preseleccion)} cumplen la regla antes de mirar su etapa actual")

    print("\n[2/5] Consultando la etapa actual de cada uno en HubSpot...")
    propiedades = leer_etapas([c for _, c, _ in preseleccion], ritmo)

    print("\n[3/5] Aplicando la regla completa...")
    a_depurar, descartados = [], defaultdict(int)
    for telefono, cid, envios in preseleccion:
        props = propiedades.get(cid)
        if props is None:
            descartados["no_legible_en_hubspot"] += 1
            continue
        decision = evaluar(
            Historial(cid, envios, respuestas.get(telefono), props.get("lifecyclestage")),
            regla, ahora,
        )
        if decision.depurar:
            nombre = f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip()
            a_depurar.append({
                "contact_id": cid,
                "telefono": telefono,
                "nombre": nombre or "(sin nombre)",
                "masivos_sin_responder": decision.masivos_contados,
                "primer_masivo": decision.primer_masivo.strftime("%Y-%m-%d"),
                "ultimo_masivo": decision.ultimo_masivo.strftime("%Y-%m-%d"),
                "ultima_respuesta": (
                    respuestas[telefono].strftime("%Y-%m-%d")
                    if telefono in respuestas else "nunca"
                ),
                "etapa_actual": props.get("lifecyclestage"),
            })
        else:
            descartados[decision.motivo] += 1

    a_depurar.sort(key=lambda x: (x["ultimo_masivo"], x["telefono"]))
    if limite:
        a_depurar = a_depurar[:limite]
        print(f"      --limite {limite}: se recortan a {len(a_depurar)}")

    print(f"\n      A DEPURAR: {len(a_depurar)}")
    if descartados:
        print("      descartados por:")
        for motivo, n in sorted(descartados.items(), key=lambda x: -x[1]):
            print(f"        {motivo:36s} {n}")

    os.makedirs(CARPETA_SALIDA, exist_ok=True)
    with open(informe, "w", encoding="utf-8-sig", newline="") as f:
        if a_depurar:
            escritor = csv.DictWriter(f, fieldnames=list(a_depurar[0].keys()))
            escritor.writeheader()
            escritor.writerows(a_depurar)
    print(f"\n[4/5] Informe escrito en:\n      {informe}")

    if not aplicar:
        print("\n[5/5] Modo informe: no se ha escrito nada en HubSpot ni en el panel.")
        print("      Revisa el CSV y, si estas conforme, relanza con --aplicar\n")
        return 0

    if not a_depurar:
        print("\n[5/5] Nada que aplicar.\n")
        return 0

    print(f"\n[5/5] Se van a mover {len(a_depurar)} contactos a '{regla.etapa_destino}'.")
    print("      Esta accion escribe en HubSpot.")
    if input("      Escribe DEPURAR para continuar: ").strip() != "DEPURAR":
        print("      Cancelado. No se ha tocado nada.\n")
        return 1

    hechos = leer_checkpoint()
    if hechos:
        print(f"      checkpoint: {len(hechos)} ya procesados en una corrida anterior")

    movidos = fallidos = 0
    errores_seguidos = 0
    for i, fila in enumerate(a_depurar, 1):
        cid = fila["contact_id"]
        if cid in hechos:
            continue
        error = mover_etapa(cid, regla.etapa_destino, ritmo)
        if not error:
            anotar_checkpoint(cid)
        if error:
            fallidos += 1
            errores_seguidos += 1
            print(f"      [{i}/{len(a_depurar)}] {cid} ERROR {error}")
            if errores_seguidos >= ERRORES_SEGUIDOS_PARA_ABORTAR:
                print("\n      ABORTADO: demasiados errores seguidos de HubSpot.")
                print("      El checkpoint queda guardado; relanza cuando se recupere.\n")
                break
        else:
            movidos += 1
            errores_seguidos = 0
            print(f"      [{i}/{len(a_depurar)}] {cid} movido", end="\r")

    print(f"\n\n      movidos: {movidos}   fallidos: {fallidos}")

    telefonos = [f["telefono"] for f in a_depurar]
    visibles = await visibles_en_panel(telefonos)
    if visibles:
        print(f"      cerrando {len(visibles)} conversaciones que seguian en el panel...")
        cerradas = await cerrar_conversaciones(visibles)
        print(f"      cerradas: {cerradas}")
    print()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aplicar", action="store_true",
                        help="escribe en HubSpot; sin este flag solo genera el informe")
    parser.add_argument("--limite", type=int, default=None,
                        help="procesa como mucho N contactos (para probar)")
    parser.add_argument("--modo", choices=sorted(BASES_DE_CONTEO), default=MODO_RACHA_SEGUIDA,
                        help="que masivos se cuentan (por defecto: %(default)s)")
    args = parser.parse_args()
    sys.exit(asyncio.run(principal(args.aplicar, args.limite, args.modo)))
