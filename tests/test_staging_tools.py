from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from middleware.staging_tools import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_staging_sentinel_is_not_available_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    response = _client().post("/staging/sentinel", json={"sentinel_id": "STAGING_ONLY_test"})

    assert response.status_code == 404


def test_staging_sentinel_creates_marker_in_all_stores(monkeypatch):
    import middleware.staging_tools as staging_tools

    async def created(sentinel_id):
        return "created"

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("STAGING_SENTINEL_ENABLED", "true")
    monkeypatch.setattr(staging_tools, "_write_mongo_sentinel", created)
    monkeypatch.setattr(staging_tools, "_write_redis_sentinel", created)
    monkeypatch.setattr(staging_tools, "_write_pg_sentinel", created)

    response = _client().post("/staging/sentinel", json={"sentinel_id": "STAGING_ONLY_test"})

    assert response.status_code == 200
    assert response.json() == {
        "sentinel_id": "STAGING_ONLY_test",
        "environment": "staging",
        "stores": {
            "mongo": "created",
            "redis": "created",
            "pgvector": "created",
        },
    }
