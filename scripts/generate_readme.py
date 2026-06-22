#!/usr/bin/env python3
"""Regenerate README.md from README.template.md using the latest GitHub Release per platform.

Platform is detected by asset file *extension* (.pkg/.dmg -> macOS, .exe/.msi -> Windows),
never by tag naming, so it stays correct no matter how a release happens to be tagged.
The "latest" per platform is the highest semantic version (tie-broken by publish date),
so it does not rely on GitHub's single global "Latest release" flag.

Runs locally (unauthenticated GitHub API) or in CI (authenticated via GITHUB_TOKEN).
Usage: python3 scripts/generate_readme.py
"""

import base64
import html
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- Configuration ----------------------------------------------------------
REPO = os.environ.get("GITHUB_REPOSITORY", "Asearis/public-releases")
ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "README.template.md"
OUTPUT_PATH = ROOT / "README.md"
INCLUDE_PRERELEASES = False

# Asset extension -> platform bucket. Extra extensions are future-proofing.
PLATFORM_BY_EXT = {
    ".pkg": "macos",
    ".dmg": "macos",
    ".exe": "windows",
    ".msi": "windows",
}

# Require a real version shape — major.minor[.patch[.build]], optionally `v`-prefixed and
# not glued to surrounding digits. This deliberately ignores date/build-id tags such as
# "2026-06-19", "build-20260101" or "nightly-20260619" so a mis-tagged release can never
# masquerade as a higher version than a real one and hijack the public download link.
VERSION_RE = re.compile(r"(?:^|[^0-9.])v?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(?![0-9.])")
MAX_MAJOR = 999  # a 4+ digit "major" is a year/CalVer date, not a SemVer release

# shields.io has no Windows logo (Microsoft's marks were removed from simple-icons),
# so the Windows badges use this inline glyph as a URL-encoded data-URI `?logo=` value.
# Kept as readable SVG (not an opaque blob) and built at render time.
_WINDOWS_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#03C4FB">'
    '<rect x="1" y="1" width="9.5" height="9.5" rx="1"/>'
    '<rect x="13.5" y="1" width="9.5" height="9.5" rx="1"/>'
    '<rect x="1" y="13.5" width="9.5" height="9.5" rx="1"/>'
    '<rect x="13.5" y="13.5" width="9.5" height="9.5" rx="1"/></svg>'
)


def windows_logo_param():
    """URL-encoded data-URI for the Windows glyph, for a shields `?logo=` value."""
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(
        _WINDOWS_LOGO_SVG.encode()
    ).decode()
    return urllib.parse.quote(data_uri, safe="")


def safe_download_url(url):
    """Allow only canonical GitHub release-asset URLs; otherwise a dead `#` link.

    The asset URL is the single value a viewer clicks to download an installer, so it must
    never be an attacker-controlled destination. GitHub always serves assets from this prefix.
    """
    prefix = f"https://github.com/{REPO}/releases/download/"
    return url if url.startswith(prefix) else "#"


