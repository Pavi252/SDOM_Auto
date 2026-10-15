from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests


BASE_URL = "https://easee-ews.webapp.inside.bosch.cloud/easee-ews"
DEFAULT_FC_FULLNAME = ""
DEFAULT_LOCAL_DIR = Path(__file__).resolve().parent / "checkout_workspace"

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


def _headers(content_type: str | None = None) -> dict[str, str]:
    headers = {"OSLC-Core-Version": "2.0", "x-requester": _requester_value()}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _print_response(label: str, response: requests.Response) -> None:
    print(f"[{label}] Status : {response.status_code}")
    print(f"[{label}] Headers: {dict(response.headers)}")
    content_type = (response.headers.get("content-type") or "").lower()
    if "application/octet-stream" in content_type:
        print(f"[{label}] Body: <binary {len(response.content)} bytes>")
        return
    text = response.text or ""
    if len(text) > 1500:
        text = text[:1500] + "\n...<truncated>..."
    print(f"[{label}] Body:\n{text}")


def _fc_name_candidates(fc_fullname: str) -> list[str]:
    raw = fc_fullname.strip()
    spaced = _to_spaced_fullname(raw)
    compact = re.sub(r"\s*:\s*", ":", raw)
    compact = re.sub(r"\s*/\s*", "/", compact)
    compact = re.sub(r"\s*;\s*", ";", compact)
    out = [raw]
    for candidate in (spaced, compact):
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _extract_variant(fc_fullname: str) -> str:
    m = re.search(r"/\s*([^;]+)\s*;", fc_fullname)
    return m.group(1).strip() if m else ""


def _extract_revision(fc_fullname: str) -> int:
    m = re.search(r";\s*(\d+)\s*$", fc_fullname)
    if not m:
        raise ValueError(f"Cannot parse revision from fullName: {fc_fullname}")
    return int(m.group(1))


def _compute_next_release_variant(variant: str) -> str:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", variant)
    if not m:
        raise ValueError(f"Unsupported variant format for release increment: {variant}")
    major = int(m.group(1))
    release = int(m.group(2)) + 1
    suffix = m.group(4)
    return f"{major}.{release}.0{suffix}"


def _build_fullname(fc_fullname: str, variant: str, revision: int = 0) -> str:
    return re.sub(
        r"/\s*([^;]+)\s*;\s*(\d+)\s*$",
        f"/ {variant}; {revision}",
        fc_fullname.strip(),
        count=1,
    )


def _to_spaced_fullname(fullname: str) -> str:
    m = re.match(
        r"^\s*([^:]+)\s*:\s*([^/]+)\s*/\s*([^;]+)\s*;\s*(\d+)\s*$",
        fullname.strip(),
    )
    if not m:
        return fullname.strip()
    return f"{m.group(1).strip()} : {m.group(2).strip()} / {m.group(3).strip()}; {m.group(4).strip()}"


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_release_checkout_xml(target_variant: str, version_comment: str) -> str:
    comment = _xml_escape(version_comment or "")
    return (
        f"<Checkout VERSIONCOMMENT=\"{comment}\">"
        f"<NewVariant type=\"Release\" variant=\"{target_variant}\" />"
        f"<VersionComment>{comment}</VersionComment>"
        "<Release>"
        f"<VersionComment>{comment}</VersionComment>"
        "</Release>"
        "</Checkout>"
    )


def _extract_checkout_fullname(body_text: str) -> str:
    txt = html.unescape((body_text or "").strip())
    m = re.search(r"<string>\s*<[^>]+>\s*<([^>]+)>\s*</string>", txt)
    return m.group(1).strip() if m else ""


def _is_already_exists_error(response: requests.Response) -> bool:
    return response.status_code == 400 and "already exists" in (response.text or "").lower()


def _is_already_checked_out_error(response: requests.Response) -> bool:
    return response.status_code == 400 and "already checked out" in (response.text or "").lower()


def _configuration_latest_exists(session: requests.Session, fullname: str) -> bool:
    resp = session.get(
        f"{BASE_URL}/configuration/latest",
        params={"oslc.where": f'oslc_scm:fullName="{_to_spaced_fullname(fullname)}"'},
        headers=_headers(),
        timeout=60,
    )
    if resp.status_code >= 500:
        raise RuntimeError(
            f"SDOM backend service error (status={resp.status_code}). "
            f"The /configuration/latest endpoint is not responding normally. "
            f"This is a backend issue, not a client-side problem. "
            f"Please check SDOM service status and retry once it recovers."
        )
    return resp.status_code == 200 and bool((resp.text or "").strip())


def _load_reference_json(reference_path: Path) -> dict:
    return json.loads(reference_path.read_text(encoding="utf-8"))


