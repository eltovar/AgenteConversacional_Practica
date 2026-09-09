"""
Exporta a Excel los contactos en "No responde" que pertenecen a una asesora.

PARA QUE
    La campana masiva de reactivacion va por 800+ enviados y quedan 483 contactos
    de Jubeny (owner 89096378) sin contactar. Este script arma la lista de trabajo
    con TODO lo que hace falta para decidir a quien escribir y en que orden, en vez
    de sacar una lista de telefonos a ciegas.

DE DONDE SALE CADA COSA
    HubSpot   quien es, telefono, correo, cuando entro, cuando se toco por ultima vez.
    MongoDB   que se le ha mandado y que ha contestado: masivos recibidos al
              embudo, primera y ultima vez, si alguna vez respondio, cuando hablo
              por ultima vez y por que canal.

    Los masivos se identifican igual que en la depuracion del embudo
    (`metadata.is_bulk_send` + `metadata.stage_id`), que es la definicion ya
    probada. No se inventa un criterio nuevo aqui.

PII
    El Excel lleva nombres y telefonos reales de clientes. Se escribe con prefijo
    `_PRIVADO_` en `scripts/salida/`, que esta en .gitignore. No subir a ningun
    sitio ni pegar su contenido en un chat.

Uso:
    python scripts/exportar_no_responde_jubeny.py
    python scripts/exportar_no_responde_jubeny.py --owner 89096380
    python scripts/exportar_no_responde_jubeny.py --etapa other --salida mi.xlsx
"""
import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(RAIZ, ".env"))

BASE_BUSQUEDA = "https://api.hubapi.com/crm/v3/objects/contacts/search"
LOTE = 100                  # maximo que admite el search de HubSpot
PETICIONES_POR_SEGUNDO = 3  # conservador: el panel comparte el presupuesto
REINTENTOS = 4

CARPETA_SALIDA = os.path.join(RAIZ, "scripts", "salida")

# Etapa "No responde" y asesora por defecto. Se pueden cambiar por argumento:
# los identificadores no se repiten sueltos por el codigo.
ETAPA_NO_RESPONDE = "other"
OWNER_JUBENY = "89096378"

PROPIEDADES = [
    "firstname", "lastname", "phone", "mobilephone", "email",
    "createdate", "lastmodifieddate", "hs_lead_status",
    "lifecyclestage", "hubspot_owner_id",
]


def _cabeceras() -> Dict[str, str]:
    clave = os.getenv("HUBSPOT_API_KEY")
    if not clave:
        sys.exit("Falta HUBSPOT_API_KEY. Revisa el .env.")
    return {"Authorization": f"Bearer {clave}", "Content-Type": "application/json"}


class Ritmo:
    """Espaciador simple: como mucho N peticiones por segundo."""

    def __init__(self, por_segundo: int):
        self.intervalo = 1.0 / max(por_segundo, 1)
        self.ultimo = 0.0

    def esperar(self) -> None:
        falta = self.intervalo - (time.monotonic() - self.ultimo)
        if falta > 0:
            time.sleep(falta)
        self.ultimo = time.monotonic()


def _buscar(cuerpo: dict, ritmo: Ritmo) -> dict:
    """
    POST al search de HubSpot, con reintentos.

    La red local corta conexiones cada tanto (WinError 10054) y HubSpot devuelve
    429 bajo carga. Un fallo aqui no puede dejar la lista a medias sin avisar, asi
    que se reintenta y, si aun asi falla, se propaga en vez de devolver vacio.
    """
    ultimo: Optional[Exception] = None
    for intento in range(REINTENTOS):
        ritmo.esperar()
        try:
            peticion = urllib.request.Request(
                BASE_BUSQUEDA, data=json.dumps(cuerpo).encode(), headers=_cabeceras()
            )
            with urllib.request.urlopen(peticion, timeout=45) as respuesta:
                return json.load(respuesta)
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code == 429 and intento < REINTENTOS - 1:
                time.sleep(3 * (intento + 1))
                continue
            raise
        except Exception as e:
            ultimo = e
            if intento < REINTENTOS - 1:
                time.sleep(2 * (intento + 1))
                continue
            raise
    raise ultimo  # type: ignore[misc]


