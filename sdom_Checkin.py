from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import html
import os
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from pathlib import PureWindowsPath

try:
    import requests
except ImportError:
    import subprocess as _sp
    _sp.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests


BASE_URL = "https://easee-ews.webapp.inside.bosch.cloud/easee-ews"
DEFAULT_FC_FULLNAME = ""

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from credentials import prompt_sdom_credentials


def _requester_value() -> str:
    candidate = (
        os.getenv("SDOM_USER")
        or os.getenv("USERNAME")
        or os.getenv("USER")
        or "copilot"
    ).strip()
    return candidate or "copilot"


def _headers() -> dict[str, str]:
    return {
        "OSLC-Core-Version": "2.0",
        "Content-Type": "application/octet-stream",
        "x-requester": _requester_value(),
    }


def _print_response(response: requests.Response) -> None:
    print("Status :", response.status_code)
    print("Headers:", dict(response.headers))
    print("Body:")
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/octet-stream" in content_type:
        print(f"<binary {len(response.content)} bytes>")
    else:
        print(response.text)


def _extract_revision(fullname: str) -> int | None:
    import re

    m = re.search(r";\s*(\d+)\s*$", fullname.strip())
    if not m:
        return None
    return int(m.group(1))


def _next_revision_value(object_fullname: str) -> str:
    revision = _extract_revision(object_fullname)
    if revision is None:
        raise ValueError(f"Cannot infer revision from fullName: {object_fullname}")
    return str(revision + 1)


def _normalize_file_name(file_name: str | None, file_path: Path) -> str:
    if file_name and file_name.strip():
        return file_name.strip()
    return file_path.name


def _to_windows_long_path(path: Path) -> str:
    p = str(path.resolve())
    if os.name != "nt":
        return p
    if p[:4] == '\\\\?\\':
        return p
    return '\\\\?\\' + p


def _path_exists(file_path: Path) -> bool:
    if file_path.exists():
        return True
    if os.name != "nt":
        return False
    try:
        return os.path.exists(_to_windows_long_path(file_path))
    except Exception:
        return False


def _build_checkoutfileex_xml(
    *,
    file_name: str,
    checkout_mode: str,
    target_variant: str | None,
    edition: bool,
    checkin: bool,
) -> str:
    mode = checkout_mode.strip().lower()
    new_variant = ""
    if mode == "revision":
        new_variant = '<NewVariant type="Revision" />'
    elif mode == "variant":
        if not target_variant:
            raise ValueError("target_variant is required for checkout_mode=variant")
        new_variant = f'<NewVariant type="Variant" variant="{target_variant}" />'
    elif mode == "none":
        new_variant = ""
    else:
        raise ValueError(f"Unsupported checkout_mode: {checkout_mode}")

    return (
        "<Checkout>"
        f"{new_variant}"
        f'<NewFile path="{file_name}" edition="{str(edition).lower()}" checkin="{str(checkin).lower()}" />'
        "</Checkout>"
    )


def _encode_xml_parameters(xml_parameters: str) -> str:
    return base64.b64encode(xml_parameters.encode("utf-8")).decode("ascii")


def _parse_class_prefix(fullname: str) -> str:
    m = re.match(r"^\s*([^:]+)\s*:", fullname.strip())
    return m.group(1).strip() if m else ""


def _parse_revision_num(fullname: str) -> int:
    m = re.search(r";\s*(\d+)\s*$", fullname.strip())
    return int(m.group(1)) if m else -1


def _parse_variant_from_fullname(fullname: str) -> str:
    m = re.search(r"/\s*([^;]+)\s*;", fullname)
    return m.group(1).strip() if m else ""


def _to_spaced_fullname(fullname: str) -> str:
    m = re.match(r"^\s*([^:]+)\s*:\s*([^/]+)\s*/\s*([^;]+)\s*;\s*(\d+)\s*$", fullname.strip())
    if not m:
        return fullname.strip()
    return f"{m.group(1).strip()} : {m.group(2).strip()} / {m.group(3).strip()}; {m.group(4).strip()}"


def _load_bc_checkout_reference(rq1_context_path: Path) -> tuple[str, str]:
    data = json.loads(rq1_context_path.read_text(encoding="utf-8"))
    checkout_plan = data.get("sdom", {}).get("checkout_plan", {})
    bc_checkout = checkout_plan.get("base_fc", {}).get("bc_checkout") or checkout_plan.get("new_fc", {}).get("bc_checkout")
    if not bc_checkout:
        raise ValueError(f"No bc_checkout found in RQ1 context: {rq1_context_path}")

    base_fullname = (bc_checkout.get("version_input") or "").strip()
    target_fullname = (bc_checkout.get("derived_version") or "").strip()
    if not base_fullname or not target_fullname:
        raise ValueError(f"Incomplete bc_checkout in RQ1 context: {rq1_context_path}")
    return _to_spaced_fullname(base_fullname), _to_spaced_fullname(target_fullname)


def _load_reference_json(reference_path: Path) -> dict:
    """Load SDOM checkout reference JSON.
    
    Expected structure:
      {
        "ifd_id": "...",
        "mode": "base" | "new",
        "plan_key": "base_fc" | "new_fc",
        "fc_checkout": {...},
        "bc_checkout": {
          "version_input": "BC : DiaApl / 358.25.0_H2CU_8; 0",
          "derived_version": "BC : DiaApl / 358.25.0_H2CU_8_test; 0",
          ...
        },
        "pver_checkout": {
          "version_input": "PVER : BMW_H2CU / H2CU_31A200812B; 1",
          "derived_version": "PVER : BMW_H2CU / H2CU_31A200812B_test; 1",
          ...
        }
      }
    """
    return json.loads(reference_path.read_text(encoding="utf-8"))


def _resolve_reference_checkout_fullname(
    *,
    reference_data: dict,
    obj_type: str,
    prefer_base: bool = False,
) -> str:
    key = f"{obj_type}_checkout"
    checkout_info = reference_data.get(key)
    if not isinstance(checkout_info, dict):
        raise ValueError(f"No {key} in reference JSON")
    field = "version_input" if prefer_base else "derived_version"
    fullname = (checkout_info.get(field) or "").strip()
    if not fullname:
        raise ValueError(f"Missing {field} in {key} of reference JSON")
    return _to_spaced_fullname(fullname)


def _checkout_reference_object(
    *,
    session: requests.Session,
    reference_data: dict,
    obj_type: str,
    version_comment: str,
) -> dict | None:
    """Checkout FC/BC/PVER from reference JSON.
    
    Args:
        session: HTTP session
        reference_data: Loaded reference JSON
        obj_type: 'fc', 'bc', or 'pver'
        version_comment: Comment for checkout
    
    Returns:
        Checkout result dict, or None if obj_type not in reference:
        {
            "requested_fullname": "...",
            "resolved_fullname": "...",
            "fallback_used": bool,
        }
    """
    key = f"{obj_type}_checkout"
    checkout_info = reference_data.get(key)
    if not checkout_info:
        return None
    
    base_fullname = (checkout_info.get("version_input") or "").strip()
    target_fullname = (checkout_info.get("derived_version") or "").strip()
    
    if not base_fullname or not target_fullname:
        raise ValueError(f"Incomplete {key} in reference JSON: base='{base_fullname}' target='{target_fullname}'")
    
    created, resolved = _checkout_container_variant_from_reference(
        session=session,
        base_fullname=_to_spaced_fullname(base_fullname),
        desired_fullname=_to_spaced_fullname(target_fullname),
        version_kind=str(checkout_info.get("version_kind") or ""),
        version_comment=version_comment,
    )

    return {
        "requested_fullname": _to_spaced_fullname(target_fullname),
        "resolved_fullname": _to_spaced_fullname(resolved),
        "fallback_used": _to_spaced_fullname(resolved) != _to_spaced_fullname(target_fullname),
    }


