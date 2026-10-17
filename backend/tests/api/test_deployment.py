from fastapi.testclient import TestClient

from mayday.api.app import build_app


def test_vercel_origin_can_read_and_submit_requests(monkeypatch, tmp_path):
    origin = "https://frontend-zeta-olive-60.vercel.app"
    monkeypatch.setenv("MAYDAY_ALLOWED_ORIGINS", origin + "/, https://preview.example.com/")
    app = build_app(autostart=False, use_agent=False, runs_dir=tmp_path / "runs")
    with TestClient(app) as client:
        response = client.get("/api/state", headers={"Origin": origin})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        preflight = client.options("/api/scenario", headers={"Origin": origin,
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        other = client.get("/api/state", headers={"Origin": "https://unconfigured.example.com"})
        assert "access-control-allow-origin" not in other.headers


def test_local_development_origins_still_work(monkeypatch, tmp_path):
    monkeypatch.delenv("MAYDAY_ALLOWED_ORIGINS", raising=False)
    app = build_app(autostart=False, use_agent=False, runs_dir=tmp_path / "runs")
    with TestClient(app) as client:
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
            assert client.get("/api/state", headers={"Origin": origin}).headers["access-control-allow-origin"] == origin
