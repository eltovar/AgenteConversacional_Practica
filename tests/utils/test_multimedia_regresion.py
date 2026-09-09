"""El multimedia sobrevive a la capa de sanitización de logs y a la firma de Twilio.

Why: el commit `4572128` («sanitize operational logs») añadió a la vez el USO de
`safe_url()` en `outbound_panel.py` y un import que la dejaba fuera:

    from utils.safe_logging import safe_error, safe_id, safe_phone, safe_text
    ...
    logger.info(f"[Panel] Bunny.net URL obtenida: {safe_url(permanent_media_url)}")

Es un f-string, así que se evalúa ANTES de llamar a `logger`. Lanzaba `NameError`,
lo capturaba el `except Exception` del bloque de media, y salía por HTTP como
`500: Error procesando archivo multimedia: name 'safe_url' is not defined`.

Medido en producción el 5-sep-2026 (Railway, deploy 88df277c, rama
dev_juanrodriguez). La traza es la prueba de que el fallo ocurre DESPUÉS del
éxito, al registrar el éxito:

    14:46:19.588  [BunnyStorage] Archivo subido exitosamente: url:…#ac3708d2
    14:46:19.627  [BunnyStorage] ✅ CDN verificado (intento 1): … status=200
    14:46:19.627  [Panel] ERROR - Error procesando multimedia: name 'safe_url' is not defined

Bunny devolvía 200 y cinco microsegundos después reventaba la línea de log. Por
eso el panel mostraba un error de multimedia mientras los logs decían que el
archivo era compatible. Sólo afectaba al multimedia porque esas líneas viven
dentro de `if media_file and media_file.filename:` — el texto ni las toca.

El test que de verdad protege esto es el estructural (G1): detecta CUALQUIER
`safe_*` usado sin ligar, en cualquier archivo, no sólo el que falló esta vez.

Ejecutar: pytest tests/test_multimedia_regresion.py -v
"""

import ast
import builtins
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from starlette.requests import Request
from twilio.request_validator import RequestValidator

from middleware import webhook_handler as wh

TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
BASE_URL = "https://agenteconversacionalpractica-production.up.railway.app"
WEBHOOK_PATH = "/whatsapp/webhook"
FULL_URL = f"{BASE_URL}{WEBHOOK_PATH}"

FAKE_PHONE = "+573001234567"
MEDIA_URL = "https://api.twilio.com/2010-04-01/Accounts/ACxxx/Messages/MMxxx/Media/MExxx"
MEDIA_CT = "audio/ogg"

DIRS_IGNORADOS = {".git", "node_modules", ".claude", ".venv", "venv", "__pycache__"}


# ════════════════════════════════════════════════════════════════════
# G1 · Guardia estática — el que detecta la reaparición del fallo
# ════════════════════════════════════════════════════════════════════

def _nombres_ligados_y_usados(arbol: ast.AST):
    """Nombres `safe_*` ligados en el módulo, y los que se leen."""
    ligados, usados = set(dir(builtins)), set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                ligados.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                ligados.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ligados.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in ast.walk(n):
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    ligados.add(t.id)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id.startswith("safe_"):
            usados.add(n.id)
    return ligados, usados


@lru_cache(maxsize=1)
def _arboles_del_repo() -> tuple:
    """Parsea el repo una sola vez para toda la sesión de tests.

    Poda el árbol en vez de filtrar después: `rglob` entraría en
    `.claude/worktrees/`, que contiene copias completas del repositorio — el
    barrido tardaría minutos y contaría cada archivo seis veces. `os.walk`
    permite podar `dirnames` in situ y no descender siquiera.

    Se memoiza porque cuatro tests recorren el mismo árbol; sin la caché el
    parseo se repetía cuatro veces y el archivo tardaba el doble.
    """
    salida = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in DIRS_IGNORADOS]
        for nombre in filenames:
            if not nombre.endswith(".py"):
                continue
            ruta = Path(dirpath) / nombre
            try:
                salida.append((ruta, ast.parse(ruta.read_text(encoding="utf-8"))))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
    return tuple(salida)


def _modulos_del_repo():
    return _arboles_del_repo()


