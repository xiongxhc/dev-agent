# Shared-State Tasks App

Build a small full-stack tasks app: a JSON HTTP API plus a web frontend that lists and adds tasks.

## API
- `GET /health` -> 200 `{"ok": true}`
- `GET /api/tasks` -> 200, a JSON array of tasks (each has an `id` and a `title`)
- `POST /api/tasks` with `{"title": "..."}` -> 200, the created task including its `id`

## Frontend
- A page at `/` that lists existing tasks and has a form to add a new one.

## Deployment shape (REQUIRED)
- The API is deployed as **multiple horizontally-scaled, stateless replicas** behind a load
  balancer. Any replica may serve any request, and replicas are added and removed freely.
- **Every replica must read and write the same shared task data.** A task created against one
  replica MUST be immediately visible to all the others. State therefore CANNOT live inside a
  single API process or its local filesystem — it must live in a shared, networked data store
  that all replicas connect to.
- The store must handle concurrent writes from many replicas consistently.

## Durability (REQUIRED)
- Tasks MUST persist across a server restart: a task created via `POST /api/tasks` is still
  returned by `GET /api/tasks` after the API process is restarted. How you achieve this — and
  which store satisfies the shared-state requirement above — is your decision.