def leer_contactos(etapa: str, owner: str) -> List[Dict[str, Any]]:
    """Todos los contactos de una etapa que pertenecen a una asesora."""
    ritmo = Ritmo(PETICIONES_POR_SEGUNDO)
    filtros = [
        {"propertyName": "lifecyclestage", "operator": "EQ", "value": etapa},
        {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner},
    ]
    contactos: List[Dict[str, Any]] = []
    despues: Optional[str] = None

    while True:
        cuerpo: Dict[str, Any] = {
            "filterGroups": [{"filters": filtros}],
            "properties": PROPIEDADES,
            "limit": LOTE,
            # Orden estable: sin sort, la paginacion de HubSpot puede repetir o
            # saltarse registros entre paginas.
            "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
        }
        if despues:
            cuerpo["after"] = despues

        datos = _buscar(cuerpo, ritmo)
        for c in datos.get("results", []):
            props = c.get("properties", {}) or {}
            props["contact_id"] = c.get("id", "")
            contactos.append(props)

        despues = (
            datos.get("paging", {}).get("next", {}).get("after")
            if datos.get("paging") else None
        )
        print(f"      {len(contactos)} contactos leidos...", end="\r")
        if not despues:
            break

    print(f"      {len(contactos)} contactos leidos.        ")
    return contactos


def _a_utc(momento: Optional[datetime]) -> Optional[datetime]:
    """
    Mongo devuelve datetimes naive que en realidad son UTC.

    Restarles un `datetime.now(TZ)` con zona lanza TypeError, y compararlos con un
    naive local miente por 5 horas. Se normaliza todo a UTC aware antes de restar.
    """
    if momento is None:
        return None
    if momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc)


async def leer_historial(etapa: str) -> Tuple[dict, dict, dict, dict]:
    """
    Historial por telefono: masivos al embudo, respuestas y ultimo movimiento.

    Devuelve (masivos, ultima_respuesta, total_respuestas, ultimo_canal).
    """
    from database.mongodb_client import get_mongo_manager

    gestor = get_mongo_manager()
    if not await gestor.connect():
        sys.exit("No se pudo conectar a MongoDB.")
    db = gestor.db

    masivos: Dict[str, List[datetime]] = defaultdict(list)
    async for m in db.messages.find(
        {"metadata.is_bulk_send": True, "metadata.stage_id": etapa},
        {"phone": 1, "timestamp": 1, "_id": 0},
    ):
        tel = (m.get("phone") or "").strip()
        if tel and m.get("timestamp"):
            masivos[tel].append(m["timestamp"])

    ultima_respuesta: Dict[str, datetime] = {}
    total_respuestas: Dict[str, int] = defaultdict(int)
    ultimo_canal: Dict[str, str] = {}
    async for m in db.messages.find(
        {"sender": "client"},
        {"phone": 1, "timestamp": 1, "channel": 1, "_id": 0},
    ):
        tel = (m.get("phone") or "").strip()
        marca = m.get("timestamp")
        if not tel or not marca:
            continue
        total_respuestas[tel] += 1
        if tel not in ultima_respuesta or marca > ultima_respuesta[tel]:
            ultima_respuesta[tel] = marca
            ultimo_canal[tel] = m.get("channel") or ""

    return masivos, ultima_respuesta, dict(total_respuestas), ultimo_canal


def _fecha(valor: Any) -> str:
    """Fecha legible a partir de un ISO de HubSpot o un datetime de Mongo."""
    if not valor:
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return str(valor)[:10]


def _dias_desde(momento: Optional[datetime], ahora: datetime) -> Optional[int]:
    momento = _a_utc(momento)
    if momento is None:
        return None
    return (ahora - momento).days


