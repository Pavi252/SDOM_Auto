from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    import subprocess as _sp
    _sp.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from SDOM_Auto.common.sdom_api import BASE_URL, build_fullname, create_session, parse_fullname
from SDOM_Auto.fcp_operations.fcp_checkout import checkout_fcp_from_data
from SDOM_Auto.sdom_release_fetch_workspace import (
    _extract_variant,
    _to_spaced_fullname,
    extract_zip_safe,
    fetch_files_zip,
)
import SDOM_Auto.sdom_Checkin as sdom_checkin

VERSION_KIND_OPTIONS = {
    "1": "alternative",
    "2": "release",
    "3": "bugfix",
    "4": "revision",
    "5": "project_specific",
}

PROJECT_SPECIFIC_PATTERN = re.compile(r"^_?[A-Za-z]+_[0-9]+$")
SDOM_FULLNAME_PATTERN = re.compile(r"^\s*[^:]+\s*:\s*[^/]+\s*/\s*[^;]+\s*;\s*\d+\s*$")


def _prompt_value(label: str, current: str | None = None) -> str:
    if current is not None and current.strip():
        return current.strip()
    value = input(f"{label}: ").strip()
    while not value:
        value = input(f"{label}: ").strip()
    return value


def _prompt_choice(label: str, choices: list[str], current: str | None = None) -> str:
    if current is not None and current.strip():
        value = current.strip()
        if value in choices:
            return value
    prompt = f"{label} [{'/'.join(choices)}]: "
    value = input(prompt).strip()
    while value not in choices:
        value = input(prompt).strip()
    return value


