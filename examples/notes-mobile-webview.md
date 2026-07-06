# Private Notes — mobile WebView

Our team wants the Private Notes app usable from phones, embedded as a WebView inside our
existing company mobile app (Android and iOS wrappers we already ship).

- Same product as before: sign up / log in with username + password; a logged-in person
  writes, lists, and deletes their own notes; notes survive restarts; one admin can list
  registered users, regular members cannot.
- The web UI must work well inside a mobile WebView: phone-sized layouts, touch-friendly
  tap targets, no hover-only interactions, and it must respect device safe areas (notches).
- The wrapper injects no cookies and shares no browser session — the app must handle its
  own login state inside the WebView.
- Deep-linkable: opening the WebView at a note's URL lands on that note after login.
