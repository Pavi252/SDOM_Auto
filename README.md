# SDOM Automation - Modular Architecture

## Overview

Reorganized SDOM checkout/checkin operations into modular, object-type-specific packages with shared utilities to eliminate redundancy.

**Directory Structure**:
```
SDOM_Auto/
├── sdom_automation.py          ← Main CLI entry point
├── common/                     ← Shared utilities (DRY)
│   ├── __init__.py
│   ├── sdom_api.py            ← Core SDOM REST API helpers
│   └── extract_sdom_checkout_reference.py  ← Reference JSON extraction
├── bc_operations/              ← BC-specific logic
│   ├── __init__.py
│   └── bc_checkout.py         ← BC checkout with suffix transformation
├── pver_operations/            ← PVER-specific logic
│   ├── __init__.py
│   └── pver_checkout.py       ← PVER checkout
├── fc_operations/              ← FC-specific logic (ready for extension)
│   └── __init__.py
├── reference_data/             ← Sample reference JSON files
│   └── sdom_checkout_reference_RQONE04873498.json
└── [legacy scripts]            ← Keep for backward compatibility
    ├── sdom_Checkin.py        (old monolithic version)
    ├── sdom_manual_*.py       (deprecated)
    └── ...
```

---

## Quick Start

### 1. Generate Reference JSON from RQ1 Context
```bash
python SDOM_Auto/common/extract_sdom_checkout_reference.py \
  RQ1_AUTO/context/RQONE04873498/rq1_context.json \
  --output SDOM_Auto/reference_data/sdom_checkout_reference_RQONE04873498.json
```

### 2. Checkout FC, BC & PVER
```bash
# Checkout all
python SDOM_Auto/sdom_automation.py --mode checkout-all \
  --reference-json SDOM_Auto/reference_data/sdom_checkout_reference_RQONE04873498.json \
  --user <your-user>

# Checkout only FC
python SDOM_Auto/sdom_automation.py --mode checkout-fc \
  --reference-json <path> --user <your-user>

# Checkout only BC
python SDOM_Auto/sdom_automation.py --mode checkout-bc \
  --reference-json <path> --user <your-user>

# Checkout only PVER
python SDOM_Auto/sdom_automation.py --mode checkout-pver \
  --reference-json <path> --user <your-user>

# Dry-run (preview, no changes)
python SDOM_Auto/sdom_automation.py --mode checkout-all \
  --reference-json <path> --dry-run
```

---

## Module Descriptions

### `common/sdom_api.py` - Shared REST API Utilities
Core functions used by all object types:

- **Session Management**
  - `create_session(user, password)`: Create authenticated SDOM session

- **Fullname Parsing**
  - `parse_fullname(fullname)`: Parse into class/name/variant/revision
  - `build_fullname(cls, name, variant, revision)`: Construct fullname

- **Variant Resolution**
  - `next_suffix_variant(variant)`: Increment suffix (TEST_1 → TEST_2)
  - `version_exists(session, fullname)`: Check if version exists in SDOM
  - `find_free_variant(session, base, desired, max_attempts)`: Auto-resolve conflicts

- **Error Handling**
  - `extract_error_message(response_text)`: Parse SDOM error XML
  - `SDOMCheckoutError`, `SDOMConflictError`: Custom exceptions

### `common/extract_sdom_checkout_reference.py` - Reference JSON Extraction
Converts RQ1 context JSON to simplified reference format:

```bash
python SDOM_Auto/common/extract_sdom_checkout_reference.py <rq1_json> --output <ref_json>
```

**Input** (RQ1 context):
```json
{
  "ifd_id": "RQONE04873498",
  "sdom": {
    "checkout_plan": {
      "base_fc": {
        "bc_checkout": {
          "version_input": "BC : DiaApl / 358.25.0_H2CU_8; 0",
          "derived_version": "BC : DiaApl / 358.25.0_H2CU_8_TEST_1; 0"
        }
      }
    }
  }
}
```

**Output** (Simplified reference):
```json
{
  "ifd_id": "RQONE04873498",
  "bc_checkout": {
    "version_input": "BC : DiaApl / 358.25.0_H2CU_8; 0",
    "derived_version": "BC : DiaApl / 358.25.0_H2CU_8_TEST_1; 0"
  },
  "pver_checkout": {
    "version_input": "PVER : BMW_H2CU / H2CU_31A200812B; 1",
    "derived_version": "PVER : BMW_H2CU / H2CU_31A200812B_test; 1"
  }
}
```

### `bc_operations/bc_checkout.py` - BC Checkout Logic
Handles BC-specific checkout including suffix transformation:

```python
from bc_operations.bc_checkout import (
    transform_bc_suffix,          # test → _TEST_1
    load_bc_reference,            # Load from JSON
    checkout_bc,                  # Core checkout logic
    checkout_bc_from_reference,   # Reference-based checkout
)
```

**Key Feature**: BC suffix transformation
- Input suffix: `test`, `test1`, `v1`
- Output: `_TEST_1`, `_TEST_1`, `_V_1` (SDOM-compatible platform pattern)
- Reason: SDOM requires `_LETTER_NUMBER` format for BC variants

