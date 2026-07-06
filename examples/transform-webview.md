# Make our existing notes app mobile-WebView ready

We already have the Team Notes app running in production — the code lives in our git repo
(web: Vite + React, api: Express, db: Postgres). Do NOT rebuild it from scratch.

Take the existing project and transform it so the current web frontend works inside our
company mobile app's WebView: phone-sized responsive layouts, touch-friendly controls,
safe-area support, and login state handled inside the WebView. Keep every existing feature
and route working exactly as it does today.
