"""Shared SDOM REST API helper module - DRY principle for checkout/checkin operations.

Common functions for all object types (FC, BC, PVER):
- Session management & authentication
- HTTP communication
- Variant resolution & conflict handling
- Version parsing & manipulation
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    import subprocess as _sp
    _sp.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests

# Import credentials module from parent directory
_parent_dir = Path(__file__).resolve().parent.parent.parent
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))
from credentials import prompt_sdom_credentials

BASE_URL = "https://easee-ews.webapp.inside.bosch.cloud/easee-ews"
DEFAULT_USER = ""
DEFAULT_PASSWORD = ""


def _requester_value(user: str | None = None) -> str:
    """Return a non-empty requester value for SDOM API calls.

    Recent SDOM versions reject requests without the x-requester header.
    Keep this aligned with the proven legacy SDOM scripts: prefer the stable
    local requester identity from the environment, then fall back to the
    supplied login only when no local value exists.
    """
    candidate = (
        os.getenv("SDOM_USER")
        or os.getenv("USERNAME")
        or os.getenv("USER")
        or (user or "")
        or "copilot"
    ).strip()
    return candidate or "copilot"


def create_session(user: str | None = None, password: str | None = None) -> requests.Session:
    """Create an authenticated SDOM session.
    
    If user/password not provided, prompts interactively on console.
    Credentials are cached for the session to avoid repeated prompts.
    """
    if not user or not password:
        user, password = prompt_sdom_credentials()
    
    session = requests.Session()
    session.auth = (user, password)
    session.headers.update({
        "OSLC-Core-Version": "2.0",
        "Content-Type": "application/octet-stream",
        "x-requester": _requester_value(user),
    })
    return session


def parse_fullname(fullname: str) -> dict[str, str]:
    """Parse SDOM fullname into components.
    
    Example: "BC : DiaApl / 358.25.0_H2CU_8_TEST_1; 0"
    Returns: {
        "class": "BC",
        "name": "DiaApl",
        "variant": "358.25.0_H2CU_8_TEST_1",
        "revision": "0"
    }
    """
    m = re.match(
        r"^\s*([^:]+)\s*:\s*([^/]+)\s*/\s*([^;]+)\s*;\s*(\d+)\s*$",
        fullname.strip()
    )
    if not m:
        return {"raw": fullname}
    
    return {
        "class": m.group(1).strip(),
        "name": m.group(2).strip(),
        "variant": m.group(3).strip(),
        "revision": m.group(4).strip(),
    }


def build_fullname(
    cls: str,
    name: str,
    variant: str,
    revision: int | str = 0,
) -> str:
    """Build a properly formatted SDOM fullname."""
    return f"{cls} : {name} / {variant}; {revision}"


def next_suffix_variant(variant: str) -> str:
    """Increment variant suffix for conflict resolution.
    
    Examples:
        TEST_1 → TEST_2
        358.25.0 → 358.25.1
        myvar1 → myvar2
    """
    m = re.match(r"^(.*?)(\d+)$", variant)
    if not m:
        return f"{variant}1"
    prefix = m.group(1)
    number = int(m.group(2)) + 1
    return f"{prefix}{number}"


def version_exists(
    session: requests.Session,
    fullname: str,
    timeout: int = 60,
) -> bool:
    """Check if a version exists in SDOM.
    
    Queries /configuration/latest endpoint with fullname filter.
    Returns True if found, False otherwise.
    Raises RuntimeError if backend service is experiencing issues.
    """
    parsed = parse_fullname(fullname)
    candidates = [fullname]
    if parsed.get("class") and parsed.get("name") and parsed.get("variant") and parsed.get("revision"):
        upper_variant = build_fullname(
            parsed["class"],
            parsed["name"],
            parsed["variant"].upper(),
            parsed["revision"],
        )
        lower_variant = build_fullname(
            parsed["class"],
            parsed["name"],
            parsed["variant"].lower(),
            parsed["revision"],
        )
        for candidate in (upper_variant, lower_variant):
            if candidate not in candidates:
                candidates.append(candidate)

    for candidate in candidates:
        requester_user = None
        if isinstance(session.auth, tuple) and len(session.auth) >= 2:
            requester_user = session.auth[0]
        resp = session.get(
            f"{BASE_URL}/configuration/latest",
            params={"oslc.where": f'oslc_scm:fullName="{candidate}"'},
            headers={
                "OSLC-Core-Version": "2.0",
                "x-requester": _requester_value(requester_user),
            },
            timeout=timeout,
        )
        if resp.status_code >= 500:
            raise RuntimeError(
                f"SDOM backend service error (status={resp.status_code}). "
                f"The /configuration/latest endpoint is not responding normally. "
                f"This is a backend issue, not a client-side problem. "
                f"Please check SDOM service status and retry once it recovers."
            )
        if resp.status_code == 200 and bool((resp.text or "").strip()):
            return True
    return False


def find_free_variant(
    session: requests.Session,
    base_fullname: str,
    desired_variant: str,
    max_attempts: int = 25,
) -> str:
    """Find a free (non-conflicting) variant by auto-incrementing suffix.
    
    Args:
        session: Authenticated SDOM session
        base_fullname: Base version to extract class/name/revision
        desired_variant: Target variant (e.g., TEST_1, 358.26.0)
        max_attempts: Max suffix iterations before failing
    
    Returns:
        Free variant name (may be incremented: TEST_1, TEST_2, ...)
    
    Raises:
        RuntimeError: If no free variant found after max_attempts
    """
    parsed = parse_fullname(base_fullname)
    if not parsed.get("class"):
        raise ValueError(f"Cannot parse base_fullname: {base_fullname}")
    
    cls = parsed["class"]
    name = parsed["name"]
    revision = int(parsed.get("revision", 0))
    
    variant = desired_variant
    for attempt in range(max_attempts):
        candidate = build_fullname(cls, name, variant, revision)
        if not version_exists(session, candidate):
            return variant
        variant = next_suffix_variant(variant)
    
    raise RuntimeError(
        f"No free variant found for {desired_variant} after {max_attempts} attempts. "
        f"Last tried: {variant}"
    )


def print_response(response: requests.Response, label: str = "") -> None:
    """Pretty-print HTTP response details."""
    if label:
        print(f"\n[Response] {label}")
    print(f"  Status: {response.status_code}")
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/octet-stream" in content_type:
        print(f"  Body: <binary {len(response.content)} bytes>")
    else:
        print(f"  Body: {response.text[:500]}")  # First 500 chars


def extract_error_message(response_text: str) -> str:
    """Extract SDOM error message from XML response."""
    # Try XML error format first
    m = re.search(r"<ErrorInfo>(.*?)</ErrorInfo>", response_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback to raw text (first 200 chars)
    return response_text[:200]


class SDOMCheckoutError(Exception):
    """Raised when SDOM checkout fails."""
    pass


class SDOMConflictError(Exception):
    """Raised when variant conflicts cannot be resolved."""
    pass
