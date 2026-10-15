#!/usr/bin/env python3
"""
Execute Manual SDOM Import from Batch JSON
============================================

Converts import_batch.json to CSV format and executes sdom_Checkin.py
in import-batch mode to actually upload files to SDOM.

Input: import_batch.json (from sdom_manual_checkin.py)
Output: Runs sdom_Checkin.py --mode import-batch to import files
"""

import json
import csv
import sys
import logging
import subprocess
from pathlib import Path
from typing import Dict, List

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s | %(message)s'
)
logger = logging.getLogger(__name__)


class ManualImportExecutor:
    """Convert JSON batch to CSV and execute SDOM import."""

    def __init__(self, batch_json: Path, sdom_user: str = None, sdom_password: str = None):
        """Initialize import executor.
        
        Args:
            batch_json: Path to import_batch.json (from sdom_manual_checkin.py)
            sdom_user: SDOM username (optional)
            sdom_password: SDOM password (optional)
        """
        self.batch_json = batch_json
        self.workspace_dir = batch_json.parent
        self.sdom_user = sdom_user
        self.sdom_password = sdom_password
        self.batch = {}

    def load_batch(self) -> bool:
        """Load import batch from JSON.
        
        Returns:
            True if loaded successfully
        """
        try:
            with open(self.batch_json, 'r', encoding='utf-8') as f:
                self.batch = json.load(f)
            logger.info(f"✓ Loaded import batch from {self.batch_json.name}")
            logger.info(f"  Objects: {len(self.batch.get('objects', []))}")
            return True
        except Exception as e:
            logger.error(f"Failed to load batch: {e}")
            return False

    def convert_to_csv(self, csv_path: Path, root_fullname: str = None) -> bool:
        """Convert JSON batch to CSV format for sdom_Checkin.py.
        
        CSV format: file_path, file_name, object_fullname, file_class, change_type, root_fullname
        
        Args:
            csv_path: Output CSV path
            root_fullname: Root SDOM fullname (optional, will try to infer from batch)
            
        Returns:
            True if converted successfully
        """
        try:
            objects = self.batch.get('objects', [])
            
            # If not provided, try to get from batch
            if not root_fullname:
                root_fullname = self.batch.get('fc_fullname', '')
            
            rows = []
            
            for obj in objects:
                obj_type = obj.get('type', '')
                file_path = obj.get('path', '')
                filename = Path(file_path).name if file_path else ''
                
                # Determine object_fullname from type and fullname/name
                if 'fullname' in obj:
                    # Explicit fullname (TDATA, SPEC)
                    object_fullname = obj['fullname']
                else:
                    # For SWSRC/SWHDR, use name as base
                    object_fullname = f"{obj_type}:{obj.get('name', '')}"
                
                rows.append({
                    'file_path': file_path,
                    'file_name': filename,
                    'object_fullname': object_fullname,
                    'file_class': obj_type,
                    'change_type': obj.get('action', 'replace'),
                    'root_fullname': root_fullname
                })
                logger.info(f"  → {obj_type}: {Path(file_path).name}")
            
            # Write CSV
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=['file_path', 'file_name', 'object_fullname', 'file_class', 'change_type', 'root_fullname']
                )
                writer.writeheader()
                writer.writerows(rows)
            
            logger.info(f"✓ Converted to CSV: {csv_path.name} ({len(rows)} objects)")
            return True
        except Exception as e:
            logger.error(f"Failed to convert to CSV: {e}")
            return False

    def execute_import(self, csv_path: Path, sdom_checkin_script: Path) -> bool:
        """Execute sdom_Checkin.py in import-single mode for each file.
        
        Uses import-single mode to directly import each file with its explicit fullname,
        rather than trying to auto-lookup base objects.
        
        Args:
            csv_path: Path to import CSV
            sdom_checkin_script: Path to sdom_Checkin.py
            
        Returns:
            True if import succeeded
        """
        try:
            # Read CSV and import each file individually
            with open(csv_path, 'r', encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    logger.error("CSV has no header")
                    return False
                
                rows = list(reader)
                if not rows:
                    logger.error("CSV has no data rows")
                    return False
                
                logger.info("Executing SDOM import...")
                logger.info("")
                
                for idx, row in enumerate(rows, start=1):
                    file_path = row.get('file_path', '').strip()
                    obj_fullname = row.get('object_fullname', '').strip()
                    file_name = row.get('file_name', '').strip()
                    
                    if not file_path or not obj_fullname:
                        logger.warning(f"  Row {idx}: Skipping - missing file_path or object_fullname")
                        continue
                    
                    # Use import-single for each file
                    cmd = [
                        sys.executable,
                        str(sdom_checkin_script),
                        "--mode", "import-single",
                        "--file-path", file_path,
                        "--file-name", file_name,
                        "--object-fullname", obj_fullname,
                        "--checkout-mode", "none",
                        "--edition",
                        "--no-checkin-flag",  # Import to inbearb, don't checkin yet
                    ]
                    
                    if self.sdom_user:
                        cmd.extend(["--user", self.sdom_user])
                    if self.sdom_password:
                        cmd.extend(["--password", self.sdom_password])
                    
                    logger.info(f"  Importing ({idx}/{len(rows)}): {file_name} → {obj_fullname}")
                    
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    
                    if result.returncode != 0:
                        logger.error(f"    ✗ Import failed")
                        if "[result]" in result.stdout:
                            for line in result.stdout.split('\n'):
                                if "[result]" in line:
                                    logger.error(f"    {line.strip()}")
                        return False
                    else:
                        logger.info(f"    ✓ Imported successfully")
                        if "[result]" in result.stdout:
                            for line in result.stdout.split('\n'):
                                if "[result]" in line and ("imported" in line.lower() or "created" in line.lower()):
                                    logger.info(f"      {line.strip()}")
                
                logger.info("")
                logger.info("✓ SDOM import completed successfully")
                logger.info("  Files imported to repository (inbearb mode)")
                logger.info("  Review changes in SDOM and finalize checkin")
                return True
                
        except subprocess.TimeoutExpired:
            logger.error("Import timeout expired")
            return False
        except Exception as e:
            logger.error(f"Import execution failed: {e}")
            return False

    def run(self) -> bool:
        """Execute full import workflow.
        
        Returns:
            True if workflow succeeded
        """
        logger.info("")
        logger.info("╔═══════════════════════════════════════════╗")
        logger.info("║  EXECUTING MANUAL SDOM IMPORT             ║")
        logger.info("╚═══════════════════════════════════════════╝")
        logger.info("")
        
        # Load batch
        if not self.load_batch():
            return False
        
        # Convert to CSV
        csv_path = self.workspace_dir / "import_batch.csv"
        if not self.convert_to_csv(csv_path):
            return False
        
        # Execute import
        sdom_script_dir = Path(__file__).parent
        sdom_script = sdom_script_dir / "sdom_Checkin.py"
        
        if not sdom_script.exists():
            logger.error(f"sdom_Checkin.py not found: {sdom_script}")
            return False
        
        return self.execute_import(csv_path, sdom_script)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Execute manual SDOM import from batch JSON',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sdom_execute_manual_import.py \\
    --batch-json "./checkout_workspace/import_batch.json" \\
    --sdom-user "<user>" \\
    --sdom-password "password"
        """
    )
    
    parser.add_argument('--batch-json', type=Path, required=True,
                       help='Path to import_batch.json (from sdom_manual_checkin.py)')
    parser.add_argument('--sdom-user', default=None,
                       help='SDOM username (prompts if omitted)')
    parser.add_argument('--sdom-password', default=None,
                       help='SDOM password (prompts if omitted)')
    
    args = parser.parse_args()
    
    if not args.batch_json.exists():
        logger.error(f"Batch JSON not found: {args.batch_json}")
        sys.exit(1)
    
    executor = ManualImportExecutor(args.batch_json, args.sdom_user, args.sdom_password)
    success = executor.run()
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
