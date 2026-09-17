"""Paridad de ruteo: frontera nueva vs legacy.

Reglas que fija este test:
1. Ninguna ruta (path + method) vive a la vez en los routers nuevos y en el
   legacy — cuando un grupo migra, se borra de legacy.
2. El router legacy va montado el ULTIMO en get_panel_router().
3. Toda ruta nueva apunta a una funcion de services.panel (nunca directo a
   middleware.outbound_panel).

No toca red ni BD: solo inspecciona declaraciones de routers.
"""

import os

os.environ.setdefault("HUBSPOT_API_KEY", "test_key")
os.environ.setdefault("ADMIN_API_KEY", "test_admin_key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from middleware import outbound_panel as legacy
from middleware.panel import get_panel_router


def _routes(router):
    out = []
    for r in router.routes:
        methods = tuple(sorted(getattr(r, "methods", None) or ()))
        out.append((getattr(r, "path", ""), methods))
    return out


def test_legacy_va_ultimo():
    router = get_panel_router()
    included = [r for r in router.routes]
    assert len(included) > 0
    # El legacy es un APIRouter montado completo: sus rutas aparecen al final
    legacy_paths = {p for p, _ in _routes(legacy.router)}
    own_paths = []
    for r in router.routes:
        if getattr(r, "path", None):
            own_paths.append(r.path)
    if legacy_paths:
        first_legacy = min(
            (i for i, p in enumerate(own_paths) if p in legacy_paths),
            default=None,
        )
        últimos_propios = [
            i for i, p in enumerate(own_paths) if p not in legacy_paths
        ]
        if first_legacy is not None and últimos_propios:
            assert first_legacy > max(últimos_propios), (
                "legacy debe ir montado al final de get_panel_router()"
            )


def test_sin_rutas_duplicadas_nuevo_vs_legacy():
    router = get_panel_router()
    seen = {}
    dupes = []
    for r in router.routes:
        path = getattr(r, "path", None)
        methods = tuple(sorted(getattr(r, "methods", None) or ()))
        if not path or not methods:
            continue
        key = (path, methods)
        if key in seen:
            dupes.append(key)
        seen[key] = r
    assert not dupes, f"rutas duplicadas nuevo+legacy: {dupes}"


def test_rutas_nuevas_apuntan_a_servicios():
    from middleware import panel as panel_pkg
    import pkgutil

    violations = []
    for mod in pkgutil.iter_modules(panel_pkg.__path__):
        if not mod.name.endswith("_routes"):
            continue
        module = __import__(
            f"middleware.panel.{mod.name}", fromlist=["router"]
        )
        for r in module.router.routes:
            endpoint = getattr(r, "endpoint", None)
            if endpoint is None:
                continue
            home = getattr(endpoint, "__module__", "")
            if not home.startswith("services.panel."):
                violations.append(
                    f"{mod.name}:{getattr(r, 'path', '?')} -> {home}"
                )
    assert not violations, f"rutas que no van a servicios: {violations}"
