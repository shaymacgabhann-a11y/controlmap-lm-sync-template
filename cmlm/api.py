"""Thin HTTP client for the ScalePad public API (ControlMap + Lifecycle Manager)."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

BASE_URLS = {
    "us": "https://api.scalepad.com",
    "eu": "https://eu.api.scalepad.com",
    "ca": "https://ca.api.scalepad.com",
    "au": "https://au.api.scalepad.com",
}

# Retrying a POST after a 5xx could create a duplicate, so only idempotent
# methods are retried on server errors. 429 is safe to retry for everything.
IDEMPOTENT = {"GET", "PUT", "PATCH", "DELETE"}
MAX_ATTEMPTS = 5


class ApiError(Exception):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")
        self.status = status


class ScalePadClient:
    def __init__(self, api_key: str, region: str = "us", timeout: float = 30.0):
        self.base_url = BASE_URLS[region]
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"x-api-key": api_key, "accept": "application/json", "content-type": "application/json"}
        )

    def request(self, method: str, path: str, *, params=None, json=None) -> Any:
        url = self.base_url + path
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = self.session.request(method, url, params=params, json=json, timeout=self.timeout)
            retryable = resp.status_code == 429 or (resp.status_code >= 500 and method in IDEMPOTENT)
            if retryable and attempt < MAX_ATTEMPTS:
                delay = float(resp.headers.get("retry-after") or 2**attempt)
                log.warning("%s %s -> %s, retrying in %.0fs", method, path, resp.status_code, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise ApiError(method, path, resp.status_code, resp.text)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise AssertionError("unreachable")

    def get(self, path: str, params=None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, json=None) -> Any:
        return self.request("POST", path, json=json)

    def put(self, path: str, json=None) -> Any:
        return self.request("PUT", path, json=json)

    def patch(self, path: str, json=None) -> Any:
        return self.request("PATCH", path, json=json)

    def paginate_get(self, path: str, params: dict | None = None, page_size: int = 200) -> Iterator[dict]:
        params = dict(params or {}, page_size=page_size)
        while True:
            page = self.get(path, params)
            yield from page.get("data", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return
            params["cursor"] = cursor

    def paginate_post(self, path: str, body: dict | None = None, page_size: int = 200) -> Iterator[dict]:
        body = dict(body or {}, page_size=page_size)
        while True:
            page = self.post(path, body)
            # Search Client Action Items nests the page under "action_items".
            container = page.get("action_items", page)
            yield from container.get("data", [])
            cursor = container.get("next_cursor")
            if not cursor:
                return
            body["cursor"] = cursor