def _canonicalize_fullname(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return text
    text = text.replace("/", " / ").replace(":", " : ").replace(";", " ; ")
    text = " ".join(text.split())
    text = text.replace(" : ", " : ").replace(" / ", " / ").replace(" ; ", "; ")
    return text


def _prompt_sdom_fullname(label: str, current: str | None = None) -> str:
    value = _canonicalize_fullname(_prompt_value(label, current))
    while not SDOM_FULLNAME_PATTERN.match(value):
        print("Invalid SDOM fullname format. Expected: <CLASS> : <NAME> / <VARIANT>; <REV>")
        value = _canonicalize_fullname(_prompt_value(label))
    return value


def _normalize_project_suffix(value: str | None) -> str | None:
    if value is None:
        return None
    suffix = value.strip().replace(" ", "")
    if not suffix:
        return None
    if not PROJECT_SPECIFIC_PATTERN.match(suffix):
        return None
    if not suffix.startswith("_"):
        suffix = f"_{suffix}"
    return suffix.upper()


def _extract_suffix_from_version(version_core: str) -> tuple[str, str | None]:
    match = re.match(r"^(?P<numeric>.*?\d)(?P<suffix>_.*)?$", version_core)
    if not match:
        return version_core, None
    return match.group("numeric"), match.group("suffix")


def _prompt_project_specific_name() -> str:
    prompt = "Enter project specific name in SDOM format (example: test_1)"
    value = input(f"{prompt}: ").strip()
    while not _normalize_project_suffix(value):
        print("Invalid format. Use letters + underscore + number (example: test_1, TEST_2).")
        value = input(f"{prompt}: ").strip()
    return value


def _prompt_alternative_options(base_version: str) -> dict[str, Any]:
    result = {
        "alt_value": _prompt_value("Enter alternative number"),
        "has_base_suffix": False,
        "base_suffix": None,
        "keep_base_suffix": None,
        "new_suffix": None,
        "combined_suffix": None,
    }

    version_part = base_version.split(";")[0].strip()
    if "/" in version_part:
        version_core = version_part.rsplit("/", 1)[1].strip()
    else:
        version_core = version_part

    _, base_suffix = _extract_suffix_from_version(version_core)
    result["has_base_suffix"] = base_suffix is not None
    result["base_suffix"] = base_suffix

    if result["has_base_suffix"]:
        keep = _prompt_choice(f"Keep existing project extension [{base_suffix}]", ["yes", "no"])
        result["keep_base_suffix"] = keep == "yes"
        if result["keep_base_suffix"]:
            result["combined_suffix"] = base_suffix
    else:
        add = _prompt_choice("Add project-specific extension", ["yes", "no"])
        if add == "yes":
            new_suffix = _prompt_project_specific_name()
            new_suffix_normalized = _normalize_project_suffix(new_suffix)
            result["new_suffix"] = new_suffix_normalized
            result["combined_suffix"] = new_suffix_normalized

    return result


def _increment_version_core(
    version_core: str,
    version_kind: str,
    version_extra: dict[str, Any] | str | None = None,
) -> str:
    match = re.match(r"^(?P<prefix>\D*)(?P<alt>\d+)\.(?P<rel>\d+)\.(?P<bug>\d+)(?P<suffix>.*)$", version_core)
    if not match:
        return version_core

    prefix = match.group("prefix")
    alt = int(match.group("alt"))
    rel = int(match.group("rel"))
    bug = int(match.group("bug"))
    suffix = match.group("suffix")

    if version_kind == "alternative":
        if isinstance(version_extra, dict):
            alt_value = version_extra.get("alt_value")
            if alt_value and str(alt_value).isdigit():
                alt = int(alt_value)
            suffix = ""
        else:
            if version_extra and str(version_extra).isdigit():
                alt = int(version_extra)
        rel = 0
        bug = 0
    elif version_kind == "release":
        rel += 1
        bug = 0
    elif version_kind == "bugfix":
        bug += 1
    elif version_kind == "project_specific":
        suffix = suffix + (_normalize_project_suffix(version_extra) or "")

    return f"{prefix}{alt}.{rel}.{bug}{suffix}"


def _build_checkout_version(
    version: str,
    version_kind: str | None = None,
    version_extra: dict[str, Any] | str | None = None,
    object_type: str = "",
) -> str:
    if not version_kind:
        return version

    version_part, revision = version.split(";", 1) if ";" in version else (version, "0")
    version_part = version_part.strip()
    revision = revision.strip()

    if "/" in version_part:
        head, tail = version_part.rsplit("/", 1)
        transformed_tail = _increment_version_core(tail.strip(), version_kind, version_extra)
        transformed = f"{head.strip()} / {transformed_tail}"
    else:
        transformed = _increment_version_core(version_part, version_kind, version_extra)

    if version_kind == "alternative" and isinstance(version_extra, dict):
        combined_suffix = version_extra.get("combined_suffix") or ""
        transformed = f"{transformed}{combined_suffix}"

    if version_kind in ("alternative", "release", "bugfix", "project_specific"):
        revision = "0"
    elif version_kind == "revision":
        try:
            revision = str(int(revision) + 1)
        except ValueError:
            revision = revision

    return f"{transformed}; {revision}"


def _prompt_version_kind(
    label: str,
    allowed_keys: list[str] | None = None,
    base_version: str | None = None,
) -> tuple[str, str, dict[str, Any] | str | None]:
    keys = allowed_keys or list(VERSION_KIND_OPTIONS.keys())
    print(label)
    for key in keys:
        print(f"  {key}. {VERSION_KIND_OPTIONS[key]}")

    prompt = f"Select version kind ({' / '.join(keys)}): "
    value = input(prompt).strip()
    while True:
        if value in keys:
            choice = value
            break
        try:
            numeric = str(int(value))
        except ValueError:
            numeric = ""
        if numeric in keys:
            choice = numeric
            break
        value = input(prompt).strip()

    kind = VERSION_KIND_OPTIONS[choice]
    extra = None

    if kind == "alternative":
        extra = _prompt_alternative_options(base_version or "")
    elif kind == "project_specific":
        extra = _prompt_project_specific_name()

    return choice, kind, extra


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _persist_fcp_checkout(reference_path: Path, reference: dict[str, Any], fcp_checkout: dict[str, Any]) -> None:
    reference["fcp_checkout"] = fcp_checkout
    _save_json(reference_path, reference)


def _sanitize_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())


def _resolve_services_json(reference_json: Path, explicit_json: Path | None, ifd_id: str) -> Path:
    if explicit_json is not None:
        return explicit_json
    return _ROOT / "RDBI_Gen" / "sources" / ifd_id / "rdbi_services.json"