def _resolve_reference_fc(reference_data: dict) -> tuple[str, str | None]:
    fc_checkout = reference_data.get("fc_checkout")
    if not isinstance(fc_checkout, dict):
        raise ValueError("No fc_checkout in reference JSON")
    base_fullname = (fc_checkout.get("version_input") or "").strip()
    target_fullname = (fc_checkout.get("derived_version") or "").strip()
    if not base_fullname:
        raise ValueError("Missing fc_checkout.version_input in reference JSON")
    return _to_spaced_fullname(base_fullname), (_to_spaced_fullname(target_fullname) if target_fullname else None)


def _next_free_release_variant(
    session: requests.Session,
    *,
    fc_fullname: str,
    initial_variant: str,
    max_attempts: int = 25,
) -> str:
    candidate_variant = initial_variant.strip()
    for _ in range(max_attempts):
        candidate_fullname = _build_fullname(fc_fullname, candidate_variant, revision=0)
        if not _configuration_latest_exists(session, candidate_fullname):
            return candidate_variant
        candidate_variant = _compute_next_release_variant(candidate_variant)
    raise RuntimeError(f"Unable to find a free release variant for {fc_fullname} starting from {initial_variant}")


def create_release_with_reservation(
    session: requests.Session,
    *,
    fc_fullname: str,
    target_variant: str,
    version_comment: str,
) -> tuple[str, requests.Response]:
    xml_payload = _build_release_checkout_xml(target_variant, version_comment)
    release_fullname = _build_fullname(fc_fullname, target_variant, revision=0)

    exists_resp = requests.Response()
    exists_resp.status_code = 200
    exists_resp._content = f"<string>{release_fullname}</string>".encode("utf-8")
    exists_resp.headers["content-type"] = "application/xml"

    if _configuration_latest_exists(session, release_fullname):
        print(f"[checkoutversionex] Target release already exists: {release_fullname} - skipping creation")
        return release_fullname, exists_resp

    last_resp: requests.Response | None = None

    for candidate in _fc_name_candidates(fc_fullname):
        resp = session.post(
            f"{BASE_URL}/configuration/checkoutversionex",
            params={"root": candidate},
            headers=_headers("application/xml"),
            data=xml_payload,
            timeout=120,
        )
        _print_response(f"checkoutversionex POST root={candidate}", resp)
        last_resp = resp
        if resp.status_code < 400:
            created = _extract_checkout_fullname(resp.text or "")
            if created:
                return created, resp
            return release_fullname, resp
        if _is_already_exists_error(resp):
            return release_fullname, resp
        if _is_already_checked_out_error(resp):
            return fc_fullname, resp
        if resp.status_code >= 500:
            if _configuration_latest_exists(session, release_fullname):
                return release_fullname, resp

    if last_resp is None:
        raise RuntimeError("No checkoutversionex request executed")
    raise RuntimeError("Release creation failed for all fullname candidates")


def fetch_files_zip(
    session: requests.Session,
    *,
    fc_fullname: str,
    dirrule: str,
    editions: int,
    keywords: int,
) -> requests.Response:
    last_resp: requests.Response | None = None
    for candidate in _fc_name_candidates(fc_fullname):
        resp = session.get(
            f"{BASE_URL}/configuration/files",
            params={
                "oslc.where": f'oslc_scm:fullName="{candidate}"',
                "dirrule": dirrule,
                "editions": str(editions),
                "keywords": str(keywords),
            },
            headers=_headers(),
            timeout=180,
        )
        _print_response(f"files GET fullName={candidate}", resp)
        last_resp = resp
        if resp.status_code < 400:
            return resp
    if last_resp is None:
        raise RuntimeError("No /configuration/files request executed")
    raise RuntimeError("Files fetch failed for all fullname candidates")


def _to_windows_long_path(path: Path) -> str:
    p = str(path.resolve())
    if os.name != "nt":
        return p
    if p[:4] == '\\\\?\\':
        return p
    return '\\\\?\\' + p


def _remove_tree_safe(path: Path) -> None:
    """Remove tree robustly on Windows long paths."""
    if not path.exists():
        return
    if os.name != "nt":
        shutil.rmtree(path, ignore_errors=True)
        return

    root = _to_windows_long_path(path)
    for current_root, dirs, files in os.walk(root, topdown=False):
        for name in files:
            try:
                os.remove(str(Path(current_root) / name))
            except FileNotFoundError:
                pass
        for name in dirs:
            try:
                os.rmdir(str(Path(current_root) / name))
            except FileNotFoundError:
                pass
    try:
        os.rmdir(root)
    except FileNotFoundError:
        pass


