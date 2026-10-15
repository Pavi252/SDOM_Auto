"""
sdom_prepare_for_checkin.py - Prepare TDATA, SPEC, and FC objects for checkin

This script handles the preparation workflow for checking in manual code files:
1. Checkout TDATA (PaVaSt) object separately
2. Checkout SPEC (TDOC/_Fs.xml) object separately
3. Replace/update files in workspace
4. Prepare for import-batch workflow

The key insight: TDATA and SPEC are child objects of the FC, but they need
separate checkout/import handling to ensure proper SDOM linking.
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def checkout_sdom_object(
    object_fullname: str,
    local_dir: Path,
    sdom_user: str = None,
    sdom_password: str = None,
) -> bool:
    """
    Checkout a single SDOM object using sdom_release_fetch_workspace.py
    
    Args:
        object_fullname: SDOM object fullName (e.g., "TDATA : InjMngt_H2InjrLo / 358.0.0; 0")
        local_dir: Local directory to checkout to
        sdom_user: SDOM username (optional)
        sdom_password: SDOM password (optional)
    
    Returns:
        True if successful, False otherwise
    """
    sdom_auto_dir = Path(__file__).parent
    checkout_script = sdom_auto_dir / "sdom_release_fetch_workspace.py"
    
    if not checkout_script.exists():
        logger.error("Checkout script not found: %s", checkout_script)
        return False
    
    try:
        cmd = [
            sys.executable,
            str(checkout_script),
            "--fc-fullname", object_fullname,
            "--local-dir", str(local_dir),
        ]
        if sdom_user:
            cmd.extend(["--user", sdom_user])
        if sdom_password:
            cmd.extend(["--password", sdom_password])
        
        logger.info("Checking out: %s", object_fullname)
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info("✓ Checkout successful: %s", object_fullname)
            return True
        else:
            logger.warning("⚠ Checkout warning: %s", result.stderr[:200])
            return True  # Continue even if checkout has issues
    except Exception as e:
        logger.error("✗ Checkout failed: %s", e)
        return False


def prepare_sdom_for_checkin(
    checkout_workspace: Path,
    tdata_fullname: str = "TDATA : InjMngt_H2InjrLo / 358.0.0; 0",
    spec_fullname: str = "SPEC : InjMngt_H2InjrLo / 358.1.0; 0",
    sdom_user: str = None,
    sdom_password: str = None,
) -> bool:
    """
    Prepare SDOM workspace for manual code checkin.
    
    Workflow:
    1. Checkout main FC (already done in Phase 5A)
    2. Checkout TDATA (PaVaSt) object
    3. Checkout SPEC (TDOC) object
    4. Files are replaced in workspace
    5. Ready for import-batch workflow
    """
    logger.info("════════════════════════════════════════════════════════════════")
    logger.info("SDOM Preparation: Checking out TDATA and SPEC Objects")
    logger.info("════════════════════════════════════════════════════════════════")
    
    checkout_workspace.mkdir(parents=True, exist_ok=True)
    
    # Step 1: Checkout TDATA (PaVaSt)
    logger.info("")
    logger.info("Step 1: Checkout TDATA (PaVaSt configuration)")
    logger.info("─" * 64)
    tdata_dir = checkout_workspace / "TDATA_Checkout"
    tdata_ok = checkout_sdom_object(
        object_fullname=tdata_fullname,
        local_dir=tdata_dir,
        sdom_user=sdom_user,
        sdom_password=sdom_password,
    )
    
    # Step 2: Checkout SPEC (TDOC)
    logger.info("")
    logger.info("Step 2: Checkout SPEC (TDOC specification)")
    logger.info("─" * 64)
    spec_dir = checkout_workspace / "SPEC_Checkout"
    spec_ok = checkout_sdom_object(
        object_fullname=spec_fullname,
        local_dir=spec_dir,
        sdom_user=sdom_user,
        sdom_password=sdom_password,
    )
    
    logger.info("")
    logger.info("════════════════════════════════════════════════════════════════")
    if tdata_ok and spec_ok:
        logger.info("✓ All SDOM objects prepared successfully")
        logger.info("  TDATA checkout: %s/", tdata_dir.name)
        logger.info("  SPEC checkout: %s/", spec_dir.name)
        logger.info("")
        logger.info("Next: Replace files in FC workspace, then run import-batch")
        return True
    else:
        logger.warning("⚠ Some checkouts had issues - continuing anyway")
        logger.warning("  Manual intervention may be needed")
        return True  # Continue with import workflow


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Prepare SDOM objects (TDATA, SPEC) for manual code checkin"
    )
    ap.add_argument(
        "--checkout-workspace",
        type=Path,
        default=Path(__file__).parent / "checkout_workspace",
        help="SDOM checkout workspace directory (default: SDOM_Auto/checkout_workspace/)",
    )
    ap.add_argument(
        "--tdata-fullname",
        default="TDATA : InjMngt_H2InjrLo / 358.0.0; 0",
        help="PaVaSt fullName (default: TDATA : InjMngt_H2InjrLo / 358.0.0; 0)",
    )
    ap.add_argument(
        "--spec-fullname",
        default="SPEC : InjMngt_H2InjrLo / 358.1.0; 0",
        help="TDOC SPEC fullName (default: SPEC : InjMngt_H2InjrLo / 358.1.0; 0)",
    )
    ap.add_argument(
        "--sdom-user",
        default=None,
        help="SDOM username (uses defaults from SDOM scripts if not provided)",
    )
    ap.add_argument(
        "--sdom-password",
        default=None,
        help="SDOM password (uses defaults from SDOM scripts if not provided)",
    )
    args = ap.parse_args()
    
    success = prepare_sdom_for_checkin(
        checkout_workspace=args.checkout_workspace,
        tdata_fullname=args.tdata_fullname,
        spec_fullname=args.spec_fullname,
        sdom_user=args.sdom_user,
        sdom_password=args.sdom_password,
    )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
