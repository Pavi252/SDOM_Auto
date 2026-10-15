"""Extract FC/FCP/CC conf/BC/PVER checkout reference from RQ1 context JSON.

Usage:
  python extract_sdom_checkout_reference.py <rq1_context_path> [--output <reference_json_path>]

Example:
  python extract_sdom_checkout_reference.py \
    RQ1_AUTO/context/RQONE04873498/rq1_context.json \
    --output SDOM_Auto/sdom_checkout_reference_RQONE04873498.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def extract_checkout_reference(rq1_context: dict[str, Any]) -> dict[str, Any]:
    """Extract FC/FCP/CC conf/BC/PVER checkout info from RQ1 context JSON.
    
    Returns:
        {
          "ifd_id": "RQONE04873498",
          "mode": "base",
          "plan_key": "base_fc",
          "fc_checkout": {...},
          "cc_conf_checkout": {...},
          "bc_checkout": {...},
          "pver_checkout": {...}
        }
    """
    ifd_id = rq1_context.get("ifd_id", "")
    sdom_section = rq1_context.get("sdom", {})
    checkout_plan = sdom_section.get("checkout_plan", {})
    mode = sdom_section.get("mode", "")
    
    # Map mode to plan key (e.g., "base" -> "base_fc", "new" -> "new_fc")
    plan_key = f"{mode}_fc"
    applicable_section = checkout_plan.get(plan_key, {})
    if not applicable_section:
        raise ValueError(f"No applicable checkout plan for mode '{mode}' (plan_key={plan_key})")
    
    return {
        "ifd_id": ifd_id,
        "mode": mode,
        "plan_key": plan_key,
        "fc_checkout": applicable_section.get("fc_checkout"),
        "fcp_checkout": applicable_section.get("fcp_checkout"),
        "cc_conf_checkout": applicable_section.get("cc_conf_checkout"),
        "bc_checkout": applicable_section.get("bc_checkout"),
        "pver_checkout": applicable_section.get("pver_checkout"),
    }


def extract_base_and_target(checkout_info: dict[str, Any], object_type: str) -> tuple[str, str]:
    """Extract base fullname and target (derived) version for BC/PVER.
    
    Args:
        checkout_info: Output from extract_checkout_reference()
        object_type: 'bc' or 'pver'
    
    Returns:
        (base_fullname, target_fullname)
    """
    key = f"{object_type}_checkout"
    checkout_obj = checkout_info.get(key)
    if not checkout_obj:
        raise ValueError(f"No {key} in checkout info")
    
    base_fullname = (checkout_obj.get("version_input") or "").strip()
    target_fullname = (checkout_obj.get("derived_version") or "").strip()
    
    if not base_fullname or not target_fullname:
        raise ValueError(f"Incomplete {key}: base='{base_fullname}' target='{target_fullname}'")
    
    return base_fullname, target_fullname


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract SDOM checkout reference from RQ1 context JSON"
    )
    parser.add_argument(
        "rq1_context",
        type=Path,
        help="Path to RQ1 context JSON file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output reference JSON path (default: derive from RQ1 context)",
    )
    
    args = parser.parse_args()
    
    if not args.rq1_context.exists():
        print(f"ERROR: RQ1 context file not found: {args.rq1_context}")
        return 1
    
    # Load RQ1 context
    rq1_data = json.loads(args.rq1_context.read_text(encoding="utf-8"))
    
    # Extract checkout reference
    checkout_ref = extract_checkout_reference(rq1_data)
    
    # Determine output path
    output_path = args.output
    if output_path is None:
        ifd_id = checkout_ref.get("ifd_id", "UNKNOWN")
        output_path = args.rq1_context.parent / f"sdom_checkout_reference_{ifd_id}.json"
    
    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Save reference JSON
    output_path.write_text(json.dumps(checkout_ref, indent=2, ensure_ascii=False), encoding="utf-8")
    
    print(f"[result] Checkout reference extracted: {output_path}")
    print(f"  IFD ID: {checkout_ref.get('ifd_id')}")
    print(f"  Mode: {checkout_ref.get('mode')}")
    
    cc_checkout = checkout_ref.get("cc_conf_checkout")
    if cc_checkout:
        print(f"  CC conf kind: {cc_checkout.get('version_kind')}")

    # Show BC/PVER details
    bc_checkout = checkout_ref.get("bc_checkout")
    if bc_checkout:
        print(f"  BC base: {bc_checkout.get('version_input')}")
        print(f"  BC target: {bc_checkout.get('derived_version')}")
    
    pver_checkout = checkout_ref.get("pver_checkout")
    if pver_checkout:
        print(f"  PVER base: {pver_checkout.get('version_input')}")
        print(f"  PVER target: {pver_checkout.get('derived_version')}")
    
    return 0


if __name__ == "__main__":
    exit(main())