def test_g1_todo_safe_star_usado_esta_importado():
    """Un `safe_*` usado sin importar es un NameError en tiempo de ejecución.

    Éste es el guardia que faltaba. El barrido por «líneas de lógica» que se usó
    en la auditoría contaba el import como una línea inocua y no podía ver que
    estaba incompleto.
    """
    huerfanos = []
    for ruta, arbol in _modulos_del_repo():
        ligados, usados = _nombres_ligados_y_usados(arbol)
        faltan = usados - ligados
        if faltan:
            huerfanos.append(f"{ruta.relative_to(ROOT)}: {sorted(faltan)}")

    assert not huerfanos, (
        "safe_* usados sin importar — cada uno es un NameError que revienta la "
        "función que lo contiene:\n  " + "\n  ".join(huerfanos)
    )


def test_g1_el_guardia_no_esta_ciego():
    """Si el barrido no encuentra ningún `safe_*`, no está probando nada."""
    total = 0
    for _, arbol in _modulos_del_repo():
        _, usados = _nombres_ligados_y_usados(arbol)
        total += len(usados)
    assert total > 20, f"sólo se vieron {total} usos de safe_*: el guardia se quedó ciego"


def test_g1_outbound_panel_importa_safe_url():
    """El caso concreto que rompió el multimedia, fijado por su nombre."""
    from middleware import outbound_panel

    assert callable(getattr(outbound_panel, "safe_url", None)), (
        "outbound_panel.safe_url no resuelve. Las líneas del bloque de "
        "multimedia lanzarán NameError y el panel devolverá HTTP 500."
    )


METODOS_DE_LOG = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}


def _es_nombre_de_logger(ident: str) -> bool:
    """El proyecto no usa un único nombre: hay `logger` y `_diag_logger`.

    Cablear la palabra `logger` daba cuatro falsos positivos en `app.py`, donde
    el logger del handler de errores se llama `_diag_logger`.
    """
    limpio = ident.lower().lstrip("_")
    return limpio == "log" or limpio == "logger" or limpio.endswith("_logger")


def _es_llamada_a_logger(nodo) -> bool:
    """Llamada a un método de logging sobre algo que se llama como un logger.

    Se exige AMBAS cosas. Con sólo el nombre, un `mi_logger.guardar(safe_id(x))`
    quedaría absuelto y eso es justo lo que el guardia debe cazar.
    """
    return (
        isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Attribute)
        and nodo.func.attr in METODOS_DE_LOG
        and isinstance(nodo.func.value, ast.Name)
        and _es_nombre_de_logger(nodo.func.value.id)
    )


def _dentro_de_logger(nodo, padre) -> bool:
    """¿Algún ancestro, hasta la sentencia que lo contiene, es un `logger.*`?"""
    actual = nodo
    while actual in padre:
        actual = padre[actual]
        if _es_llamada_a_logger(actual):
            return True
        if isinstance(actual, ast.stmt):
            return False
    return False


