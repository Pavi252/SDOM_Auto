"""PVER (Platform Version) checkout operations for SDOM.

This module handles PVER-specific checkout logic including:
- Direct suffix application (no transformation needed)
- Conflict detection and resolution
- Reference JSON loading and processing
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    import subprocess as _sp
    _sp.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests

# Import shared helpers
import sys
sys.path.insert(0, str(Path(__file__).parent))
from sdom_api import (
    BASE_URL,
    create_session,
    parse_fullname,
    build_fullname,
    find_free_variant,
    extract_error_message,
    SDOMCheckoutError,
    SDOMConflictError,
)


def load_pver_reference(reference_path: Path) -> dict[str, Any]:
    """Load PVER checkout reference from JSON.
    
    Expected structure:
    {
      "ifd_id": "...",
      "pver_checkout": {
        "version_input": "PVER : BMW_H2CU / H2CU_31A200812B; 1",
        "derived_version": "PVER : BMW_H2CU / H2CU_31A200812B_test; 1"
      }
    }
    """
    data = json.loads(reference_path.read_text(encoding="utf-8"))
    pver_checkout = data.get("pver_checkout")
    if not pver_checkout:
        raise ValueError(f"No pver_checkout in reference JSON: {reference_path}")
    return pver_checkout


def checkout_pver(
    session: requests.Session,
    base_fullname: str,
    target_fullname: str,
    version_comment: str = "Auto checkout by script",
    auto_suffix_on_conflict: bool = True,
) -> tuple[str, str]:
    """Checkout a PVER variant from SDOM.
    
    Args:
        session: Authenticated SDOM session
        base_fullname: Base version fullname (e.g., PVER : BMW_H2CU / H2CU_31A200812B; 1)
        target_fullname: Target version fullname with desired variant
        version_comment: Comment for checkout
        auto_suffix_on_conflict: Auto-increment suffix if variant exists
    
    Returns:
        (created_fullname, resolved_fullname): What was created and final checked-out version
    
    Raises:
        SDOMCheckoutError: If checkout fails
        SDOMConflictError: If conflicts cannot be resolved
    """
    base_parsed = parse_fullname(base_fullname)
    target_parsed = parse_fullname(target_fullname)
    
    if not base_parsed.get("class") or not target_parsed.get("class"):
        raise ValueError(f"Invalid fullname format. Base: {base_fullname}, Target: {target_fullname}")
    
    # Get target variant
    target_variant = target_parsed["variant"]
    
    # Find free variant if needed
    if auto_suffix_on_conflict:
        try:
            resolved_variant = find_free_variant(
                session,
                base_fullname,
                target_variant,
                max_attempts=25,
            )
            if resolved_variant != target_variant:
                print(f"[PVER Checkout] Variant '{target_variant}' exists, using '{resolved_variant}'")
        except Exception as e:
            raise SDOMConflictError(f"Cannot resolve PVER variant conflict: {e}")
    else:
        resolved_variant = target_variant
    
    # Build resolved fullname
    resolved_fullname = build_fullname(
        base_parsed["class"],
        base_parsed["name"],
        resolved_variant,
        base_parsed.get("revision", 0),
    )
    
    # Build XML payload for checkoutversionex
    xml = (
        "<Checkout>"
        f'<NewVariant type="Variant" variant="{resolved_variant}" />'
        f"<VersionComment>{version_comment}</VersionComment>"
        "</Checkout>"
    )
    
    # Execute checkout with root parameter
    response = session.post(
        f"{BASE_URL}/configuration/checkoutversionex",
        params={"root": base_fullname},
        headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
        data=xml,
        timeout=120,
    )
    
    if response.status_code >= 400:
        error_msg = extract_error_message(response.text or "")
        raise SDOMCheckoutError(
            f"PVER checkout failed: status={response.status_code}, detail={error_msg}"
        )
    
    return resolved_fullname, resolved_fullname


def checkout_pver_from_reference(
    session: requests.Session,
    reference_path: Path,
    version_comment: str = "Auto checkout by script",
) -> dict[str, str]:
    """Checkout PVER using reference JSON.
    
    Returns:
        {
          "base_fullname": "...",
          "target_fullname": "...",
          "resolved_fullname": "...",  # May be auto-incremented
          "status": "success" | "error",
          "error": "..." (if status is error)
        }
    """
    try:
        pver_ref = load_pver_reference(reference_path)
        base_fullname = pver_ref["version_input"]
        target_fullname = pver_ref["derived_version"]
        
        created, resolved = checkout_pver(
            session,
            base_fullname,
            target_fullname,
            version_comment,
        )
        
        fallback_used = resolved != target_fullname

        return {
            "base_fullname": base_fullname,
            "target_fullname": target_fullname,
            "resolved_fullname": resolved,
            "fallback_used": fallback_used,
            "status": "success",
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }
