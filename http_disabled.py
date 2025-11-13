"""Disabled local http helper (renamed).

This file is the previous local `http.py` kept for reference. It was
renamed to avoid shadowing the standard library `http` package which
caused `urllib3`/`requests` to fail importing `http.client`.

Keep this file around for debugging; the active HTTP helper is
`http_client.py` in the same folder.
"""
import importlib
import sys
try:
    # ensure stdlib http.client is available as a loaded module
    if 'http.client' not in sys.modules:
        sys.modules['http.client'] = importlib.import_module('http.client')
except Exception:
    # best-effort: ignore if stdlib import fails in unusual envs
    pass

# try to import the real helper implementation (http_client.py)
_real = None
try:
    import http_client as _real
except Exception:
    _real = None

def get(*args, **kwargs):
    """Proxy to http_client.get if available, otherwise raise informative error."""
    if _real and hasattr(_real, 'get'):
        return _real.get(*args, **kwargs)
    raise ImportError('http_client module not available; import http_client.py instead')
