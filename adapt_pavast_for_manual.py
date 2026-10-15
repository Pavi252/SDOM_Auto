"""
adapt_pavast_for_manual.py - Adapt PaVaSt naming for manual (non-gen) FC part

For manual FC parts (without _GEN suffix), the PaVaSt component naming must be:
  - SW-FEATURE: EcuDiag_InjMngt (parent FC name) - KEEP AS IS
  - SW-FEATURE-VARIANT: InjMngt_H2InjrLo (NOT EcuDiag_InjMngt_H2InjrLo_GEN)
  - Export interface: InjMngt_H2InjrLo_Ex (NOT EcuDiag_InjMngt_H2InjrLo_Ex)
  - Import interface: InjMngt_H2InjrLo_Im (NOT EcuDiag_InjMngt_H2InjrLo_Im)

This ensures SDOM properly links the manual PaVaSt to the FC-part container.
"""

import argparse
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def adapt_pavast_naming(
    pavast_file: Path,
    parent_fc: str = "EcuDiag_InjMngt",
    fc_part_name: str = "InjMngt_H2InjrLo",
) -> bool:
    """
    Adapt PaVaSt XML naming for manual (non-gen) FC part.
    
    Replaces:
      - OLD: EcuDiag_InjMngt_H2InjrLo_GEN → NEW: InjMngt_H2InjrLo
      - OLD: EcuDiag_InjMngt_H2InjrLo_Ex → NEW: InjMngt_H2InjrLo_Ex
      - OLD: EcuDiag_InjMngt_H2InjrLo_Im → NEW: InjMngt_H2InjrLo_Im
    """
    if not pavast_file.exists():
        logger.error("PaVaSt file not found: %s", pavast_file)
        return False
    
    try:
        with open(pavast_file, "r", encoding="iso-8859-1") as f:
            content = f.read()
        
        original_content = content
        
        # Define replacements (order matters - most specific first!)
        # Note: The generated PaVaSt uses combined naming like "EcuDiag_InjMngt_H2InjrLo_GEN"
        # Extract just the suffix part (H2InjrLo) from fc_part_name (InjMngt_H2InjrLo)
        fc_parts = fc_part_name.split("_")
        suffix = fc_parts[-1]  # Get last part after split
        
        combined_gen = f"{parent_fc}_{suffix}_GEN"
        combined_ex = f"{parent_fc}_{suffix}_Ex"
        combined_im = f"{parent_fc}_{suffix}_Im"
        
        replacements = [
            # Replace export/import interfaces first
            (combined_ex, f"{fc_part_name}_Ex"),
            (combined_im, f"{fc_part_name}_Im"),
            # Then replace _GEN variant name
            (combined_gen, fc_part_name),
        ]
        
        for old, new in replacements:
            if old in content:
                count = content.count(old)
                content = content.replace(old, new)
                logger.info("  ✓ Replaced %d occurrence(s): %s → %s", count, old, new)
        
        # Verify changes were made
        if content == original_content:
            logger.warning("⚠ No replacements made - checking actual file content...")
            # Debug: show what patterns we're looking for
            logger.info("  Pattern 1 (%s): %s", "Ex", combined_ex)
            logger.info("  Pattern 2 (%s): %s", "Im", combined_im)
            logger.info("  Pattern 3 (%s): %s", "GEN", combined_gen)
            
            # Check if it's already been adapted
            if fc_part_name in content and "_GEN" not in content:
                logger.info("  File appears to be already adapted")
        
        # Write back
        with open(pavast_file, "w", encoding="iso-8859-1") as f:
            f.write(content)
        
        logger.info("✓ PaVaSt file adapted: %s", pavast_file.name)
        return True
    
    except Exception as e:
        logger.error("✗ Failed to adapt PaVaSt: %s", e)
        return False


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Adapt PaVaSt naming for manual (non-gen) FC part"
    )
    ap.add_argument(
        "--pavast-file",
        type=Path,
        required=True,
        help="Path to PaVaSt XML file to adapt",
    )
    ap.add_argument(
        "--parent-fc",
        default="EcuDiag_InjMngt",
        help="Parent FC name (default: EcuDiag_InjMngt)",
    )
    ap.add_argument(
        "--fc-part-name",
        default="InjMngt_H2InjrLo",
        help="FC-part name for naming scheme (default: InjMngt_H2InjrLo)",
    )
    args = ap.parse_args()
    
    logger.info("════════════════════════════════════════════════════════════════")
    logger.info("Adapting PaVaSt Naming for Manual FC Part")
    logger.info("════════════════════════════════════════════════════════════════")
    
    success = adapt_pavast_naming(
        pavast_file=args.pavast_file,
        parent_fc=args.parent_fc,
        fc_part_name=args.fc_part_name,
    )
    
    logger.info("════════════════════════════════════════════════════════════════")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

