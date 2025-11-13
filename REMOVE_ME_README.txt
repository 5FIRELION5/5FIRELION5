Temporary cleanup notes:

- I removed the local `http` package and `http.py` shim because they were
  shadowing the standard library `http` package and causing urllib3/requests
  to fail importing `http.client`.
- The project's HTTP helper is `http_client.py` (use this for network calls).
- A copy of the previous `http.py` contents was kept in `http_disabled.py`.

If you prefer I can restore a safer shim, but the cleanest solution is to
not have any top-level `http.py` or `http/` package in the project so the
interpreter imports the system `http` package normally.
