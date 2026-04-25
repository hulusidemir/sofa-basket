"""Minimal SofaScore client with basic in-memory caching and defensive errors.

All helpers return parsed JSON on success or ``None`` on any failure so callers
can render "Veri yok" without trying/catching everywhere.

SofaScore'un Varnish/edge katmanı Python ``requests``'in TLS parmak izini (JA3)
algılayıp 403 dönüyor. Bu yüzden ``curl_cffi`` üzerinden Chrome'un TLS parmak
izini taklit ederek istek atıyoruz. ``curl_cffi`` yoksa son çare olarak
``requests``'e düşüyoruz; bu durumda istekler büyük olasılıkla 403 dönecek
ve kullanıcıya uyarı log'u basılacak.
"""

from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Any, Optional

log = logging.getLogger(__name__)

try:
    from curl_cffi import requests as _http  # type: ignore
    _IMPERSONATE = "chrome120"
    _HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover - fallback
    import requests as _http  # type: ignore
    _IMPERSONATE = None
    _HAS_CURL_CFFI = False
    log.warning(
        "curl_cffi yüklü değil; SofaScore büyük olasılıkla 403 dönecek. "
        "Çözüm: pip install curl_cffi"
    )

# api.sofascore.com Varnish 403 dönmüyor; www. de aynı şekilde çalışıyor.
BASE_URL = "https://api.sofascore.com/api/v1"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
}

REQUEST_TIMEOUT = 10


class _TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = Lock()

    def get(self, key: str, ttl: float) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            ts, value = entry
            if time.time() - ts > ttl:
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.time(), value)


_cache = _TTLCache()


def _do_request(url: str):
    if _HAS_CURL_CFFI:
        return _http.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT,
            impersonate=_IMPERSONATE,
        )
    return _http.get(url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)


def _get_json(path: str, ttl: float = 15.0) -> Optional[Any]:
    cached = _cache.get(path, ttl)
    if cached is not None:
        return cached
    url = f"{BASE_URL}{path}"
    try:
        response = _do_request(url)
    except Exception as exc:  # curl_cffi/requests farklı hata sınıfları kullanır
        log.warning("SofaScore istek hatası %s: %s", path, exc)
        return None

    if response.status_code == 404:
        _cache.set(path, None)
        return None
    if response.status_code == 403:
        log.warning(
            "SofaScore 403 - TLS parmak izi engellenmiş olabilir. "
            "curl_cffi yüklü mü? path=%s", path,
        )
        return None
    if response.status_code != 200:
        log.warning("SofaScore %s -> %s", path, response.status_code)
        return None

    try:
        data = response.json()
    except ValueError:
        log.warning("SofaScore JSON parse hatası %s", path)
        return None

    _cache.set(path, data)
    return data


def get_live_events() -> Optional[dict]:
    """Canlı basketbol maçları listesi."""
    return _get_json("/sport/basketball/events/live", ttl=20.0)


def get_event(event_id: int | str) -> Optional[dict]:
    return _get_json(f"/event/{event_id}", ttl=12.0)


def get_statistics(event_id: int | str) -> Optional[dict]:
    return _get_json(f"/event/{event_id}/statistics", ttl=15.0)


def get_incidents(event_id: int | str) -> Optional[dict]:
    return _get_json(f"/event/{event_id}/incidents", ttl=15.0)


def get_lineups(event_id: int | str) -> Optional[dict]:
    return _get_json(f"/event/{event_id}/lineups", ttl=25.0)


def get_h2h(custom_id: str) -> Optional[dict]:
    if not custom_id:
        return None
    return _get_json(f"/event/{custom_id}/h2h/events", ttl=600.0)
