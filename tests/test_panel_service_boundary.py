from pathlib import Path


PANEL_ROUTES_DIR = Path("middleware/panel")
SERVICE_DIR = Path("services/panel")


def test_panel_route_modules_depend_on_services_not_legacy_panel():
    route_files = [
        path
        for path in PANEL_ROUTES_DIR.glob("*_routes.py")
        if path.name != "__init__.py"
    ]

    assert route_files
    for path in route_files:
        source = path.read_text(encoding="utf-8")
        assert "from middleware.outbound_panel import" not in source
        assert "import middleware.outbound_panel" not in source


def test_panel_service_facades_are_explicit_migration_boundary():
    service_files = sorted(SERVICE_DIR.glob("*_service.py"))

    assert service_files
    for path in service_files:
        source = path.read_text(encoding="utf-8")
        assert "middleware.outbound_panel" in source