def _update_reference_json_on_fallback(
    *,
    reference_path: Path,
    reference_data: dict,
    obj_type: str,
    requested_fullname: str,
    resolved_fullname: str,
) -> bool:
    if _to_spaced_fullname(requested_fullname) == _to_spaced_fullname(resolved_fullname):
        return False

    key = f"{obj_type}_checkout"
    section = reference_data.get(key)
    if not isinstance(section, dict):
        return False

    section["requested_derived_version"] = _to_spaced_fullname(requested_fullname)
    section["derived_version"] = _to_spaced_fullname(resolved_fullname)
    reference_path.write_text(json.dumps(reference_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def _next_variant_suffix(variant: str) -> str:
    m = re.match(r"^(.*?)(\d+)$", variant)
    if not m:
        return f"{variant}1"
    prefix = m.group(1)
    value = int(m.group(2)) + 1
    return f"{prefix}{value}"


def _next_variant_for_requested_kind(variant: str, version_kind: str | None = None) -> str:
    kind = (version_kind or "").strip().lower()
    if kind == "revision":
        return variant
    if kind in ("release", "bugfix", "alternative"):
        match = re.match(
            r"^(?P<prefix>\D*)(?P<alt>\d+)\.(?P<rel>\d+)\.(?P<bug>\d+)(?P<suffix>.*)$",
            variant,
        )
        if match:
            prefix = match.group("prefix")
            alt = int(match.group("alt"))
            rel = int(match.group("rel"))
            bug = int(match.group("bug"))
            suffix = match.group("suffix")
            if kind == "alternative":
                alt += 1
                rel = 0
                bug = 0
            elif kind == "release":
                rel += 1
                bug = 0
            else:
                bug += 1
            return f"{prefix}{alt}.{rel}.{bug}{suffix}"
    return _next_variant_suffix(variant)


def _next_revision_for_requested_kind(revision: int, version_kind: str | None = None) -> int:
    kind = (version_kind or "").strip().lower()
    if kind == "revision":
        return revision + 1
    return revision


def _next_target_for_requested_kind(
    variant: str,
    revision: int,
    version_kind: str | None = None,
) -> tuple[str, int]:
    return (
        _next_variant_for_requested_kind(variant, version_kind),
        _next_revision_for_requested_kind(revision, version_kind),
    )


def _build_version_fullname(base_fullname: str, variant: str, revision: int | None = None) -> str:
    cls = _parse_class_prefix(base_fullname)
    version_name = _parse_version_name(base_fullname)
    rev = _parse_revision_num(base_fullname) if revision is None else revision
    return _to_spaced_fullname(f"{cls} : {version_name} / {variant}; {rev}")


def _configuration_latest_exists(session: requests.Session, fullname: str) -> bool:
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params={"oslc.where": f'oslc_scm:fullName="{_to_spaced_fullname(fullname)}"'},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    return resp.status_code == 200 and bool((resp.text or "").strip())


def _configuration_latest_exists_with_case_variants(
    session: requests.Session,
    fullname: str,
) -> tuple[bool, str]:
    canonical = _to_spaced_fullname(fullname)
    cls = _parse_class_prefix(canonical)
    version_name = _parse_version_name(canonical)
    variant = _parse_variant_from_fullname(canonical)
    revision = _parse_revision_num(canonical)

    candidates = [canonical]
    if cls and version_name and variant and revision >= 0:
        upper_variant = _to_spaced_fullname(f"{cls} : {version_name} / {variant.upper()}; {revision}")
        lower_variant = _to_spaced_fullname(f"{cls} : {version_name} / {variant.lower()}; {revision}")
        for candidate in (upper_variant, lower_variant):
            if candidate not in candidates:
                candidates.append(candidate)

    for candidate in candidates:
        if _configuration_latest_exists(session, candidate):
            return True, candidate
    return False, canonical


def _reference_object_checkout_status(
    *,
    session: requests.Session,
    reference_data: dict,
    obj_type: str,
) -> dict | None:
    key = f"{obj_type}_checkout"
    checkout_info = reference_data.get(key)
    if not checkout_info:
        return None

    base_fullname = (checkout_info.get("version_input") or "").strip()
    target_fullname = (checkout_info.get("derived_version") or "").strip()
    if not base_fullname or not target_fullname:
        raise ValueError(f"Incomplete {key} in reference JSON: base='{base_fullname}' target='{target_fullname}'")

    requested = _to_spaced_fullname(target_fullname)
    exists, matched = _configuration_latest_exists_with_case_variants(session, requested)
    return {
        "base_fullname": _to_spaced_fullname(base_fullname),
        "requested_fullname": requested,
        "matched_fullname": matched if exists else "",
        "checked_out": exists,
    }


def _resolve_unique_variant_target(
    *,
    session: requests.Session,
    base_fullname: str,
    desired_fullname: str,
    version_kind: str | None = None,
    max_attempts: int = 25,
) -> str:
    desired_variant = _parse_variant_from_fullname(desired_fullname)
    revision = _parse_revision_num(desired_fullname)
    if not desired_variant:
        raise ValueError(f"Cannot parse target variant from fullName: {desired_fullname}")

    variant = desired_variant
    for _ in range(max_attempts):
        candidate_fullname = _build_version_fullname(base_fullname, variant, revision=revision)
        if not _configuration_latest_exists(session, candidate_fullname):
            return candidate_fullname
        variant, revision = _next_target_for_requested_kind(variant, revision, version_kind)

    raise RuntimeError(
        f"Unable to find a free variant for {desired_fullname} after {max_attempts} attempts"
    )


def _checkout_container_variant_from_reference(
    *,
    session: requests.Session,
    base_fullname: str,
    desired_fullname: str,
    version_kind: str | None = None,
    version_comment: str = "Auto child variant align",
    max_attempts: int = 25,
) -> tuple[str, str]:
    desired_variant = _parse_variant_from_fullname(desired_fullname)
    revision = _parse_revision_num(desired_fullname)
    if not desired_variant:
        raise ValueError(f"Cannot parse target variant from fullName: {desired_fullname}")

    variant = desired_variant
    last_error = ""
    for _ in range(max_attempts):
        candidate_fullname = _build_version_fullname(base_fullname, variant, revision=revision)
        if _configuration_latest_exists(session, candidate_fullname):
            last_error = f"already exists: {candidate_fullname}"
            variant, revision = _next_target_for_requested_kind(variant, revision, version_kind)
            continue

        try:
            created = _checkout_container_variant(
                session=session,
                base_fullname=base_fullname,
                target_variant=variant,
                version_kind=version_kind,
                version_comment=version_comment,
            )
            return created, _to_spaced_fullname(created)
        except RuntimeError as ex:
            message = str(ex)
            lowered = message.lower()
            if "already exists" in lowered or "already checked out" in lowered or "invalid variant" in lowered:
                last_error = message
                variant, revision = _next_target_for_requested_kind(variant, revision, version_kind)
                continue
            raise

    raise RuntimeError(
        f"Unable to checkout a variant from {desired_fullname} after {max_attempts} attempts: {last_error}"
    )


def _extract_fc_part_sysfile(zip_entry: str, *, level_from_leaf: int = 0) -> str:
    rel = str(PureWindowsPath(zip_entry))
    parent = str(PureWindowsPath(rel).parent)
    if not parent or parent == ".":
        return ""
    # Take the requested *.cont directory around the changed file.
    # level_from_leaf=0 => closest *.cont (current behavior)
    # level_from_leaf=1 => parent *.cont of that container (used for nested DOCMISC/MISC)
    parts = parent.split("\\")
    cont_parts = [name for name in parts if name.lower().endswith(".cont")]
    if not cont_parts:
        return ""
    idx = len(cont_parts) - 1 - max(0, level_from_leaf)
    if idx < 0:
        idx = 0
    return cont_parts[idx]


def _ensure_specific_container_variant_matches_parent(
    *,
    session: requests.Session,
    fc_root_fullname: str,
    container_fullname: str,
) -> str:
    parent_variant = _parse_variant_from_fullname(fc_root_fullname)
    container_variant = _parse_variant_from_fullname(container_fullname)
    if not parent_variant or not container_variant or container_variant == parent_variant:
        return _to_spaced_fullname(container_fullname)

    try:
        aligned = _checkout_container_variant(
            session=session,
            base_fullname=container_fullname,
            target_variant=parent_variant,
        )
        _replace_child_reference_resilient(
            session=session,
            container_fullname=fc_root_fullname,
            old_child_fullname=container_fullname,
            new_child_fullname=aligned,
        )
        print(
            f"[import-batch] parent child relinked: {_to_spaced_fullname(container_fullname)} -> "
            f"{_to_spaced_fullname(aligned)} under {_to_spaced_fullname(fc_root_fullname)}"
        )
        return _to_spaced_fullname(aligned)
    except Exception as ex:
        print(
            f"[import-batch] warning: could not align parent container variant to {_to_spaced_fullname(fc_root_fullname)}. "
            f"Using current container. Detail: {ex}"
        )
        return _to_spaced_fullname(container_fullname)


def _resolve_manual_docmisc_parent_root(
    *,
    session: requests.Session,
    fc_root_fullname: str,
    zip_entry: str,
    configtree_cache: dict[str, dict[str, list[str]]],
) -> str:
    # For manual XML under nested MISC.cont, the direct leaf container can be MISC.
    # We need the parent GEN FC-part as DOCMISC relink anchor.
    parent_part_sysfile = _extract_fc_part_sysfile(zip_entry, level_from_leaf=1)
    if not parent_part_sysfile:
        return _to_spaced_fullname(fc_root_fullname)

    if fc_root_fullname not in configtree_cache:
        configtree_cache[fc_root_fullname] = _fetch_configtree_container_map(session, fc_root_fullname)
    mapping = configtree_cache[fc_root_fullname]
    parent_candidates = mapping.get(parent_part_sysfile, [])
    selected = _select_best_container_fullname(fc_root_fullname=fc_root_fullname, candidates=parent_candidates)
    if not selected:
        selected = _find_child_container_fullname_in_parent(
            session=session,
            parent_fullname=fc_root_fullname,
            part_sysfile=parent_part_sysfile,
        )
    if not selected:
        return _to_spaced_fullname(fc_root_fullname)
    return _to_spaced_fullname(selected)


def _fetch_configtree_container_map(session: requests.Session, fc_fullname: str) -> dict[str, list[str]]:
    params = {"oslc.where": f'oslc_scm:fullName="{fc_fullname}"'}
    try:
        resp = session.get(
            f"{BASE_URL}/configuration/configtree",
            params=params,
            headers={"OSLC-Core-Version": "2.0"},
            timeout=120,
        )
    except requests.RequestException as ex:
        print(f"[import-batch] warning: configtree request failed for {fc_fullname}: {ex}")
        return {}
    if resp.status_code >= 400 or not (resp.text or "").strip():
        print(f"[import-batch] warning: configtree request failed for {fc_fullname}: HTTP {resp.status_code}")
        return {}

    ns = {
        "easee": "http://de.bosch.com/easee/ns/core#",
        "oslc_scm": "http://open-services.net/ns/scm#",
    }
    root = ET.fromstring(resp.text)
    out: dict[str, list[str]] = {}
    for cfg in root.findall(".//oslc_scm:Configuration", ns):
        sys_node = cfg.find("easee:sysFileName", ns)
        full_node = cfg.find("oslc_scm:fullName", ns)
        if sys_node is None or full_node is None:
            continue
        sys_name = (sys_node.text or "").strip()
        full_name = (full_node.text or "").strip()
        if not sys_name or not full_name:
            continue
        out.setdefault(sys_name, []).append(full_name)
    return out


def _fetch_latest_fullname(session: requests.Session, fullname: str) -> str:
    params = {"oslc.where": f'oslc_scm:fullName="{fullname}"'}
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params=params,
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        return fullname
    try:
        ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
        root = ET.fromstring(resp.text)
        node = root.find(".//oslc_scm:fullName", ns)
        if node is not None and (node.text or "").strip():
            return (node.text or "").strip()
    except ET.ParseError:
        return fullname
    return fullname


def _fetch_latest_fullname_if_exists(session: requests.Session, fullname: str) -> str | None:
    resolved = _fetch_latest_fullname(session, fullname)
    if _to_spaced_fullname(resolved) == _to_spaced_fullname(fullname):
        if not _configuration_latest_exists(session, fullname):
            return None
    return _to_spaced_fullname(resolved)


def _select_best_container_fullname(*, fc_root_fullname: str, candidates: list[str]) -> str | None:
    if not candidates:
        return None
    fc_class = _parse_class_prefix(fc_root_fullname)
    pref_p = f"{fc_class}-P" if fc_class else ""
    pref_b = f"{fc_class}-B" if fc_class else ""
    if fc_class.endswith("-B"):
        class_priority = [pref_b, pref_p, fc_class]
    elif fc_class.endswith("-P"):
        class_priority = [pref_p, pref_b, fc_class]
    else:
        class_priority = [pref_p, pref_b, fc_class]

    def score(fullname: str) -> tuple[int, int]:
        cls = _parse_class_prefix(fullname)
        rank = 9
        for idx, preferred in enumerate(class_priority):
            if preferred and cls == preferred:
                rank = idx
                break
        if rank == 9 and cls.startswith(fc_class):
            rank = len(class_priority) + 1
        # prefer latest revision among same class rank
        return (rank, -_parse_revision_num(fullname))

    return sorted(candidates, key=score)[0]


def _resolve_import_root_fullname(
    *,
    session: requests.Session,
    fc_root_fullname: str,
    zip_entry: str,
    configtree_cache: dict[str, dict[str, list[str]]],
) -> str:
    part_sysfile = _extract_fc_part_sysfile(zip_entry)
    if not part_sysfile:
        return fc_root_fullname

    if fc_root_fullname not in configtree_cache:
        configtree_cache[fc_root_fullname] = _fetch_configtree_container_map(session, fc_root_fullname)
    mapping = configtree_cache[fc_root_fullname]
    candidates = mapping.get(part_sysfile, [])
    selected = _select_best_container_fullname(fc_root_fullname=fc_root_fullname, candidates=candidates)
    if not selected:
        # Fallback when configtree is unavailable: resolve child FC-part directly from current children.
        selected = _find_child_container_fullname_in_parent(
            session=session,
            parent_fullname=fc_root_fullname,
            part_sysfile=part_sysfile,
        )
    if not selected:
        return _to_spaced_fullname(fc_root_fullname)
    # Keep the exact child currently linked under parent FC (from configtree),
    # not the latest revision globally.
    return _to_spaced_fullname(selected)


def _find_child_container_fullname_in_parent(
    *,
    session: requests.Session,
    parent_fullname: str,
    part_sysfile: str,
) -> str | None:
    parent_id = _fetch_identifier_for_fullname(session, _to_spaced_fullname(parent_fullname))
    if not parent_id:
        return None
    try:
        resp = session.get(
            f"{BASE_URL}/configuration/children",
            params={"id": parent_id, "ctx1": parent_id, "ctx2": "-1"},
            headers={"OSLC-Core-Version": "2.0"},
            timeout=120,
        )
    except requests.RequestException:
        return None
    if resp.status_code >= 400 or not (resp.text or "").strip():
        return None

    target_name = Path(part_sysfile).stem.lower()
    ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None

    candidates: list[str] = []
    for cfg in root.findall(".//oslc_scm:Configuration", ns):
        full = (cfg.findtext("oslc_scm:fullName", default="", namespaces=ns) or "").strip()
        if not full:
            continue
        version_name = _parse_version_name(full).lower()
        if version_name != target_name:
            continue
        candidates.append(_to_spaced_fullname(full))

    return _select_best_container_fullname(fc_root_fullname=parent_fullname, candidates=candidates)


def _checkout_container_variant(
    *,
    session: requests.Session,
    base_fullname: str,
    target_variant: str,
    version_kind: str | None = None,
    version_comment: str = "Auto child variant align",
) -> str:
    kind = (version_kind or "").strip().lower()
    escaped_comment = html.escape(version_comment or "")
    if kind == "release":
        xml = (
            f'<Checkout VERSIONCOMMENT="{escaped_comment}">'
            f'<NewVariant type="Release" variant="{target_variant}" />'
            f"<VersionComment>{escaped_comment}</VersionComment>"
            "<Release>"
            f"<VersionComment>{escaped_comment}</VersionComment>"
            "</Release>"
            "</Checkout>"
        )
    elif kind == "revision":
        xml = (
            "<Checkout>"
            '<NewVariant type="Revision" />'
            f"<VersionComment>{escaped_comment}</VersionComment>"
            "</Checkout>"
        )
    else:
        xml = (
            "<Checkout>"
            f'<NewVariant type="Variant" variant="{target_variant}" />'
            f"<VersionComment>{escaped_comment}</VersionComment>"
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
        if created:
            return _to_spaced_fullname(created)
    if "already exists" in detail.lower():
        cls = _parse_class_prefix(base_fullname)
        version_name = _parse_version_name(base_fullname)
        expected = f"{cls} : {version_name} / {target_variant}; 0"
        existing = _fetch_latest_fullname_if_exists(session, expected)
        if existing:
            return existing
        raise RuntimeError(
            "checkoutversionex reported variant already exists, but it could not be resolved via configuration/latest "
            f"for expected={_to_spaced_fullname(expected)}"
        )
    if "already checked out" in detail.lower() or "object is already checked out" in detail.lower():
        # Container is already in edition. Resolve and return the existing checked-out version.
        cls = _parse_class_prefix(base_fullname)
        version_name = _parse_version_name(base_fullname)
        # For revision checkout the newly created rev is rev+1; for variant it stays at rev 0.
        if (version_kind or "").strip().lower() == "revision":
            # Try to find the latest revision (will be the edition just created or an existing one).
            for rev in range(1, 20):
                candidate = f"{cls} : {version_name} / {target_variant}; {rev}"
                existing = _fetch_latest_fullname_if_exists(session, candidate)
                if existing:
                    return existing
        expected = f"{cls} : {version_name} / {target_variant}; 0"
        existing = _fetch_latest_fullname_if_exists(session, expected)
        if existing:
            return existing
        raise RuntimeError(
            "checkoutversionex reported already-checked-out but no existing version could be resolved "
            f"for expected={_to_spaced_fullname(expected)}"
        )
    raise RuntimeError(
        f"checkoutversionex failed for base={base_fullname}, target_variant={target_variant}, "
        f"status={resp.status_code}, detail={detail}"
    )


def _parse_version_name(fullname: str) -> str:
    m = re.match(r"^\s*[^:]+\s*:\s*([^/]+)\s*/", fullname.strip())
    return m.group(1).strip() if m else ""


def _ensure_container_variant_matches_parent(
    *,
    session: requests.Session,
    fc_root_fullname: str,
    zip_entry: str,
    configtree_cache: dict[str, dict[str, list[str]]],
) -> str:
    parent_variant = _parse_variant_from_fullname(fc_root_fullname)
    resolved_root = _resolve_import_root_fullname(
        session=session,
        fc_root_fullname=fc_root_fullname,
        zip_entry=zip_entry,
        configtree_cache=configtree_cache,
    )
    resolved_variant = _parse_variant_from_fullname(resolved_root)
    if not parent_variant or not resolved_variant or resolved_variant == parent_variant:
        return resolved_root

    # Need container variant to follow parent FC variant.
    part_sysfile = _extract_fc_part_sysfile(zip_entry)
    mapping = configtree_cache.get(fc_root_fullname, {})
    candidates = mapping.get(part_sysfile, [])
    if not candidates:
        return resolved_root

    # Try oldest base first because latest revision may already be checked out and blocked for variant creation.
    preferred_class = _parse_class_prefix(resolved_root)

    def by_class_and_revision(name: str) -> tuple[int, int]:
        cls_rank = 0 if _parse_class_prefix(name) == preferred_class else 1
        rev = _parse_revision_num(name)
        return (cls_rank, rev if rev >= 0 else 9999)

    last_error = ""
    for base in sorted(candidates, key=by_class_and_revision):
        try:
            created = _checkout_container_variant(session=session, base_fullname=base, target_variant=parent_variant)
            # Ensure parent FC references the aligned child container variant.
            try:
                _replace_child_reference(
                    session=session,
                    container_fullname=fc_root_fullname,
                    old_child_fullname=base,
                    new_child_fullname=created,
                )
                print(
                    f"[import-batch] parent child relinked: {_to_spaced_fullname(base)} -> "
                    f"{_to_spaced_fullname(created)} under {_to_spaced_fullname(fc_root_fullname)}"
                )
            except Exception as link_ex:
                print(f"[import-batch] warning: parent relink skipped/failed: {link_ex}")
            return created
        except Exception as ex:
            last_error = str(ex)
            continue

    # fallback to resolved root when creation failed
    print(f"[import-batch] warning: could not align container variant to parent ({parent_variant}). {last_error}")
    return resolved_root


def _infer_file_class_from_name(file_name: str) -> str:
    ext = Path(file_name).suffix.lower()
    if ext in (".c", ".cpp", ".cxx"):
        return "SWSRC"
    if ext in (".h", ".hpp", ".hh"):
        return "SWHDR"
    if ext == ".arxml":
        return "CONFDATA"
    if ext == ".xml":
        return "DOCMISC"
    return "SWSRC"


def _nearest_gen_base_from_path(file_path: Path) -> str | None:
    for part in reversed(file_path.parts):
        if part.lower().endswith("_gen.cont"):
            return Path(part).stem
    return None


def _resolve_lookup_class_stem(*, file_path: Path, file_name: str) -> tuple[str, str]:
    low = file_name.lower()
    ext = Path(file_name).suffix.lower()
    stem = Path(file_name).stem
    gen_base = _nearest_gen_base_from_path(file_path)

    if ext in (".c", ".cpp", ".cxx"):
        return ("SWSRC", gen_base or stem)
    if ext in (".h", ".hpp", ".hh"):
        return ("SWHDR", gen_base or stem)
    if ext == ".arxml":
        if low.endswith("_ar422_ecucvalues.arxml"):
            return ("CONFDATA", f"{gen_base}_ar422_ecucvalues" if gen_base else stem)
        if low.endswith("_ar_ecucvalues.arxml"):
            return ("CONFDATA", f"{gen_base}_ar_ecucvalues" if gen_base else stem)
        if low.endswith("_ecucvalues.arxml"):
            return ("CONFDATA", f"{gen_base}_ecucvalues" if gen_base else stem)
        return ("CONFDATA", stem)
    if ext == ".xml":
        if low.endswith("_prot_confdata.xml") or low.endswith("_confdata.xml"):
            return ("CONFDATA", f"{gen_base}_prot" if gen_base else stem)
        if low.endswith("_manual.xml"):
            manual_stem = (gen_base[:-4] + "_manual") if gen_base and gen_base.lower().endswith("_gen") else (f"{gen_base}_manual" if gen_base else stem)
            return ("DOCMISC", manual_stem)
        if low.endswith("_pavast.xml"):
            tdata_stem = stem.removesuffix("_pavast")
            return ("TDATA", gen_base or tdata_stem or stem)
        if low.endswith("_fs.xml"):
            spec_stem = stem[:-3] if stem.lower().endswith("_fs") else stem
            return ("SPEC", spec_stem)
        return ("DOCMISC", gen_base or stem)
    return (_infer_file_class_from_name(file_name), stem)


def _resolve_docmisc_container(
    *,
    session: requests.Session,
    resolved_root: str,
    file_path: Path,
) -> str:
    """Resolve the MISC container that stores DOCMISC manual XML under a GEN part."""
    candidate_stems: list[str] = []

    def _add(stem: str | None) -> None:
        s = (stem or "").strip()
        if s and s.lower() not in [c.lower() for c in candidate_stems]:
            candidate_stems.append(s)

    _add(_parse_version_name(resolved_root))
    _add(_nearest_gen_base_from_path(file_path))
    _add(file_path.parent.stem)
    if file_path.parent.parent != file_path.parent:
        _add(file_path.parent.parent.stem)

    for stem in candidate_stems:
        misc_container = _find_child_file_fullname_in_container(
            session=session,
            container_fullname=resolved_root,
            file_class="MISC",
            stem=stem,
        )
        if misc_container:
            return misc_container

    # Fallback: if exactly one MISC child exists, use it even when the stem differs.
    cid = _fetch_identifier_for_fullname(session, _to_spaced_fullname(resolved_root))
    if cid:
        resp = session.get(
            f"{BASE_URL}/configuration/children",
            params={"id": cid, "ctx1": cid, "ctx2": "-1"},
            headers={"OSLC-Core-Version": "2.0"},
            timeout=120,
        )
        if resp.status_code < 400 and (resp.text or "").strip():
            ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
            try:
                root = ET.fromstring(resp.text)
            except ET.ParseError:
                return resolved_root
            misc_children: list[str] = []
            for cfg in root.findall(".//oslc_scm:Configuration", ns):
                full = (cfg.findtext("oslc_scm:fullName", default="", namespaces=ns) or "").strip()
                if not full:
                    continue
                if _parse_class_prefix(full).upper() != "MISC":
                    continue
                misc_children.append(_to_spaced_fullname(full))
            if len(misc_children) == 1:
                return misc_children[0]

    return resolved_root


def _resolve_spec_container_for_tdoc(
    *,
    session: requests.Session,
    fc_root_fullname: str,
    tdoc_stem: str,
) -> str | None:
    def _norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

    fc_id = _fetch_identifier_for_fullname(session, _to_spaced_fullname(fc_root_fullname))
    if not fc_id:
        return None
    resp = session.get(
        f"{BASE_URL}/configuration/children",
        params={"id": fc_id, "ctx1": fc_id, "ctx2": "-1"},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=120,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        return None

    ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None

    exact: list[str] = []
    fuzzy: list[str] = []
    specs: list[str] = []
    target_norm = _norm(tdoc_stem)

    for cfg in root.findall(".//oslc_scm:Configuration", ns):
        full = (cfg.findtext("oslc_scm:fullName", default="", namespaces=ns) or "").strip()
        if not full or _parse_class_prefix(full).upper() != "SPEC":
            continue
        spec_fullname = _to_spaced_fullname(full)
        specs.append(spec_fullname)

        version_name = _parse_version_name(full)
        version_norm = _norm(version_name)
        if target_norm and (version_norm == target_norm or target_norm in version_norm or version_norm in target_norm):
            exact.append(spec_fullname)

    if exact:
        return sorted(exact, key=_parse_revision_num, reverse=True)[0]

    for spec in specs:
        version_name = _parse_version_name(spec)
        version_norm = _norm(version_name)
        if target_norm and (target_norm in version_norm or version_norm in target_norm):
            fuzzy.append(spec)
    if fuzzy:
        return sorted(fuzzy, key=_parse_revision_num, reverse=True)[0]
    if len(specs) == 1:
        return specs[0]

    target_norms = {
        _norm(tdoc_stem),
        _norm(Path(tdoc_stem).stem),
    }
    for base in list(target_norms):
        if base.endswith("fs"):
            target_norms.add(base[:-2])
        if base.endswith("fsxml"):
            target_norms.add(base[:-5])

    configtree_matches: list[str] = []
    try:
        configtree_map = _fetch_configtree_container_map(session, fc_root_fullname)
    except Exception:
        configtree_map = {}
    for sys_name, fullnames in configtree_map.items():
        sys_norm = _norm(Path(sys_name).stem)
        if sys_norm not in target_norms and not any(t in sys_norm or sys_norm in t for t in target_norms):
            continue
        for full in fullnames:
            if _parse_class_prefix(full).upper() != "SPEC":
                continue
            configtree_matches.append(_to_spaced_fullname(full))
    if configtree_matches:
        return sorted(configtree_matches, key=_parse_revision_num, reverse=True)[0]

    return None


def _resolve_file_object_fullname_for_checkin(
    *,
    session: requests.Session,
    resolved_root_fullname: str,
    file_name: str,
) -> str:
    variant = _parse_variant_from_fullname(resolved_root_fullname)
    stem = Path(file_name).stem
    clazz = _infer_file_class_from_name(file_name)
    candidate = f"{clazz} : {stem} / {variant}; 0"
    latest = _fetch_latest_fullname(session, candidate)
    return _to_spaced_fullname(latest)


def _find_latest_file_fullname(
    *,
    session: requests.Session,
    file_class: str,
    stem: str,
    variant: str,
) -> str | None:
    candidate = f"{file_class} : {stem} / {variant}; 0"
    params = {"oslc.where": f'oslc_scm:fullName="{candidate}"'}
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params=params,
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if resp.status_code == 204:
        return None
    if resp.status_code >= 400 or not (resp.text or "").strip():
        return None
    try:
        ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
        root = ET.fromstring(resp.text)
        node = root.find(".//oslc_scm:fullName", ns)
        if node is not None and (node.text or "").strip():
            return _to_spaced_fullname((node.text or "").strip())
    except ET.ParseError:
        return None
    return None


def _fetch_identifier_for_fullname(session: requests.Session, fullname: str) -> str | None:
    params = {"oslc.where": f'oslc_scm:fullName="{fullname}"'}
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params=params,
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        return None
    try:
        ns = {
            "oslc_scm": "http://open-services.net/ns/scm#",
            "dcterms": "http://purl.org/dc/terms/",
        }
        root = ET.fromstring(resp.text)
        node = root.find(".//dcterms:identifier", ns)
        if node is not None and (node.text or "").strip():
            return (node.text or "").strip()
    except ET.ParseError:
        return None
    return None


def _find_child_file_fullname_in_container(
    *,
    session: requests.Session,
    container_fullname: str,
    file_class: str,
    stem: str,
) -> str | None:
    cid = _fetch_identifier_for_fullname(session, _to_spaced_fullname(container_fullname))
    if not cid:
        return None
    resp = session.get(
        f"{BASE_URL}/configuration/children",
        params={"id": cid, "ctx1": cid, "ctx2": "-1"},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=120,
    )
    if resp.status_code >= 400 or not (resp.text or "").strip():
        return None
    ns = {"oslc_scm": "http://open-services.net/ns/scm#"}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None

    candidates: list[str] = []
    for cfg in root.findall(".//oslc_scm:Configuration", ns):
        full = (cfg.findtext("oslc_scm:fullName", default="", namespaces=ns) or "").strip()
        if not full:
            continue
        if _parse_class_prefix(full) != file_class:
            continue
        if _parse_version_name(full).lower() != stem.lower():
            continue
        candidates.append(_to_spaced_fullname(full))
    if not candidates:
        return None
    # Prefer highest revision among matches.
    return sorted(candidates, key=_parse_revision_num, reverse=True)[0]


def _find_variant_fallback_file_fullname(
    *,
    session: requests.Session,
    file_class: str,
    stem: str,
    preferred_variant: str,
) -> str | None:
    fallback_variants = [preferred_variant]
    if file_class in ("DOCMISC", "TDATA", "TDOC", "SPEC", "CONFDATA"):
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", preferred_variant.strip())
        if m:
            major = int(m.group(1))
            suffix = m.group(4)
            base_line_variant = f"{major}.0.0{suffix}"
            if base_line_variant not in fallback_variants:
                fallback_variants.append(base_line_variant)

    for variant in fallback_variants:
        found = _find_latest_file_fullname(
            session=session,
            file_class=file_class,
            stem=stem,
            variant=variant,
        )
        if found:
            return found
    return None


def _checkout_file_variant_from_file_root(
    *,
    session: requests.Session,
    base_file_fullname: str,
    file_path: Path,
    file_name: str,
    target_variant: str,
) -> str:
    xml_parameters = _build_checkoutfileex_xml(
        file_name=file_name,
        checkout_mode="variant",
        target_variant=target_variant,
        edition=True,
        checkin=False,
    )
    encoded = _encode_xml_parameters(xml_parameters)
    headers = {
        "OSLC-Core-Version": "2.0",
        "Content-Type": "application/octet-stream",
        "RootVersion": _to_spaced_fullname(base_file_fullname),
        "FileName": file_name,
        "XmlParameters": encoded,
    }
    with open(_to_windows_long_path(file_path), "rb") as payload:
        resp = session.post(f"{BASE_URL}/configuration/checkoutfileex", headers=headers, data=payload, timeout=120)

    detail = (resp.text or "").strip()
    if resp.status_code < 400:
        created = _extract_created_fullname(detail)
        if created:
            return _to_spaced_fullname(created)

    # handle already-existing/new-variant-already-exists cases
    lower = detail.lower()
    if "already exists" in lower or "already checked out" in lower:
        cls = _parse_class_prefix(base_file_fullname)
        vname = _parse_version_name(base_file_fullname)
        expected = f"{cls} : {vname} / {target_variant}; 0"
        latest = _fetch_latest_fullname(session, expected)
        return _to_spaced_fullname(latest)

    raise RuntimeError(
        f"checkoutfileex(file-root) failed for base={base_file_fullname}, target_variant={target_variant}, "
        f"status={resp.status_code}, detail={detail}"
    )


def _replace_child_reference(
    *,
    session: requests.Session,
    container_fullname: str,
    old_child_fullname: str,
    new_child_fullname: str,
) -> None:
    if _to_spaced_fullname(old_child_fullname) == _to_spaced_fullname(new_child_fullname):
        return

    xml = (
        "<Replacements>"
        f'<Replacement oldVersion="{_to_spaced_fullname(old_child_fullname)}" '
        f'newVersion="{_to_spaced_fullname(new_child_fullname)}" />'
        "</Replacements>"
    )
    resp = session.post(
        f"{BASE_URL}/configuration/replacechildren",
        params={"root": _to_spaced_fullname(container_fullname)},
        headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
        data=xml,
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"replacechildren failed root={container_fullname}, old={old_child_fullname}, new={new_child_fullname}, "
            f"status={resp.status_code}, detail={(resp.text or '').strip()}"
        )


def _replace_child_reference_resilient(
    *,
    session: requests.Session,
    container_fullname: str,
    old_child_fullname: str,
    new_child_fullname: str,
) -> None:
    """Replace child link and retry with current linked child when SDOM fullname drift occurs.

    The server can answer with a generic 400 even when the requested replacement has
    already been applied (for example a live MISC/DOCMISC relink). In that case we
    re-query the current container state and treat the operation as successful when the
    target child is already the expected one.
    """
    if _to_spaced_fullname(old_child_fullname) == _to_spaced_fullname(new_child_fullname):
        return

    try:
        _replace_child_reference(
            session=session,
            container_fullname=container_fullname,
            old_child_fullname=old_child_fullname,
            new_child_fullname=new_child_fullname,
        )
        return
    except Exception as first_ex:
        child_class = _parse_class_prefix(old_child_fullname)
        child_name = _parse_version_name(old_child_fullname)

        current_linked = None
        if child_class and child_name:
            current_linked = _find_child_file_fullname_in_container(
                session=session,
                container_fullname=container_fullname,
                file_class=child_class,
                stem=child_name,
            )

        if current_linked and _to_spaced_fullname(current_linked) == _to_spaced_fullname(new_child_fullname):
            return

        if not current_linked:
            # Some SDOM flows already promote the child under the live container, even if the
            # original old child fullname is stale. Re-check the direct container state using the
            # new child as a final confirmation before declaring a real failure.
            lookup_class = _parse_class_prefix(new_child_fullname)
            lookup_name = _parse_version_name(new_child_fullname)
            if lookup_class and lookup_name:
                current_linked = _find_child_file_fullname_in_container(
                    session=session,
                    container_fullname=container_fullname,
                    file_class=lookup_class,
                    stem=lookup_name,
                )
                if current_linked and _to_spaced_fullname(current_linked) == _to_spaced_fullname(new_child_fullname):
                    return

        if not current_linked:
            raise first_ex

        try:
            _replace_child_reference(
                session=session,
                container_fullname=container_fullname,
                old_child_fullname=current_linked,
                new_child_fullname=new_child_fullname,
            )
            return
        except Exception:
            # Final guard: if the container already contains the expected child, the relink is
            # effectively complete even if a server-side precheck rejects the old/new pair.
            post_linked = _find_child_file_fullname_in_container(
                session=session,
                container_fullname=container_fullname,
                file_class=_parse_class_prefix(new_child_fullname),
                stem=_parse_version_name(new_child_fullname),
            )
            if post_linked and _to_spaced_fullname(post_linked) == _to_spaced_fullname(new_child_fullname):
                return
            raise


def import_file_checkoutfileex(
    session: requests.Session,
    *,
    file_path: Path,
    file_name: str,
    root_fullname: str,
    checkout_mode: str,
    target_variant: str | None,
    edition: bool,
    checkin: bool,
) -> requests.Response:
    xml_parameters = _build_checkoutfileex_xml(
        file_name=file_name,
        checkout_mode=checkout_mode,
        target_variant=target_variant,
        edition=edition,
        checkin=checkin,
    )
    xml_parameters_encoded = _encode_xml_parameters(xml_parameters)
    headers = {
        "OSLC-Core-Version": "2.0",
        "Content-Type": "application/octet-stream",
        "RootVersion": _to_spaced_fullname(root_fullname),
        "FileName": file_name,
        "XmlParameters": xml_parameters_encoded,
    }
    with open(_to_windows_long_path(file_path), "rb") as payload:
        return session.post(
            f"{BASE_URL}/configuration/checkoutfileex",
            headers=headers,
            data=payload,
            timeout=120,
        )


def _extract_created_fullname(response_text: str) -> str:
    txt = html.unescape((response_text or "").strip())
    import re

    m = re.search(r"<string>\s*<[^>]+>\s*<([^>]+)>\s*</string>", txt)
    if m:
        return m.group(1).strip()
    # Alternate response shape seen in checkoutfileex:
    # <78711475><SWHDR : <module>_gen / <variant>; <rev>>
    m2 = re.search(r"<\d+>\s*<([^>]+)>", txt)
    if m2:
        return m2.group(1).strip()
    return ""


def _latest_zip_in_workspace(base_dir: Path) -> Path | None:
    if not base_dir.exists():
        return None
    zips = sorted(base_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0] if zips else None


def _latest_bsh_output_folder(bsh_dir: Path) -> Path | None:
    if not bsh_dir.exists():
        return None
    candidates = sorted(
        [p for p in bsh_dir.glob("Out_beanshell_*") if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _resolve_target_gen_part_dir(*, workspace_dir: Path, gen_fcpart_name: str) -> Path:
    def rank_gen_dir(p: Path) -> tuple[int, int]:
        base = p.stem.lower()
        files = [
            f
            for f in p.iterdir()
            if f.is_file()
            and f.suffix.lower() in (".c", ".h", ".xml", ".arxml")
            and f.stem.lower().startswith(base)
        ]
        depth = len(p.parts)
        return (len(files), -depth)

    if gen_fcpart_name.strip():
        expected = f"{gen_fcpart_name.strip()}.cont".lower()
        matches = [p for p in workspace_dir.rglob("*.cont") if p.is_dir() and p.name.lower() == expected]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise SystemExit(f"No matching gen FC-part folder found for --gen-fcpart-name {gen_fcpart_name}")
        ranked = sorted(matches, key=rank_gen_dir, reverse=True)
        if len(ranked) >= 2 and rank_gen_dir(ranked[0]) == rank_gen_dir(ranked[1]):
            listing = "\n  ".join(str(m) for m in matches)
            raise SystemExit(f"Ambiguous gen FC-part folder for {gen_fcpart_name}. Matches:\n  {listing}")
        return ranked[0]

    auto = [p for p in workspace_dir.rglob("*_gen.cont") if p.is_dir()]
    if len(auto) == 1:
        return auto[0]
    if not auto:
        raise SystemExit(f"No *_gen.cont folder found under workspace: {workspace_dir}")
    ranked = sorted(auto, key=rank_gen_dir, reverse=True)
    if len(ranked) >= 2 and rank_gen_dir(ranked[0]) == rank_gen_dir(ranked[1]):
        listing = "\n  ".join(str(m) for m in auto)
        raise SystemExit(
            "Multiple *_gen.cont folders found. Provide --gen-fcpart-name to select one:\n"
            f"  {listing}"
        )
    return ranked[0]


def _pick_target_name_for_bsh_file(*, src_name: str, target_base: str, target_dir: Path) -> str | None:
    src_path = Path(src_name)
    stem = src_path.stem
    ext = src_path.suffix
    low = stem.lower()
    idx = low.find("_gen")
    if idx < 0:
        return None
    suffix = stem[idx + 4 :]  # e.g. "", "_ecucvalues", "_confdata", "_pavast"

    # Exact target stem mapping first.
    candidates = [f"{target_base}{suffix}{ext}"]
    if suffix.lower() == "_confdata":
        candidates.append(f"{target_base}_prot_confdata{ext}")
    for name in candidates:
        if (target_dir / name).exists():
            return name

    # Fallback: one unique same-extension target file containing the suffix token.
    token = suffix.strip("_").lower()
    pool = [
        p.name
        for p in target_dir.glob(f"*{ext}")
        if p.is_file() and p.stem.lower().startswith(target_base.lower())
    ]
    if token:
        pool = [name for name in pool if token in name.lower()]
    if len(pool) == 1:
        return pool[0]
    return None


def _resolve_manual_xml_target(*, target_dir: Path, target_base: str) -> Path | None:
    nested = target_dir / f"{target_base}.cont"
    candidates: list[Path] = []
    if nested.exists() and nested.is_dir():
        candidates.extend(sorted(p for p in nested.glob("*manual*.xml") if p.is_file()))
    if not candidates:
        candidates.extend(sorted(p for p in target_dir.rglob("*manual*.xml") if p.is_file()))
    if not candidates:
        return None
    # Prefer the shortest path depth (closest manual file).
    candidates = sorted(candidates, key=lambda p: len(p.parts))
    return candidates[0]


def _rewrite_generated_text_for_target(content: str, target_base: str) -> str:
    old_names: set[str] = set()
    patterns = [
        r"<SHORT-NAME>([^<]+_gen)</SHORT-NAME>",
        r'<SignalConfigExporter\s+filename="([^"]+_gen[^"]*)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, content, flags=re.IGNORECASE)
        if m and (m.group(1) or "").strip():
            old_names.add((m.group(1) or "").strip())

    rewritten = content
    for old_name in old_names:
        rewritten = rewritten.replace(old_name, target_base)
        rewritten = rewritten.replace(old_name.lower(), target_base.lower())

    # Ensure PaVaSt component naming follows checked-out FC-part naming.
    if "<CATEGORY>PaVaSt</CATEGORY>" in rewritten:
        # Keep generic _gen replacement here; detailed PaVaSt naming is applied later
        # once parent FC and FC-part context is known.
        pass
    return rewritten


def _derive_pavast_component_names(*, target_dir: Path, target_base: str) -> tuple[str, str, str, str]:
    parent_fc = target_dir.parent.stem
    part_core = target_base[:-4] if target_base.lower().endswith("_gen") else target_base
    parent_tail = parent_fc.split("_")[-1].lower() if parent_fc else ""
    if parent_tail and part_core.lower().startswith(parent_tail + "_"):
        part_suffix = part_core[len(parent_tail) + 1 :]
    else:
        part_suffix = part_core
    variant_name = f"{parent_fc}_{part_suffix}_GEN" if parent_fc else f"{part_core}_GEN"
    feature_name = parent_fc or part_core
    return feature_name, variant_name, f"{variant_name}_Exx", f"{variant_name}_Im"


def _rewrite_pavast_component_names(*, content: str, target_dir: Path, target_base: str) -> str:
    if "<CATEGORY>PaVaSt</CATEGORY>" not in content:
        return content
    feature_name, variant_name, export_name, import_name = _derive_pavast_component_names(
        target_dir=target_dir,
        target_base=target_base,
    )
    rewritten = content
    rewritten = re.sub(
        r"(<SW-FEATURE>\s*<SHORT-NAME>)([^<]+)(</SHORT-NAME>)",
        rf"\1{feature_name}\3",
        rewritten,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    rewritten = re.sub(
        r"(<SW-FEATURE-VARIANT>\s*<SHORT-NAME>)([^<]+)(</SHORT-NAME>)",
        rf"\1{variant_name}\3",
        rewritten,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )
    rewritten = re.sub(
        r"(<SW-INSTANCE-TREE>\s*<SHORT-NAME>)([^<]+)(</SHORT-NAME>)",
        rf"\1{variant_name}\3",
        rewritten,
        count=1,
        flags=re.IGNORECASE | re.DOTALL,
    )

    interface_pattern = r"(<SW-FEATURE-INTERFACE>\s*<SHORT-NAME>)([^<]+)(</SHORT-NAME>)"
    repl_values = [export_name, import_name]
    idx = {"i": 0}

    def _interface_repl(match: re.Match[str]) -> str:
        i = idx["i"]
        idx["i"] = i + 1
        new_name = repl_values[i] if i < len(repl_values) else match.group(2)
        return f"{match.group(1)}{new_name}{match.group(3)}"

    rewritten = re.sub(interface_pattern, _interface_repl, rewritten, flags=re.IGNORECASE | re.DOTALL)
    return rewritten


def apply_bsh_generated_to_workspace(
    *,
    bsh_output_dir: Path,
    workspace_dir: Path,
    gen_fcpart_name: str,
) -> list[Path]:
    if not bsh_output_dir.exists() or not bsh_output_dir.is_dir():
        raise SystemExit(f"BSH output folder does not exist: {bsh_output_dir}")
    target_dir = _resolve_target_gen_part_dir(workspace_dir=workspace_dir, gen_fcpart_name=gen_fcpart_name)
    target_base = target_dir.stem

    source_files = sorted(
        [
            p
            for p in bsh_output_dir.iterdir()
            if p.is_file() and p.suffix.lower() in (".c", ".h", ".xml", ".arxml", ".sig")
        ]
    )
    if not source_files:
        raise SystemExit(f"No generated .c/.h/.xml/.arxml/.sig files found in BSH output folder: {bsh_output_dir}")

    copied = 0
    replaced_paths: list[Path] = []
    for src in source_files:
        if src.suffix.lower() == ".sig":
            manual_target = _resolve_manual_xml_target(target_dir=target_dir, target_base=target_base)
            if manual_target is None:
                print(f"[bsh-sync] skip (no manual xml target): {src.name}")
                continue
            manual_text = src.read_text(encoding="latin-1")
            manual_text = _rewrite_generated_text_for_target(manual_text, target_base)
            manual_target.write_text(manual_text, encoding="latin-1", newline="")
            copied += 1
            replaced_paths.append(manual_target)
            print(f"[bsh-sync] replaced: {src.name} -> {manual_target.name}")
            continue

        target_name = _pick_target_name_for_bsh_file(src_name=src.name, target_base=target_base, target_dir=target_dir)
        if not target_name:
            print(f"[bsh-sync] skip (no target match): {src.name}")
            continue
        dst = target_dir / target_name
        if src.suffix.lower() in (".xml", ".arxml"):
            text = src.read_text(encoding="latin-1")
            text = _rewrite_generated_text_for_target(text, target_base)
            if target_name.lower().endswith("_pavast.xml"):
                text = _rewrite_pavast_component_names(content=text, target_dir=target_dir, target_base=target_base)
            dst.write_text(text, encoding="latin-1", newline="")
        else:
            shutil.copy2(src, dst)
        copied += 1
        replaced_paths.append(dst)
        print(f"[bsh-sync] replaced: {src.name} -> {dst.name}")

    if copied == 0:
        raise SystemExit(
            f"BSH sync found files but could not map any target file under {target_dir}. "
            "Check naming or pass --gen-fcpart-name."
        )
    print(f"[bsh-sync] target gen part: {target_dir}")
    print(f"[bsh-sync] total files replaced: {copied}")
    return replaced_paths


def _rows_from_replaced_paths(*, replaced_paths: list[Path], workspace_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in replaced_paths:
        if not path.exists() or not path.is_file():
            continue
        rel = str(path.relative_to(workspace_dir)).replace("/", "\\")
        rows.append(
            {
                "file_path": str(path),
                "file_name": path.name,
                "change_type": "modified",
                "zip_entry": rel,
            }
        )
    return rows


def _derive_root_fullname_from_zip_name(zip_path: Path) -> str | None:
    stem = zip_path.stem
    parts = stem.split("_")
    if len(parts) < 4:
        return None
    clazz = parts[0]
    revision = parts[-1]
    variant = parts[-2]
    version_name = "_".join(parts[1:-2])
    if not revision.isdigit():
        return None
    return f"{clazz} : {version_name} / {variant}; {revision}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(_to_windows_long_path(path), "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel_path_is_under_gen_container(rel_path: str) -> bool:
    """Return True when at least one directory segment in rel_path ends with _Gen.cont (case-insensitive).

    Only directory parts are checked; the final file name component is excluded.
    """
    parts = rel_path.replace("\\", "/").split("/")
    return any(p.lower().endswith("_gen.cont") for p in parts[:-1])


def _rel_path_matches_selected_gen_container(rel_path: str, selected_gen_fcpart_name: str = "") -> bool:
    """Filter a zip path to the exact selected *_gen.cont part when multiple exist."""
    if not selected_gen_fcpart_name.strip():
        return _rel_path_is_under_gen_container(rel_path)

    target = selected_gen_fcpart_name.strip().lower()
    if target.endswith(".cont"):
        target = target[:-5]
    target_parts = [
        target,
        f"{target}_gen",
        f"{target}_gen.cont",
        f"{target}.cont",
    ]
    parts = [p.lower() for p in rel_path.replace("\\", "/").split("/")]
    return any(part in target_parts or part.endswith(f"{suffix}.cont") for part in parts for suffix in (target, f"{target}_gen"))


def _rel_path_matches_selected_non_gen_container(rel_path: str, selected_gen_fcpart_name: str = "") -> bool:
    """Filter a zip path to the non-_gen sibling of the selected *_gen.cont part."""
    if not selected_gen_fcpart_name.strip():
        return False

    target = selected_gen_fcpart_name.strip().lower()
    if target.endswith(".cont"):
        target = target[:-5]
    if target.endswith("_gen"):
        sibling = target[:-4]
    else:
        sibling = target

    sibling_parts = [
        sibling,
        f"{sibling}.cont",
    ]
    parts = [p.lower() for p in rel_path.replace("\\", "/").split("/")]
    return any(part in sibling_parts or part.endswith(f"{sibling}.cont") for part in parts)


def detect_changed_files_from_zip(
    *,
    zip_path: Path,
    workspace_dir: Path,
    gen_container_only: bool = False,
    gen_fcpart_name: str = "",
    include_selected_non_gen: bool = False,
) -> list[dict[str, str]]:
    """Compare zip contents against the local workspace and return changed/missing entries.
 
    Args:
        zip_path: Original FC zip downloaded from SDOM.
        workspace_dir: Extracted workspace directory.
        gen_container_only: When True, only include files that live under a *_Gen.cont
            directory.  Use this for RDBI flows where only the generated FC-part is
            modified; non-gen containers must not be checked out.
        gen_fcpart_name: Exact *_gen container name (without .cont) to scope the delta.
        include_selected_non_gen: When True together with gen_container_only, also include
            the non-_gen sibling container of the selected *_gen FC-part. Use this for
            RDBI manual-stub flows where SPEC files are updated in the active non-_gen
            container alongside the generated artifacts.
    """
    changed: list[dict[str, str]] = []

    with zipfile.ZipFile(zip_path, "r") as zf:
        zip_file_entries = [i for i in zf.infolist() if not i.is_dir()]
        zip_index = {entry.filename.replace("/", "\\"): entry for entry in zip_file_entries}

        # Changed/removed files compared to original zip.
        for rel_path, entry in zip_index.items():
            if gen_container_only:
                selected_match = _rel_path_matches_selected_gen_container(rel_path, gen_fcpart_name)
                selected_non_gen_match = include_selected_non_gen and _rel_path_matches_selected_non_gen_container(
                    rel_path,
                    gen_fcpart_name,
                )
                if not selected_match and not selected_non_gen_match and not rel_path.lower().endswith("_fs.xml"):
                    continue  # skip files outside the selected *_Gen.cont for RDBI flow

            ws_file = workspace_dir / rel_path
            if not _path_exists(ws_file):  # use long-path-safe check
                changed.append(
                    {
                        "file_path": str(ws_file),
                        "file_name": Path(rel_path).name,
                        "change_type": "missing",
                        "zip_entry": rel_path,
                    }
                )
                continue

            original_hash = _sha256_bytes(zf.read(entry))
            current_hash = _sha256_file(ws_file)
            # In RDBI gen-container mode always include manual XML in nested *_gen.cont/MISC flow,
            # even if hash is equal, so that manual XML import is never skipped.
            always_include_manual = gen_container_only and rel_path.lower().endswith("_manual.xml")
            if original_hash != current_hash or always_include_manual:
                changed.append(
                    {
                        "file_path": str(ws_file),
                        "file_name": ws_file.name,
                        "change_type": "modified",
                        "zip_entry": rel_path,
                    }
                )

    # Intentionally ignore workspace-only "new" files.
    # SDOM import flow here is for replacement of existing fetched files.
    return changed


def write_detected_changes_csv(*, rows: list[dict[str, str]], out_csv: Path, root_fullname: str) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["file_path", "root_fullname", "file_name", "change_type", "zip_entry"],
        )
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["root_fullname"] = root_fullname
            writer.writerow(out)


def checkin_single_file(
    session: requests.Session,
    *,
    file_path: Path,
    file_name: str,
    object_fullname: str,
    checkin_type: str,
    value: str | None,
) -> requests.Response:
    """
    Single-object check-in via /configuration/checkin.
    type:
      - R/r = revision
      - V/v = variant
    """
    url = f"{BASE_URL}/configuration/checkin"
    params: dict[str, str] = {
        "fileName": file_name,
        "oslc.where": f'oslc_scm:fullName="{object_fullname}"',
        "type": checkin_type,
    }
    if value:
        params["value"] = value

    with open(_to_windows_long_path(file_path), "rb") as payload:
        return session.post(
            url,
            params=params,
            headers=_headers(),
            data=payload,
            timeout=120,
        )


def checkin_batch_csv(
    session: requests.Session,
    *,
    csv_path: Path,
    default_type: str,
) -> int:
    required_cols = {"file_path", "object_fullname"}
    success_count = 0

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise SystemExit(f"CSV has no header: {csv_path}")
        missing = required_cols - set(reader.fieldnames)
        if missing:
            raise SystemExit(f"CSV missing required columns {sorted(missing)}: {csv_path}")

        for idx, row in enumerate(reader, start=2):
            raw_path = (row.get("file_path") or "").strip()
            object_fullname = (row.get("object_fullname") or "").strip()
            if not raw_path or not object_fullname:
                raise SystemExit(f"CSV row {idx}: file_path/object_fullname must not be empty")

            file_path = Path(raw_path)
            if not _path_exists(file_path):
                raise SystemExit(f"CSV row {idx}: file does not exist: {file_path}")

            checkin_type = ((row.get("type") or default_type) or "R").strip()
            value = (row.get("value") or "").strip() or None
            if checkin_type.lower() == "r" and not value:
                value = _next_revision_value(object_fullname)

            file_name = _normalize_file_name(row.get("file_name"), file_path)
            print(f"\n[batch] row={idx} file={file_path} fileName={file_name} type={checkin_type} value={value}")
            response = checkin_single_file(
                session,
                file_path=file_path,
                file_name=file_name,
                object_fullname=object_fullname,
                checkin_type=checkin_type,
                value=value,
            )
            _print_response(response)
            if response.status_code >= 400:
                raise SystemExit(f"Batch check-in failed at CSV row {idx}")
            created = _extract_created_fullname(response.text or "")
            if created:
                print(f"[batch] created: {created}")
            success_count += 1

    return success_count


def import_batch_csv(
    session: requests.Session,
    *,
    csv_path: Path,
    checkout_mode: str,
    target_variant: str | None,
    edition: bool,
    checkin: bool,
    checkin_csv: Path | None = None,
) -> int:
    required_cols = {"file_path", "root_fullname"}
    success_count = 0
    checkin_rows: list[dict[str, str]] = []
    configtree_cache: dict[str, dict[str, list[str]]] = {}

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise SystemExit(f"CSV has no header: {csv_path}")
        missing = required_cols - set(reader.fieldnames)
        if missing:
            raise SystemExit(f"CSV missing required columns {sorted(missing)}: {csv_path}")

        for idx, row in enumerate(reader, start=2):
            raw_path = (row.get("file_path") or "").strip()
            root_fullname = (row.get("root_fullname") or "").strip()
            if not raw_path or not root_fullname:
                raise SystemExit(f"CSV row {idx}: file_path/root_fullname must not be empty")

            file_path = Path(raw_path)
            if not _path_exists(file_path):
                raise SystemExit(f"CSV row {idx}: file does not exist: {file_path}")

            file_name = _normalize_file_name(row.get("file_name"), file_path)
            if Path(file_name).suffix.lower() not in (".c", ".h", ".xml", ".arxml"):
                print(f"[import-batch] skip unsupported file type at row={idx}: {file_name}")
                continue
            row_mode = (row.get("checkout_mode") or checkout_mode).strip()
            row_variant = (row.get("target_variant") or target_variant or "").strip() or None
            row_edition = str(row.get("edition") or str(edition)).strip().lower() in ("1", "true", "yes", "y")
            row_checkin = str(row.get("checkin") or str(checkin)).strip().lower() in ("1", "true", "yes", "y")
            row_change_type = (row.get("change_type") or "").strip().lower()
            zip_entry = (row.get("zip_entry") or "").strip()
            is_manual_xml = file_name.lower().endswith("_manual.xml")
            if is_manual_xml:
                # IOCBI-style behavior for nested DOCMISC: resolve parent GEN FC-part as anchor,
                # then resolve/align MISC child under that parent.
                resolved_root = _resolve_manual_docmisc_parent_root(
                    session=session,
                    fc_root_fullname=root_fullname,
                    zip_entry=zip_entry,
                    configtree_cache=configtree_cache,
                )
                resolved_root = _ensure_specific_container_variant_matches_parent(
                    session=session,
                    fc_root_fullname=root_fullname,
                    container_fullname=resolved_root,
                )
            else:
                resolved_root = _ensure_container_variant_matches_parent(
                    session=session,
                    fc_root_fullname=root_fullname,
                    zip_entry=zip_entry,
                    configtree_cache=configtree_cache,
                )
            if row_mode.lower() == "none":
                parent_variant = _parse_variant_from_fullname(root_fullname)
                parent_revision = _parse_revision_num(root_fullname)
                # Match strategy with parent FC state:
                # - parent revision 0 => variant-style propagation (release/bugfix variant line)
                # - parent revision > 0 => revision-style propagation
                if parent_variant and parent_revision == 0:
                    row_mode = "variant"
                    row_variant = parent_variant
                else:
                    row_mode = "revision"

            print(
                f"\n[import-batch] row={idx} file={file_path} root={root_fullname} "
                f"resolvedRoot={resolved_root} fileName={file_name} mode={row_mode} "
                f"edition={row_edition} checkin={row_checkin}"
            )
            parent_variant = _parse_variant_from_fullname(root_fullname)
            base_variant = _parse_variant_from_fullname(resolved_root)
            file_class, stem = _resolve_lookup_class_stem(file_path=file_path, file_name=file_name)
            container_fullname = resolved_root
            
            # Detect if file comes from a non-_gen container (manual stub files)
            is_from_non_gen = zip_entry and not any(
                part.lower().endswith("_gen.cont") for part in zip_entry.replace("\\", "/").split("/")[:-1]
            )
            
            if file_class == "DOCMISC":
                misc_container = _resolve_docmisc_container(
                    session=session,
                    resolved_root=resolved_root,
                    file_path=file_path,
                )
                if misc_container:
                    container_fullname = misc_container
                # IOCBI parity: DOCMISC requires an editable MISC container. Always prepare MISC
                # in edition mode: variant-align when needed, otherwise revision checkout.
                container_variant = _parse_variant_from_fullname(container_fullname)
                if container_fullname and parent_variant and container_variant:
                    try:
                        version_kind = None
                        action_label = "variant-align"
                        if container_variant == parent_variant:
                            version_kind = "revision"
                            action_label = "revision-checkout"

                        aligned_misc = _checkout_container_variant(
                            session=session,
                            base_fullname=container_fullname,
                            target_variant=parent_variant,
                            version_kind=version_kind,
                        )

                        if _to_spaced_fullname(aligned_misc) != _to_spaced_fullname(container_fullname):
                            _replace_child_reference_resilient(
                                session=session,
                                container_fullname=resolved_root,
                                old_child_fullname=container_fullname,
                                new_child_fullname=aligned_misc,
                            )
                            print(
                                f"[import-batch] {action_label} MISC under parent: "
                                f"{_to_spaced_fullname(container_fullname)} -> {_to_spaced_fullname(aligned_misc)}"
                            )
                        container_fullname = _to_spaced_fullname(aligned_misc)
                    except Exception as align_ex:
                        raise SystemExit(
                            f"Import batch failed at CSV row {idx}. MISC checkout/relink failed under "
                            f"{resolved_root}. Detail: {align_ex}"
                        )
            elif file_class == "TDATA" and not is_from_non_gen:
                # Ensure the parent GEN container itself is in edition before TDATA relink.
                container_variant = _parse_variant_from_fullname(container_fullname)
                if container_fullname and parent_variant and container_variant:
                    try:
                        version_kind = None
                        action_label = "variant-align"
                        if container_variant == parent_variant:
                            version_kind = "revision"
                            action_label = "revision-checkout"

                        editable_container = _checkout_container_variant(
                            session=session,
                            base_fullname=container_fullname,
                            target_variant=parent_variant,
                            version_kind=version_kind,
                        )

                        if _to_spaced_fullname(editable_container) != _to_spaced_fullname(container_fullname):
                            _replace_child_reference_resilient(
                                session=session,
                                container_fullname=root_fullname,
                                old_child_fullname=container_fullname,
                                new_child_fullname=editable_container,
                            )
                            print(
                                f"[import-batch] {action_label} GEN container under FC: "
                                f"{_to_spaced_fullname(container_fullname)} -> {_to_spaced_fullname(editable_container)}"
                            )
                        container_fullname = _to_spaced_fullname(editable_container)
                    except Exception as tdata_ex:
                        raise SystemExit(
                            f"Import batch failed at CSV row {idx}. TDATA parent container checkout/relink failed under "
                            f"{root_fullname}. Detail: {tdata_ex}"
                        )
            elif file_class in ("TDOC", "SPEC") and not is_from_non_gen:
                try:
                    spec_parent_container = resolved_root
                    spec_container = _resolve_spec_container_for_tdoc(
                        session=session,
                        fc_root_fullname=spec_parent_container,
                        tdoc_stem=stem,
                    )
                    if not spec_container and zip_entry:
                        parent_part_sysfile = _extract_fc_part_sysfile(zip_entry, level_from_leaf=1)
                        if parent_part_sysfile:
                            if root_fullname not in configtree_cache:
                                configtree_cache[root_fullname] = _fetch_configtree_container_map(session, root_fullname)
                            parent_candidates = configtree_cache.get(root_fullname, {}).get(parent_part_sysfile, [])
                            alt_parent = _select_best_container_fullname(
                                fc_root_fullname=root_fullname,
                                candidates=parent_candidates,
                            )
                            if not alt_parent:
                                alt_parent = _find_child_container_fullname_in_parent(
                                    session=session,
                                    parent_fullname=root_fullname,
                                    part_sysfile=parent_part_sysfile,
                                )
                            if alt_parent:
                                spec_parent_container = _ensure_specific_container_variant_matches_parent(
                                    session=session,
                                    fc_root_fullname=root_fullname,
                                    container_fullname=alt_parent,
                                )
                                spec_container = _resolve_spec_container_for_tdoc(
                                    session=session,
                                    fc_root_fullname=spec_parent_container,
                                    tdoc_stem=stem,
                                )
                    if not spec_container:
                        raise RuntimeError(
                            f"SPEC parent container could not be resolved under {_to_spaced_fullname(spec_parent_container)} "
                            f"for TDOC stem {stem}"
                        )

                    # TDOC files live inside the nested TDOC container below SPEC, so resolve that
                    # child container first and import against it instead of the outer SPEC container.
                    nested_tdoc_resolved = False
                    tdoc_part_sysfile = _extract_fc_part_sysfile(zip_entry, level_from_leaf=0)
                    if tdoc_part_sysfile:
                        nested_tdoc_container = _find_child_container_fullname_in_parent(
                            session=session,
                            parent_fullname=spec_container,
                            part_sysfile=tdoc_part_sysfile,
                        )
                        if nested_tdoc_container:
                            nested_tdoc_resolved = True
                            tdoc_container_variant = _parse_variant_from_fullname(nested_tdoc_container)
                            version_kind = None
                            action_label = "variant-align"
                            if tdoc_container_variant == parent_variant:
                                version_kind = "revision"
                                action_label = "revision-checkout"

                            try:
                                aligned_tdoc = _checkout_container_variant(
                                    session=session,
                                    base_fullname=nested_tdoc_container,
                                    target_variant=parent_variant,
                                    version_kind=version_kind,
                                )
                                if _to_spaced_fullname(aligned_tdoc) != _to_spaced_fullname(nested_tdoc_container):
                                    _replace_child_reference_resilient(
                                        session=session,
                                        container_fullname=spec_container,
                                        old_child_fullname=nested_tdoc_container,
                                        new_child_fullname=aligned_tdoc,
                                    )
                                    print(
                                        f"[import-batch] {action_label} TDOC under SPEC: "
                                        f"{_to_spaced_fullname(nested_tdoc_container)} -> {_to_spaced_fullname(aligned_tdoc)}"
                                    )
                                container_fullname = _to_spaced_fullname(aligned_tdoc)
                            except Exception as tdoc_cont_ex:
                                raise RuntimeError(
                                    f"TDOC child container checkout/relink failed under {_to_spaced_fullname(spec_container)}. "
                                    f"Detail: {tdoc_cont_ex}"
                                )
                        else:
                            container_fullname = _to_spaced_fullname(spec_container)
                    else:
                        container_fullname = _to_spaced_fullname(spec_container)

                    if not nested_tdoc_resolved:
                        spec_variant = _parse_variant_from_fullname(spec_container)
                        version_kind = None
                        action_label = "variant-align"
                        if spec_variant == parent_variant:
                            version_kind = "revision"
                            action_label = "revision-checkout"

                        editable_spec = _checkout_container_variant(
                            session=session,
                            base_fullname=spec_container,
                            target_variant=parent_variant,
                            version_kind=version_kind,
                        )

                        if _to_spaced_fullname(editable_spec) != _to_spaced_fullname(spec_container):
                            _replace_child_reference_resilient(
                                session=session,
                                container_fullname=spec_parent_container,
                                old_child_fullname=spec_container,
                                new_child_fullname=editable_spec,
                            )
                            print(
                                f"[import-batch] {action_label} SPEC under parent container: "
                                f"{_to_spaced_fullname(spec_container)} -> {_to_spaced_fullname(editable_spec)}"
                            )
                        container_fullname = _to_spaced_fullname(editable_spec)
                except Exception as tdoc_ex:
                    raise SystemExit(
                        f"Import batch failed at CSV row {idx}. TDOC parent SPEC checkout/relink failed under "
                        f"{root_fullname}. Detail: {tdoc_ex}"
                    )
            
            # For non-_gen files (manual stubs), if not DOCMISC, import directly into resolved_root container
            if is_from_non_gen and file_class != "DOCMISC" and file_class not in ("TDATA", "TDOC", "SPEC"):
                print(f"[import-batch] manual-stub (non-_gen) file: {file_class} at {_to_spaced_fullname(container_fullname)}")
            
            linked_child_fullname = _find_child_file_fullname_in_container(
                session=session,
                container_fullname=container_fullname,
                file_class=file_class,
                stem=stem,
            )
            base_file_fullname = _find_latest_file_fullname(
                session=session,
                file_class=file_class,
                stem=stem,
                variant=base_variant,
            )
            if not base_file_fullname:
                base_file_fullname = _find_child_file_fullname_in_container(
                    session=session,
                    container_fullname=container_fullname,
                    file_class=file_class,
                    stem=stem,
                )
            if not base_file_fullname:
                base_file_fullname = _find_variant_fallback_file_fullname(
                    session=session,
                    file_class=file_class,
                    stem=stem,
                    preferred_variant=base_variant,
                )
            if not base_file_fullname:
                if row_change_type == "new":
                    print(
                        f"[import-batch] skip unmapped new file at row={idx}: "
                        f"{file_class}:{stem}/{base_variant};*"
                    )
                    continue
                raise SystemExit(
                    f"Import batch failed at CSV row {idx}. Could not find base file object "
                    f"{file_class}:{stem}/{base_variant};*"
                )

            target_variant = parent_variant or base_variant
            try:
                new_file_fullname = _checkout_file_variant_from_file_root(
                    session=session,
                    base_file_fullname=base_file_fullname,
                    file_path=file_path,
                    file_name=file_name,
                    target_variant=target_variant,
                )
            except Exception as ex:
                raise SystemExit(f"Import batch failed at CSV row {idx}. {ex}")

            print(f"[import-batch] file variant prepared: {new_file_fullname}")
            current_linked = _find_child_file_fullname_in_container(
                session=session,
                container_fullname=container_fullname,
                file_class=file_class,
                stem=stem,
            )
            if current_linked and _to_spaced_fullname(current_linked) == _to_spaced_fullname(new_file_fullname):
                print(
                    f"[import-batch] child already linked under container: {container_fullname} -> "
                    f"{new_file_fullname}; skipping replacechildren"
                )
            elif linked_child_fullname or current_linked:
                old_for_relink = linked_child_fullname or current_linked
                try:
                    _replace_child_reference_resilient(
                        session=session,
                        container_fullname=container_fullname,
                        old_child_fullname=old_for_relink,
                        new_child_fullname=new_file_fullname,
                    )
                    print(f"[import-batch] child link updated under: {container_fullname}")
                except Exception as relink_ex:
                    live_after = _find_child_file_fullname_in_container(
                        session=session,
                        container_fullname=container_fullname,
                        file_class=file_class,
                        stem=stem,
                    )
                    if live_after and _to_spaced_fullname(live_after) == _to_spaced_fullname(new_file_fullname):
                        print(
                            f"[import-batch] warning: relink was already effective under {container_fullname}; "
                            f"skipping fallback error handling."
                        )
                    elif file_class == "DOCMISC":
                        # DOCMISC/MISC must be linked - hard fail.
                        raise SystemExit(
                            f"Import batch failed at CSV row {idx}. DOCMISC replacechildren failed under "
                            f"{container_fullname} for {file_class}:{stem}. Detail: {relink_ex}"
                        )
                    else:
                        # TDATA/SWSRC/SWHDR/CONFDATA: SDOM may return 400 for replacechildren
                        # even when checkout succeeded (server restriction). Warn and continue.
                        print(
                            f"[import-batch] warning: replacechildren at row={idx} failed for "
                            f"{file_class}:{stem}; continuing (checkout succeeded). Detail: {relink_ex}"
                        )
            else:
                print(
                    f"[import-batch] no linked child found under container; "
                    f"skip replacechildren for {file_class}:{stem}"
                )

            checkin_object = new_file_fullname
            checkin_rows.append(
                {
                    "file_path": str(file_path),
                    "object_fullname": checkin_object,
                    "file_name": file_name,
                    "type": "R",
                    "value": "",
                }
            )
            print(f"[import-batch] checkin target: {checkin_object}")
            success_count += 1

    if checkin_csv:
        checkin_csv.parent.mkdir(parents=True, exist_ok=True)
        with checkin_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["file_path", "object_fullname", "file_name", "type", "value"],
            )
            writer.writeheader()
            for row in checkin_rows:
                writer.writerow(row)
        print(f"[import-batch] checkin CSV saved: {checkin_csv}")

    return success_count


def checkin_fc_recursive(
    session: requests.Session,
    *,
    fc_fullname: str,
    payload_file: Path | None,
) -> requests.Response:
    """
    FC-level recursive check-in via /configuration/checkinrecursive.

    root must be fullName string:
      <class_name>:<version_name>/<variant>;revision
    Example:
      FC-ARB : ExampleFeature / 1.2.3; 0

    If payload_file is provided, it is streamed as request body.
    """
    url = f"{BASE_URL}/configuration/checkinrecursive"
    params = {"root": fc_fullname}

    if payload_file:
        with open(_to_windows_long_path(payload_file), "rb") as payload:
            return session.post(
                url,
                params=params,
                headers=_headers(),
                data=payload,
                timeout=180,
            )

    return session.post(
        url,
        params=params,
        headers=_headers(),
        timeout=180,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="SDOM check-in/import helper.")
    parser.add_argument(
        "--mode",
        choices=[
            "guided-runtime",
            "single",
            "batch",
            "fc-recursive",
            "checkout-version",
            "checkout-status",
            "import-single",
            "import-batch",
            "prepare-import",
            "checkin-confirmed",
        ],
        default="guided-runtime",
        help="prepare-import detects changed files; import-* uploads editions (inbearb); checkin-confirmed runs final checkin",
    )
    parser.add_argument("--user", default="", help="SDOM user")
    parser.add_argument("--password", default="", help="SDOM password")
    parser.add_argument("--dry-run", action="store_true", help="Print request details only")
    parser.add_argument(
        "--base-fullname",
        default="",
        help="Base version fullName for checkout-version mode (e.g. BC : DiaApl / 358.25.0_H2CU_8; 0)",
    )
    parser.add_argument(
        "--version-comment",
        default="Auto checkout by script",
        help="Version comment for checkout-version mode",
    )
    parser.add_argument(
        "--rq1-context",
        type=Path,
        default=None,
        help="RQ1 context JSON used to derive BC checkout base and target version",
    )
    parser.add_argument(
        "--reference-json",
        type=Path,
        default=None,
        help="SDOM checkout reference JSON (simplified, extracted from RQ1)",
    )
    parser.add_argument(
        "--checkout-objects",
        default="bc,pver",
        help="Comma-separated list of objects to checkout: bc, pver, fc (default: bc,pver)",
    )
    parser.add_argument(
        "--status-output",
        type=Path,
        default=None,
        help="Optional output JSON path for checkout-status results",
    )

    # Single-file mode args
    parser.add_argument(
        "--file-path",
        type=Path,
        default=Path(__file__).resolve().parent / "rba_IoSigIfAdapt_Cfg_IcuMok.h",
        help="Local file path to upload in single mode",
    )
    parser.add_argument(
        "--file-name",
        default="",
        help="Target repository filename (single mode). Default: basename of --file-path",
    )
    parser.add_argument(
        "--object-fullname",
        default="SWHDR : rba_IoSigIfAdapt_Cfg_IcuMok / BSW2003; 3",
        help="Object fullName (single mode), without oslc wrapper",
    )
    parser.add_argument(
        "--type",
        default="R",
        choices=["R", "r", "V", "v"],
        help="Check-in type for single mode",
    )
    parser.add_argument(
        "--value",
        default=None,
        help="Variant/revision value. If empty and type=R/r, script auto-uses next revision from --object-fullname",
    )

    # Batch mode args
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=None,
        help="CSV for batch mode. Required columns: file_path,object_fullname. Optional: file_name,type,value",
    )
    parser.add_argument(
        "--changes-csv",
        type=Path,
        default=Path(__file__).resolve().parent / "checkout_workspace" / "changed_files.csv",
        help="Output/input CSV for changed files workflow",
    )
    parser.add_argument(
        "--checkin-csv",
        type=Path,
        default=Path(__file__).resolve().parent / "checkout_workspace" / "imported_for_checkin.csv",
        help="CSV generated after import, then used by checkin-confirmed",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=None,
        help="Extracted FC workspace folder to scan for changed files",
    )
    parser.add_argument(
        "--zip-path",
        type=Path,
        default=None,
        help="Original FC zip path to compare against workspace",
    )
    parser.add_argument(
        "--confirm-checkin",
        action="store_true",
        help="Required in checkin-confirmed mode to prevent accidental final checkin",
    )
    parser.add_argument(
        "--bsh-output-dir",
        type=Path,
        default=None,
        help="Optional BSH Out_beanshell_* folder for auto replace into checkout workspace before import",
    )
    parser.add_argument(
        "--gen-container-only",
        dest="gen_container_only",
        action="store_true",
        default=False,
        help="Only include files under *_Gen.cont containers when detecting changes (RDBI flow).",
    )
    parser.add_argument(
        "--gen-fcpart-name",
        default="",
        help="Optional explicit target gen FC-part name (without .cont), e.g. MyModule_gen",
    )
    parser.add_argument(
        "--include-selected-non-gen",
        dest="include_selected_non_gen",
        action="store_true",
        default=False,
        help="When used with --gen-container-only, also include the selected non-_gen sibling container.",
    )

    # FC recursive mode args
    parser.add_argument(
        "--fc-fullname",
        default=DEFAULT_FC_FULLNAME,
        help="FC fullName root for recursive check-in",
    )
    parser.add_argument(
        "--payload-file",
        type=Path,
        default=None,
        help="Optional request body file for /configuration/checkinrecursive",
    )

    # Import mode args
    parser.add_argument(
        "--root-fullname",
        default="",
        help="Root version fullName for checkoutfileex import mode (e.g. FC-ARB : ExampleFeature / 1.2.3; 0)",
    )
    parser.add_argument(
        "--checkout-mode",
        choices=["none", "revision", "variant"],
        default="none",
        help="checkoutfileex NewVariant mode for import",
    )
    parser.add_argument(
        "--target-variant",
        default="",
        help="Variant value when --checkout-mode variant is used",
    )
    parser.add_argument(
        "--checkin-flag",
        dest="checkin_flag",
        action="store_true",
        help="Set NewFile checkin=true for import mode (default false for inbearb)",
    )
    parser.add_argument(
        "--no-checkin-flag",
        dest="checkin_flag",
        action="store_false",
        help="Set NewFile checkin=false for import mode",
    )
    parser.add_argument(
        "--edition",
        dest="edition",
        action="store_true",
        help="Set NewFile edition=true for import mode",
    )
    parser.add_argument(
        "--no-edition",
        dest="edition",
        action="store_false",
        help="Set NewFile edition=false for import mode",
    )
    parser.set_defaults(edition=True, checkin_flag=False)

    args = parser.parse_args()
    import_csv = args.csv_path if args.csv_path is not None else args.changes_csv

    if args.dry_run:
        print(f"Mode       : {args.mode}")
        print(f"Base URL   : {BASE_URL}")
        if args.mode == "guided-runtime":
            print("Phase      : prepare-import -> import-batch (inbearb only)")
            print(f"workspace  : {args.workspace_dir}")
            print(f"zip_path   : {args.zip_path}")
            print(f"root       : {args.root_fullname}")
            print(f"bsh_output : {args.bsh_output_dir}")
            print(f"gen_fcpart : {args.gen_fcpart_name}")
            print(f"changes_csv: {args.changes_csv}")
            print(f"checkin_csv: {args.checkin_csv}")
            print(f"edition    : {args.edition}")
            print(f"checkin    : {args.checkin_flag}")
        elif args.mode == "single":
            print("Endpoint   : /configuration/checkin")
            print(f"file_path  : {args.file_path}")
            print(f"file_name  : {_normalize_file_name(args.file_name, args.file_path)}")
            print(f"full_name  : {args.object_fullname}")
            dry_value = args.value
            if args.type.lower() == "r" and not dry_value:
                dry_value = _next_revision_value(args.object_fullname)
            print(f"type/value : {args.type}/{dry_value}")
        elif args.mode == "batch":
            print("Endpoint   : /configuration/checkin (batch)")
            print(f"csv_path   : {args.csv_path}")
            print(f"default typ: {args.type}")
        elif args.mode == "import-single":
            print("Endpoint   : /configuration/checkoutfileex")
            print(f"file_path  : {args.file_path}")
            print(f"file_name  : {_normalize_file_name(args.file_name, args.file_path)}")
            print(f"root       : {args.root_fullname}")
            print(f"mode       : {args.checkout_mode}")
            print(f"variant    : {args.target_variant}")
            print(f"edition    : {args.edition}")
            print(f"checkin    : {args.checkin_flag}")
        elif args.mode == "import-batch":
            print("Endpoint   : /configuration/checkoutfileex (batch)")
            print(f"csv_path   : {import_csv}")
            print(f"mode       : {args.checkout_mode}")
            print(f"variant    : {args.target_variant}")
            print(f"edition    : {args.edition}")
            print(f"checkin    : {args.checkin_flag}")
        elif args.mode == "prepare-import":
            print("Phase      : detect changed files for import")
            print(f"workspace  : {args.workspace_dir}")
            print(f"zip_path   : {args.zip_path}")
            print(f"root       : {args.root_fullname}")
            print(f"changes_csv: {args.changes_csv}")
        elif args.mode == "checkin-confirmed":
            print("Phase      : final checkin from imported list")
            print(f"checkin_csv: {args.checkin_csv}")
            print(f"confirmed  : {args.confirm_checkin}")
        elif args.mode == "checkout-version":
            print("Endpoint   : /configuration/checkoutversionex")
            if args.reference_json is not None:
                print(f"reference  : {args.reference_json}")
                ref_data = _load_reference_json(args.reference_json)
                print(f"ifd_id     : {ref_data.get('ifd_id')}")
                print(f"mode       : {ref_data.get('mode')}")
                checkout_objs = [s.strip() for s in (args.checkout_objects or "bc,pver").split(",")]
                for obj in checkout_objs:
                    checkout_info = ref_data.get(f"{obj}_checkout")
                    if checkout_info:
                        print(f"{obj} base   : {checkout_info.get('version_input')}")
                        print(f"{obj} target : {checkout_info.get('derived_version')}")
            elif args.rq1_context is not None:
                print(f"rq1        : {args.rq1_context}")
                base_fullname, target_fullname = _load_bc_checkout_reference(args.rq1_context)
                print(f"base       : {base_fullname}")
                print(f"variant    : {_parse_variant_from_fullname(target_fullname)}")
            else:
                print(f"base       : {args.base_fullname}")
                print(f"variant    : {args.target_variant}")
            print(f"comment    : {args.version_comment}")
        elif args.mode == "checkout-status":
            print("Endpoint   : /configuration/latest")
            print(f"reference  : {args.reference_json}")
            print(f"objects    : {args.checkout_objects}")
            print(f"out_json   : {args.status_output}")
        else:
            print("Endpoint   : /configuration/checkinrecursive")
            print(f"fc_fullname: {args.fc_fullname}")
            print(f"payload    : {args.payload_file}")
        return

    if args.mode == "single" and not args.file_path.exists():
        raise SystemExit(f"Single mode file does not exist: {args.file_path}")

    if args.mode == "batch" and (args.csv_path is None or not args.csv_path.exists()):
        raise SystemExit("Batch mode requires an existing --csv-path")

    if args.mode == "import-batch" and not import_csv.exists():
        raise SystemExit(f"Import-batch requires existing CSV: {import_csv}")
    if args.mode == "prepare-import":
        if args.workspace_dir is None or not args.workspace_dir.exists():
            raise SystemExit("Prepare-import requires existing --workspace-dir")
        if args.zip_path is None or not args.zip_path.exists():
            raise SystemExit("Prepare-import requires existing --zip-path")
        if not args.root_fullname.strip():
            raise SystemExit("Prepare-import requires --root-fullname")
    if args.mode == "guided-runtime":
        checkout_workspace_dir = Path(__file__).resolve().parent / "checkout_workspace"
        zip_path = args.zip_path if args.zip_path is not None else _latest_zip_in_workspace(checkout_workspace_dir)
        if zip_path is None or not zip_path.exists():
            raise SystemExit("Guided mode could not find zip. Provide --zip-path.")
        workspace_dir = args.workspace_dir if args.workspace_dir is not None else (checkout_workspace_dir / zip_path.stem)
        if not workspace_dir.exists():
            raise SystemExit("Guided mode could not find extracted workspace. Provide --workspace-dir.")
        root_fullname = args.root_fullname.strip() if args.root_fullname.strip() else (_derive_root_fullname_from_zip_name(zip_path) or "")
        if not root_fullname:
            raise SystemExit("Guided mode could not derive root fullname. Provide --root-fullname.")
    if args.mode == "checkin-confirmed":
        if not args.confirm_checkin:
            raise SystemExit("checkin-confirmed requires --confirm-checkin")
        if not args.checkin_csv.exists():
            raise SystemExit(f"checkin-confirmed requires existing --checkin-csv: {args.checkin_csv}")

    if args.mode == "fc-recursive" and args.payload_file and (not args.payload_file.exists()):
        raise SystemExit(f"Payload file does not exist: {args.payload_file}")
    if args.mode == "fc-recursive" and not args.fc_fullname.strip():
        if args.reference_json is None:
            raise SystemExit("fc-recursive mode requires --fc-fullname or --reference-json")
        if not args.reference_json.exists():
            raise SystemExit(f"fc-recursive mode requires existing --reference-json: {args.reference_json}")
        reference_data = _load_reference_json(args.reference_json)
        args.fc_fullname = _resolve_reference_checkout_fullname(
            reference_data=reference_data,
            obj_type="fc",
            prefer_base=False,
        )

    if args.mode == "import-single":
        if not args.file_path.exists():
            raise SystemExit(f"Import file does not exist: {args.file_path}")
        if not args.root_fullname.strip():
            raise SystemExit("Import-single mode requires --root-fullname")
    if args.mode == "checkout-version":
        if args.reference_json is not None and not args.reference_json.exists():
            raise SystemExit(f"checkout-version mode requires existing --reference-json: {args.reference_json}")
        if args.rq1_context is not None and not args.rq1_context.exists():
            raise SystemExit(f"checkout-version mode requires existing --rq1-context: {args.rq1_context}")
        if args.reference_json is None and args.rq1_context is None:
            if not args.base_fullname.strip():
                raise SystemExit("checkout-version mode requires --base-fullname or --reference-json or --rq1-context")
            if not args.target_variant.strip():
                raise SystemExit("checkout-version mode requires --target-variant")
    if args.mode == "checkout-status":
        if args.reference_json is None:
            raise SystemExit("checkout-status mode requires --reference-json")
        if not args.reference_json.exists():
            raise SystemExit(f"checkout-status mode requires existing --reference-json: {args.reference_json}")

    if args.mode == "prepare-import":
        rows = detect_changed_files_from_zip(
            zip_path=args.zip_path,
            workspace_dir=args.workspace_dir,
            gen_container_only=args.gen_container_only,
            gen_fcpart_name=args.gen_fcpart_name,
            include_selected_non_gen=args.include_selected_non_gen,
        )
        write_detected_changes_csv(rows=rows, out_csv=args.changes_csv, root_fullname=args.root_fullname)
        print(f"[result] Changed files detected: {len(rows)}")
        print(f"[result] Changes CSV: {args.changes_csv}")
        return

    user = (args.user or "").strip()
    password = args.password or ""
    if not user or not password:
        user, password = prompt_sdom_credentials()

    session = requests.Session()
    session.auth = (user, password)
    session.headers.update({
        "OSLC-Core-Version": "2.0",
        "Content-Type": "application/octet-stream",
        "x-requester": _requester_value(),
    })

    if args.mode == "single":
        value = args.value
        if args.type.lower() == "r" and not value:
            value = _next_revision_value(args.object_fullname)

        response = checkin_single_file(
            session,
            file_path=args.file_path,
            file_name=_normalize_file_name(args.file_name, args.file_path),
            object_fullname=args.object_fullname,
            checkin_type=args.type,
            value=value,
        )
        _print_response(response)
        if response.status_code >= 400:
            raise SystemExit("Single-file check-in failed.")
        created = _extract_created_fullname(response.text or "")
        if created:
            print(f"[result] Created/updated: {created}")
    elif args.mode == "batch":
        count = checkin_batch_csv(
            session,
            csv_path=args.csv_path,
            default_type=args.type,
        )
        print(f"[result] Batch check-in completed. Files processed: {count}")
    elif args.mode == "import-single":
        response = import_file_checkoutfileex(
            session,
            file_path=args.file_path,
            file_name=_normalize_file_name(args.file_name, args.file_path),
            root_fullname=args.root_fullname,
            checkout_mode=args.checkout_mode,
            target_variant=args.target_variant or None,
            edition=args.edition,
            checkin=args.checkin_flag,
        )
        _print_response(response)
        if response.status_code >= 400:
            raise SystemExit("Import-single failed.")
        created = _extract_created_fullname(response.text or "")
        if created:
            print(f"[result] Imported/updated object: {created}")
    elif args.mode == "import-batch":
        count = import_batch_csv(
            session,
            csv_path=import_csv,
            checkout_mode=args.checkout_mode,
            target_variant=args.target_variant or None,
            edition=args.edition,
            checkin=args.checkin_flag,
            checkin_csv=args.checkin_csv,
        )
        print(f"[result] Import batch completed. Files processed: {count}")
    elif args.mode == "checkin-confirmed":
        count = checkin_batch_csv(
            session,
            csv_path=args.checkin_csv,
            default_type=args.type,
        )
        print(f"[result] Final check-in completed. Files processed: {count}")
    elif args.mode == "guided-runtime":
        # Auto apply latest BSH generated files to target *_gen.cont workspace before change detection/import.
        repo_root = Path(__file__).resolve().parent.parent
        bsh_dir = repo_root / "BSH"
        bsh_output_dir = args.bsh_output_dir if args.bsh_output_dir is not None else _latest_bsh_output_folder(bsh_dir)
        bsh_rows: list[dict[str, str]] = []
        if bsh_output_dir is not None:
            replaced_paths = apply_bsh_generated_to_workspace(
                bsh_output_dir=bsh_output_dir,
                workspace_dir=workspace_dir,
                gen_fcpart_name=args.gen_fcpart_name,
            )
            bsh_rows = _rows_from_replaced_paths(replaced_paths=replaced_paths, workspace_dir=workspace_dir)
        else:
            print("[bsh-sync] skip: no BSH Out_beanshell_* folder found")

        rows = detect_changed_files_from_zip(zip_path=zip_path, workspace_dir=workspace_dir)
        # Ensure all explicit BSH replacements are included even if zip baseline cannot map them.
        by_file: dict[str, dict[str, str]] = {r["file_path"]: r for r in rows}
        for r in bsh_rows:
            by_file[r["file_path"]] = r
        rows = list(by_file.values())
        write_detected_changes_csv(rows=rows, out_csv=args.changes_csv, root_fullname=root_fullname)
        print(f"[phase] Changed files detected: {len(rows)}")
        print(f"[phase] Changes CSV: {args.changes_csv}")
        if not rows:
            print("[result] No changed files found. Nothing to import/checkin.")
            return

        import_count = import_batch_csv(
            session,
            csv_path=args.changes_csv,
            checkout_mode=args.checkout_mode,
            target_variant=args.target_variant or None,
            edition=args.edition,
            checkin=args.checkin_flag,
            checkin_csv=args.checkin_csv,
        )
        print(f"[phase] Import completed. Files processed: {import_count}")
        print(f"[phase] Review changes in SDOM (inbearb).")
        print(f"[phase] Checkin CSV prepared: {args.checkin_csv}")
        print("[result] Guided runtime finished at import phase (check-in step disabled).")
    elif args.mode == "checkout-version":
        if args.reference_json is not None:
            # Load reference JSON and checkout all specified objects
            reference_data = _load_reference_json(args.reference_json)
            ifd_id = reference_data.get("ifd_id", "UNKNOWN")
            checkout_objs = [s.strip() for s in (args.checkout_objects or "bc,pver").split(",")]
            
            print(f"[checkout] IFD ID: {ifd_id}")
            print(f"[checkout] Objects to checkout: {', '.join(checkout_objs)}")
            
            results = {}
            for obj_type in checkout_objs:
                try:
                    result = _checkout_reference_object(
                        session=session,
                        reference_data=reference_data,
                        obj_type=obj_type.lower(),
                        version_comment=args.version_comment,
                    )
                    if result:
                        results[obj_type] = result
                        resolved = result.get("resolved_fullname", "")
                        print(f"[result] {obj_type.upper()} checked out: {resolved}")
                        if result.get("fallback_used"):
                            updated = _update_reference_json_on_fallback(
                                reference_path=args.reference_json,
                                reference_data=reference_data,
                                obj_type=obj_type.lower(),
                                requested_fullname=str(result.get("requested_fullname", "")),
                                resolved_fullname=str(result.get("resolved_fullname", "")),
                            )
                            if updated:
                                print(
                                    f"[result] {obj_type.upper()} fallback used -> reference JSON updated: "
                                    f"{result.get('requested_fullname')} -> {result.get('resolved_fullname')}"
                                )
                    else:
                        print(f"[skip] {obj_type.upper()} not in reference JSON")
                except Exception as e:
                    print(f"[error] {obj_type.upper()} checkout failed: {e}")
        elif args.rq1_context is not None:
            base_fullname, target_fullname = _load_bc_checkout_reference(args.rq1_context)
            created, resolved_fullname = _checkout_container_variant_from_reference(
                session=session,
                base_fullname=base_fullname,
                desired_fullname=target_fullname,
                version_comment=args.version_comment,
            )
            print(f"[result] RQ1 base     : {base_fullname}")
            print(f"[result] RQ1 desired  : {target_fullname}")
            print(f"[result] RQ1 resolved : {resolved_fullname}")
        else:
            created = _checkout_container_variant(
                session=session,
                base_fullname=args.base_fullname,
                target_variant=args.target_variant,
                version_comment=args.version_comment,
            )
            print(f"[result] Checkout created/used: {created}")
    elif args.mode == "checkout-status":
        reference_data = _load_reference_json(args.reference_json)
        ifd_id = reference_data.get("ifd_id", "UNKNOWN")
        checkout_objs = [s.strip().lower() for s in (args.checkout_objects or "bc,pver").split(",") if s.strip()]

        print(f"[status] IFD ID: {ifd_id}")
        print(f"[status] Objects to check: {', '.join(checkout_objs)}")

        report: dict[str, object] = {
            "ifd_id": ifd_id,
            "reference_json": str(args.reference_json),
            "objects": {},
        }

        checked_total = 0
        present_total = 0
        for obj_type in checkout_objs:
            try:
                status = _reference_object_checkout_status(
                    session=session,
                    reference_data=reference_data,
                    obj_type=obj_type,
                )
                if not status:
                    print(f"[skip] {obj_type.upper()} not in reference JSON")
                    continue
                checked_total += 1
                checked_out = bool(status.get("checked_out"))
                if checked_out:
                    present_total += 1
                yes_no = "YES" if checked_out else "NO"
                requested_name = str(status.get("requested_fullname", ""))
                matched_name = str(status.get("matched_fullname", ""))
                if checked_out and matched_name and _to_spaced_fullname(matched_name) != _to_spaced_fullname(requested_name):
                    print(
                        f"[status] {obj_type.upper()} checked out: {yes_no} -> {requested_name} "
                        f"(matched as {matched_name})"
                    )
                else:
                    print(f"[status] {obj_type.upper()} checked out: {yes_no} -> {requested_name}")
                objects = report.get("objects")
                if isinstance(objects, dict):
                    objects[obj_type] = status
            except Exception as ex:
                print(f"[error] {obj_type.upper()} status check failed: {ex}")
                objects = report.get("objects")
                if isinstance(objects, dict):
                    objects[obj_type] = {"error": str(ex)}

        report["summary"] = {
            "checked_objects": checked_total,
            "present_objects": present_total,
            "all_present": checked_total > 0 and checked_total == present_total,
        }

        if args.status_output is not None:
            args.status_output.parent.mkdir(parents=True, exist_ok=True)
            args.status_output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[status] Report saved: {args.status_output}")
    else:
        response = checkin_fc_recursive(
            session,
            fc_fullname=args.fc_fullname,
            payload_file=args.payload_file,
        )
        _print_response(response)
        if response.status_code >= 400:
            raise SystemExit("FC recursive check-in failed.")
        print("[result] FC recursive check-in request accepted.")


if __name__ == "__main__":
    main()