def _pick_signal_value(signal: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = signal.get(key)
        if value not in (None, ""):
            return value
    return None


def _extract_service_entries(model: Any) -> list[dict[str, Any]]:
    if isinstance(model, list):
        return [entry for entry in model if isinstance(entry, dict)]
    if isinstance(model, dict):
        services = model.get("rdbi_services")
        if services is None:
            services = model.get("rdbi_signals", [])
        if isinstance(services, list):
            return [entry for entry in services if isinstance(entry, dict)]
    raise SystemExit(
        "Unsupported services JSON structure. Expected either a list of service entries "
        "or an object containing 'rdbi_services'/'rdbi_signals'."
    )


def _parse_system_constant_name(signal: dict[str, Any]) -> str:
    direct = str(
        _pick_signal_value(
            signal,
            "system_constant_name",
            "Derived DCM SC",
            "Sysconst",
        )
        or ""
    ).strip()
    if direct:
        return direct
    expr = str(
        _pick_signal_value(
            signal,
            "system_constants",
            "Derived DCM SC Expression",
            "Logical Expression",
        )
        or ""
    ).strip()
    if not expr:
        return ""
    if "==" in expr:
        return expr.split("==", 1)[0].strip()
    return expr


def _version_token_to_vf(token: str) -> str:
    text = str(token or "").strip()
    match = re.fullmatch(r"V?(\d+)_(\d+)_(\d+)", text, flags=re.IGNORECASE)
    if not match:
        return text
    major, minor, patch = match.groups()
    return f"{int(major)}{int(minor)}{int(patch)}"


def _parse_system_constant_value(signal: dict[str, Any], default_vf: str | None) -> str:
    for key in (
        "system_constant_value",
        "system_constants_value",
        "system_constant_vf",
        "VF",
        "vf",
        "value",
    ):
        raw = signal.get(key)
        if raw is None:
            continue
        txt = str(raw).strip()
        if txt:
            return _version_token_to_vf(txt)

    expr = str(
        _pick_signal_value(
            signal,
            "system_constants",
            "Derived DCM SC Expression",
            "Logical Expression",
        )
        or ""
    ).strip()
    if "==" in expr:
        rhs = expr.split("==", 1)[1].strip()
        if rhs:
            return _version_token_to_vf(rhs)
    return default_vf or ""


def _collect_fcp_constants(model: Any, service_filter: str, default_vf: str | None) -> list[dict[str, str]]:
    services = _extract_service_entries(model)
    selected: list[dict[str, str]] = []
    missing_values: list[str] = []
    seen_values: dict[str, str] = {}
    normalized_service = service_filter.strip().lower()
    if normalized_service in ("", "all", "*"):
        normalized_service = ""

    for signal in services:
        service = str(_pick_signal_value(signal, "service", "Service") or "").strip().lower()
        if normalized_service and service != normalized_service:
            continue

        name = _parse_system_constant_name(signal)
        if not name:
            continue

        value = _parse_system_constant_value(signal, default_vf)
        if not value:
            missing_values.append(name)
            continue

        if name in seen_values:
            if seen_values[name] != value:
                raise SystemExit(
                    f"Conflicting VF values for {name}: '{seen_values[name]}' vs '{value}'"
                )
            continue

        seen_values[name] = value
        selected.append(
            {
                "id": f"ID{name}",
                "short_name": name,
                "category": "FIXED",
                "vf": value,
                "compu_method": "OneToOne",
                "calibration_access": "READ-ONLY",
            }
        )

    if missing_values:
        joined = ", ".join(sorted(missing_values))
        raise SystemExit(
            "Missing VF value for one or more FCP system constants. "
            "Provide values in the source JSON or pass --default-vf. "
            f"Constants: {joined}"
        )

    if not selected:
        raise SystemExit(f"No system constants found for service filter '{service_filter}'")

    return selected


def _find_single_fcp_target_xml(root_dir: Path) -> Path:
    candidates: list[Path] = []
    for path in root_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (".xml", ".arxml"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "<SW-SYSTEMCONSTS" in text and "</SW-SYSTEMCONSTS>" in text:
            candidates.append(path)

    if not candidates:
        raise SystemExit(f"No FCP XML containing <SW-SYSTEMCONSTS> found under {root_dir}")

    ranked = sorted(
        candidates,
        key=lambda p: (
            0 if "conf" in p.name.lower() or "diaapl" in p.name.lower() else 1,
            len(p.parts),
            len(p.name),
            str(p).lower(),
        ),
    )
    if len(ranked) > 1 and ranked[0].name.lower() == ranked[1].name.lower():
        listing = "\n".join(str(p) for p in ranked[:10])
        raise SystemExit(
            "Ambiguous FCP XML target; multiple candidates contain <SW-SYSTEMCONSTS>:\n"
            f"{listing}"
        )
    return ranked[0]


def _render_systemconst_block(item: dict[str, str]) -> str:
    return (
        f'          <SW-SYSTEMCONST ID="{item["id"]}">\n'
        f'            <SHORT-NAME>{item["short_name"]}</SHORT-NAME>\n'
        f'            <CATEGORY>{item["category"]}</CATEGORY>\n'
        f'            <SW-VALUES-CODED>\n'
        f'              <VF>{item["vf"]}</VF>\n'
        f'            </SW-VALUES-CODED>\n'
        f'            <SW-DATA-DEF-PROPS>\n'
        f'              <SW-CALIBRATION-ACCESS>{item["calibration_access"]}</SW-CALIBRATION-ACCESS>\n'
        f'              <SW-COMPU-METHOD-REF>{item["compu_method"]}</SW-COMPU-METHOD-REF>\n'
        f'            </SW-DATA-DEF-PROPS>\n'
        f'          </SW-SYSTEMCONST>\n'
    )


def _append_missing_system_constants(xml_text: str, items: list[dict[str, str]]) -> tuple[str, list[str], list[str]]:
    existing = set(
        match.group(1).strip()
        for match in re.finditer(
            r"<SW-SYSTEMCONST\b[^>]*>.*?<SHORT-NAME>\s*([^<]+)\s*</SHORT-NAME>.*?</SW-SYSTEMCONST>",
            xml_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    missing = [item for item in items if item["short_name"] not in existing]
    if not missing:
        return xml_text, [], sorted(existing)

    end_match = re.search(r"</SW-SYSTEMCONSTS>", xml_text, flags=re.IGNORECASE)
    if not end_match:
        raise SystemExit("Target FCP XML does not contain closing </SW-SYSTEMCONSTS> tag")

    insertion = "\n" + "".join(_render_systemconst_block(item) for item in missing)
    updated = xml_text[: end_match.start()] + insertion + xml_text[end_match.start() :]
    return updated, [item["short_name"] for item in missing], sorted(existing)


def _append_missing_systemconst_refs(xml_text: str, items: list[dict[str, str]]) -> tuple[str, list[str]]:
    names_in_order = [item["short_name"] for item in items]
    if not names_in_order:
        return xml_text, []

    added: list[str] = []

    def _replacer(match: re.Match[str]) -> str:
        block_start, block_content, block_end = match.groups()
        block_names = set(
            re.findall(
                r"<SW-SYSTEMCONST-REF>\s*([^<]+?)\s*</SW-SYSTEMCONST-REF>",
                block_content,
                flags=re.IGNORECASE,
            )
        )
        missing = [name for name in names_in_order if name not in block_names]
        if not missing:
            return match.group(0)

        added.extend(missing)
        indent_match = re.search(r"([ \t]+)<SW-SYSTEMCONST-REF>", block_content, flags=re.IGNORECASE)
        indent = indent_match.group(1) if indent_match else " " * 20
        inserted = "".join(f"{indent}<SW-SYSTEMCONST-REF>{name}</SW-SYSTEMCONST-REF>\n" for name in missing)
        content = block_content.rstrip()
        if content:
            content = content + "\n"
        return f"{block_start}\n{content}{inserted}{block_end}"

    updated = re.sub(
        r"(<SW-SYSTEMCONST-REFS\b[^>]*>)(.*?)(</SW-SYSTEMCONST-REFS>)",
        _replacer,
        xml_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return updated, list(dict.fromkeys(added))


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _fetch_checked_out_fcp(
    session: requests.Session,
    checked_out_fcp: str,
    local_dir: Path,
    *,
    editions: int,
    keywords: int,
) -> Path:
    local_dir.mkdir(parents=True, exist_ok=True)
    response = fetch_files_zip(
        session,
        fc_fullname=checked_out_fcp,
        dirrule="",
        editions=editions,
        keywords=keywords,
    )
    safe_name = _sanitize_name(checked_out_fcp) + ".zip"
    zip_path = local_dir / safe_name
    zip_path.write_bytes(response.content)

    extract_dir = local_dir / zip_path.stem
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_zip_safe(zip_path, extract_dir)
    return extract_dir


def _upload_fcp_file(
    session: requests.Session,
    *,
    checked_out_fcp: str,
    file_path: Path,
) -> dict[str, Any]:
    ns = {
        "oslc_scm": "http://open-services.net/ns/scm#",
        "dcterms": "http://purl.org/dc/terms/",
    }
    latest_resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params={"oslc.where": f'oslc_scm:fullName="{checked_out_fcp}"'},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if latest_resp.status_code >= 400 or not (latest_resp.text or "").strip():
        return {
            "status_code": latest_resp.status_code,
            "detail": (latest_resp.text or "").strip()[:1000],
            "ok": False,
            "step": "resolve_fcp_latest",
        }
    latest_root = ET.fromstring(latest_resp.text)
    id_node = latest_root.find(".//dcterms:identifier", ns)
    if id_node is None or not (id_node.text or "").strip():
        return {
            "status_code": latest_resp.status_code,
            "detail": "Missing dcterms:identifier for checked-out FCP",
            "ok": False,
            "step": "resolve_fcp_identifier",
        }
    fcp_id = (id_node.text or "").strip()
    children_resp = session.get(
        f"{BASE_URL}/configuration/children",
        params={"id": fcp_id, "ctx1": fcp_id, "ctx2": "-1"},
        headers={"OSLC-Core-Version": "2.0"},
        timeout=120,
    )
    if children_resp.status_code >= 400 or not (children_resp.text or "").strip():
        return {
            "status_code": children_resp.status_code,
            "detail": (children_resp.text or "").strip()[:1000],
            "ok": False,
            "step": "resolve_fcp_children",
        }
    children_root = ET.fromstring(children_resp.text)
    tdata_children: list[str] = []
    for cfg in children_root.findall(".//oslc_scm:Configuration", ns):
        full = (cfg.findtext("oslc_scm:fullName", default="", namespaces=ns) or "").strip()
        if full.upper().startswith("TDATA:"):
            tdata_children.append(_to_spaced_fullname(full))
    if not tdata_children:
        return {
            "status_code": children_resp.status_code,
            "detail": "No TDATA child found under checked-out FCP",
            "ok": False,
            "step": "resolve_tdata_child",
        }
    if len(tdata_children) > 1:
        return {
            "status_code": children_resp.status_code,
            "detail": f"Ambiguous TDATA children under FCP: {tdata_children}",
            "ok": False,
            "step": "resolve_tdata_child",
        }

    base_tdata = tdata_children[0]
    target_variant = _extract_variant(checked_out_fcp)
    response = sdom_checkin.import_file_checkoutfileex(
        session,
        file_path=file_path,
        file_name=file_path.name,
        root_fullname=base_tdata,
        checkout_mode="variant",
        target_variant=target_variant,
        edition=True,
        checkin=False,
    )
    detail = (response.text or "").strip()
    created = _to_spaced_fullname(sdom_checkin._extract_created_fullname(detail))
    lower_detail = detail.lower()
    if response.status_code >= 400 and ("already exists" in lower_detail or "already checked out" in lower_detail):
        parsed_tdata = parse_fullname(base_tdata)
        if parsed_tdata.get("class") and parsed_tdata.get("name"):
            created = _to_spaced_fullname(
                build_fullname(parsed_tdata["class"], parsed_tdata["name"], target_variant, 0)
            )
    if response.status_code >= 400 and not created:
        return {
            "status_code": response.status_code,
            "base_tdata": base_tdata,
            "detail": detail[:1000],
            "ok": False,
            "step": "checkoutfileex",
        }
    relink_ok = True
    relink_detail = ""
    if created and _to_spaced_fullname(created) != _to_spaced_fullname(base_tdata):
        relink_xml = (
            "<Replacements>"
            f'<Replacement oldVersion="{_to_spaced_fullname(base_tdata)}" '
            f'newVersion="{_to_spaced_fullname(created)}" />'
            "</Replacements>"
        )
        relink_resp = session.post(
            f"{BASE_URL}/configuration/replacechildren",
            params={"root": checked_out_fcp},
            headers={"OSLC-Core-Version": "2.0", "Content-Type": "application/xml"},
            data=relink_xml,
            timeout=120,
        )
        relink_ok = relink_resp.status_code < 400
        relink_detail = (relink_resp.text or "").strip()[:1000]
        if not relink_ok:
            return {
                "status_code": relink_resp.status_code,
                "created_fullname": created,
                "base_tdata": base_tdata,
                "detail": relink_detail,
                "ok": False,
                "step": "replacechildren",
            }
    return {
        "status_code": response.status_code,
        "created_fullname": created,
        "base_tdata": base_tdata,
        "relink_ok": relink_ok,
        "relink_detail": relink_detail,
        "detail": detail[:1000],
        "ok": response.status_code < 400 or bool(created),
    }


def _resolve_fcp_checkout(reference: dict[str, Any], *, force_prompt: bool = True) -> dict[str, Any]:
    fcp_checkout = reference.get("fcp_checkout")
    if not force_prompt and isinstance(fcp_checkout, dict) and fcp_checkout.get("version_input") and fcp_checkout.get("derived_version"):
        return fcp_checkout

    base_fcp = _prompt_sdom_fullname("Enter base FCP fullname (exact SDOM fullName)", "")
    version_choice, version_kind, version_extra = _prompt_version_kind(
        "Enter FCP versioning kind",
        base_version=base_fcp,
    )
    base_fcp = _canonicalize_fullname(base_fcp)
    derived = _canonicalize_fullname(_build_checkout_version(base_fcp, version_kind, version_extra, "fcp"))
    return {
        "version_input": base_fcp,
        "version_kind_code": version_choice,
        "version_kind": version_kind,
        "version_extra": version_extra,
        "derived_version": derived,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FCP-only workflow: checkout, fetch, append DCM system constants, upload back."
    )
    parser.add_argument("--reference-json", required=True, type=Path)
    parser.add_argument("--services-json", type=Path, default=None)
    parser.add_argument("--service-filter", default="0x22")
    parser.add_argument("--default-vf", default=None)
    parser.add_argument("--checkout-root", type=Path, default=_ROOT / "RDBI_Gen" / "checkout_workspace")
    parser.add_argument("--comment", default="Auto checkout by script")
    parser.add_argument("--skip-checkout", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--editions", type=int, default=1)
    parser.add_argument("--keywords", type=int, default=1)
    parser.add_argument("--checkout-attempts", type=int, default=3)
    parser.add_argument(
        "--checked-out-fcp",
        default="",
        help="Use this already checked-out FCP fullName directly (skips checkout creation logic).",
    )
    parser.add_argument("--user", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--use-reference-fcp", action="store_true", help="Reuse stored fcp_checkout from the reference JSON instead of prompting again")
    args = parser.parse_args()

    if not args.reference_json.exists():
        raise SystemExit(f"Reference JSON not found: {args.reference_json}")

    reference = _load_json(args.reference_json)
    ifd_id = str(reference.get("ifd_id") or "").strip() or "UNKNOWN"
    fcp_checkout = _resolve_fcp_checkout(reference, force_prompt=not args.use_reference_fcp)

    services_json = _resolve_services_json(args.reference_json, args.services_json, ifd_id)
    if not services_json.exists():
        raise SystemExit(f"Services JSON not found: {services_json}")
    services_model = _load_json(services_json)
    constants = _collect_fcp_constants(services_model, args.service_filter, args.default_vf)

    run_root = args.checkout_root / ifd_id
    base_root = run_root / "base_FCP"
    generated_root = run_root / "generated_FCP"
    report_path = run_root / "fcp_only_result.json"

    requested_fcp = _to_spaced_fullname(
        str(fcp_checkout.get("derived_version") or fcp_checkout.get("version_input") or "").strip()
    )
    if not requested_fcp:
        raise SystemExit(f"Incomplete fcp_checkout in reference JSON: {args.reference_json}")

    existing_fcp_checkout = reference.get("fcp_checkout")
    if args.use_reference_fcp and (
        isinstance(existing_fcp_checkout, dict)
        and existing_fcp_checkout.get("version_input")
        and existing_fcp_checkout.get("derived_version")
    ):
        _persist_fcp_checkout(args.reference_json, reference, existing_fcp_checkout)

    if args.dry_run:
        payload = {
            "ifd_id": ifd_id,
            "requested_fcp": requested_fcp,
            "services_json": str(services_json),
            "base_root": str(base_root),
            "generated_root": str(generated_root),
            "service_filter": args.service_filter,
            "default_vf": args.default_vf,
            "constant_count": len(constants),
            "constants_preview": constants[:10],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    session = create_session(args.user, args.password)

    explicit_checked_out = _to_spaced_fullname(str(args.checked_out_fcp or "").strip())
    checked_out_fcp = explicit_checked_out or requested_fcp
    checkout_result: dict[str, Any] = {
        "status": "skipped",
        "resolved_fullname": checked_out_fcp,
    }
    if explicit_checked_out:
        checkout_result["reason"] = "used_explicit_checked_out_fcp"
        checkout_result["created_fullname"] = explicit_checked_out
    elif not args.skip_checkout:
        checkout_result = checkout_fcp_from_data(
            session,
            fcp_checkout,
            args.comment,
            max_attempts=max(1, int(args.checkout_attempts)),
        )
        if checkout_result.get("status") != "success":
            raise SystemExit(f"FCP checkout failed: {checkout_result.get('error')}")
        checked_out_fcp = _to_spaced_fullname(
            str(
                checkout_result.get("created_fullname")
                or checkout_result.get("resolved_fullname")
                or requested_fcp
            )
        )

    fetched_base_dir = _fetch_checked_out_fcp(
        session,
        checked_out_fcp,
        base_root,
        editions=args.editions,
        keywords=args.keywords,
    )
    base_target_xml = _find_single_fcp_target_xml(fetched_base_dir)
    fetched_generated_dir = generated_root / fetched_base_dir.name
    generated_target_xml = fetched_generated_dir / base_target_xml.name
    _copy_file(base_target_xml, generated_target_xml)

    original_xml = generated_target_xml.read_text(encoding="utf-8", errors="ignore")
    updated_xml, added_names, existing_names = _append_missing_system_constants(original_xml, constants)
    updated_xml, added_ref_names = _append_missing_systemconst_refs(updated_xml, constants)
    generated_target_xml.write_text(updated_xml, encoding="utf-8", newline="")
    _copy_file(generated_target_xml, base_target_xml)

    upload_result: dict[str, Any] = {
        "status": "skipped",
        "ok": True,
    }
    if not args.skip_upload:
        upload_result = _upload_fcp_file(
            session,
            checked_out_fcp=checked_out_fcp,
            file_path=base_target_xml,
        )
        if not upload_result.get("ok"):
            raise SystemExit(
                f"FCP upload failed: HTTP {upload_result.get('status_code')} {upload_result.get('detail')}"
            )

    payload = {
        "ifd_id": ifd_id,
        "services_json": str(services_json),
        "requested_fcp": requested_fcp,
        "checked_out_fcp": checked_out_fcp,
        "target_variant": _extract_variant(checked_out_fcp),
        "checkout": checkout_result,
        "base_fcp_dir": str(fetched_base_dir),
        "generated_fcp_dir": str(fetched_generated_dir),
        "base_target_file": str(base_target_xml),
        "generated_target_file": str(generated_target_xml),
        "adapted_file": str(base_target_xml),
        "service_filter": args.service_filter,
        "existing_system_constants": existing_names[:100],
        "added_system_constants": added_names,
        "added_systemconst_refs": added_ref_names,
        "upload": upload_result,
    }
    _save_json(report_path, payload)
    print(f"[result] FCP-only workflow complete: {report_path}")
    print(f"[result] Checked-out FCP: {checked_out_fcp}")
    print(f"[result] Adapted file    : {base_target_xml}")
    print(f"[result] Added constants: {len(added_names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
