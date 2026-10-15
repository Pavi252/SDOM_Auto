#!/usr/bin/env python3
"""
Variant-aligned manual SDOM import for SWSRC/SWHDR/TDATA/SPEC/TDOC.

Creates/updates all relevant child objects on the same variant as parent FC-ARB-P,
then relinks FC-ARB-P -> SWSRC/SWHDR/TDATA/SPEC and SPEC -> TDOC.
Objects remain in Inbearb (no final checkin here).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess as _sp
    _sp.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests

import sdom_Checkin as sc

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from credentials import prompt_sdom_credentials


logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def _latest(session: requests.Session, fullname: str) -> str:
    return sc._to_spaced_fullname(sc._fetch_latest_fullname(session, sc._to_spaced_fullname(fullname)))


def _find_child(session: requests.Session, container_fullname: str, file_class: str, stem: str) -> str | None:
    return sc._find_child_file_fullname_in_container(
        session=session,
        container_fullname=sc._to_spaced_fullname(container_fullname),
        file_class=file_class,
        stem=stem,
    )


def _load_batch(batch_json: Path) -> dict[str, dict]:
    data = json.loads(batch_json.read_text(encoding="utf-8"))
    objects = data.get("objects", [])
    by_type: dict[str, dict] = {}
    for obj in objects:
        typ = (obj.get("type") or "").upper().strip()
        if typ:
            by_type[typ] = obj
    return by_type


def _checkout_and_relink_file(
    *,
    session: requests.Session,
    parent_container_fullname: str,
    file_class: str,
    file_stem: str,
    base_fullname: str,
    file_path: Path,
    target_variant: str,
) -> str:
    base_latest = _latest(session, base_fullname)
    new_fullname = sc._checkout_file_variant_from_file_root(
        session=session,
        base_file_fullname=base_latest,
        file_path=file_path,
        file_name=file_path.name,
        target_variant=target_variant,
    )
    logger.info("%s variant ready : %s", file_class, new_fullname)

    linked = _find_child(session, parent_container_fullname, file_class, file_stem)
    if linked and sc._to_spaced_fullname(linked) != sc._to_spaced_fullname(new_fullname):
        sc._replace_child_reference(
            session=session,
            container_fullname=parent_container_fullname,
            old_child_fullname=linked,
            new_child_fullname=new_fullname,
        )
        logger.info("%s linked        : %s -> %s", file_class, linked, new_fullname)
    elif linked:
        logger.info("%s linked        : %s", file_class, linked)
    else:
        logger.warning("Could not find existing %s child in %s for stem=%s", file_class, parent_container_fullname, file_stem)

    return new_fullname


def run_variant_aligned_import(
    *,
    batch_json: Path,
    fc_fullname: str,
    sdom_user: str,
    sdom_password: str,
    target_variant: str,
) -> bool:
    by_type = _load_batch(batch_json)

    swsrc = by_type.get("SWSRC")
    swhdr = by_type.get("SWHDR")
    tdata = by_type.get("TDATA")
    spec = by_type.get("SPEC")

    if not swsrc or not Path(swsrc.get("path") or "").exists():
        raise SystemExit("SWSRC entry missing/invalid in import_batch.json")
    if tdata is None or not (tdata.get("fullname") or "").strip() or not Path(tdata.get("path") or "").exists():
        raise SystemExit("TDATA entry missing/invalid in import_batch.json")
    if spec is None or not (spec.get("fullname") or "").strip() or not Path(spec.get("path") or "").exists():
        raise SystemExit("SPEC entry missing/invalid in import_batch.json")

    swsrc_path = Path(swsrc.get("path") or "")
    swsrc_stem = (swsrc.get("name") or swsrc_path.stem).strip()

    swhdr_path = Path(swhdr.get("path") or "") if swhdr else None
    swhdr_stem = (swhdr.get("name") or (swhdr_path.stem if swhdr_path else "")).strip() if swhdr else ""

    tdata_path = Path(tdata.get("path") or "")
    tdata_base_from_batch = (tdata.get("fullname") or "").strip()
    tdata_stem = Path(tdata_path.name).stem.replace("_pavast", "")

    spec_path = Path(spec.get("path") or "")
    spec_base_from_batch = (spec.get("fullname") or "").strip()
    tdoc_base_from_batch = spec_base_from_batch.replace("SPEC:", "TDOC:", 1)
    tdoc_stem = Path(spec_path.name).stem.replace("_fs", "")

    session = requests.Session()
    session.auth = (sdom_user, sdom_password)
    session.headers.update({
        "OSLC-Core-Version": "2.0",
        "x-requester": (os.getenv("SDOM_USER") or os.getenv("USERNAME") or os.getenv("USER") or (sdom_user or "copilot")).strip() or "copilot",
    })

    fc_fullname = sc._to_spaced_fullname(fc_fullname)
    tdata_base = _latest(session, tdata_base_from_batch)
    spec_base = _latest(session, spec_base_from_batch)
    tdoc_base = _latest(session, tdoc_base_from_batch)

    logger.info("Target FC part      : %s", fc_fullname)
    logger.info("Target variant      : %s", target_variant)
    logger.info("SWSRC stem          : %s", swsrc_stem)
    logger.info("SWHDR stem          : %s", swhdr_stem if swhdr_stem else "(not provided)")
    logger.info("TDATA base          : %s", tdata_base)
    logger.info("SPEC base           : %s", spec_base)
    logger.info("TDOC base           : %s", tdoc_base)

    # 1) SWSRC variant checkout + relink
    swsrc_linked = _find_child(session, fc_fullname, "SWSRC", swsrc_stem)
    swsrc_base = swsrc_linked or f"SWSRC : {swsrc_stem} / {target_variant}; 0"
    _checkout_and_relink_file(
        session=session,
        parent_container_fullname=fc_fullname,
        file_class="SWSRC",
        file_stem=swsrc_stem,
        base_fullname=swsrc_base,
        file_path=swsrc_path,
        target_variant=target_variant,
    )

    # 2) SWHDR variant checkout + relink (optional, only when present)
    if swhdr_path and swhdr_path.exists() and swhdr_stem:
        swhdr_linked = _find_child(session, fc_fullname, "SWHDR", swhdr_stem)
        swhdr_base = swhdr_linked or f"SWHDR : {swhdr_stem} / {target_variant}; 0"
        _checkout_and_relink_file(
            session=session,
            parent_container_fullname=fc_fullname,
            file_class="SWHDR",
            file_stem=swhdr_stem,
            base_fullname=swhdr_base,
            file_path=swhdr_path,
            target_variant=target_variant,
        )
    else:
        logger.warning("SWHDR entry missing or file not found; skipping header import.")

    # 3) TDATA variant checkout + relink
    tdata_new = _checkout_and_relink_file(
        session=session,
        parent_container_fullname=fc_fullname,
        file_class="TDATA",
        file_stem=tdata_stem,
        base_fullname=tdata_base,
        file_path=tdata_path,
        target_variant=target_variant,
    )

    # 4) SPEC container variant checkout + relink
    spec_new = sc._checkout_container_variant(
        session=session,
        base_fullname=spec_base,
        target_variant=target_variant,
    )
    logger.info("SPEC variant ready  : %s", spec_new)
    linked_spec = _find_child(session, fc_fullname, "SPEC", sc._parse_version_name(spec_base))
    if linked_spec and sc._to_spaced_fullname(linked_spec) != sc._to_spaced_fullname(spec_new):
        sc._replace_child_reference(
            session=session,
            container_fullname=fc_fullname,
            old_child_fullname=linked_spec,
            new_child_fullname=spec_new,
        )
        logger.info("SPEC linked         : %s -> %s", linked_spec, spec_new)
    elif linked_spec:
        logger.info("SPEC linked         : %s", linked_spec)
    else:
        logger.warning("Could not find existing SPEC child in FC for stem=%s", sc._parse_version_name(spec_base))

    # 5) TDOC variant checkout + relink under SPEC variant
    tdoc_new = _checkout_and_relink_file(
        session=session,
        parent_container_fullname=spec_new,
        file_class="TDOC",
        file_stem=tdoc_stem,
        base_fullname=tdoc_base,
        file_path=spec_path,
        target_variant=target_variant,
    )

    logger.info("Manual variant-aligned import complete (Inbearb).")
    logger.info("Summary             : SWSRC/SWHDR/TDATA/SPEC/TDOC prepared on variant %s", target_variant)
    logger.info("Prepared objects    : %s | %s | %s", tdata_new, spec_new, tdoc_new)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Variant-aligned SDOM import for manual SWSRC/SWHDR/TDATA/SPEC/TDOC.")
    parser.add_argument("--batch-json", required=True, type=Path)
    parser.add_argument("--fc-fullname", required=True, help="FC-ARB-P fullname (e.g. FC-ARB-P : InjMngt_H2InjrLo / 358.2.0; 0)")
    parser.add_argument("--sdom-user", default="")
    parser.add_argument("--sdom-password", default="")
    parser.add_argument("--target-variant", default="358.2.0")
    args = parser.parse_args()

    sdom_user = (args.sdom_user or "").strip()
    sdom_password = args.sdom_password or ""
    if not sdom_user or not sdom_password:
        sdom_user, sdom_password = prompt_sdom_credentials()

    ok = run_variant_aligned_import(
        batch_json=args.batch_json,
        fc_fullname=args.fc_fullname,
        sdom_user=sdom_user,
        sdom_password=sdom_password,
        target_variant=args.target_variant,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
