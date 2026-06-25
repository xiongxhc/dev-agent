# Persistent Tasks App

Build a small full-stack tasks app: a JSON HTTP API plus a web frontend that lists and adds tasks.

## API
- `GET /health` -> 200 `{"ok": true}`
- `GET /api/tasks` -> 200, a JSON array of tasks (each has an `id` and a `title`)
- `POST /api/tasks` with `{"title": "..."}` -> 200, the created task including its `id`

## Frontend
- A page at `/` that lists existing tasks and has a form to add a new one.

## Durability (REQUIRED)
- Tasks MUST persist across a server restart: a task created via `POST /api/tasks` is still
  returned by `GET /api/tasks` after the API process is restarted. How you achieve this is
  your decision.
