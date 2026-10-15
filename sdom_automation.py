"""Unified SDOM automation CLI - Orchestrates BC, PVER, FC operations.

Usage:
    python sdom_automation.py --mode checkout-bc --reference-json <path>
    python sdom_automation.py --mode checkout-fc --reference-json <path>
    python sdom_automation.py --mode checkout-fcp --reference-json <path>
    python sdom_automation.py --mode checkout-pver --reference-json <path>
    python sdom_automation.py --mode checkout-all --reference-json <path>
    python sdom_automation.py --mode checkout-bc --dry-run --reference-json <path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add common module to path
sys.path.insert(0, str(Path(__file__).parent / "common"))
sys.path.insert(0, str(Path(__file__).parent / "bc_operations"))
sys.path.insert(0, str(Path(__file__).parent / "pver_operations"))
sys.path.insert(0, str(Path(__file__).parent / "fc_operations"))
sys.path.insert(0, str(Path(__file__).parent / "fcp_operations"))

from sdom_api import create_session, DEFAULT_USER, DEFAULT_PASSWORD
from bc_checkout import checkout_bc_from_reference
from pver_checkout import checkout_pver_from_reference
from fc_checkout import checkout_fc_from_reference
from fcp_checkout import checkout_fcp_from_reference


def print_header(text: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {text}")
    print(f"{'='*70}")


def update_reference_on_fallback(
    reference_path: Path,
    object_key: str,
    requested_fullname: str,
    resolved_fullname: str,
) -> bool:
    """Update reference JSON only when fallback variant was used."""
    if requested_fullname == resolved_fullname:
        return False

    data = json.loads(reference_path.read_text(encoding="utf-8"))
    section = data.get(object_key)
    if not isinstance(section, dict):
        return False

    section["requested_derived_version"] = requested_fullname
    section["derived_version"] = resolved_fullname
    reference_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified SDOM automation CLI for BC, PVER, FC operations"
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["checkout-bc", "checkout-fc", "checkout-fcp", "checkout-pver", "checkout-all"],
        help="Operation mode",
    )
    parser.add_argument(
        "--reference-json",
        type=Path,
        required=True,
        help="Path to SDOM checkout reference JSON",
    )
    parser.add_argument(
        "--user",
        default=DEFAULT_USER,
        help="SDOM username",
    )
    parser.add_argument(
        "--password",
        default=DEFAULT_PASSWORD,
        help="SDOM password",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print request details only (no actual checkout)",
    )
    parser.add_argument(
        "--comment",
        default="Auto checkout by script",
        help="Checkout version comment",
    )
    
    args = parser.parse_args()
    
    # Validate reference JSON
    if not args.reference_json.exists():
        print(f"ERROR: Reference JSON not found: {args.reference_json}")
        return 1
    
    # Load and display reference data
    print_header("SDOM Automation - Reference Data")
    ref_data = json.loads(args.reference_json.read_text(encoding="utf-8"))
    print(f"  IFD ID: {ref_data.get('ifd_id', 'UNKNOWN')}")
    print(f"  Mode: {ref_data.get('mode', 'N/A')}")
    
    if args.dry_run:
        print_header("DRY RUN - No changes will be made")
        print(f"  Mode: {args.mode}")
        print(f"  Reference: {args.reference_json}")
        
        if args.mode in ("checkout-bc", "checkout-all"):
            bc_ref = ref_data.get("bc_checkout")
            if bc_ref:
                print(f"\n  BC Checkout:")
                print(f"    Base: {bc_ref.get('version_input')}")
                print(f"    Target: {bc_ref.get('derived_version')}")

        if args.mode in ("checkout-fc", "checkout-all"):
            fc_ref = ref_data.get("fc_checkout")
            if fc_ref:
                print(f"\n  FC Checkout:")
                print(f"    Base: {fc_ref.get('version_input')}")
                print(f"    Target: {fc_ref.get('derived_version')}")

        if args.mode in ("checkout-fcp", "checkout-all"):
            fcp_ref = ref_data.get("fcp_checkout")
            if fcp_ref:
                print(f"\n  FCP Checkout:")
                print(f"    Base: {fcp_ref.get('version_input')}")
                print(f"    Target: {fcp_ref.get('derived_version')}")
        
        if args.mode in ("checkout-pver", "checkout-all"):
            pver_ref = ref_data.get("pver_checkout")
            if pver_ref:
                print(f"\n  PVER Checkout:")
                print(f"    Base: {pver_ref.get('version_input')}")
                print(f"    Target: {pver_ref.get('derived_version')}")
        
        return 0
    
    # Create authenticated session
    session = create_session(args.user, args.password)
    
    results = {}
    
    # Checkout BC
    if args.mode in ("checkout-bc", "checkout-all"):
        print_header("Checking out BC...")
        result = checkout_bc_from_reference(session, args.reference_json, args.comment)
        results["bc"] = result
        if result.get("status") == "success":
            print(f"  [OK] BC: {result.get('resolved_fullname')}")
            if result.get("fallback_used"):
                updated = update_reference_on_fallback(
                    args.reference_json,
                    "bc_checkout",
                    str(result.get("target_fullname", "")),
                    str(result.get("resolved_fullname", "")),
                )
                if updated:
                    print("  [INFO] BC reference updated with resolved checkout version")
        else:
            print(f"  [ERROR] BC: {result.get('error')}")

    # Checkout FC
    if args.mode in ("checkout-fc", "checkout-all"):
        print_header("Checking out FC...")
        result = checkout_fc_from_reference(session, args.reference_json, args.comment)
        results["fc"] = result
        if result.get("status") == "success":
            print(f"  [OK] FC: {result.get('resolved_fullname')}")
            if result.get("fallback_used"):
                updated = update_reference_on_fallback(
                    args.reference_json,
                    "fc_checkout",
                    str(result.get("target_fullname", "")),
                    str(result.get("resolved_fullname", "")),
                )
                if updated:
                    print("  [INFO] FC reference updated with resolved checkout version")
        else:
            print(f"  [ERROR] FC: {result.get('error')}")

    # Checkout FCP
    if args.mode in ("checkout-fcp", "checkout-all"):
        print_header("Checking out FCP...")
        result = checkout_fcp_from_reference(session, args.reference_json, args.comment)
        results["fcp"] = result
        if result.get("status") == "success":
            print(f"  [OK] FCP: {result.get('resolved_fullname')}")
            if result.get("fallback_used"):
                updated = update_reference_on_fallback(
                    args.reference_json,
                    "fcp_checkout",
                    str(result.get("target_fullname", "")),
                    str(result.get("resolved_fullname", "")),
                )
                if updated:
                    print("  [INFO] FCP reference updated with resolved checkout version")
        else:
            print(f"  [ERROR] FCP: {result.get('error')}")
    
    # Checkout PVER
    if args.mode in ("checkout-pver", "checkout-all"):
        print_header("Checking out PVER...")
        result = checkout_pver_from_reference(session, args.reference_json, args.comment)
        results["pver"] = result
        if result.get("status") == "success":
            print(f"  [OK] PVER: {result.get('resolved_fullname')}")
            if result.get("fallback_used"):
                updated = update_reference_on_fallback(
                    args.reference_json,
                    "pver_checkout",
                    str(result.get("target_fullname", "")),
                    str(result.get("resolved_fullname", "")),
                )
                if updated:
                    print("  [INFO] PVER reference updated with resolved checkout version")
        else:
            print(f"  [ERROR] PVER: {result.get('error')}")
    
    # Summary
    print_header("Checkout Results")
    for obj_type, result in results.items():
        status = result.get("status", "unknown")
        if status == "success":
            print(f"  [OK] {obj_type.upper()}: {result.get('resolved_fullname')}")
        else:
            print(f"  [ERROR] {obj_type.upper()}: {result.get('error')}")
    
    # Return appropriate exit code
    failed = sum(1 for r in results.values() if r.get("status") != "success")
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    exit(main())
