"""Shared HTTP session with retries and simple helpers.

Provides a requests.Session configured with Retry/backoff and a small
wrapper helper `get` that forwards to the session. This centralizes
timeouts and retry behavior for the scraper.
"""
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("fide_http_client")

# Configure a session with retries/backoff
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
adapter = HTTPAdapter(max_retries=retries)
session.mount("https://", adapter)
session.mount("http://", adapter)

DEFAULT_TIMEOUT = 20

def get(url, **kwargs):
    """Wrapper around session.get that applies a default timeout and logs errors."""
    timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)
    headers = kwargs.pop("headers", None)
    try:
        resp = session.get(url, timeout=timeout, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp
    except Exception as e:
        logger.debug("HTTP GET failed: %s %s", url, e)
        raise