def _ambito(nodo, padre):
    """Función o módulo que contiene al nodo."""
    actual = nodo
    while actual in padre:
        actual = padre[actual]
        if isinstance(actual, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            return actual
    return None


def _solo_se_consume_en_logs(nodo, padre) -> bool:
    """El `safe_*` se asigna a una variable que sólo se lee dentro de logs."""
    # La sentencia que contiene la llamada tiene que ser una asignación simple.
    actual = nodo
    while actual in padre and not isinstance(actual, ast.stmt):
        actual = padre[actual]
    if not (
        isinstance(actual, ast.Assign)
        and len(actual.targets) == 1
        and isinstance(actual.targets[0], ast.Name)
    ):
        return False

    nombre = actual.targets[0].id
    ambito = _ambito(nodo, padre)
    if ambito is None:
        return False

    # Mapa de padres local al ámbito, para clasificar cada lectura.
    padre_local = {}
    for m in ast.walk(ambito):
        for h in ast.iter_child_nodes(m):
            padre_local[h] = m

    lecturas = [
        m for m in ast.walk(ambito)
        if isinstance(m, ast.Name) and isinstance(m.ctx, ast.Load) and m.id == nombre
    ]
    if not lecturas:
        return False  # se asigna y no se usa: sospechoso, que lo mire un humano

    return all(_dentro_de_logger(l, padre_local) for l in lecturas)


def test_g1_ningun_safe_star_fuera_de_una_llamada_a_logger():
    """`safe_*` enmascara para LOGS. Fuera de un log, corrompe el dato.

    Es el otro patrón, distinto del anterior: en la primera auditoría se
    inyectaron `safe_id`/`safe_phone` dentro de `mark_visit_completed()` y
    escribían el valor enmascarado en Mongo (corregido en `af425be`).

    Se admiten los dos usos legítimos ya revisados: `app.py` construyendo un
    preview de texto y `crm_agent.py` dentro de un `RuntimeError`.
    """
    # `crm_agent.py` enmascara dentro del mensaje de un RuntimeError. No es un
    # log, pero tampoco corrompe un dato: es texto de excepción, revisado y
    # aceptado. Cualquier otro archivo tiene que pasar por mérito propio.
    EXCEPCIONES = {"crm_agent.py"}
    infractores = []

    for ruta, arbol in _modulos_del_repo():
        if ruta.name in EXCEPCIONES or ruta.parent.name == "tests":
            continue

        # Mapa hijo -> padre. Hace falta porque la pregunta no es «cómo es esta
        # llamada» sino «dentro de qué está»: el patrón A2 era
        # `mark_visit_completed(safe_id(x))`, una llamada simple (ast.Name), no
        # un `objeto.metodo()`. Mirar sólo las llamadas con atributo dejaba
        # fuera justo el caso que ocurrió.
        padre = {}
        for n in ast.walk(arbol):
            for h in ast.iter_child_nodes(n):
                padre[h] = n

        for n in ast.walk(arbol):
            if not (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id.startswith("safe_")
            ):
                continue

            # Caso 1: está dentro de la propia llamada a logger.*
            if _dentro_de_logger(n, padre):
                continue

            # Caso 2: se asigna a una variable que SÓLO se consume en logs.
            # Es un patrón legítimo y frecuente — se arma un fragmento y se
            # concatena en el logger de dos líneas más abajo:
            #
            #     name_info = f", Nombre: {safe_text(nombre, 40)}"
            #     logger.info(f"... {name_info}")
            #
            # (sofia_brain.py:395, app.py `body_preview`). No toca el dato: el
            # fragmento nunca sale del log. Si esa variable se usara en
            # cualquier otro sitio, esto NO la absuelve y el guardia dispara.
            if _solo_se_consume_en_logs(n, padre):
                continue

            infractores.append(
                f"{ruta.relative_to(ROOT)}:{n.lineno} -> {n.func.id}()"
            )

    assert not infractores, (
        "safe_* fuera de un logger: el valor enmascarado acabaría persistido "
        "o enviado como dato real:\n  " + "\n  ".join(sorted(set(infractores)))
    )


# ════════════════════════════════════════════════════════════════════
# G2 · Saliente del panel — el envío que se rompía
# ════════════════════════════════════════════════════════════════════

RUTA_PANEL = ROOT / "middleware" / "outbound_panel.py"


def _bloque_de_media_del_panel() -> ast.AST:
    """El cuerpo de `send_message`, que es donde vive el bloque de multimedia."""
    arbol = ast.parse(RUTA_PANEL.read_text(encoding="utf-8"))
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "send_message":
            return n
    pytest.fail("no se encontró send_message() en outbound_panel.py")


def test_g2_el_bloque_de_media_no_usa_nombres_sin_ligar():
    """Ningún nombre del bloque de media puede quedar sin resolver.

    Reproduce la condición exacta del fallo: el `except Exception` de ese bloque
    convierte cualquier NameError en un HTTP 500 que la asesora lee como
    «error procesando archivo multimedia».
    """
    from middleware import outbound_panel

    fn = _bloque_de_media_del_panel()
    modulo = vars(outbound_panel)
    sin_resolver = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id.startswith("safe_") and n.id not in modulo:
                sin_resolver.add(n.id)

    assert not sin_resolver, (
        f"send_message() usa {sorted(sin_resolver)} sin que el módulo los "
        "resuelva: todo envío de multimedia devolverá 500."
    )


def test_g2_safe_url_es_invocable_sobre_una_url_de_bunny():
    """La función tiene que tolerar la URL real que devuelve Bunny."""
    from middleware.outbound_panel import safe_url

    url = "https://inmobiliaria-media.b-cdn.net/audios_asesores/573001112233_1788620944.mp3"
    salida = safe_url(url)
    assert isinstance(salida, str) and salida
    assert "573001112233" not in salida, "safe_url dejó pasar el teléfono del cliente"
    assert "b-cdn.net" in salida, "safe_url perdió el host: el log deja de servir"


def test_g2_las_lineas_de_log_del_bloque_de_media_siguen_ahi():
    """El arreglo es importar, no borrar el log.

    Si alguien «arregla» esto quitando las llamadas, el enmascaramiento anti-PII
    desaparece y volvemos a volcar la URL completa de Bunny.
    """
    fuente = RUTA_PANEL.read_text(encoding="utf-8")
    assert fuente.count("safe_url(") >= 3, (
        "se esperaban al menos 3 usos de safe_url() en outbound_panel.py; "
        "si se borraron, la URL completa vuelve a los logs"
    )


# ════════════════════════════════════════════════════════════════════
# G3 · Compatibilidad firma de Twilio ↔ multimedia entrante
# ════════════════════════════════════════════════════════════════════

class _DummyBackgroundTasks:
    """Doble de BackgroundTasks: registra sin ejecutar nada."""

    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


def _make_request(headers: dict, body: bytes, query_string: bytes = b"") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": WEBHOOK_PATH,
        "query_string": query_string,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "scheme": "https",
        "server": ("agenteconversacionalpractica-production.up.railway.app", 443),
    }
    return Request(scope, receive)


