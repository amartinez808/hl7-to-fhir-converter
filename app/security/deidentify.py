"""Privacy helpers for masking PHI in FHIR resources."""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping, MutableMapping

__all__ = ["mask_patient"]


def _mask_identifier(identifier: MutableMapping[str, object]) -> None:
    value = identifier.get("value")
    if value:
        identifier["value"] = "****"


def _mask_name(name: MutableMapping[str, object]) -> None:
    name["family"] = "REDACTED"
    given = name.get("given")
    if isinstance(given, list):
        name["given"] = ["REDACTED"]
    else:
        name["given"] = ["REDACTED"]
    if "text" in name:
        name["text"] = "REDACTED"


def mask_patient(resource: Mapping[str, object]) -> dict:
    """Return a deep-copied Patient resource with basic identifiers masked."""
    if not isinstance(resource, Mapping):
        return {}
    if resource.get("resourceType") != "Patient":
        return deepcopy(resource)

    masked = deepcopy(resource)
    for identifier in masked.get("identifier", []) or []:
        if isinstance(identifier, MutableMapping):
            _mask_identifier(identifier)
    for name in masked.get("name", []) or []:
        if isinstance(name, MutableMapping):
            _mask_name(name)
    if "telecom" in masked:
        masked["telecom"] = []
    return masked