# --- GitHub API -------------------------------------------------------------
def _build_ssl_context():
    """Default TLS context, with a system-CA fallback for trust stores that ship empty.

    CI runners trust certs out of the box. Some local interpreters (notably the python.org
    macOS build) have an unconfigured store and fail with CERTIFICATE_VERIFY_FAILED; in that
    case we load a known system CA bundle. Verification stays ON — we never disable it.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        for bundle in (
            "/etc/ssl/cert.pem",
            "/usr/local/etc/openssl@3/cert.pem",
            "/opt/homebrew/etc/ca-certificates/cert.pem",
        ):
            if os.path.exists(bundle):
                ctx.load_verify_locations(bundle)
                break
    return ctx


_SSL_CONTEXT = _build_ssl_context()
_TOKEN_RESOLVED = False
_TOKEN = None


def _github_token():
    """GITHUB_TOKEN if set (CI); otherwise borrow the gh CLI's token for local runs.

    Authenticating lifts the API rate limit from 60/hr (unauthenticated) to 5000/hr, so
    local `edit template -> regenerate` loops don't hit `403 rate limit exceeded`. The gh
    lookup is optional and fully guarded — its absence simply falls back to unauthenticated.
    """
    global _TOKEN_RESOLVED, _TOKEN
    if _TOKEN_RESOLVED:
        return _TOKEN
    _TOKEN_RESOLVED = True
    _TOKEN = os.environ.get("GITHUB_TOKEN")
    if not _TOKEN:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                _TOKEN = result.stdout.strip() or None
        except (OSError, ValueError):
            _TOKEN = None
    return _TOKEN


def _api(url):
    """Return parsed JSON for a GitHub API URL, with optional token auth."""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "asearis-release-readme-bot",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = _github_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
        return json.load(resp)


def fetch_releases():
    """Fetch all releases (paginated), newest first as GitHub returns them."""
    releases, page = [], 1
    while page <= 20:  # hard cap: 2000 releases is far beyond any real need
        batch = _api(
            f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}"
        )
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    else:
        # Loop exhausted the cap without a short final page: results may be truncated.
        print(
            "::warning::Release pagination hit the 20-page cap; "
            "latest-per-platform may be incomplete.",
            file=sys.stderr,
        )
    return releases


# --- Pure helpers -----------------------------------------------------------
def parse_version(tag):
    """Extract a comparable (major, minor, patch, build) tuple from a tag, or None."""
    match = VERSION_RE.search(tag or "")
    if not match:
        return None
    parts = tuple(int(group) if group else 0 for group in match.groups())
    if parts[0] > MAX_MAJOR:
        return None
    return parts


def platform_for_asset(name):
    """Map an asset filename to a platform via its extension, or None."""
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return PLATFORM_BY_EXT.get(ext)


def human_size(num_bytes):
    """Human-readable size using binary units (matches what GitHub shows)."""
    if not num_bytes:
        return "—"
    mb = num_bytes / (1024 * 1024)
    if mb < 1:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def format_date(iso):
    """ISO-8601 timestamp -> 'Jun 19, 2026'; tolerant of fractional seconds / offsets."""
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return "—"


# --- Selection --------------------------------------------------------------
def pick_latest_per_platform(releases):
    """Return {platform: release-asset dict} for the highest version per platform."""
    best = {}
    for release in releases:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not INCLUDE_PRERELEASES:
            continue
        version = parse_version(release.get("tag_name", ""))
        if version is None:
            continue
        sort_key = (version, release.get("published_at") or "")
        for asset in release.get("assets", []):
            platform = platform_for_asset(asset.get("name", ""))
            if not platform:
                continue
            current = best.get(platform)
            if current is None or sort_key > current["_key"]:
                major, minor, patch = version[0], version[1], version[2]
                best[platform] = {
                    "_key": sort_key,
                    "version": f"v{major}.{minor}.{patch}",
                    "tag": release.get("tag_name", ""),
                    # asset name flows into HTML/markdown — escape it defensively even
                    # though GitHub already normalizes asset filenames on upload.
                    "asset": html.escape(asset.get("name", ""), quote=True),
                    "url": safe_download_url(asset.get("browser_download_url", "")),
                    "size": human_size(asset.get("size", 0)),
                    "date": format_date(release.get("published_at", "")),
                }
    return best


# --- Rendering --------------------------------------------------------------
def build_fields(best):
    """Flatten the selection into the {{TOKEN}} substitution map."""
    fields = {}
    for platform in ("macos", "windows"):
        entry = best.get(platform)
        fields[f"{platform.upper()}_URL"] = entry["url"] if entry else "#"
    fields["UPDATED"] = datetime.now(timezone.utc).strftime("%b %d, %Y")
    fields["WIN_LOGO"] = windows_logo_param()
    return fields


def render(best):
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    output = template
    for token, value in build_fields(best).items():
        output = output.replace("{{" + token + "}}", value)
    leftover = re.findall(r"\{\{[A-Z0-9_]+\}\}", output)
    if leftover:
        raise SystemExit(f"Unresolved template tokens: {sorted(set(leftover))}")
    return output


# --- Entry point ------------------------------------------------------------
def main():
    try:
        releases = fetch_releases()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # URLError (incl. HTTPError), OSError (socket timeout) and ValueError (JSON decode)
        # all leave README.md untouched — fail safe, never overwrite with partial data.
        print(f"::error::Failed to fetch releases: {exc}", file=sys.stderr)
        return 1

    best = pick_latest_per_platform(releases)
    if not best:
        print("::warning::No macOS/Windows release assets found; README unchanged.")
        return 0

    OUTPUT_PATH.write_text(render(best), encoding="utf-8")
    for platform, entry in sorted(best.items()):
        print(
            f"{platform:8} -> {entry['version']:10} {entry['asset']} "
            f"({entry['size']}, {entry['date']})"
        )
    print(f"Wrote {OUTPUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
