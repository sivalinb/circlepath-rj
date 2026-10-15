from fastapi.testclient import TestClient

from circlepath.__main__ import app_for


def test_http_authentication_and_protected_file_routes(demo):
    _, _, _, settings, _, credentials = demo
    app = app_for(settings, seed_demo=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/config")
        assert response.status_code == 401 or response.json().get("auth_required") is True
        assert client.get("/gradio_api/file=" + str(settings.db_path)).status_code in {401, 403}
        assert client.post("/login", data={"username": "manager", "password": "wrong"}).status_code == 400
        result = client.post("/login", data={"username": "manager", "password": credentials["manager"]})
        assert result.status_code == 200
        assert client.get("/config").status_code == 200
        assert client.get("/gradio_api/file=" + str(settings.db_path)).status_code == 403
        metrics = client.get("/metrics/")
        assert metrics.status_code == 200
        assert "circlepath_cases" in metrics.text
        client.get("/logout")
        response = client.get("/config")
        assert response.status_code == 401 or response.json().get("auth_required") is True
