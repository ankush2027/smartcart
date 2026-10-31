import hashlib
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from validate_release_apk import validate


def make_apk(tmp_path):
    apk = tmp_path / 'app-release.apk'
    # ZIP_STORED keeps this test artifact above the validator's minimum size.
    with zipfile.ZipFile(apk, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('classes.dex', b'x' * 5_200_000)
        archive.writestr('AndroidManifest.xml', b'fake manifest test fixture')
    return apk


def test_release_apk_requires_current_tunnel_url_and_matching_digest(tmp_path):
    apk = make_apk(tmp_path)
    url = 'https://demo-123.trycloudflare.com'
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    (tmp_path / 'smartcart-release-receipt.txt').write_text(f'api_url={url}\napk_sha256={digest}\n')
    assert validate(apk, url) == digest
    with pytest.raises(ValueError, match='receipt'):
        validate(apk, 'https://stale-456.trycloudflare.com')


@pytest.mark.parametrize('url', ['http://demo.trycloudflare.com', 'https://demo.example.com', 'https://example.trycloudflare.com/path'])
def test_release_apk_rejects_non_https_or_invalid_domain(tmp_path, url):
    with pytest.raises(ValueError):
        validate(tmp_path / 'missing.apk', url)
