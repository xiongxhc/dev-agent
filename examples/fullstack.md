# Tasks app (fullstack)

Build a small task tracker with a web UI and a JSON API.

## Backend (API)
- GET /health -> {"ok": true}
- GET /api/tasks -> a JSON array of tasks, each {id, title, done}
- POST /api/tasks -> create a task from {title}

## Frontend (Web)
- "/" lists tasks fetched from the API and has an input to add one.
- An <h1> with the app title is present on "/".