### `fc_operations/fc_checkout.py` - FC Checkout Logic
Handles FC-specific release checkout:

```python
from fc_operations.fc_checkout import (
    load_fc_reference,            # Load from JSON
    next_release_variant,         # 358.2.0 -> 358.3.0
    checkout_fc,                  # Core FC release checkout logic
    checkout_fc_from_reference,   # Reference-based checkout
)
```

**Key Feature**: FC release fallback
- Uses release-style `checkoutversionex` payload with required `VERSIONCOMMENT`
- If requested release already exists, automatically tries the next free release
- Example: `358.2.0` exists -> create `358.3.0`

### `pver_operations/pver_checkout.py` - PVER Checkout Logic
Handles PVER-specific checkout (no transformation needed):

```python
from pver_operations.pver_checkout import (
    load_pver_reference,          # Load from JSON
    checkout_pver,                # Core checkout logic
    checkout_pver_from_reference, # Reference-based checkout
)
```

**Note**: PVER suffixes work as-is (e.g., `_test` → `_test`)

### `sdom_automation.py` - Unified CLI Entry Point
Orchestrates FC, BC, and PVER checkout operations:

```bash
python SDOM_Auto/sdom_automation.py \
  --mode {checkout-fc|checkout-bc|checkout-pver|checkout-all} \
  --reference-json <path> \
  [--user <user>] \
  [--password <pass>] \
  [--comment "Custom message"] \
  [--dry-run]
```

**Modes**:
- `checkout-fc`: Check out FC only
- `checkout-bc`: Check out BC only
- `checkout-pver`: Check out PVER only
- `checkout-all`: Check out FC, BC, and PVER

---

## Design Principles

### 1. **DRY (Don't Repeat Yourself)**
- Common REST API logic in `sdom_api.py`
- Session management, fullname parsing, variant resolution shared
- Object-type modules (BC, PVER) only include unique logic

### 2. **Modularity**
- Each object type has its own package: `fc_operations/`, `bc_operations/`, `pver_operations/`
- Easy to add new object types using the same pattern
- Shared utilities in `common/`

### 3. **Reusability**
- Import individual modules as needed
- Direct Python imports: `from bc_operations.bc_checkout import checkout_bc`
- CLI for typical use cases

### 4. **Extensibility**
- Reference JSON structure supports FC/BC/PVER from a single source of truth
- Easy to add checkin, fetch-only, or other operations
- Error handling with custom exceptions

---

## FC Checkout Behavior

- FC checkout uses `fc_checkout.version_input` from the reference JSON as base
- FC checkout uses `fc_checkout.derived_version` as the requested target
- If the requested FC release already exists, the modular flow advances to the next free release
- The resolved version is written back to the reference JSON while preserving the originally requested target in `requested_derived_version`

---

## Migration Guide

**Old approach** (monolithic):
```bash
python SDOM_Auto/sdom_Checkin.py --mode checkout-version \
  --reference-json <path> --checkout-objects bc,pver
```

**New approach** (modular):
```bash
python SDOM_Auto/sdom_automation.py --mode checkout-all \
  --reference-json <path>
```

Both work, but new approach is recommended for:
- Better code organization
- Easier testing and debugging
- Selective object checkout
- Future extensibility

Legacy `sdom_Checkin.py` also supports a pre-check mode to verify required
target versions before checkout:

```bash
python SDOM_Auto/sdom_Checkin.py --mode checkout-status \
  --reference-json SDOM_Auto/reference_data/sdom_checkout_reference_RQONE04873498.json \
  --checkout-objects fc,bc,pver
```

---

## Testing & Verification

All modules tested:
- ✅ `sdom_api.py`: Session creation, fullname parsing, variant resolution
- ✅ `fc_checkout.py`: FC release checkout, conflict resolution
- ✅ `bc_checkout.py`: BC suffix transformation, conflict detection, checkout
- ✅ `pver_checkout.py`: PVER checkout, conflict resolution
- ✅ `sdom_automation.py`: CLI orchestration, dry-run mode
- ✅ Reference JSON extraction from RQ1 context

---

## Reference Data Examples

Sample reference JSONs in `reference_data/`:
- `sdom_checkout_reference_RQONE04873498.json` - FC + BC + PVER checkout example

Add new reference files as needed for different I-FD requirements.

---

## Troubleshooting

**Import Errors**:
- Ensure `__init__.py` exists in all package directories
- Run scripts from SDOM_Auto root or add to PYTHONPATH

**SDOM API Errors**:
- Check credentials: `--user` and `--password`
- Verify base version exists before checkout
- Check network connectivity to `easee-ews.webapp.inside.bosch.cloud`

**Variant Conflicts**:
- Auto-suffix resolution tries up to 25 variants
- If exceeded, manually delete old variants in SDOM or adjust suffix pattern

---

## Future Enhancements

- [ ] Checkin operations (mirror of checkout)
- [ ] Fetch-only mode for base FC
- [ ] Batch operations (multiple I-FDs)
- [ ] Workspace management (local file syncing)
- [ ] Import workflow integration