def armar_filas(
    contactos: List[Dict[str, Any]],
    masivos: dict,
    ultima_respuesta: dict,
    total_respuestas: dict,
    ultimo_canal: dict,
) -> List[Dict[str, Any]]:
    """Cruza cada contacto con su historial y decide su prioridad de envio."""
    from middleware.phone_normalizer import PhoneNormalizer

    normalizador = PhoneNormalizer()
    ahora = datetime.now(timezone.utc)
    filas = []

    for c in contactos:
        crudo = (c.get("phone") or c.get("mobilephone") or "").strip()
        resultado = normalizador.normalize(crudo) if crudo else None
        telefono = resultado.normalized if (resultado and resultado.is_valid) else crudo
        valido = bool(resultado and resultado.is_valid)

        envios = sorted(masivos.get(telefono, []))
        respuesta = ultima_respuesta.get(telefono)
        contesto_tras_ultimo = bool(envios and respuesta and _a_utc(respuesta) > _a_utc(envios[-1]))

        nombre = " ".join(
            p for p in [(c.get("firstname") or "").strip(), (c.get("lastname") or "").strip()] if p
        )

        filas.append({
            "contact_id": c.get("contact_id", ""),
            "nombre": nombre,
            "telefono": telefono,
            "telefono_valido": "si" if valido else "NO",
            "email": (c.get("email") or "").strip(),
            "canal_ultimo_mensaje": ultimo_canal.get(telefono, ""),
            "masivos_recibidos": len(envios),
            "primer_masivo": _fecha(envios[0]) if envios else "",
            "ultimo_masivo": _fecha(envios[-1]) if envios else "",
            "dias_desde_ultimo_masivo": _dias_desde(envios[-1], ahora) if envios else None,
            "respuestas_del_cliente": total_respuestas.get(telefono, 0),
            "alguna_vez_respondio": "si" if respuesta else "no",
            "ultima_respuesta": _fecha(respuesta),
            "dias_en_silencio": _dias_desde(respuesta, ahora) if respuesta else None,
            "contesto_al_ultimo_masivo": "si" if contesto_tras_ultimo else "no",
            "creado": _fecha(c.get("createdate")),
            "ultima_modificacion": _fecha(c.get("lastmodifieddate")),
            "estado_lead": (c.get("hs_lead_status") or "").strip(),
        })

    # Primero quien nunca ha recibido un masivo (es el objetivo de la campana),
    # despues los mas antiguos. Dentro de cada grupo, el silencio mas largo antes.
    filas.sort(key=lambda f: (
        f["masivos_recibidos"],
        -(f["dias_en_silencio"] or 0),
        f["nombre"],
    ))
    return filas


