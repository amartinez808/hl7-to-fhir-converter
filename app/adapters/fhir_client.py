"""Minimal FHIR client adapter built on httpx."""

from __future__ import annotations

import os
from typing import Any, Mapping

import httpx

__all__ = ["FHIRClient"]


class FHIRClient:
    """Tiny FHIR client for create/search operations."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token or os.getenv("FHIR_TOKEN")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/fhir+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def create(self, resource_type: str, body: Mapping[str, Any]) -> httpx.Response:
        url = f"{self.base_url}/{resource_type}"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, headers=self._headers(), json=body)
        response.raise_for_status()
        return response

    def search(self, resource_type: str, params: Mapping[str, Any]) -> httpx.Response:
        url = f"{self.base_url}/{resource_type}"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=self._headers(), params=params)
        response.raise_for_status()
        return response
