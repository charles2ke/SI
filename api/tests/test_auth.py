from sqlalchemy import select

from si.auth import hash_key
from si.db import APIKey, Audit


def test_requires_valid_credentials(client):
    assert client.get("/v1/agents", headers={"x-api-key": ""}).status_code == 401
    assert client.get("/v1/agents", headers={"x-api-key": "invalid"}).status_code == 401


def test_roles_and_one_time_key(client, database):
    assert client.post("/v1/collections", json={"name": "No"},
                       headers={"x-api-key": "viewer"}).status_code == 403
    assert client.post("/v1/api-keys", json={"name": "No"},
                       headers={"x-api-key": "developer"}).status_code == 403
    created = client.post("/v1/api-keys", json={"name": "test", "role": "viewer"}).json()
    assert created["key"].startswith("si_")
    listing = client.get("/v1/api-keys").json()
    assert all("key" not in item and "key_hash" not in item for item in listing)
    with database() as session:
        assert session.get(APIKey, created["id"]).key_hash == hash_key(created["key"])
        assert session.scalar(select(Audit).where(Audit.action == "api_key.create"))
    assert client.get("/v1/agents", headers={"x-api-key": created["key"]}).status_code == 200
    assert client.delete(f"/v1/api-keys/{created['id']}").status_code == 204
    assert client.get("/v1/agents", headers={"x-api-key": created["key"]}).status_code == 401


def test_project_ownership(client):
    collection = client.post("/v1/collections", json={"name": "Private"}).json()
    agent = client.post("/v1/agents", json={"name": "Private"}).json()
    other = {"x-api-key": "other"}
    assert client.get("/v1/collections", headers=other).json() == []
    assert client.patch(f"/v1/agents/{agent['id']}", json={"name": "stolen"},
                        headers=other).status_code == 404
    assert client.post("/v1/agents", json={"name": "bad", "collection_ids": [collection["id"]]},
                       headers=other).status_code == 404
