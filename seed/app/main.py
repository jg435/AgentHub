"""Tiny todo service. Tests: pytest -q"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="todo")

_todos: dict[int, dict] = {}
_next_id = 1


class TodoIn(BaseModel):
    title: str
    done: bool = False


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/todos")
def list_todos():
    return list(_todos.values())


@app.post("/todos", status_code=201)
def create_todo(todo: TodoIn):
    global _next_id
    item = {"id": _next_id, **todo.model_dump()}
    _todos[_next_id] = item
    _next_id += 1
    return item


@app.delete("/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: int):
    if todo_id not in _todos:
        raise HTTPException(status_code=404, detail="not found")
    del _todos[todo_id]
