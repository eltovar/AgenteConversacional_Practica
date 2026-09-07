"""
Profiler de consultas — el equivalente de P6SPY (Java) para este stack.

Mide dónde se va el tiempo de cada request del panel, desglosado por capa:
MongoDB, HubSpot (httpx) y el total del endpoint. Sin esto, decidir qué
optimizar es adivinar.

Tres piezas independientes, todas apagables:

  1. MongoCommandLogger  — pymongo.monitoring.CommandListener. Loguea cada
                           comando Mongo con su duración real medida por el
                           driver. Es literalmente lo que hace P6Spy con SQL.
  2. httpx event_hooks   — latencia por llamada a HubSpot.
  3. ServerTimingMiddleware — acumula ambos por request y emite el header
                           `Server-Timing`, que Chrome DevTools grafica
                           nativamente en la pestaña Network.

⚠️ PII (CLAUDE.md regla 2): los eventos de PyMongo traen el filtro completo
de la query, que incluye teléfonos y nombres de clientes. NUNCA se loguea el
filtro — solo nombre de comando, colección y duración. Las URLs de HubSpot
se recortan a su path sin query string por la misma razón.

Sin dependencias nuevas: pymongo.monitoring viene en pymongo, event_hooks en
httpx. Cero llamadas de red adicionales.

Control por entorno:
    QUERY_PROFILER_ENABLED   "true"/"false"  (default: true)
    QUERY_PROFILER_SLOW_MS   umbral de log en ms (default: 50)
    SERVER_TIMING_ENABLED    header Server-Timing (default: true)
"""
import contextvars
import logging
import os
import re
import time
from typing import Any, Dict, Optional

from utils.safe_logging import obs_event

logger = logging.getLogger(__name__)


# Rachas de 6+ dígitos en una URL = teléfono o ID de contacto. El panel tiene
# rutas como /contacts/{phone}/detail, /conversations/{phone}, /reset-bot/{phone},
# así que el path CRUDO lleva PII y no puede ir a los logs (CLAUDE.md regla 2).
_PII_IN_PATH = re.compile(r"\+?\d{6,}")


def _safe_path(path: str) -> str:
    """
    Enmascara identificadores largos en un path antes de loguearlo.

    '/panel/contacts/+573138405930/detail' → '/panel/contacts/{id}/detail'

    Además de proteger PII, agrupa: todas las llamadas a esa ruta se
    agregan bajo la misma etiqueta en vez de aparecer como miles de paths
    distintos.
    """
    try:
        return _PII_IN_PATH.sub("{id}", path)
    except Exception:
        return "?"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


PROFILER_ENABLED = _env_bool("QUERY_PROFILER_ENABLED", True)
SLOW_MS = float(os.getenv("QUERY_PROFILER_SLOW_MS", "50"))
SERVER_TIMING_ENABLED = _env_bool("SERVER_TIMING_ENABLED", True)

# Comandos internos del driver — ruido puro, no son consultas de negocio.
_IGNORED_COMMANDS = frozenset({
    "ismaster", "hello", "ping", "buildInfo", "getLastError",
    "saslStart", "saslContinue", "authenticate", "endSessions",
    "createIndexes", "listIndexes",
})


# ═══════════════════════════════════════════════════════════════════════════
# Acumulador por request (contextvars — seguro con asyncio concurrente)
# ═══════════════════════════════════════════════════════════════════════════
# Un ContextVar se propaga a las tasks hijas de un request pero NO se comparte
# entre requests concurrentes, que es exactamente lo que necesitamos. Un dict
# global se mezclaría entre asesoras usando el panel a la vez.

_request_stats: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "query_profiler_stats", default=None
)


def start_request_profile() -> Dict[str, Any]:
    """Inicia el acumulador del request actual. Lo llama el middleware."""
    stats: Dict[str, Any] = {
        "mongo_ms": 0.0, "mongo_count": 0,
        "hubspot_ms": 0.0, "hubspot_count": 0,
    }
    _request_stats.set(stats)
    return stats


