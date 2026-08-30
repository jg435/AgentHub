from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"ok": True}


def test_create_and_list():
    r = client.post("/todos", json={"title": "write tests"})
    assert r.status_code == 201
    todo = r.json()
    assert todo["title"] == "write tests" and todo["done"] is False
    assert todo in client.get("/todos").json()


def test_delete():
    todo_id = client.post("/todos", json={"title": "temp"}).json()["id"]
    assert client.delete(f"/todos/{todo_id}").status_code == 204
    assert client.delete(f"/todos/{todo_id}").status_code == 404