def extract_zip_safe(zip_path: Path, extract_dir: Path) -> None:
    extract_dir.mkdir(parents=True, exist_ok=True)
    extract_root = extract_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            rel = (info.filename or "").replace("/", "\\").strip()
            if not rel or rel.endswith("\\"):
                continue

            rel = rel.lstrip("\\/")
            rel = rel.replace(":", "_")
            rel = rel.replace("..\\", "").replace("../", "")
            if not rel:
                continue

            target = (extract_root / rel).resolve()
            try:
                target.relative_to(extract_root)
            except ValueError:
                continue

            os.makedirs(_to_windows_long_path(target.parent), exist_ok=True)
            with zf.open(info, "r") as src, open(_to_windows_long_path(target), "wb") as dst:
                dst.write(src.read())


def main() -> None:
    parser = argparse.ArgumentParser(description="Create FC release and fetch extracted workspace (Ctrl+Alt+F-like).")
    parser.add_argument("--user", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--fc-fullname", default=DEFAULT_FC_FULLNAME)
    parser.add_argument("--reference-json", type=Path, default=None, help="Optional SDOM checkout reference JSON")
    parser.add_argument("--target-variant", default="", help="Optional target release variant. Default: auto-next release.")
    parser.add_argument("--version-comment", default="Auto release by script")
    parser.add_argument("--local-dir", type=Path, default=DEFAULT_LOCAL_DIR)
    parser.add_argument("--dirrule", default="", help='Directory rule (auto when empty, e.g. "FC-ARB;1").')
    parser.add_argument("--editions", type=int, default=1)
    parser.add_argument("--keywords", type=int, default=1)
    parser.add_argument("--skip-checkout", action="store_true")
    parser.add_argument("--delete-zip", action="store_true", help="Delete downloaded zip after extraction.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    fc_fullname = args.fc_fullname.strip()
    target_variant = args.target_variant.strip()
    if args.reference_json is not None:
        if not args.reference_json.exists():
            raise SystemExit(f"reference JSON not found: {args.reference_json}")
        reference_data = _load_reference_json(args.reference_json)
        ref_base_fullname, ref_target_fullname = _resolve_reference_fc(reference_data)
        if not fc_fullname:
            fc_fullname = ref_base_fullname
        if not target_variant and ref_target_fullname:
            target_variant = _extract_variant(ref_target_fullname)
    if not fc_fullname:
        raise SystemExit("FC checkout requires --fc-fullname or --reference-json with fc_checkout")

    source_variant = _extract_variant(fc_fullname)
    source_revision = _extract_revision(fc_fullname)
    target_variant = target_variant or _compute_next_release_variant(source_variant)
    release_fullname = _build_fullname(fc_fullname, target_variant, revision=0)

    if args.dry_run:
        print("Mode             : release-create + files-fetch + extract")
        print(f"FC fullName      : {fc_fullname}")
        print(f"Source variant   : {source_variant}")
        print(f"Source revision  : {source_revision}")
        print(f"Target variant   : {target_variant}")
        print(f"Target fullName  : {release_fullname}")
        print(f"Checkout XML     : {_build_release_checkout_xml(target_variant, args.version_comment)}")
        print(f"Local dir        : {args.local_dir}")
        print(f"Delete zip       : {args.delete_zip}")
        return

    args.local_dir.mkdir(parents=True, exist_ok=True)
    user = (args.user or "").strip()
    password = args.password or ""
    if not user or not password:
        user, password = prompt_sdom_credentials()

    session = requests.Session()
    session.auth = (user, password)
    session.headers.update({
        "OSLC-Core-Version": "2.0",
        "x-requester": _requester_value(),
    })

    if args.skip_checkout:
        # Fetch-only mode: use exact target fullname from reference/user input, no variant auto-bump.
        release_fullname = _build_fullname(fc_fullname, target_variant, revision=0)
    else:
        target_variant = _next_free_release_variant(
            session,
            fc_fullname=fc_fullname,
            initial_variant=target_variant,
        )
        release_fullname = _build_fullname(fc_fullname, target_variant, revision=0)
        release_fullname, _ = create_release_with_reservation(
            session,
            fc_fullname=fc_fullname,
            target_variant=target_variant,
            version_comment=args.version_comment,
        )

    dirrule = args.dirrule.strip()
    print(f"[files] Using dirrule: {dirrule if dirrule else '<empty>'}")

    files_resp = fetch_files_zip(
        session,
        fc_fullname=release_fullname,
        dirrule=dirrule,
        editions=args.editions,
        keywords=args.keywords,
    )

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", release_fullname) + ".zip"
    zip_path = args.local_dir / safe_name
    zip_path.write_bytes(files_resp.content)
    print(f"[zip] Saved to: {zip_path}")

    extract_dir = args.local_dir / zip_path.stem
    if extract_dir.exists():
        _remove_tree_safe(extract_dir)
    extract_zip_safe(zip_path, extract_dir)
    print(f"[extract] Extracted to: {extract_dir}")

    if args.delete_zip:
        zip_path.unlink(missing_ok=True)
        print(f"[zip] Deleted: {zip_path}")

    print(f"[result] Ready workspace fullName: {release_fullname}")


if __name__ == "__main__":
    main()


