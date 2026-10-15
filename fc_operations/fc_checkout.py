"""FC (Functional Component) checkout operations for SDOM.

This module handles FC-specific checkout logic including:
- Release-style checkout payloads
- Release conflict detection and next-release fallback
- Reference JSON loading and processing
"""

from __future__ import annotations

import html
import json
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

from SDOM_Auto.common.sdom_api import (
    BASE_URL,
    parse_fullname,
    build_fullname,
    version_exists,
    extract_error_message,
    SDOMCheckoutError,
    SDOMConflictError,
)
from SDOM_Auto import sdom_release_fetch_workspace as _fc_release_flow


def load_fc_reference(reference_path: Path) -> dict[str, Any]:
    """Load FC checkout reference from JSON."""
    data = json.loads(reference_path.read_text(encoding="utf-8"))
    fc_checkout = data.get("fc_checkout")
    if not fc_checkout:
        raise ValueError(f"No fc_checkout in reference JSON: {reference_path}")
    return fc_checkout


def next_release_variant(variant: str) -> str:
    """Increment the release segment for FC release checkout."""
    match = re.match(r"^(?P<a>\d+)\.(?P<b>\d+)\.(?P<c>\d+)(?P<suffix>.*)$", variant.strip())
    if not match:
        raise ValueError(f"Unsupported FC release variant format: {variant}")
    return f"{int(match.group('a'))}.{int(match.group('b')) + 1}.0{match.group('suffix')}"


def _to_spaced_fullname(fullname: str) -> str:
    match = re.match(
        r"^\s*([^:]+)\s*:\s*([^/]+)\s*/\s*([^;]+)\s*;\s*(\d+)\s*$",
        fullname.strip(),
    )
    if not match:
        return fullname.strip()
    return f"{match.group(1).strip()} : {match.group(2).strip()} / {match.group(3).strip()}; {match.group(4).strip()}"


def _fc_name_candidates(fullname: str) -> list[str]:
    raw = fullname.strip()
    spaced = _to_spaced_fullname(raw)
    compact = re.sub(r"\s*:\s*", ":", raw)
    compact = re.sub(r"\s*/\s*", "/", compact)
    compact = re.sub(r"\s*;\s*", ";", compact)

    candidates: list[str] = []
    for candidate in (raw, spaced, compact):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def find_free_release_variant(
    session: requests.Session,
    base_fullname: str,
    desired_variant: str,
    max_attempts: int = 25,
) -> str:
    """Find a free FC release variant by incrementing the release segment."""
    parsed = parse_fullname(base_fullname)
    if not parsed.get("class"):
        raise ValueError(f"Cannot parse base_fullname: {base_fullname}")

    candidate_variant = desired_variant
    for _ in range(max_attempts):
        candidate = build_fullname(parsed["class"], parsed["name"], candidate_variant, 0)
        if not version_exists(session, candidate):
            return candidate_variant
        candidate_variant = next_release_variant(candidate_variant)

    raise SDOMConflictError(
        f"No free FC release variant found for {desired_variant} after {max_attempts} attempts"
    )


def build_release_checkout_xml(target_variant: str, version_comment: str) -> str:
    """Build the release-specific checkoutversionex XML payload for FC."""
    comment = html.escape(version_comment or "")
    return (
        f'<Checkout VERSIONCOMMENT="{comment}">'
        f'<NewVariant type="Release" variant="{target_variant}" />'
        f"<VersionComment>{comment}</VersionComment>"
        "<Release>"
        f"<VersionComment>{comment}</VersionComment>"
        "</Release>"
        "</Checkout>"
    )


def extract_created_fullname(response_text: str) -> str:
    """Extract created fullname from checkoutversionex response."""
    txt = html.unescape((response_text or "").strip())
    match = re.search(r"<string>\s*<[^>]+>\s*<([^>]+)>\s*</string>", txt)
    return match.group(1).strip() if match else ""


def checkout_fc(
    session: requests.Session,
    base_fullname: str,
    target_fullname: str,
    version_comment: str = "Auto checkout by script",
    auto_release_on_conflict: bool = True,
) -> tuple[str, str]:
    """Checkout an FC release from SDOM.

    Delegate the actual release creation to the established release-fetch helper
    so this modular path stays byte-for-byte aligned with the FC flow that has
    already been proven against SDOM.
    """
    base_parsed = parse_fullname(base_fullname)
    target_parsed = parse_fullname(target_fullname)

    if not base_parsed.get("class") or not target_parsed.get("class"):
        raise ValueError(f"Invalid fullname format. Base: {base_fullname}, Target: {target_fullname}")

    target_variant = target_parsed["variant"]

    if auto_release_on_conflict:
        resolved_variant = _fc_release_flow._next_free_release_variant(
            session,
            fc_fullname=_fc_release_flow._to_spaced_fullname(base_fullname),
            initial_variant=target_variant,
            max_attempts=25,
        )
        if resolved_variant != target_variant:
            print(f"[FC Checkout] Release '{target_variant}' exists, using '{resolved_variant}'")
    else:
        resolved_variant = target_variant

    normalized_base = _fc_release_flow._to_spaced_fullname(base_fullname)
    resolved_fullname = _fc_release_flow._build_fullname(
        normalized_base,
        resolved_variant,
        revision=0,
    )

    target_release_fullname = _fc_release_flow._build_fullname(normalized_base, resolved_variant, revision=0)
    if _fc_release_flow._configuration_latest_exists(session, target_release_fullname):
        print(f"[FC Checkout] Target release already exists: {target_release_fullname} - reusing it")
        return target_release_fullname, resolved_fullname

    try:
        created_fullname, response = _fc_release_flow.create_release_with_reservation(
            session,
            fc_fullname=normalized_base,
            target_variant=resolved_variant,
            version_comment=version_comment,
        )
    except Exception as ex:
        raise SDOMCheckoutError(f"FC checkout failed: {ex}") from ex

    if response.status_code >= 400:
        error_msg = extract_error_message(response.text or "")
        raise SDOMCheckoutError(
            f"FC checkout failed: status={response.status_code}, detail={error_msg}"
        )

    return created_fullname or resolved_fullname, resolved_fullname


def checkout_fc_from_reference(
    session: requests.Session,
    reference_path: Path,
    version_comment: str = "Auto checkout by script",
) -> dict[str, str]:
    """Checkout FC using reference JSON."""
    try:
        fc_ref = load_fc_reference(reference_path)
        base_fullname = fc_ref["version_input"]
        target_fullname = fc_ref["derived_version"]

        created, resolved = checkout_fc(
            session,
            base_fullname,
            target_fullname,
            version_comment,
        )

        fallback_used = resolved != target_fullname

        return {
            "base_fullname": base_fullname,
            "target_fullname": target_fullname,
            "created_fullname": created,
            "resolved_fullname": resolved,
            "fallback_used": fallback_used,
            "status": "success",
        }
    except Exception as ex:
        return {
            "status": "error",
            "error": str(ex),
        }
