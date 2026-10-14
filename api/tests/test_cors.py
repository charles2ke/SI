from fastapi.testclient import TestClient

from si.main import app


def test_console_origin_can_preflight():
    client = TestClient(app)
    response = client.options("/v1/chat/completions", headers={
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_untrusted_origin_is_rejected():
    client = TestClient(app)
    response = client.options("/v1/chat/completions", headers={
        "Origin": "https://untrusted.example",
        "Access-Control-Request-Method": "POST",
    })
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