def _params_media_legacy() -> dict:
    """Entrante con media, formato Programmable Messaging (legacy)."""
    return {
        "From": f"whatsapp:{FAKE_PHONE}",
        "Body": "",
        "MessageSid": "MM00000000000000000000000000000001",
        "NumMedia": "1",
        "MediaUrl0": MEDIA_URL,
        "MediaContentType0": MEDIA_CT,
    }


def _peticion_media_firmada(params: dict):
    """Petición form-encoded con media y firma VÁLIDA."""
    sig = RequestValidator(TOKEN).compute_signature(FULL_URL, params)
    body = urlencode(params).encode()
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-twilio-signature": sig,
    }
    return _make_request(headers, body), body


async def test_g3_firma_valida_sobre_un_entrante_con_multimedia(monkeypatch):
    """Un mensaje con media firmado por Twilio valida igual que uno de texto.

    Los campos de media (NumMedia, MediaUrl0, MediaContentType0) entran en el
    cálculo de la firma. Si alguien los tocara antes de validar, la firma
    dejaría de calzar y en modo `enforce` se rechazaría el 100% del multimedia.
    """
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    req, body = _peticion_media_firmada(_params_media_legacy())
    assert await wh._twilio_signature_is_valid(req, body, is_json=False) is True


async def test_g3_media_alterada_invalida_la_firma(monkeypatch):
    """Cambiar la MediaUrl0 después de firmar tiene que romper la validación."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    params = _params_media_legacy()
    sig = RequestValidator(TOKEN).compute_signature(FULL_URL, params)

    alterado = dict(params, MediaUrl0="https://atacante.example/payload.exe")
    body = urlencode(alterado).encode()
    req = _make_request(
        {"content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig},
        body,
    )
    assert await wh._twilio_signature_is_valid(req, body, is_json=False) is False


async def test_g3_la_validacion_de_firma_no_consume_los_campos_de_media(monkeypatch):
    """El punto de contacto real entre firma y multimedia.

    `_twilio_signature_is_valid` hace `await request.form()` para validar. El
    handler ya había consumido el cuerpo con `await request.body()` y repuesto
    `request._receive` con `_replay_body`. Si ese replay fallara, el parseo
    posterior vería un form vacío y la media se perdería en silencio: el cliente
    manda un audio y en el panel no aparece nada.
    """
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "log_only")

    req, _ = _peticion_media_firmada(_params_media_legacy())
    bg = _DummyBackgroundTasks()
    await wh.whatsapp_webhook(req, bg)

    assert bg.tasks, "el entrante con media no llegó a encolarse"
    _, args, _kwargs = bg.tasks[0]
    assert MEDIA_URL in args, (
        "la MediaUrl0 no sobrevivió a la validación de firma: el form se "
        "consumió y el replay del cuerpo no la repuso"
    )
    assert MEDIA_CT in args, "el content-type de la media se perdió"


async def test_g3_enforce_con_firma_valida_deja_pasar_la_media(monkeypatch):
    """En `enforce`, un multimedia legítimo NO puede quedar bloqueado."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "enforce")

    req, _ = _peticion_media_firmada(_params_media_legacy())
    bg = _DummyBackgroundTasks()
    resp = await wh.whatsapp_webhook(req, bg)

    assert resp.status_code != 403, "una media firmada correctamente fue rechazada"
    assert bg.tasks, "la media firmada no se encoló para procesar"


# ════════════════════════════════════════════════════════════════════
# G4 · Las tres ramas del parser conservan la media
# ════════════════════════════════════════════════════════════════════