def get_request_profile() -> Optional[Dict[str, Any]]:
    """Stats del request en curso, o None si no hay profiling activo."""
    return _request_stats.get()


def _record(layer: str, elapsed_ms: float) -> None:
    """Suma al acumulador del request. No-op fuera de un request."""
    stats = _request_stats.get()
    if stats is None:
        return
    stats[f"{layer}_ms"] = stats.get(f"{layer}_ms", 0.0) + elapsed_ms
    stats[f"{layer}_count"] = stats.get(f"{layer}_count", 0) + 1


# ═══════════════════════════════════════════════════════════════════════════
# 1. MongoDB — CommandListener (el P6Spy de este stack)
# ═══════════════════════════════════════════════════════════════════════════

def _build_mongo_listener():
    """
    Construye el listener. Import diferido para que este módulo pueda
    importarse en entornos sin pymongo (tests de análisis estático).
    """
    from pymongo import monitoring

    class MongoCommandLogger(monitoring.CommandListener):
        """
        Loguea duración de cada comando Mongo.

        ⚠️ `event.command` contiene el filtro con PII (teléfonos, nombres).
        Solo se leen `command_name`, el nombre de la colección y la duración.
        El filtro NUNCA se toca.
        """

        @staticmethod
        def _collection(event) -> str:
            """Nombre de colección sin exponer nada del filtro."""
            try:
                # El primer valor del comando es el nombre de la colección
                # en find/aggregate/update/etc. Es un string, nunca PII.
                value = event.command.get(event.command_name)
                return value if isinstance(value, str) else "?"
            except Exception:
                return "?"

        def started(self, event):
            pass

        def succeeded(self, event):
            if event.command_name in _IGNORED_COMMANDS:
                return
            ms = event.duration_micros / 1000.0
            _record("mongo", ms)
            if ms >= SLOW_MS:
                logger.warning(
                    "[QueryProfiler][Mongo] SLOW %s on %s — %.1fms",
                    event.command_name, self._collection(event), ms,
                )
                logger.warning(
                    obs_event(
                        "mongo",
                        "query_profiler",
                        "mongo_slow",
                        status="slow",
                        command=event.command_name,
                        collection=self._collection(event),
                        duration_ms=round(ms),
                    )
                )

        def failed(self, event):
            if event.command_name in _IGNORED_COMMANDS:
                return
            ms = event.duration_micros / 1000.0
            _record("mongo", ms)
            logger.error(
                "[QueryProfiler][Mongo] FAILED %s — %.1fms",
                event.command_name, ms,
            )
            logger.error(
                obs_event(
                    "mongo",
                    "query_profiler",
                    "mongo_failed",
                    status="failed",
                    command=event.command_name,
                    duration_ms=round(ms),
                )
            )

    return MongoCommandLogger()


_mongo_listener_registered = False


