"""SDOM Integration: FC → BC, FCP → CC conf, BC → PVER

Step 1  FC→BC      : Replace old FC child inside the new BC with our checked-out FC.
Step 2  FCP→CCconf : Replace old FCP child inside the new CC conf with our checked-out FCP.
Step 3  BC→PVER    : Replace old BC child inside the new PVER with our checked-out BC.

Both steps use:
    POST /configuration/replacechildren?root=<parent_fullname>
    Body: <Replacements><Replacement oldVersion="..." newVersion="..."/></Replacements>

Usage
-----
    python sdom_integrate.py \\
        --reference  SDOM_Auto/reference_data/sdom_checkout_reference_RQONE04873498.json \\
        --status     SDOM_Auto/reference_data/checkout_status_after_create.json \\
        --output     SDOM_Auto/reference_data/integration_result.json \\
        [--user <user>] [--password <pwd>] [--dry-run]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess as _sp
    _sp.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests


# ── Connection ────────────────────────────────────────────────────────────────
BASE_URL = "https://easee-ews.webapp.inside.bosch.cloud/easee-ews"
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from credentials import prompt_sdom_credentials


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_spaced_fullname(fullname: str) -> str:
    """Normalise a SDOM fullname to canonical spaced form: TYPE : Name / Ver; Rev"""
    m = re.match(
        r"^\s*([^:]+)\s*:\s*([^/]+)\s*/\s*([^;]+)\s*;\s*(\d+)\s*$",
        fullname.strip(),
    )
    if not m:
        return fullname.strip()
    return f"{m.group(1).strip()} : {m.group(2).strip()} / {m.group(3).strip()}; {m.group(4).strip()}"


def _load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save_json(data: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


SDOM_FULLNAME_PATTERN = re.compile(r"^\s*[^:]+\s*:\s*[^/]+\s*/\s*[^;]+\s*;\s*\d+\s*$")


def _parse_fullname(fullname: str) -> tuple[str, str, str, int] | None:
    m = re.match(
        r"^\s*([^:]+)\s*:\s*([^/]+)\s*/\s*([^;]+)\s*;\s*(\d+)\s*$",
        fullname.strip(),
    )
    if not m:
        return None
    return (
        m.group(1).strip(),
        m.group(2).strip(),
        m.group(3).strip(),
        int(m.group(4).strip()),
    )


def _class_matches(actual: str, expected: str) -> bool:
    actual_norm = (actual or "").strip().upper()
    expected_norm = (expected or "").strip().upper()
    if not actual_norm or not expected_norm:
        return False
    return (
        actual_norm == expected_norm
        or actual_norm.startswith(expected_norm)
        or expected_norm.startswith(actual_norm)
    )


def _fetch_identifier_for_fullname(session: requests.Session, fullname: str) -> str:
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params={"oslc.where": f'oslc_scm:fullName="{_to_spaced_fullname(fullname)}"'},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        raise RuntimeError(
            f"Unable to resolve identifier for {fullname}. "
            f"status={resp.status_code}, detail={(resp.text or '').strip()[:200]}"
        )
    ns = {
        "dcterms": "http://purl.org/dc/terms/",
    }
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as ex:
        raise RuntimeError(f"Invalid XML from configuration/latest for {fullname}: {ex}") from ex
    node = root.find(".//dcterms:identifier", ns)
    identifier = (node.text or "").strip() if node is not None else ""
    if not identifier:
        raise RuntimeError(f"No identifier found in configuration/latest response for {fullname}")
    return identifier


def _fetch_latest_fullname(session: requests.Session, fullname: str) -> str:
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params={"oslc.where": f'oslc_scm:fullName="{_to_spaced_fullname(fullname)}"'},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        raise RuntimeError(
            f"Unable to resolve latest fullname for {fullname}. "
            f"status={resp.status_code}, detail={(resp.text or '').strip()[:200]}"
        )
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as ex:
        raise RuntimeError(f"Invalid XML from configuration/latest for {fullname}: {ex}") from ex
    ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
    node = root.find(".//oslc_scm:fullName", ns)
    latest = (node.text or "").strip() if node is not None else ""
    if not latest:
        raise RuntimeError(f"No fullName found in configuration/latest response for {fullname}")
    return _to_spaced_fullname(latest)


def _fetch_latest_response_text(session: requests.Session, fullname: str) -> str:
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params={"oslc.where": f'oslc_scm:fullName="{_to_spaced_fullname(fullname)}"'},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        raise RuntimeError(
            f"Unable to resolve latest response for {fullname}. "
            f"status={resp.status_code}, detail={(resp.text or '').strip()[:200]}"
        )
    return resp.text


def _fetch_children_fullnames(session: requests.Session, parent_fullname: str) -> list[str]:
    identifier = _fetch_identifier_for_fullname(session, parent_fullname)
    resp = session.get(
        f"{BASE_URL}/configuration/children",
        params={"id": identifier, "ctx1": identifier, "ctx2": "-1"},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=120,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        raise RuntimeError(
            f"Unable to fetch children for {parent_fullname}. "
            f"status={resp.status_code}, detail={(resp.text or '').strip()[:200]}"
        )
    ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as ex:
        raise RuntimeError(f"Invalid XML from configuration/children for {parent_fullname}: {ex}") from ex
    children: list[str] = []
    for cfg in root.findall(".//oslc_scm:Configuration", ns):
        full = (cfg.findtext("oslc_scm:fullName", default="", namespaces=ns) or "").strip()
        if full:
            children.append(_to_spaced_fullname(full))
    return children


def _fetch_children_entries(session: requests.Session, parent_fullname: str) -> list[dict]:
    identifier = _fetch_identifier_for_fullname(session, parent_fullname)
    resp = session.get(
        f"{BASE_URL}/configuration/children",
        params={"id": identifier, "ctx1": identifier, "ctx2": "-1"},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=120,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        raise RuntimeError(
            f"Unable to fetch children for {parent_fullname}. "
            f"status={resp.status_code}, detail={(resp.text or '').strip()[:200]}"
        )
    ns = {
        "oslc_scm": "http://open-services.net/ns/scm#",
        "easee_sdom": "http://de.bosch.com/easee/ns/sdom#",
        "easee": "http://de.bosch.com/easee/ns/core#",
        "dcterms": "http://purl.org/dc/terms/",
    }
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as ex:
        raise RuntimeError(f"Invalid XML from configuration/children for {parent_fullname}: {ex}") from ex

    entries: list[dict] = []
    for cfg in root.findall(".//oslc_scm:Configuration", ns):
        full = (cfg.findtext("oslc_scm:fullName", default="", namespaces=ns) or "").strip()
        if not full:
            continue
        parsed = _parse_fullname(full)
        entries.append(
            {
                "class": parsed[0] if parsed else "",
                "name": parsed[1] if parsed else "",
                "variant": parsed[2] if parsed else "",
                "revision": parsed[3] if parsed else None,
                "full": _to_spaced_fullname(full),
                "row": (cfg.findtext("easee_sdom:rowNum", default="", namespaces=ns) or "").strip(),
                "identifier": (cfg.findtext("dcterms:identifier", default="", namespaces=ns) or "").strip(),
            }
        )
    return entries


def _resolve_corresponding_bc_in_pver(
    session: requests.Session,
    *,
    pver_fullname: str,
    requested_bc_fullname: str,
) -> str:
    requested = _parse_fullname(requested_bc_fullname)
    if not requested:
        raise RuntimeError(f"Invalid BC fullname in reference JSON: {requested_bc_fullname}")
    requested_type, requested_name, _, _ = requested

    children = _fetch_children_fullnames(session, pver_fullname)
    bc_candidates: list[tuple[int, str]] = []
    for child in children:
        parsed = _parse_fullname(child)
        if not parsed:
            continue
        child_type, child_name, _, child_rev = parsed
        if child_type.upper() != requested_type.upper():
            continue
        if child_name.strip().lower() != requested_name.strip().lower():
            continue
        bc_candidates.append((child_rev, child))

    if not bc_candidates:
        available_bc = []
        for child in children:
            parsed = _parse_fullname(child)
            if parsed and parsed[0].upper() == "BC":
                available_bc.append(child)
        raise RuntimeError(
            f"Could not find corresponding BC '{requested_name}' inside PVER '{pver_fullname}'. "
            f"Available BC children: {available_bc}"
        )

    bc_candidates.sort(key=lambda item: item[0], reverse=True)
    return bc_candidates[0][1]


def _resolve_corresponding_fc_in_bc(
    session: requests.Session,
    *,
    bc_fullname: str,
    requested_fc_fullname: str,
) -> dict:
    requested = _parse_fullname(requested_fc_fullname)
    if not requested:
        raise RuntimeError(f"Invalid FC fullname in reference JSON: {requested_fc_fullname}")
    requested_type, requested_name, requested_variant, _ = requested

    def search(container_fullname: str, depth: int = 0, max_depth: int = 6) -> dict | None:
        entries = _fetch_children_entries(session, container_fullname)

        # 1) direct match by class/name/variant
        direct_matches: list[dict] = []
        for entry in entries:
            if not entry["class"]:
                continue
            if not entry["class"].upper().startswith("FC"):
                continue
            if entry["name"].strip().lower() != requested_name.strip().lower():
                continue
            if entry["variant"].strip() != requested_variant.strip():
                continue
            direct_matches.append(entry)
        if direct_matches:
            direct_matches.sort(key=lambda item: int(item["revision"] or 0), reverse=True)
            return {
                "container": _to_spaced_fullname(container_fullname),
                "resolved_old_fc": direct_matches[0]["full"],
                "resolved_new_fc": _to_spaced_fullname(requested_fc_fullname),
                "matched_via": "name+variant",
                "depth": depth,
            }

        if depth >= max_depth:
            return None

        for entry in entries:
            if entry["class"].upper() not in ("GC", "MC", "BC"):
                continue
            nested = search(entry["full"], depth + 1, max_depth)
            if nested:
                return nested
        return None

    result = search(bc_fullname)
    if result:
        return result

    entries = _fetch_children_entries(session, bc_fullname)
    gc_mc_children = [e["full"] for e in entries if e["class"].upper() in ("GC", "MC")]
    raise RuntimeError(
        f"Could not find FC '{requested_name}' / '{requested_variant}' inside BC '{bc_fullname}'. "
        f"Available GC/MC children: {gc_mc_children}"
    )


def _extract_predecessor_identifier(session: requests.Session, fullname: str) -> str:
    text = _fetch_latest_response_text(session, fullname)
    match = re.search(r'predecessor1 rdf:resource="[^"]*/configuration/(\d+)"', text)
    if not match:
        raise RuntimeError(f"No predecessor found for {fullname}")
    return match.group(1)


def _fetch_parent_entries(session: requests.Session, version_id: str) -> list[dict]:
    resp = session.get(
        f"{BASE_URL}/configuration/parents",
        params={"id": version_id},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=120,
    )
    if resp.status_code == 204:
        return []
    if resp.status_code >= 400 or not (resp.text or "").strip():
        raise RuntimeError(
            f"Unable to fetch parents for id={version_id}. "
            f"status={resp.status_code}, detail={(resp.text or '').strip()[:200]}"
        )
    ns = {
        "oslc_scm": "http://open-services.net/ns/scm#",
        "easee": "http://de.bosch.com/easee/ns/core#",
        "dcterms": "http://purl.org/dc/terms/",
    }
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as ex:
        raise RuntimeError(f"Invalid XML from configuration/parents for id={version_id}: {ex}") from ex

    entries: list[dict] = []
    for cfg in root.findall(".//oslc_scm:Configuration", ns):
        full = (cfg.findtext("oslc_scm:fullName", default="", namespaces=ns) or "").strip()
        if not full:
            continue
        parsed = _parse_fullname(full)
        entries.append(
            {
                "class": parsed[0] if parsed else "",
                "name": parsed[1] if parsed else "",
                "variant": parsed[2] if parsed else "",
                "revision": parsed[3] if parsed else None,
                "full": _to_spaced_fullname(full),
                "identifier": (cfg.findtext("dcterms:identifier", default="", namespaces=ns) or "").strip(),
            }
        )
    return entries


def _variant_sort_key(variant: str) -> tuple:
    match = re.match(r"^(?P<a>\d+)\.(?P<b>\d+)\.(?P<c>\d+)(?P<suffix>.*)$", variant.strip())
    if not match:
        return (variant,)
    return (
        int(match.group("a")),
        int(match.group("b")),
        int(match.group("c")),
        match.group("suffix"),
    )


def _normalize_project_suffix(value: str | None) -> str:
    suffix = (value or "").strip().replace(" ", "")
    if not suffix:
        return ""
    if not re.match(r"^_?[A-Za-z]+_[0-9]+$", suffix):
        raise RuntimeError(
            f"Invalid project-specific suffix '{value}'. Use letters + underscore + number (example: TEST_1)."
        )
    if not suffix.startswith("_"):
        suffix = f"_{suffix}"
    return suffix.upper()


def _build_project_specific_target(base_fullname: str, suffix: str) -> str:
    parsed = _parse_fullname(base_fullname)
    if not parsed:
        raise RuntimeError(f"Invalid CC conf fullname for project-specific checkout: {base_fullname}")
    cls, name, variant, _rev = parsed
    normalized_suffix = _normalize_project_suffix(suffix)
    if variant.upper().endswith(normalized_suffix.upper()):
        return _to_spaced_fullname(f"{cls} : {name} / {variant}; 0")
    return _to_spaced_fullname(f"{cls} : {name} / {variant}{normalized_suffix}; 0")


def _prompt_sdom_fullname(label: str) -> str:
    value = input(f"{label}: ").strip()
    while not SDOM_FULLNAME_PATTERN.match(value):
        print("Invalid SDOM fullname format. Expected: <CLASS> : <NAME> / <VARIANT>; <REV>")
        value = input(f"{label}: ").strip()
    return _to_spaced_fullname(value)


def _prompt_cc_conf_versioning() -> tuple[str, str | None]:
    print("Enter CC conf versioning kind")
    print("  1) revision")
    print("  2) project_specific")
    choice = input("Select versioning kind [1/2]: ").strip()
    while choice not in {"1", "2"}:
        choice = input("Select versioning kind [1/2]: ").strip()
    if choice == "1":
        return "revision", None
    suffix = input("Enter project specific name in SDOM format (example: test_1): ").strip()
    while True:
        try:
            normalized = _normalize_project_suffix(suffix)
        except RuntimeError:
            normalized = ""
        if normalized:
            return "project_specific", normalized.lstrip("_")
        print("Invalid format. Use letters + underscore + number (example: test_1, TEST_2).")
        suffix = input("Enter project specific name in SDOM format (example: test_1): ").strip()


def _resolve_current_parent_from_manual(
    session: requests.Session,
    *,
    pver_fullname: str,
    manual_cc_fullname: str,
) -> dict:
    manual_norm = _to_spaced_fullname(manual_cc_fullname)
    manual_parsed = _parse_fullname(manual_norm)
    if not manual_parsed:
        raise RuntimeError(f"Invalid manual CC conf fullname: {manual_cc_fullname}")

    entries = _fetch_children_entries(session, pver_fullname)
    for entry in entries:
        if _to_spaced_fullname(entry.get("full", "")) == manual_norm:
            return entry

    for entry in entries:
        if not _class_matches(entry.get("class", ""), manual_parsed[0]):
            continue
        if entry.get("name", "").strip().lower() != manual_parsed[1].strip().lower():
            continue
        return entry

    raise RuntimeError(
        f"Manual CC conf '{manual_norm}' is not present under PVER '{_to_spaced_fullname(pver_fullname)}'"
    )


def _find_current_child_by_class_and_name(session: requests.Session, container_fullname: str, child_class: str, child_name: str) -> dict | None:
    for entry in _fetch_children_entries(session, container_fullname):
        if not _class_matches(entry["class"], child_class):
            continue
        if entry["name"].strip().lower() != child_name.strip().lower():
            continue
        return entry
    return None


def _select_parent_source(
    session: requests.Session,
    *,
    container_fullname: str,
    predecessor_id: str,
    allowed_classes: tuple[str, ...],
) -> tuple[dict, dict]:
    parent_entries = _fetch_parent_entries(session, predecessor_id)
    if not parent_entries:
        raise RuntimeError(f"No parent containers found for predecessor id={predecessor_id}")

    candidates: list[tuple[tuple, dict, dict]] = []
    for parent in parent_entries:
        if not any(_class_matches(parent["class"], allowed) for allowed in allowed_classes):
            continue
        current = _find_current_child_by_class_and_name(session, container_fullname, parent["class"], parent["name"])
        if not current:
            continue
        candidates.append((_variant_sort_key(parent["variant"]), parent, current))

    if not candidates:
        raise RuntimeError(
            f"No parent from predecessor id={predecessor_id} matches the current tree '{container_fullname}'"
        )

    candidates.sort(key=lambda item: item[0])
    _, source_parent, current_parent = candidates[0]
    return source_parent, current_parent


def _compute_next_release_variant(variant: str) -> str:
    match = re.match(r"^(?P<a>\d+)\.(?P<b>\d+)\.(?P<c>\d+)(?P<suffix>.*)$", variant.strip())
    if not match:
        raise RuntimeError(f"Unsupported variant format for release increment: {variant}")
    return f"{int(match.group('a'))}.{int(match.group('b')) + 1}.0{match.group('suffix')}"


def _configuration_latest_exists(session: requests.Session, fullname: str) -> bool:
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params={"oslc.where": f'oslc_scm:fullName="{_to_spaced_fullname(fullname)}"'},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if resp.status_code != 200 or not (resp.text or "").strip():
        return False
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return False
    ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
    actual = (root.findtext(".//oslc_scm:fullName", default="", namespaces=ns) or "").strip()
    if not actual:
        return False
    return _to_spaced_fullname(actual) == _to_spaced_fullname(fullname)


def _build_release_checkout_xml(target_variant: str, version_comment: str) -> str:
    comment = html.escape(version_comment or "")
    return (
        f'<Checkout VERSIONCOMMENT="{comment}">'
        f'<NewVariant type="Release" variant="{html.escape(target_variant)}" />'
        f"<VersionComment>{comment}</VersionComment>"
        "<Release>"
        f"<VersionComment>{comment}</VersionComment>"
        "</Release>"
        "</Checkout>"
    )


def _extract_created_fullname(response_text: str) -> str:
    txt = html.unescape((response_text or "").strip())
    match = re.search(r"<string>\s*<[^>]+>\s*<([^>]+)>\s*</string>", txt)
    if match:
        return _to_spaced_fullname(match.group(1).strip())
    return ""


def _checkout_next_release_container(
    session: requests.Session,
    *,
    base_fullname: str,
    version_comment: str,
    max_attempts: int = 250,
) -> str:
    base_variant = _parse_fullname(base_fullname)
    if not base_variant:
        raise RuntimeError(f"Invalid base fullname for release checkout: {base_fullname}")
    candidate_variant = _compute_next_release_variant(base_variant[2])

    for _ in range(max_attempts):
        resp = session.post(
            f"{BASE_URL}/configuration/checkoutversionex",
            params={"root": _to_spaced_fullname(base_fullname)},
            headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
            data=_build_release_checkout_xml(candidate_variant, version_comment),
            timeout=120,
        )
        detail = (resp.text or "").strip()
        if resp.status_code < 400:
            created = _extract_created_fullname(detail)
            if created:
                return created
            return _to_spaced_fullname(
                f"{base_variant[0]} : {base_variant[1]} / {candidate_variant}; 0"
            )
        if "already exists" in detail.lower():
            candidate_variant = _compute_next_release_variant(candidate_variant)
            continue
        if "already checked out" in detail.lower() or "object is already checked out" in detail.lower():
            return _fetch_latest_fullname(session, base_fullname)
        raise RuntimeError(
            f"checkoutversionex release failed for base={base_fullname}, variant={candidate_variant}, "
            f"status={resp.status_code}, detail={detail}"
        )

    raise RuntimeError(f"Unable to find a free release variant for {base_fullname}")


def _checkout_container_for_fc_versioning(
    session: requests.Session,
    *,
    base_fullname: str,
    fc_fullname: str,
    fc_version_kind: str,
    version_comment: str,
) -> str:
    """Checkout parent container using the same versioning mode as FC checkout."""
    kind = (fc_version_kind or "").strip().lower()
    if kind == "release":
        return _checkout_next_release_container(
            session,
            base_fullname=base_fullname,
            version_comment=version_comment,
        )

    parsed_base = _parse_fullname(base_fullname)
    parsed_fc = _parse_fullname(fc_fullname)
    if not parsed_base or not parsed_fc:
        raise RuntimeError(
            f"Invalid fullname for parent checkout. base={base_fullname}, fc={fc_fullname}"
        )

    if kind == "revision":
        xml = (
            "<Checkout>"
            '<NewVariant type="Revision" />'
            f"<VersionComment>{html.escape(version_comment or '')}</VersionComment>"
            "</Checkout>"
        )
        resp = session.post(
            f"{BASE_URL}/configuration/checkoutversionex",
            params={"root": _to_spaced_fullname(base_fullname)},
            headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
            data=xml,
            timeout=120,
        )
        detail = (resp.text or "").strip()
        if resp.status_code < 400:
            created = _extract_created_fullname(detail)
            return created or _to_spaced_fullname(base_fullname)
        raise RuntimeError(
            f"checkoutversionex revision failed for base={base_fullname}, "
            f"status={resp.status_code}, detail={detail}"
        )

    # project_specific / bugfix / variant-like flows:
    # align parent to the FC target variant line.
    target_variant = parsed_fc[2]
    xml = (
        "<Checkout>"
        f'<NewVariant type="Variant" variant="{html.escape(target_variant)}" />'
        f"<VersionComment>{html.escape(version_comment or '')}</VersionComment>"
        "</Checkout>"
    )
    resp = session.post(
        f"{BASE_URL}/configuration/checkoutversionex",
        params={"root": _to_spaced_fullname(base_fullname)},
        headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
        data=xml,
        timeout=120,
    )
    detail = (resp.text or "").strip()
    if resp.status_code < 400:
        created = _extract_created_fullname(detail)
        return created or _to_spaced_fullname(
            f"{parsed_base[0]} : {parsed_base[1]} / {target_variant}; 0"
        )
    if "already exists" in detail.lower() or "already checked out" in detail.lower():
        return _fetch_latest_fullname(
            session,
            f"{parsed_base[0]} : {parsed_base[1]} / {target_variant}; 0",
        )
    raise RuntimeError(
        f"checkoutversionex variant failed for base={base_fullname}, target={target_variant}, "
        f"status={resp.status_code}, detail={detail}"
    )


def _run_fc_hierarchy_integration(
    session: requests.Session,
    *,
    bc_fullname: str,
    new_fc_fullname: str,
    fc_version_kind: str,
    dry_run: bool = False,
) -> dict:
    fc_parsed = _parse_fullname(new_fc_fullname)
    if not fc_parsed:
        raise RuntimeError(f"Invalid FC fullname: {new_fc_fullname}")
    fc_class, fc_name = fc_parsed[0], fc_parsed[1]
    predecessor_id = _extract_predecessor_identifier(session, new_fc_fullname)
    source_parent, current_parent = _select_parent_source(
        session,
        container_fullname=bc_fullname,
        predecessor_id=predecessor_id,
        allowed_classes=("GC", "MC", "BC"),
    )

    created_parent = _checkout_container_for_fc_versioning(
        session,
        base_fullname=current_parent["full"],
        fc_fullname=new_fc_fullname,
        fc_version_kind=fc_version_kind,
        version_comment="Auto child variant align",
    )
    created_parent = _fetch_latest_fullname(session, created_parent)

    old_fc_entry = _find_current_child_by_class_and_name(
        session,
        created_parent,
        fc_class,
        fc_name,
    )
    if not old_fc_entry:
        raise RuntimeError(
            f"Checked-out parent '{created_parent}' does not contain FC '{fc_name}'"
        )

    fc_replace = _replace_child(
        session,
        container=created_parent,
        old_child=old_fc_entry["full"],
        new_child=new_fc_fullname,
        dry_run=dry_run,
    )
    parent_replace = _replace_child(
        session,
        container=bc_fullname,
        old_child=current_parent["full"],
        new_child=created_parent,
        dry_run=dry_run,
    )

    return {
        "ok": fc_replace["ok"] and parent_replace["ok"],
        "source_parent": source_parent["full"],
        "current_parent_in_bc": current_parent["full"],
        "created_parent": created_parent,
        "old_fc": old_fc_entry["full"],
        "new_fc": _to_spaced_fullname(new_fc_fullname),
        "fc_replace": fc_replace,
        "parent_replace": parent_replace,
    }


def _run_fcp_hierarchy_integration(
    session: requests.Session,
    *,
    pver_fullname: str,
    new_fcp_fullname: str,
    cc_version_kind: str,
    cc_version_extra: dict | str | None = None,
    dry_run: bool = False,
) -> dict:
    fcp_parsed = _parse_fullname(new_fcp_fullname)
    if not fcp_parsed:
        raise RuntimeError(f"Invalid FCP fullname: {new_fcp_fullname}")
    fcp_class, fcp_name = fcp_parsed[0], fcp_parsed[1]
    predecessor_id = _extract_predecessor_identifier(session, new_fcp_fullname)
    parent_resolution = "auto"
    try:
        source_parent, current_parent = _select_parent_source(
            session,
            container_fullname=pver_fullname,
            predecessor_id=predecessor_id,
            allowed_classes=("CC",),
        )
    except RuntimeError as ex:
        print(f"[integrate] Auto CC conf parent resolution failed: {ex}")
        manual_base = _prompt_sdom_fullname("Enter base CC conf fullname (exact SDOM fullName)")
        current_parent = _resolve_current_parent_from_manual(
            session,
            pver_fullname=pver_fullname,
            manual_cc_fullname=manual_base,
        )
        source_parent = {"full": manual_base}
        parent_resolution = "manual"
        if (cc_version_kind or "").strip().lower() not in {"revision", "project_specific"}:
            cc_version_kind, cc_version_extra = _prompt_cc_conf_versioning()

    cc_kind = (cc_version_kind or "").strip().lower()
    if cc_kind not in {"revision", "project_specific"}:
        raise RuntimeError(
            f"Unsupported CC conf version kind '{cc_version_kind}'. Allowed: revision, project_specific."
        )

    if cc_kind == "project_specific":
        cc_suffix = _normalize_project_suffix(
            cc_version_extra if isinstance(cc_version_extra, str) else None
        )
        if not cc_suffix:
            cc_kind, cc_version_extra = _prompt_cc_conf_versioning()
            if cc_kind != "project_specific":
                cc_target_fullname = current_parent["full"]
            else:
                cc_suffix = _normalize_project_suffix(cc_version_extra if isinstance(cc_version_extra, str) else None)
                cc_target_fullname = _build_project_specific_target(current_parent["full"], cc_suffix)
        else:
            cc_target_fullname = _build_project_specific_target(current_parent["full"], cc_suffix)
    else:
        cc_target_fullname = current_parent["full"]

    created_parent = _checkout_container_for_fc_versioning(
        session,
        base_fullname=current_parent["full"],
        fc_fullname=cc_target_fullname,
        fc_version_kind=cc_kind,
        version_comment="Auto CC conf checkout for FCP integration",
    )
    created_parent = _fetch_latest_fullname(session, created_parent)

    old_fcp_entry = _find_current_child_by_class_and_name(
        session,
        created_parent,
        fcp_class,
        fcp_name,
    )
    if not old_fcp_entry:
        raise RuntimeError(
            f"Checked-out CC conf '{created_parent}' does not contain FCP '{fcp_name}'"
        )

    fcp_replace = _replace_child(
        session,
        container=created_parent,
        old_child=old_fcp_entry["full"],
        new_child=new_fcp_fullname,
        dry_run=dry_run,
    )
    parent_replace = _replace_child(
        session,
        container=pver_fullname,
        old_child=current_parent["full"],
        new_child=created_parent,
        dry_run=dry_run,
    )

    return {
        "ok": fcp_replace["ok"] and parent_replace["ok"],
        "source_parent": source_parent["full"],
        "parent_resolution": parent_resolution,
        "current_parent_in_pver": current_parent["full"],
        "cc_checkout_kind": cc_kind,
        "cc_target_fullname": _to_spaced_fullname(cc_target_fullname),
        "created_parent": created_parent,
        "old_fcp": old_fcp_entry["full"],
        "new_fcp": _to_spaced_fullname(new_fcp_fullname),
        "fcp_replace": fcp_replace,
        "parent_replace": parent_replace,
    }


def _precheck_bc_replacement(
    session: requests.Session,
    *,
    pver_fullname: str,
    requested_bc_fullname: str,
    new_bc_fullname: str,
) -> dict:
    """
    Validate whether the BC replacement target looks sane before replacechildren.

    The goal is to fail fast when the selected BC is not actually present under the
    chosen PVER or when the new BC cannot be resolved in SDOM.
    """
    pver_norm = _to_spaced_fullname(pver_fullname)
    requested_norm = _to_spaced_fullname(requested_bc_fullname)
    new_norm = _to_spaced_fullname(new_bc_fullname)

    requested = _parse_fullname(requested_norm)
    if not requested:
        raise RuntimeError(f"Invalid requested BC fullname: {requested_bc_fullname}")
    if requested[0].strip().upper() != "BC":
        raise RuntimeError(f"Requested object is not a BC fullname: {requested_bc_fullname}")

    new_parsed = _parse_fullname(new_norm)
    if not new_parsed:
        raise RuntimeError(f"Invalid checked-out BC fullname: {new_bc_fullname}")
    if new_parsed[0].strip().upper() != "BC":
        raise RuntimeError(f"Replacement target is not a BC fullname: {new_bc_fullname}")

    children = _fetch_children_fullnames(session, pver_norm)
    bc_children = []
    for child in children:
        parsed = _parse_fullname(child)
        if parsed and parsed[0].strip().upper() == "BC":
            bc_children.append(child)

    if not bc_children:
        raise RuntimeError(f"No BC children were found inside PVER '{pver_norm}'")

    resolved_old = _resolve_corresponding_bc_in_pver(
        session,
        pver_fullname=pver_norm,
        requested_bc_fullname=requested_norm,
    )

    old_exists = any(_to_spaced_fullname(child) == _to_spaced_fullname(resolved_old) for child in children)
    if not old_exists:
        raise RuntimeError(
            f"Resolved old BC '{resolved_old}' was not found in the child list of PVER '{pver_norm}'"
        )

    new_identifier = _fetch_identifier_for_fullname(session, new_norm)

    return {
        "ok": True,
        "replaceable": True,
        "container": pver_norm,
        "requested_bc": requested_norm,
        "resolved_old_bc": _to_spaced_fullname(resolved_old),
        "new_bc": new_norm,
        "new_bc_identifier": new_identifier,
        "pver_child_count": len(children),
        "bc_children": bc_children,
    }


# ── Core integration call ─────────────────────────────────────────────────────

def _replace_child(
    session: requests.Session,
    *,
    container: str,
    old_child: str,
    new_child: str,
    dry_run: bool = False,
) -> dict:
    """
    Call POST /configuration/replacechildren to swap old_child → new_child
    inside *container*.  Returns a result dict with keys: ok, status_code, detail.
    """
    container_norm = _to_spaced_fullname(container)
    old_norm = _to_spaced_fullname(old_child)
    new_norm = _to_spaced_fullname(new_child)

    print(f"  [integrate] container : {container_norm}")
    print(f"  [integrate] old child : {old_norm}")
    print(f"  [integrate] new child : {new_norm}")

    if old_norm == new_norm:
        print("  [integrate] SKIP – old and new are identical")
        return {"ok": True, "status_code": None, "detail": "old == new, skipped"}

    xml_body = (
        "<Replacements>"
        f'<Replacement oldVersion="{old_norm}" newVersion="{new_norm}"/>'
        "</Replacements>"
    )

    if dry_run:
        print("  [integrate] DRY-RUN – no API call made")
        return {"ok": True, "status_code": None, "detail": "dry-run"}

    resp = session.post(
        f"{BASE_URL}/configuration/replacechildren",
        params={"root": container_norm},
        headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
        data=xml_body,
        timeout=120,
    )
    detail = (resp.text or "").strip()
    ok = resp.status_code < 400

    if ok:
        print(f"  [integrate] SUCCESS  HTTP {resp.status_code}")
    else:
        print(f"  [integrate] FAILED   HTTP {resp.status_code}: {detail[:200]}")

    return {"ok": ok, "status_code": resp.status_code, "detail": detail}


# ── Main integration flow ─────────────────────────────────────────────────────

def run_integration(
    session: requests.Session,
    reference_json: Path,
    status_json: Path,
    output_json: Path,
    fcp_result_json: Path | None = None,
    dry_run: bool = False,
) -> int:
    """
    Execute the configured integration steps.  Returns 0 on full success, 1 on any failure.
    """
    # ── Load inputs ───────────────────────────────────────────────────────────
    ref = _load_json(reference_json)
    status = _load_json(status_json)

    ifd_id = ref.get("ifd_id", "unknown")
    print(f"[integrate] IFD ID : {ifd_id}")

    # Pre-flight gate: all versions must be checked out
    if not status.get("summary", {}).get("all_present"):
        print("[integrate] ERROR: not all versions are checked out – abort.")
        print("            Run checkout-status mode first and confirm all_present=true.")
        return 1

    fc = ref["fc_checkout"]
    bc = ref["bc_checkout"]
    fcp = ref.get("fcp_checkout")
    cc_conf = ref.get("cc_conf_checkout")
    bc_status = status["objects"]["bc"]
    pver_status = status["objects"]["pver"]
    fc_checked_out = _fetch_latest_fullname(
        session,
        fc.get("derived_version") or ref["fc_checkout"].get("requested_derived_version") or fc["version_input"],
    )
    pver_checked_out = _fetch_latest_fullname(session, pver_status.get("matched_fullname") or pver_status.get("requested_fullname") or pver_status.get("base_fullname") or ref["pver_checkout"]["derived_version"])
    bc_checked_out = _fetch_latest_fullname(session, bc_status.get("matched_fullname") or bc_status.get("requested_fullname") or bc_status.get("base_fullname") or bc["derived_version"])

    fcp_checked_out = ""
    if fcp_result_json is not None and fcp_result_json.exists():
        fcp_result = _load_json(fcp_result_json)
        fcp_checked_out = _to_spaced_fullname(str(fcp_result.get("checked_out_fcp") or "").strip())
    if not fcp_checked_out and isinstance(fcp, dict):
        fcp_checked_out = _fetch_latest_fullname(
            session,
            fcp.get("derived_version") or fcp.get("version_input") or "",
        )

    precheck: dict | None = None
    try:
        precheck = _precheck_bc_replacement(
            session,
            pver_fullname=pver_checked_out,
            requested_bc_fullname=bc["version_input"],
            new_bc_fullname=bc_checked_out,
        )
    except RuntimeError as ex:
        result = {
            "ifd_id": ifd_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "overall_success": False,
            "precheck": {
                "ok": False,
                "replaceable": False,
                "container": _to_spaced_fullname(pver_checked_out),
                "requested_bc": _to_spaced_fullname(bc["version_input"]),
                "new_bc": _to_spaced_fullname(bc_checked_out),
                "error": str(ex),
            },
            "steps": {},
        }
        _save_json(result, output_json)
        print()
        print(f"[integrate] PRECHECK FAILED -> see {output_json}")
        return 1

    # ── Step 1: FC → BC  (replace old FC inside the resolved parent container) ─
    print()
    print("-- Step 1: Integrate FC into BC ----------------------------------------")
    step1 = _run_fc_hierarchy_integration(
        session,
        bc_fullname=bc_checked_out,
        new_fc_fullname=fc_checked_out,
        fc_version_kind=str(fc.get("version_kind") or ""),
        dry_run=dry_run,
    )

    fcp_step: dict | None = None
    if fcp_checked_out:
        cc_kind = ""
        cc_extra: dict | str | None = None
        if isinstance(cc_conf, dict):
            cc_kind = str(cc_conf.get("version_kind") or "").strip().lower()
            cc_extra = cc_conf.get("version_extra")
        if not cc_kind:
            cc_kind = "revision"

        print()
        print("-- Step 2: Checkout CC conf, integrate FCP, then relink CC conf in PVER --")
        fcp_step = _run_fcp_hierarchy_integration(
            session,
            pver_fullname=pver_checked_out,
            new_fcp_fullname=fcp_checked_out,
            cc_version_kind=cc_kind,
            cc_version_extra=cc_extra,
            dry_run=dry_run,
        )

    # ── Step 3: BC → PVER  (replace old BC inside new PVER) ──────────────────
    print()
    print("-- Step 3: Integrate BC into PVER --------------------------------------")
    step2 = _replace_child(
        session,
        container=pver_checked_out,
        old_child=precheck["resolved_old_bc"],
        new_child=bc_checked_out,
        dry_run=dry_run,
    )

    # ── Write result ──────────────────────────────────────────────────────────
    overall_ok = step1["ok"] and step2["ok"] and (True if fcp_step is None else fcp_step["ok"])
    result = {
        "ifd_id": ifd_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "overall_success": overall_ok,
        "precheck": precheck,
        "steps": {
            "fc_into_bc": {
                **step1,
            },
            "fcp_into_cc_conf": fcp_step,
            "bc_into_pver": {
                "container": _to_spaced_fullname(pver_checked_out),
                "old_child": _to_spaced_fullname(precheck["resolved_old_bc"]),
                "new_child": _to_spaced_fullname(bc_checked_out),
                **step2,
            },
        },
    }
    _save_json(result, output_json)

    print()
    if overall_ok:
        print(f"[integrate] All steps succeeded -> {output_json}")
    else:
        print(f"[integrate] One or more steps FAILED -> see {output_json}")

    return 0 if overall_ok else 1


def run_fc_only(
    session: requests.Session,
    reference_json: Path,
    status_json: Path,
    output_json: Path,
    dry_run: bool = False,
) -> int:
    ref = _load_json(reference_json)
    status = _load_json(status_json)

    ifd_id = ref.get("ifd_id", "unknown")
    print(f"[fc-only] IFD ID : {ifd_id}")

    if not status.get("summary", {}).get("all_present"):
        print("[fc-only] ERROR: not all versions are checked out – abort.")
        return 1

    fc = ref["fc_checkout"]
    bc = ref["bc_checkout"]
    bc_status = status["objects"]["bc"]
    fc_checked_out = _fetch_latest_fullname(
        session,
        fc.get("derived_version") or ref["fc_checkout"].get("requested_derived_version") or fc["version_input"],
    )
    bc_checked_out = _fetch_latest_fullname(
        session,
        bc_status.get("matched_fullname") or bc_status.get("requested_fullname") or bc_status.get("base_fullname") or bc["derived_version"],
    )
    step1 = _run_fc_hierarchy_integration(
        session,
        bc_fullname=bc_checked_out,
        new_fc_fullname=fc_checked_out,
        fc_version_kind=str(fc.get("version_kind") or ""),
        dry_run=dry_run,
    )

    result = {
        "ifd_id": ifd_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "overall_success": step1["ok"],
        "steps": {
            "fc_into_bc": {
                **step1,
            }
        },
    }
    _save_json(result, output_json)
    print()
    if step1["ok"]:
        print(f"[fc-only] FC step succeeded -> {output_json}")
    else:
        print(f"[fc-only] FC step FAILED -> {output_json}")
    return 0 if step1["ok"] else 1


def run_precheck(
    session: requests.Session,
    reference_json: Path,
    status_json: Path,
    output_json: Path,
) -> int:
    """
    Run only the BC->PVER precheck and write a JSON report.
    """
    ref = _load_json(reference_json)
    status = _load_json(status_json)

    ifd_id = ref.get("ifd_id", "unknown")
    print(f"[precheck] IFD ID : {ifd_id}")

    if not status.get("summary", {}).get("all_present"):
        print("[precheck] ERROR: not all versions are checked out – abort.")
        print("           Run checkout-status mode first and confirm all_present=true.")
        result = {
            "ifd_id": ifd_id,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "ok": False,
            "replaceable": False,
            "error": "not all versions are checked out",
        }
        _save_json(result, output_json)
        return 1

    bc = ref["bc_checkout"]
    pver_status = status["objects"]["pver"]
    bc_status = status["objects"]["bc"]
    pver_checked_out = _fetch_latest_fullname(session, pver_status.get("matched_fullname") or pver_status.get("requested_fullname") or pver_status.get("base_fullname") or ref["pver_checkout"]["derived_version"])
    bc_checked_out = _fetch_latest_fullname(session, bc_status.get("matched_fullname") or bc_status.get("requested_fullname") or bc_status.get("base_fullname") or bc["derived_version"])

    precheck = _precheck_bc_replacement(
        session,
        pver_fullname=pver_checked_out,
        requested_bc_fullname=bc["version_input"],
        new_bc_fullname=bc_checked_out,
    )

    result = {
        "ifd_id": ifd_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        **precheck,
    }
    _save_json(result, output_json)

    print(f"[precheck] replaceable = {precheck['replaceable']}")
    print(f"[precheck] old child   = {precheck['resolved_old_bc']}")
    print(f"[precheck] new child   = {precheck['new_bc']}")
    print(f"[precheck] wrote -> {output_json}")
    return 0


# ── CLI entry point ───────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SDOM Integration: FC→BC, FCP→CC conf→PVER, BC→PVER")
    p.add_argument(
        "--reference",
        required=True,
        help="Path to sdom_checkout_reference_<IFD>.json",
    )
    p.add_argument(
        "--status",
        required=True,
        help="Path to checkout_status_after_create.json (must have all_present=true)",
    )
    p.add_argument(
        "--output",
        default="SDOM_Auto/reference_data/integration_result.json",
        help="Where to write the integration result JSON",
    )
    p.add_argument(
        "--fcp-result",
        default="",
        help="Optional path to fcp_only_result.json for FCP→CC conf integration",
    )
    p.add_argument("--user", default="")
    p.add_argument("--password", default="")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making any API calls",
    )
    p.add_argument(
        "--precheck-only",
        action="store_true",
        help="Run the BC→PVER validation precheck only and exit",
    )
    p.add_argument(
        "--fc-only",
        action="store_true",
        help="Run only the FC→BC integration step and exit",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    user = (args.user or "").strip()
    password = args.password or ""
    if not user or not password:
        user, password = prompt_sdom_credentials()

    session = requests.Session()
    session.auth = (user, password)
    session.headers.update({
        "OSLC-Core-Version": "2.0",
        "x-requester": (os.getenv("SDOM_USER") or os.getenv("USERNAME") or os.getenv("USER") or user or "copilot").strip() or "copilot",
    })

    sys.exit(
        run_precheck(
            session=session,
            reference_json=Path(args.reference),
            status_json=Path(args.status),
            output_json=Path(args.output),
        )
        if args.precheck_only
        else run_fc_only(
            session=session,
            reference_json=Path(args.reference),
            status_json=Path(args.status),
            output_json=Path(args.output),
            dry_run=args.dry_run,
        )
        if args.fc_only
        else run_integration(
            session=session,
            reference_json=Path(args.reference),
            status_json=Path(args.status),
            output_json=Path(args.output),
            fcp_result_json=Path(args.fcp_result) if str(args.fcp_result).strip() else None,
            dry_run=args.dry_run,
        )
    )
