"""Validate release APK structure and the build receipt for a URL."""
import hashlib
import re
import sys
import zipfile
from pathlib import Path

URL = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$")


def validate(apk_path, api_url):
    apk = Path(apk_path)
    if not URL.fullmatch(api_url):
        raise ValueError("expected an HTTPS trycloudflare.com URL")
    if not apk.is_file() or apk.stat().st_size < 5_000_000:
        raise ValueError("APK is missing or unexpectedly small")
    with zipfile.ZipFile(apk) as archive:
        if archive.testzip() or not {"classes.dex", "AndroidManifest.xml"} <= set(archive.namelist()):
            raise ValueError("APK ZIP structure or CRC validation failed")
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    receipt = apk.parent / "smartcart-release-receipt.txt"
    if not receipt.is_file() or receipt.read_text() != f"api_url={api_url}\napk_sha256={digest}\n":
        raise ValueError("APK receipt does not match this build URL and artifact")
    return digest


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: validate_release_apk.py APK_PATH HTTPS_QUICK_TUNNEL_URL")
    try:
        digest = validate(sys.argv[1], sys.argv[2])
        print(f"APK validation passed (SHA-256 {digest[:16]}…).")
    except Exception as error:
        raise SystemExit(f"APK validation failed: {error}") from None
