"""
replace_fc_files.py - Replace manual code files in checked-out FC workspace

This script takes generated files from Manual_Code/Generated/ and replaces them
in the checked-out FC structure within SDOM_Auto/checkout_workspace/

File mapping:
  Manual_Code/Generated/{module_name}.c              → InjMngt_H2InjrLo.cont/{module_name}.c
  Manual_Code/Generated/{module_name}.h              → InjMngt_H2InjrLo.cont/{module_name}.h
  Manual_Code/Generated/{module_name}_pavast.xml     → InjMngt_H2InjrLo.cont/{module_name}_pavast.xml
  Manual_Code/Generated/{module_name}_fs.xml         → InjMngt_H2InjrLo.cont/InjMngt_H2InjrLo.cont/{module_name}_fs.xml

Workflow:
  1. Locate the checked-out FC folder (FC-ARB_EcuDiag_InjMngt_*)
  2. Navigate to EcuDiag_InjMngt.cont/InjMngt_H2InjrLo.cont/ (non-gen container)
  3. Replace C, header, PaVaSt files at this level
  4. Replace _fs.xml file in nested InjMngt_H2InjrLo.cont/ subfolder
"""

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def _find_checkout_fc_folder(checkout_workspace: Path, fc_prefix: str = "FC-ARB_EcuDiag") -> Optional[Path]:
    """Find the FC folder in checkout_workspace, e.g., FC-ARB_EcuDiag_InjMngt_358.2.0_0"""
    if not checkout_workspace.exists():
        return None
    
    for item in checkout_workspace.iterdir():
        if item.is_dir() and item.name.startswith(fc_prefix):
            return item
    
    return None


def _get_fc_container_path(fc_folder: Path) -> Optional[Path]:
    """Get path to EcuDiag_InjMngt.cont (main container)"""
    ec_cont = fc_folder / "EcuDiag_InjMngt.cont"
    if ec_cont.exists():
        return ec_cont
    
    return None


def _get_non_gen_fc_part(ec_cont: Path, module_name: str = "InjMngt_H2InjrLo") -> Optional[Path]:
    """Get path to InjMngt_H2InjrLo.cont (non-gen FC part)"""
    non_gen = ec_cont / f"{module_name}.cont"
    if non_gen.exists() and non_gen.is_dir():
        return non_gen
    
    return None


def _get_nested_container(non_gen_fc: Path, module_name: str = "InjMngt_H2InjrLo") -> Optional[Path]:
    """Get path to nested InjMngt_H2InjrLo.cont/ subfolder (for _fs.xml)"""
    nested = non_gen_fc / f"{module_name}.cont"
    if nested.exists() and nested.is_dir():
        return nested
    
    return None


def replace_fc_files(
    manual_code_dir: Path,
    checkout_workspace: Path,
    module_name: str = "InjMngt_H2InjrLo",
) -> bool:
    """
    Replace FC files in checked-out workspace with generated manual code files.
    
    Returns: True if successful, False otherwise
    """
    logger.info("════════════════════════════════════════════════════════════════")
    logger.info("SDOM FC File Replacement: Manual Code → Checked-out Workspace")
    logger.info("════════════════════════════════════════════════════════════════")
    
    # Step 1: Find checkout FC folder
    fc_folder = _find_checkout_fc_folder(checkout_workspace)
    if not fc_folder:
        logger.error("No FC folder found in checkout_workspace: %s", checkout_workspace)
        return False
    logger.info("Found FC folder: %s", fc_folder.name)
    
    # Step 2: Get EcuDiag_InjMngt.cont
    ec_cont = _get_fc_container_path(fc_folder)
    if not ec_cont:
        logger.error("EcuDiag_InjMngt.cont not found in FC folder")
        return False
    logger.info("Found main container: %s", ec_cont.name)
    
    # Step 3: Get non-gen FC part (InjMngt_H2InjrLo.cont)
    non_gen_fc = _get_non_gen_fc_part(ec_cont, module_name)
    if not non_gen_fc:
        logger.error("Non-gen FC part %s.cont not found in main container", module_name)
        return False
    logger.info("Found non-gen FC part: %s", non_gen_fc.name)
    
    # Step 4: Get nested container (for _fs.xml)
    nested_cont = _get_nested_container(non_gen_fc, module_name)
    if not nested_cont:
        logger.error("Nested container %s.cont not found in non-gen FC part", module_name)
        return False
    logger.info("Found nested container: %s", nested_cont.name)
    
    # Step 5: Replace files at non-gen FC level
    logger.info("")
    logger.info("Step 1: Replacing files in non-gen FC part (%s.cont/)", module_name)
    logger.info("─" * 64)
    
    files_to_replace = [
        (manual_code_dir / f"{module_name}.c", non_gen_fc / f"{module_name}.c"),
        (manual_code_dir / f"{module_name}.h", non_gen_fc / f"{module_name}.h"),
        (manual_code_dir / f"{module_name}_pavast.xml", non_gen_fc / f"{module_name}_pavast.xml"),
    ]
    
    for src, dst in files_to_replace:
        if not src.exists():
            logger.warning("Source file not found: %s", src)
            continue
        
        try:
            shutil.copy2(src, dst)
            logger.info("✓ Replaced: %s", dst.name)
        except Exception as e:
            logger.error("✗ Failed to replace %s: %s", dst.name, e)
            return False
    
    # Step 6: Replace _fs.xml in nested container
    logger.info("")
    logger.info("Step 2: Replacing _fs.xml in nested container (.cont/%s.cont/)", module_name)
    logger.info("─" * 64)
    
    src_fs = manual_code_dir / f"{module_name}_fs.xml"
    dst_fs = nested_cont / f"{module_name}_fs.xml"
    
    if not src_fs.exists():
        logger.warning("Source _fs.xml not found: %s", src_fs)
    else:
        try:
            shutil.copy2(src_fs, dst_fs)
            logger.info("✓ Replaced: %s", dst_fs.name)
        except Exception as e:
            logger.error("✗ Failed to replace %s: %s", dst_fs.name, e)
            return False
    
    logger.info("")
    logger.info("════════════════════════════════════════════════════════════════")
    logger.info("✓ All FC files replaced successfully")
    logger.info("  Workspace: %s", fc_folder)
    logger.info("════════════════════════════════════════════════════════════════")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Replace FC files in checked-out SDOM workspace with manual code"
    )
    ap.add_argument(
        "--manual-code-dir",
        type=Path,
        default=Path(__file__).parent.parent / "Manual_Code" / "Generated",
        help="Source directory with generated manual code files (default: Manual_Code/Generated/)",
    )
    ap.add_argument(
        "--checkout-workspace",
        type=Path,
        default=Path(__file__).parent / "checkout_workspace",
        help="SDOM checkout workspace directory (default: SDOM_Auto/checkout_workspace/)",
    )
    ap.add_argument(
        "--module-name",
        default="InjMngt_H2InjrLo",
        help="Module name for files to replace (default: InjMngt_H2InjrLo)",
    )
    args = ap.parse_args()
    
    success = replace_fc_files(
        manual_code_dir=args.manual_code_dir,
        checkout_workspace=args.checkout_workspace,
        module_name=args.module_name,
    )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
