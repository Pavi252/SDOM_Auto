"""FCP checkout operations for SDOM.

FCP follows the same checkout mechanics as FC, but uses its own reference JSON
section and naming.
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

from SDOM_Auto.fc_operations.fc_checkout import next_release_variant
from SDOM_Auto.common.sdom_api import BASE_URL, build_fullname, extract_error_message, parse_fullname, version_exists


def _normalize_sdom_fullname(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return value
    value = value.replace("/", " / ").replace(":", " : ").replace(";", " ; ")
    value = " ".join(value.split())
    value = value.replace(" : ", " : ").replace(" / ", " / ").replace(" ; ", "; ")
    return value


def _fcp_name_candidates(fullname: str) -> list[str]:
    raw = _normalize_sdom_fullname(fullname)
    compact = re.sub(r"\s*:\s*", ":", raw)
    compact = re.sub(r"\s*/\s*", "/", compact)
    compact = re.sub(r"\s*;\s*", ";", compact)
    out = [raw]
    if compact != raw:
        out.append(compact)
    return out


def _extract_checkout_fullname(body_text: str) -> str:
    txt = html.unescape((body_text or "").strip())
    match = re.search(r"<string>\s*<[^>]+>\s*<([^>]+)>\s*</string>", txt)
    return _normalize_sdom_fullname(match.group(1).strip()) if match else ""


def _print_response(label: str, response: requests.Response) -> None:
    print(f"[{label}] Status : {response.status_code}")
    print(f"[{label}] Headers: {dict(response.headers)}")
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/octet-stream" in content_type:
        print(f"[{label}] Body: <binary {len(response.content)} bytes>")
        return
    text = response.text or ""
    if len(text) > 1500:
        text = text[:1500] + "\n...<truncated>..."
    print(f"[{label}] Body:\n{text}")


def _checkout_variant(
    session: requests.Session,
    *,
    root_fullname: str,
    target_variant: str,
    version_comment: str,
) -> requests.Response:
    comment = html.escape(version_comment or "")
    xml_payload = (
        "<Checkout>"
        f'<NewVariant type="Variant" variant="{html.escape(target_variant.strip())}" />'
        f"<VersionComment>{comment}</VersionComment>"
        "</Checkout>"
    )
    last_resp: requests.Response | None = None
    for candidate in _fcp_name_candidates(root_fullname):
        resp = session.post(
            f"{BASE_URL}/configuration/checkoutversionex",
            params={"root": candidate},
            headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
            data=xml_payload,
            timeout=120,
        )
        _print_response(f"checkoutversionex POST root={candidate}", resp)
        last_resp = resp
        if resp.status_code < 400:
            return resp
    if last_resp is None:
        raise RuntimeError("No checkoutversionex request executed")
    detail = extract_error_message(last_resp.text or "")
    raise RuntimeError(f"Variant checkout failed for {root_fullname} -> {target_variant}: {detail}")


def _checkout_revision(
    session: requests.Session,
    *,
    root_fullname: str,
    version_comment: str,
) -> str:
    comment = html.escape(version_comment or "")
    xml_payload = (
        "<Checkout>"
        '<NewVariant type="Revision" />'
        f"<VersionComment>{comment}</VersionComment>"
        "</Checkout>"
    )
    last_resp: requests.Response | None = None
    for candidate in _fcp_name_candidates(root_fullname):
        resp = session.post(
            f"{BASE_URL}/configuration/checkoutversionex",
            params={"root": candidate},
            headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
            data=xml_payload,
            timeout=120,
        )
        _print_response(f"checkoutversionex POST root={candidate}", resp)
        last_resp = resp
        if resp.status_code < 400:
            created = _extract_checkout_fullname(resp.text or "")
            return created or _normalize_sdom_fullname(root_fullname)
    detail = extract_error_message((last_resp.text if last_resp is not None else "") or "")
    raise RuntimeError(f"Revision checkout failed for {root_fullname}: {detail}")


def checkout_fcp(
    session: requests.Session,
    base_fullname: str,
    target_fullname: str,
    version_kind: str | None = None,
    version_comment: str = "Auto checkout by script",
    max_attempts: int = 3,
) -> tuple[str, str]:
    """Checkout FCP using Variant/Revision payloads compatible with FCP classes."""
    base_fullname = _normalize_sdom_fullname(base_fullname)
    target_fullname = _normalize_sdom_fullname(target_fullname)
    kind = (version_kind or "release").strip().lower()
    print(f"[FCP Checkout] Base       : {base_fullname}")
    print(f"[FCP Checkout] Requested : {target_fullname}")

    parsed_target = parse_fullname(target_fullname)
    if not parsed_target.get("variant"):
        raise ValueError(f"Invalid target FCP fullname: {target_fullname}")

    base_parsed = parse_fullname(base_fullname)
    if not base_parsed.get("class") or not base_parsed.get("name"):
        raise ValueError(f"Invalid base FCP fullname: {base_fullname}")

    if kind == "revision":
        created = _checkout_revision(
            session,
            root_fullname=base_fullname,
            version_comment=version_comment,
        )
        return created, target_fullname

    candidate_variant = parsed_target["variant"]

    if kind == "release":
        resolved_variant: str | None = None
        for attempt in range(max_attempts):
            candidate_fullname = _normalize_sdom_fullname(
                build_fullname(base_parsed["class"], base_parsed["name"], candidate_variant, 0)
            )
            print(f"[FCP Checkout] Probe {attempt + 1}: {candidate_fullname}")
            if version_exists(session, candidate_fullname):
                print(f"[FCP Checkout] Release '{candidate_variant}' exists, trying next release")
                candidate_variant = next_release_variant(candidate_variant)
                continue
            resolved_variant = candidate_variant
            break

        if not resolved_variant:
            raise RuntimeError(
                f"No free FCP release variant found after {max_attempts} checks from {parsed_target['variant']}"
            )

        resolved_fullname = _normalize_sdom_fullname(
            build_fullname(base_parsed["class"], base_parsed["name"], resolved_variant, 0)
        )
        print(f"[FCP Checkout] Using free release version: {resolved_fullname}")
        resp = _checkout_variant(
            session,
            root_fullname=base_fullname,
            target_variant=resolved_variant,
            version_comment=version_comment,
        )
        created = _extract_checkout_fullname(resp.text or "") or resolved_fullname
        created_parsed = parse_fullname(created)
        created_variant = str(created_parsed.get("variant") or "").strip()
        created_revision = str(created_parsed.get("revision") or "").strip()
        if created_variant != resolved_variant or created_revision != "0":
            raise RuntimeError(
                "Release checkout returned non-release result: "
                f"expected '{resolved_fullname}', got '{created}'"
            )
        return created, resolved_fullname

    candidate_fullname = _normalize_sdom_fullname(
        build_fullname(base_parsed["class"], base_parsed["name"], candidate_variant, 0)
    )
    if version_exists(session, candidate_fullname):
        print(f"[FCP Checkout] Target exists, checking out revision: {candidate_fullname}")
        created = _checkout_revision(
            session,
            root_fullname=candidate_fullname,
            version_comment=version_comment,
        )
        return created, candidate_fullname

    resp = _checkout_variant(
        session,
        root_fullname=base_fullname,
        target_variant=candidate_variant,
        version_comment=version_comment,
    )
    created = _extract_checkout_fullname(resp.text or "") or candidate_fullname
    return created, candidate_fullname


def load_fcp_reference(reference_path: Path) -> dict[str, Any]:
    """Load FCP checkout reference from JSON."""
    data = json.loads(reference_path.read_text(encoding="utf-8"))
    fcp_checkout = data.get("fcp_checkout")
    if not fcp_checkout:
        raise ValueError(f"No fcp_checkout in reference JSON: {reference_path}")
    return fcp_checkout


def checkout_fcp_from_reference(
    session: requests.Session,
    reference_path: Path,
    version_comment: str = "Auto checkout by script",
) -> dict[str, str]:
    """Checkout FCP using reference JSON."""
    try:
        fcp_ref = load_fcp_reference(reference_path)
        return checkout_fcp_from_data(session, fcp_ref, version_comment)
    except Exception as ex:
        return {
            "status": "error",
            "error": str(ex),
        }


def checkout_fcp_from_data(
    session: requests.Session,
    fcp_ref: dict[str, Any],
    version_comment: str = "Auto checkout by script",
    max_attempts: int = 3,
) -> dict[str, str]:
    """Checkout FCP using an already resolved checkout payload."""
    try:
        base_fullname = fcp_ref["version_input"]
        target_fullname = fcp_ref["derived_version"]

        created, resolved = checkout_fcp(
            session,
            base_fullname,
            target_fullname,
            str(fcp_ref.get("version_kind") or ""),
            version_comment,
            max_attempts=max_attempts,
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