def escribir_excel(filas: List[Dict[str, Any]], ruta: str, etiqueta_owner: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    libro = Workbook()

    # ── Hoja 1: los contactos ────────────────────────────────────────────────
    hoja = libro.active
    hoja.title = "Contactos"
    columnas = list(filas[0].keys()) if filas else []
    hoja.append([c.replace("_", " ") for c in columnas])

    encabezado = Font(bold=True, color="FFFFFF")
    fondo = PatternFill("solid", fgColor="1A1A1A")
    for celda in hoja[1]:
        celda.font = encabezado
        celda.fill = fondo
        celda.alignment = Alignment(horizontal="center", vertical="center")

    aviso = PatternFill("solid", fgColor="FFF3CD")   # sin masivos todavia
    alerta = PatternFill("solid", fgColor="F8D7DA")  # telefono invalido
    for fila in filas:
        hoja.append([fila[c] for c in columnas])
        actual = hoja[hoja.max_row]
        if fila["telefono_valido"] == "NO":
            for celda in actual:
                celda.fill = alerta
        elif fila["masivos_recibidos"] == 0:
            for celda in actual:
                celda.fill = aviso

    for i, nombre in enumerate(columnas, start=1):
        ancho = max(len(nombre) + 2, *(len(str(f[nombre])) + 2 for f in filas)) if filas else 14
        hoja.column_dimensions[get_column_letter(i)].width = min(ancho, 38)
    hoja.freeze_panes = "A2"
    hoja.auto_filter.ref = hoja.dimensions

    # ── Hoja 2: el resumen que evita tener que contar a mano ─────────────────
    resumen = libro.create_sheet("Resumen")
    sin_masivo = sum(1 for f in filas if f["masivos_recibidos"] == 0)
    un_masivo = sum(1 for f in filas if f["masivos_recibidos"] == 1)
    dos_o_mas = sum(1 for f in filas if f["masivos_recibidos"] >= 2)
    respondieron = sum(1 for f in filas if f["alguna_vez_respondio"] == "si")
    invalidos = sum(1 for f in filas if f["telefono_valido"] == "NO")
    sin_telefono = sum(1 for f in filas if not f["telefono"])

    for etiqueta, valor in [
        ("Generado", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Asesora (owner)", etiqueta_owner),
        ("Etapa", "No responde"),
        ("", ""),
        ("Contactos en total", len(filas)),
        ("Sin ningun masivo todavia", sin_masivo),
        ("Con 1 masivo recibido", un_masivo),
        ("Con 2 o mas masivos", dos_o_mas),
        ("", ""),
        ("Hablaron alguna vez", respondieron),
        ("Nunca escribieron", len(filas) - respondieron),
        ("", ""),
        ("Telefono no valido", invalidos),
        ("Sin telefono", sin_telefono),
    ]:
        resumen.append([etiqueta, valor])
    for celda in resumen["A"]:
        celda.font = Font(bold=True)
    resumen.column_dimensions["A"].width = 30
    resumen.column_dimensions["B"].width = 22

    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    libro.save(ruta)


async def principal() -> None:
    analizador = argparse.ArgumentParser(description=__doc__)
    analizador.add_argument("--owner", default=OWNER_JUBENY,
                            help="hubspot_owner_id de la asesora (por defecto: %(default)s)")
    analizador.add_argument("--etapa", default=ETAPA_NO_RESPONDE,
                            help="lifecyclestage a exportar (por defecto: %(default)s)")
    analizador.add_argument("--salida", default=None, help="ruta del .xlsx")
    args = analizador.parse_args()

    from utils.advisors_registry import ADVISORS

    nombre_owner = (ADVISORS.get(args.owner) or {}).get("name", args.owner)
    etiqueta = f"{nombre_owner} ({args.owner})"

    print(f"\nExportando 'No responde' de {etiqueta}\n")

    print("[1/4] Leyendo contactos de HubSpot...")
    contactos = leer_contactos(args.etapa, args.owner)
    if not contactos:
        sys.exit("No hay contactos que exportar.")

    print("[2/4] Leyendo historial de masivos y respuestas en MongoDB...")
    masivos, respuestas, totales, canales = await leer_historial(args.etapa)
    print(f"      {len(masivos)} telefonos con al menos un masivo al embudo")

    print("[3/4] Cruzando...")
    filas = armar_filas(contactos, masivos, respuestas, totales, canales)

    ruta = args.salida or os.path.join(
        CARPETA_SALIDA,
        f"_PRIVADO_no_responde_{nombre_owner.lower()}_{datetime.now():%Y%m%d}.xlsx",
    )
    print("[4/4] Escribiendo Excel...")
    escribir_excel(filas, ruta, etiqueta)

    sin_masivo = sum(1 for f in filas if f["masivos_recibidos"] == 0)
    con_masivo = len(filas) - sin_masivo
    respondieron = sum(1 for f in filas if f["alguna_vez_respondio"] == "si")
    invalidos = sum(1 for f in filas if f["telefono_valido"] == "NO")

    print(f"\n  Contactos exportados       : {len(filas)}")
    print(f"  Sin ningun masivo todavia  : {sin_masivo}")
    print(f"  Ya recibieron algun masivo : {con_masivo}")
    print(f"  Hablaron alguna vez        : {respondieron}")
    print(f"  Telefono no valido         : {invalidos}")
    print(f"\n  {ruta}\n")


if __name__ == "__main__":
    asyncio.run(principal())