async def test_g4_rama_legacy_conserva_la_media(monkeypatch):
    """Programmable Messaging: NumMedia/MediaUrl0/MediaContentType0 planos."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")

    body = urlencode(_params_media_legacy()).encode()
    req = _make_request({"content-type": "application/x-www-form-urlencoded"}, body)
    bg = _DummyBackgroundTasks()
    await wh.whatsapp_webhook(req, bg)

    assert bg.tasks, "la rama legacy no encoló el entrante con media"
    _, args, _ = bg.tasks[0]
    assert MEDIA_URL in args and MEDIA_CT in args


async def test_g4_rama_json_conversations_conserva_la_media(monkeypatch):
    """Conversations API por JSON."""
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")

    payload = {
        "Author": f"whatsapp:{FAKE_PHONE}",
        "Body": "",
        "MessageSid": "MM00000000000000000000000000000002",
        "NumMedia": "1",
        "MediaUrl0": MEDIA_URL,
        "MediaContentType0": MEDIA_CT,
    }
    body = json.dumps(payload).encode()
    req = _make_request({"content-type": "application/json"}, body)
    bg = _DummyBackgroundTasks()
    await wh.whatsapp_webhook(req, bg)

    assert bg.tasks, "la rama JSON no encoló el entrante con media"
    _, args, _ = bg.tasks[0]
    assert MEDIA_URL in args and MEDIA_CT in args


def test_g4_las_tres_ramas_parsean_los_campos_de_media():
    """Guardia estructural: si nace una cuarta rama sin media, falla.

    Las tres ramas del parser (JSON Conversations, form Conversations, legacy)
    leen NumMedia/MediaUrl0/MediaContentType0. Una rama nueva que no lo haga
    descartaría el multimedia en silencio.
    """
    fuente = (ROOT / "middleware" / "webhook_handler.py").read_text(encoding="utf-8")
    assert fuente.count("MediaUrl0 ") + fuente.count("MediaUrl0=") >= 3, (
        "se esperaban al menos 3 ramas del parser leyendo MediaUrl0"
    )
    assert "MediaContentType0" in fuente


# ════════════════════════════════════════════════════════════════════
# G5 · BSUID + multimedia — se afirma el comportamiento, no se cambia
# ════════════════════════════════════════════════════════════════════

BSUID_FROM = "whatsapp:CO.ABCD1234EFGH5678"


async def test_g5_un_bsuid_con_media_conserva_la_media(monkeypatch):
    """La rama BSUID no puede perder el archivo por el camino.

    NO modifica la lógica de BSUID: sólo fija que, con la feature activa (como
    está en producción), un entrante BSUID con multimedia llega a
    `_process_message_deferred` con su MediaUrl0 intacta.
    """
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setattr(wh, "_feature_whatsapp_bsuid_support_enabled", lambda: True)

    params = dict(_params_media_legacy(), From=BSUID_FROM)
    body = urlencode(params).encode()
    req = _make_request({"content-type": "application/x-www-form-urlencoded"}, body)
    bg = _DummyBackgroundTasks()
    await wh.whatsapp_webhook(req, bg)

    assert bg.tasks, "un BSUID con media no se encoló"
    _, args, kwargs = bg.tasks[0]
    assert MEDIA_URL in args, "la rama BSUID perdió la MediaUrl0"
    assert kwargs.get("identity_type") == "bsuid", (
        "el entrante BSUID no se marcó como tal: se estaría tratando como teléfono"
    )


async def test_g5_el_safe_stop_de_bsuid_sigue_intacto(monkeypatch):
    """Con la feature apagada, un BSUID no entra al pipeline telefónico.

    Fija la garantía de seguridad que cierra la rama del «10% inventa un
    teléfono». Este test NO aprueba el descarte silencioso (es el defecto B-b
    del ADR-PLAT-001, pendiente): sólo impide que el arreglo del multimedia
    reabra por accidente la normalización errónea.
    """
    monkeypatch.setattr(wh, "TWILIO_WEBHOOK_BASE_URL", BASE_URL)
    monkeypatch.setattr(wh, "TWILIO_SIGNATURE_MODE", "off")
    monkeypatch.setattr(wh, "_feature_whatsapp_bsuid_support_enabled", lambda: False)

    params = dict(_params_media_legacy(), From=BSUID_FROM)
    body = urlencode(params).encode()
    req = _make_request({"content-type": "application/x-www-form-urlencoded"}, body)
    bg = _DummyBackgroundTasks()
    await wh.whatsapp_webhook(req, bg)

    assert not bg.tasks, (
        "un BSUID llegó al pipeline con la feature apagada: el safe-stop se rompió"
    )
