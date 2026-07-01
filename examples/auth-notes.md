# Auth Notes — a personal notes app with login and roles

Build a small notes web app with a backend API.

## Requirements
- Users register and log in (username + password). Sessions are token-based.
- A logged-in user can create, list, and delete their own notes. Notes persist across restarts.
- Two roles: `admin` and `member`. Only an admin may access `GET /admin/users` (list all users);
  a member hitting it gets 403. An unauthenticated request to any protected route gets 401.