def register_mongo_listener() -> bool:
    """
    Registra el listener global de PyMongo. Idempotente.

    pymongo.monitoring.register() es process-wide y NO se puede desregistrar,
    por eso la guarda: registrarlo dos veces duplicaría cada medición.

    Returns:
        True si quedó registrado (ahora o antes), False si está desactivado.
    """
    global _mongo_listener_registered
    if not PROFILER_ENABLED:
        return False
    if _mongo_listener_registered:
        return True
    try:
        from pymongo import monitoring
        monitoring.register(_build_mongo_listener())
        _mongo_listener_registered = True
        logger.info(
            "[QueryProfiler] Listener MongoDB registrado (umbral %.0fms)", SLOW_MS
        )
        return True
    except Exception as e:
        # Nunca romper el arranque por instrumentación
        logger.warning("[QueryProfiler] No se pudo registrar listener Mongo: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 2. HubSpot — event hooks de httpx
# ═══════════════════════════════════════════════════════════════════════════

async def _on_request(request):
    request.extensions["_qp_start"] = time.perf_counter()


async def _on_response(response):
    start = response.request.extensions.get("_qp_start")
    if start is None:
        return
    ms = (time.perf_counter() - start) * 1000.0
    _record("hubspot", ms)
    if ms >= SLOW_MS:
        # Solo path — el query string puede llevar teléfonos/emails.
        logger.warning(
            "[QueryProfiler][HubSpot] SLOW %s %s → %s — %.1fms",
            response.request.method,
            _safe_path(response.request.url.path),
            response.status_code,
            ms,
        )
        logger.warning(
            obs_event(
                "hubspot",
                "query_profiler",
                "http_slow",
                status=response.status_code,
                method=response.request.method,
                route=_safe_path(response.request.url.path),
                duration_ms=round(ms),
            )
        )


def build_httpx_event_hooks() -> Dict[str, list]:
    """
    event_hooks para httpx.AsyncClient. Vacío si el profiler está apagado.

    Uso:
        httpx.AsyncClient(..., event_hooks=build_httpx_event_hooks())
    """
    if not PROFILER_ENABLED:
        return {}
    return {"request": [_on_request], "response": [_on_response]}


# ═══════════════════════════════════════════════════════════════════════════
# 3. Middleware FastAPI — header Server-Timing
# ═══════════════════════════════════════════════════════════════════════════

async def server_timing_middleware(request, call_next):
    """
    Mide el request completo y emite `Server-Timing`.

    DevTools de Chrome grafica ese header nativamente, así que el desglose
    mongo/hubspot/total se ve por request sin instalar nada.

    Fail-open: cualquier error aquí no debe tumbar el request. Si el
    profiling falla, la respuesta sale igual, solo que sin header.
    """
    if not PROFILER_ENABLED:
        return await call_next(request)

    start_request_profile()
    t0 = time.perf_counter()
    response = await call_next(request)
    total_ms = (time.perf_counter() - t0) * 1000.0

    try:
        stats = get_request_profile() or {}
        mongo_ms = stats.get("mongo_ms", 0.0)
        hubspot_ms = stats.get("hubspot_ms", 0.0)
        # "otro" = tiempo que no es ni Mongo ni HubSpot: Redis, CPU, serialización
        other_ms = max(0.0, total_ms - mongo_ms - hubspot_ms)

        if SERVER_TIMING_ENABLED:
            response.headers["Server-Timing"] = ", ".join([
                f'mongo;dur={mongo_ms:.1f};desc="MongoDB ({stats.get("mongo_count", 0)} cmd)"',
                f'hubspot;dur={hubspot_ms:.1f};desc="HubSpot ({stats.get("hubspot_count", 0)} req)"',
                f'other;dur={other_ms:.1f};desc="Redis+CPU"',
                f"total;dur={total_ms:.1f}",
            ])

        if total_ms >= SLOW_MS * 10:  # request lento = 10x el umbral de query
            logger.warning(
                "[QueryProfiler][Request] SLOW %s %s — total=%.0fms "
                "(mongo=%.0fms/%d, hubspot=%.0fms/%d, otro=%.0fms)",
                request.method, _safe_path(request.url.path), total_ms,
                mongo_ms, stats.get("mongo_count", 0),
                hubspot_ms, stats.get("hubspot_count", 0),
                other_ms,
            )
            logger.warning(
                obs_event(
                    "server",
                    "request",
                    "slow_request",
                    status=getattr(response, "status_code", "unknown"),
                    method=request.method,
                    route=_safe_path(request.url.path),
                    duration_ms=round(total_ms),
                    mongo_ms=round(mongo_ms),
                    mongo_count=stats.get("mongo_count", 0),
                    hubspot_ms=round(hubspot_ms),
                    hubspot_count=stats.get("hubspot_count", 0),
                    other_ms=round(other_ms),
                )
            )
    except Exception as e:
        logger.debug("[QueryProfiler] Error emitiendo Server-Timing: %s", e)

    return response
