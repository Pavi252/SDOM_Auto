"""BC (Base Component) checkout operations for SDOM.

This module handles BC-specific checkout logic including:
- SDOM-compatible suffix transformation
- Conflict detection and resolution
- Reference JSON loading and processing
"""

from __future__ import annotations

import json
import re
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


def transform_bc_suffix(suffix: str | None) -> str | None:
    """Transform project-specific suffix to SDOM-compatible BC pattern.
    
    SDOM requires platform-like pattern for BC: _LETTER_NUMBER
    
    Examples:
        test → _TEST_1
        test1 → _TEST_1
        test2 → _TEST_2
        v1 → _V_1
        None → None
    """
    if suffix is None:
        return None
    
    suffix = suffix.strip().replace(" ", "")
    if not suffix:
        return None
    
    # Match letter-number pattern: "test1" or "v2"
    m = re.match(r"^([a-zA-Z]+)(\d*)$", suffix)
    if m:
        letters = m.group(1).upper()
        number = int(m.group(2)) if m.group(2) else 1
        return f"_{letters}_{number}"
    
    # Fallback: assume it's just letters, default to _1
    return f"_{suffix.upper()}_1"


def normalize_bc_variant_for_sdom(variant: str) -> str:
    """Normalize BC variant tail to SDOM-compatible uppercase pattern.

    Examples:
      358.25.0_H2CU_13_test_1 -> 358.25.0_H2CU_13_TEST_1
      358.25.0_H2CU_13_test   -> 358.25.0_H2CU_13_TEST_1
      358.25.0_H2CU_13_TEST_1 -> unchanged
    """
    if not variant:
        return variant

    # Already SDOM-style tail.
    if re.match(r"^.+_[A-Z]+_\d+$", variant):
        return variant

    m = re.match(r"^(.*)_([A-Za-z]+)_(\d+)$", variant)
    if m:
        return f"{m.group(1)}_{m.group(2).upper()}_{int(m.group(3))}"

    m = re.match(r"^(.*)_([A-Za-z]+)$", variant)
    if m:
        return f"{m.group(1)}_{m.group(2).upper()}_1"

    m = re.match(r"^(.*)_([A-Za-z]+)(\d+)$", variant)
    if m:
        return f"{m.group(1)}_{m.group(2).upper()}_{int(m.group(3))}"

    return variant


def load_bc_reference(reference_path: Path) -> dict[str, Any]:
    """Load BC checkout reference from JSON.
    
    Expected structure:
    {
      "ifd_id": "...",
      "bc_checkout": {
        "version_input": "BC : DiaApl / 358.25.0_H2CU_8; 0",
        "derived_version": "BC : DiaApl / 358.25.0_H2CU_8_TEST_1; 0"
      }
    }
    """
    data = json.loads(reference_path.read_text(encoding="utf-8"))
    bc_checkout = data.get("bc_checkout")
    if not bc_checkout:
        raise ValueError(f"No bc_checkout in reference JSON: {reference_path}")
    return bc_checkout


def checkout_bc(
    session: requests.Session,
    base_fullname: str,
    target_fullname: str,
    version_comment: str = "Auto checkout by script",
    auto_suffix_on_conflict: bool = True,
) -> tuple[str, str]:
    """Checkout a BC variant from SDOM.
    
    Args:
        session: Authenticated SDOM session
        base_fullname: Base version fullname (e.g., BC : DiaApl / 358.25.0_H2CU_8; 0)
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
    target_variant = normalize_bc_variant_for_sdom(target_parsed["variant"])
    
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
                print(f"[BC Checkout] Variant '{target_variant}' exists, using '{resolved_variant}'")
        except Exception as e:
            raise SDOMConflictError(f"Cannot resolve BC variant conflict: {e}")
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
            f"BC checkout failed: status={response.status_code}, detail={error_msg}"
        )
    
    return resolved_fullname, resolved_fullname


def checkout_bc_from_reference(
    session: requests.Session,
    reference_path: Path,
    version_comment: str = "Auto checkout by script",
) -> dict[str, str]:
    """Checkout BC using reference JSON.
    
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
        bc_ref = load_bc_reference(reference_path)
        base_fullname = bc_ref["version_input"]
        target_fullname = bc_ref["derived_version"]
        
        created, resolved = checkout_bc(
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
