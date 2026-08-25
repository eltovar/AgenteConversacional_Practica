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
from datetime import datetime, timezone
from typing import Dict, List, Optional

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(RAIZ, ".env"))

from middleware.depuracion_no_responde import (  # noqa: E402
    BASES_DE_CONTEO,
    MODO_RACHA_SEGUIDA,
    UMBRAL_MASIVOS_SIN_RESPUESTA,
    Decision,
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
REINTENTOS_ESCRITURA = 3    # los mismos que ya tenía la lectura

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
    Devuelve, por telefono: masivos al embudo, ultima respuesta y contact_id.

    La consulta NO vive aqui: la comparte con el job periodico
    (middleware/job_depuracion_masivos.py). Tenerla duplicada garantizaba que un
    dia el script y el job vieran cosas distintas y nadie supiera cual manda.

    Lo que si es propio de este fichero es la conexion: el script corre fuera del
    servidor y abre su propio cliente, mientras que el job usa el singleton de
    Mongo. Por eso la funcion compartida recibe la base de datos ya abierta.
    """
    from motor.motor_asyncio import AsyncIOMotorClient

    from middleware.job_depuracion_masivos import leer_historial_masivos

    uri = os.getenv("MONGO_PUBLIC_URL") or os.getenv("MONGO_URL")
    if not uri:
        sys.exit("Falta MONGO_URL / MONGO_PUBLIC_URL. Revisa el .env.")

    cliente = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=30000)
    try:
        return await leer_historial_masivos(
            cliente.get_database("inmobiliaria_chat"), etapa_origen
        )
    finally:
        cliente.close()


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


def leer_excluidos(ruta: Optional[str]) -> Dict[str, str]:
    """
    Contactos que NO se tocan pase lo que pase, con el motivo de cada uno.

    Es una anulación humana por encima de cualquier regla: los que contestaron
    algo al masivo y siguen vivos. El fichero se genera desde la clasificación
    de sus respuestas, no se escribe a mano, y el motivo viaja dentro para que
    dentro de tres meses se sepa por qué se salvó cada uno.
    """
    if not ruta:
        return {}
    if not os.path.exists(ruta):
        sys.exit(f"No existe el fichero de exclusion: {ruta}")
    excluidos: Dict[str, str] = {}
    with open(ruta, "r", encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            cid = (fila.get("contact_id") or "").strip()
            if cid:
                excluidos[cid] = (fila.get("motivo") or "sin_motivo").strip()
    if not excluidos:
        sys.exit(f"El fichero de exclusion no tiene ningun contact_id: {ruta}")
    return excluidos


def leer_por_owner(owner_id: str, etapa_origen: str, ritmo: Ritmo) -> Dict[str, str]:
    """
    Contactos del embudo de origen que pertenecen a un propietario. {id: telefono}

    Es un criterio ADMINISTRATIVO, no de negocio: "estos se gestionan por otro
    lado". Por eso vive aquí y no en el motor, que decide una sola cosa —si un
    lead está perdido por no responder— y tiene 73 tests defendiendo esa
    respuesta. Meter aquí dentro el propietario ensuciaría esa regla.

    Se filtra por etapa Y por owner en la misma consulta: pedir todos los del
    propietario y filtrar después traería contactos de todo el CRM.
    """
    encontrados: Dict[str, str] = {}
    despues = None
    while True:
        cuerpo = {
            "filterGroups": [{"filters": [
                {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
                {"propertyName": "lifecyclestage", "operator": "EQ", "value": etapa_origen},
            ]}],
            "properties": ["phone"],
            "limit": 100,
        }
        if despues:
            cuerpo["after"] = despues
        peticion = urllib.request.Request(
            f"{BASE_HUBSPOT}/search", data=json.dumps(cuerpo).encode(),
            headers=_cabeceras(),
        )
        ritmo.esperar()
        try:
            with urllib.request.urlopen(peticion, timeout=60) as resp:
                datos = json.load(resp)
        except Exception as e:
            print(f"    aviso: busqueda por owner interrumpida ({e})")
            break
        for r in datos.get("results", []):
            encontrados[r["id"]] = (r.get("properties") or {}).get("phone") or ""
        despues = (datos.get("paging", {}).get("next") or {}).get("after")
        print(f"    encontrados {len(encontrados)}", end="\r")
        if not despues:
            break
    print()
    return encontrados

# ── Escritura ───────────────────────────────────────────────────────────────

def _fijar_etapa(contact_id: str, valor: str, ritmo: Ritmo) -> Optional[str]:
    """
    Un PATCH de lifecyclestage, con reintentos. Devuelve None si fue bien.

    La lectura ya reintentaba 3 veces y la escritura ninguna. Esa asimetria
    costo un contacto el 19-ago-2026: un corte de red pasajero (WinError 10054)
    dejo el cambio a medias.
    """
    ultimo = None
    for intento in range(REINTENTOS_ESCRITURA):
        cuerpo = json.dumps({"properties": {"lifecyclestage": valor}}).encode()
        peticion = urllib.request.Request(
            f"{BASE_HUBSPOT}/{contact_id}", data=cuerpo,
            headers=_cabeceras(), method="PATCH",
        )
        ritmo.esperar()
        try:
            with urllib.request.urlopen(peticion, timeout=60) as resp:
                if resp.status == 200:
                    return None
                ultimo = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            ultimo = f"HTTP {e.code}"
            # Un 4xx que no sea 429 no se arregla insistiendo.
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:
            ultimo = str(e)
        if intento < REINTENTOS_ESCRITURA - 1:
            time.sleep(2 * (intento + 1))
    return ultimo


def mover_etapa(
    contact_id: str, etapa_origen: str, etapa_destino: str, ritmo: Ritmo
) -> Optional[str]:
    """
    Cambia lifecyclestage en dos pasos: limpiar y fijar.

    HubSpot solo deja avanzar el lifecyclestage; sin el paso de limpieza un
    movimiento "hacia atrás" devuelve 200 y no cambia nada. Es el mismo
    procedimiento que usa el panel en PATCH /contacts/{id}/stage.

    Entre los dos pasos el contacto no está en ningún embudo. Si el segundo
    falla hay que DESHACER, o queda invisible para todo el mundo: no sale en el
    panel y tampoco vuelve a entrar en la regla, que exige estar en el embudo de
    origen para tocarlo. Devolverlo a su sitio lo deja donde estaba y la
    siguiente pasada lo reintenta sola.
    """
    error = _fijar_etapa(contact_id, "", ritmo)
    if error:
        return error  # no se llego a tocar: sigue en su embudo

    error = _fijar_etapa(contact_id, etapa_destino, ritmo)
    if not error:
        return None

    vuelta = _fijar_etapa(contact_id, etapa_origen, ritmo)
    if vuelta:
        return f"{error}; ADEMAS quedo SIN ETAPA (no se pudo deshacer: {vuelta})"
    return f"{error} (deshecho: sigue en {etapa_origen})"


def rescatar_sin_etapa(contact_ids: List[str], etapa_origen: str, ritmo: Ritmo) -> int:
    """
    Devuelve al embudo de origen a quien se quedó sin etapa ninguna.

    Un contacto sin lifecyclestage no es una decisión de nadie: es una escritura
    que se rompió por la mitad. Se le devuelve a su embudo, no se le empuja al
    destino, para que pase por el mismo camino auditado que los demás.
    """
    rescatados = 0
    for cid in contact_ids:
        error = _fijar_etapa(cid, etapa_origen, ritmo)
        if error:
            print(f"    aviso: {cid} sigue sin etapa ({error})")
        else:
            rescatados += 1
    return rescatados


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

async def principal(
    aplicar: bool,
    limite: Optional[int],
    modo: str,
    umbral: int,
    owner: Optional[str],
    ruta_exclusion: Optional[str],
    corte_fecha: Optional[datetime],
) -> int:
    from middleware.outbound_panel import (
        HUBSPOT_STAGE_CERRADO_PERDIDO,
        HUBSPOT_STAGE_NO_RESPONDE,
    )

    regla = Regla(
        etapa_origen=HUBSPOT_STAGE_NO_RESPONDE,
        etapa_destino=HUBSPOT_STAGE_CERRADO_PERDIDO,
        umbral=umbral,
        excluir_racha_desde=corte_fecha,
        modo=modo,
    )
    excluidos = leer_excluidos(ruta_exclusion)
    # UTC, que es como MongoDB devuelve los timestamps de `messages` (los
    # guarda con zona y PyMongo los convierte). Con `datetime.now()` la
    # espera de 48h se corria tantas horas como la zona de quien lanzara
    # el script: desde un portatil en Bogota eran 53.
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    ritmo = Ritmo(PETICIONES_POR_SEGUNDO)
    informe = _ruta_informe(regla.modo)

    print("\n" + "=" * 70)
    print(f"  DEPURACION  {regla.etapa_origen}  ->  {regla.etapa_destino}")
    print(f"  conteo: {regla.modo} (umbral {regla.umbral})")
    print(f"  corte por fecha: {corte_fecha.strftime('%Y-%m-%d') if corte_fecha else 'ninguno'}")
    if owner:
        print(f"  ademas: todos los del propietario {owner}")
    if excluidos:
        print(f"  intocables: {len(excluidos)} contactos de la lista de exclusion")
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

    # Los del propietario entran aunque no cumplan la regla de masivos: el
    # criterio es otro ("se gestionan por otro lado"). Se marcan con envios=None
    # para que mas abajo se les pida solo estar en el embudo.
    por_owner: Dict[str, str] = {}
    if owner:
        print(f"\n[1b/5] Buscando contactos del propietario {owner} en el embudo...")
        por_owner = leer_por_owner(owner, regla.etapa_origen, ritmo)
        ya = {c for _, c, _ in preseleccion}
        nuevos = 0
        for cid, telefono in por_owner.items():
            if cid not in ya:
                preseleccion.append((telefono, cid, None))
                nuevos += 1
        print(f"      {len(por_owner)} del propietario, {nuevos} que la regla no cogia")

    print("\n[2/5] Consultando la etapa actual de cada uno en HubSpot...")
    propiedades = leer_etapas([c for _, c, _ in preseleccion], ritmo)

    print("\n[3/5] Aplicando la regla completa...")
    a_depurar, descartados = [], defaultdict(int)
    sin_etapa = []
    for telefono, cid, envios in preseleccion:
        if cid in excluidos:
            # Contesto algo al masivo y sigue vivo. Manda por encima de todo,
            # incluido el criterio del propietario.
            descartados["excluido_" + excluidos[cid]] += 1
            continue
        props = propiedades.get(cid)
        if props is None:
            descartados["no_legible_en_hubspot"] += 1
            continue
        # Sin etapa ninguna = escritura rota, no decisión de una asesora. No
        # entra en la regla (que exige el embudo de origen), así que si no se
        # rescata aquí se queda invisible para siempre.
        if not props.get("lifecyclestage"):
            sin_etapa.append(cid)
            continue
        if envios is None:
            # Vino por el propietario: basta con que siga en el embudo, cosa
            # que la lectura de arriba ya confirmo.
            decision = Decision(True, "propietario", 0, None, None)
        else:
            decision = evaluar(
                Historial(cid, envios, respuestas.get(telefono), props.get("lifecyclestage")),
                regla, ahora,
            )
        if decision.depurar:
            nombre = f"{props.get('firstname') or ''} {props.get('lastname') or ''}".strip()

            # Los que entran por propietario no tienen masivos, asi que no tienen
            # fechas. Se marcan con un guion en vez de reventar el informe.
            def _fecha(marca):
                return marca.strftime("%Y-%m-%d") if marca else "-"

            a_depurar.append({
                "contact_id": cid,
                "telefono": telefono,
                "nombre": nombre or "(sin nombre)",
                "motivo": decision.motivo,
                "masivos_sin_responder": decision.masivos_contados,
                "primer_masivo": _fecha(decision.primer_masivo),
                "ultimo_masivo": _fecha(decision.ultimo_masivo),
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
    if sin_etapa:
        print(f"      SIN ETAPA (escritura rota): {len(sin_etapa)}")
        print(f"        {', '.join(sin_etapa)}")
        print(f"        se devolveran a '{regla.etapa_origen}' si se aplica")

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

    if not a_depurar and not sin_etapa:
        print("\n[5/5] Nada que aplicar.\n")
        return 0

    print(f"\n[5/5] Se van a mover {len(a_depurar)} contactos a '{regla.etapa_destino}'.")
    if sin_etapa:
        print(f"      Y devolver {len(sin_etapa)} sin etapa a '{regla.etapa_origen}'.")
    print("      Esta accion escribe en HubSpot.")
    if input("      Escribe DEPURAR para continuar: ").strip() != "DEPURAR":
        print("      Cancelado. No se ha tocado nada.\n")
        return 1

    if sin_etapa:
        rescatados = rescatar_sin_etapa(sin_etapa, regla.etapa_origen, ritmo)
        print(f"      rescatados sin etapa: {rescatados}/{len(sin_etapa)}"
              f" (los movera la proxima pasada)")

    hechos = leer_checkpoint()
    if hechos:
        print(f"      checkpoint: {len(hechos)} ya procesados en una corrida anterior")

    movidos = fallidos = 0
    errores_seguidos = 0
    for i, fila in enumerate(a_depurar, 1):
        cid = fila["contact_id"]
        if cid in hechos:
            continue
        error = mover_etapa(cid, regla.etapa_origen, regla.etapa_destino, ritmo)
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
    parser.add_argument("--umbral", type=int, default=UMBRAL_MASIVOS_SIN_RESPUESTA,
                        help="cuantos masivos hacen falta (por defecto: %(default)s)")
    parser.add_argument("--owner", default=None,
                        help="anade TODOS los del propietario que esten en el embudo")
    parser.add_argument("--excluir", default=None, metavar="CSV",
                        help="fichero con contact_id que no se tocan nunca")
    # El corte por fecha nacio con umbral 2: no cerrar a quien empezaba su racha
    # en la campana de julio porque solo llevaba un intento. Con --umbral 1 ese
    # razonamiento se cae solo, asi que hay que poder quitarlo, pero a mano.
    parser.add_argument("--corte-fecha", default=EXCLUIR_RACHA_DESDE.strftime("%Y-%m-%d"),
                        metavar="AAAA-MM-DD",
                        help="no depurar si el primer masivo contado es de esta "
                             "fecha en adelante. 'ninguno' lo desactiva. "
                             "(por defecto: %(default)s)")
    parser.add_argument("--modo", choices=sorted(BASES_DE_CONTEO), default=MODO_RACHA_SEGUIDA,
                        help="que masivos se cuentan (por defecto: %(default)s)")
    args = parser.parse_args()
    if str(args.corte_fecha).strip().lower() in ("ninguno", "ninguna", "no", ""):
        corte = None
    else:
        try:
            corte = datetime.strptime(args.corte_fecha, "%Y-%m-%d")
        except ValueError:
            sys.exit(f"--corte-fecha no es una fecha AAAA-MM-DD: {args.corte_fecha}")

    sys.exit(asyncio.run(principal(
        args.aplicar, args.limite, args.modo, args.umbral, args.owner, args.excluir,
        corte,
    )))
