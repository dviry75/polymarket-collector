"""Public-ingress artifact checks use disposable files; no host/router mutation."""
import json
from pathlib import Path

import pytest

from demo_v2.deploy.vm import public_https as ingress


def bundle(tmp_path):
    for name in ingress.FILES:
        (tmp_path/name).write_bytes(('fake-reviewed-'+name).encode())
    manifest={'purpose':'DEMO_HTTPS_443_ONLY','domain':ingress.HOST,'lan_ipv4':ingress.LAN,
        'caddy_version':ingress.VERSION,
        'files':{name:ingress.sha((tmp_path/name).read_bytes()) for name in ingress.FILES}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    return ingress.sha((tmp_path/'manifest.json').read_bytes())


def test_bundle_validation_before_any_privileged_action(tmp_path,monkeypatch):
    checksum=bundle(tmp_path)
    verified=[]
    monkeypatch.setattr(ingress,'verify_package',lambda path:verified.append(path))
    ingress.verify_bundle(tmp_path,checksum)
    assert verified==[tmp_path]
    (tmp_path/'Caddyfile').write_text('tampered route')
    with pytest.raises(ValueError,match='artifact changed'):ingress.verify_bundle(tmp_path,checksum)
    assert verified==[tmp_path]


def test_manifest_and_symlink_replacement_fail_closed(tmp_path,monkeypatch):
    checksum=bundle(tmp_path)
    monkeypatch.setattr(ingress,'verify_package',lambda path:pytest.fail('must reject first'))
    with pytest.raises(ValueError,match='manifest changed'):ingress.verify_bundle(tmp_path,'0'*64)
    content=(tmp_path/'Caddyfile').read_bytes()
    (tmp_path/'Caddyfile').unlink()
    (tmp_path/'elsewhere').write_bytes(content)
    (tmp_path/'Caddyfile').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(ValueError,match='artifact changed'):ingress.verify_bundle(tmp_path,checksum)


def test_unprivileged_apply_cannot_touch_system(tmp_path,monkeypatch):
    monkeypatch.setattr(ingress.os,'geteuid',lambda:1000)
    monkeypatch.setattr(ingress,'verify_bundle',lambda *args:pytest.fail('must reject first'))
    with pytest.raises(PermissionError):ingress.apply(tmp_path,'unused')


def test_unsigned_index_or_wrong_package_is_not_accepted(tmp_path,monkeypatch):
    index=f'Package: caddy\nVersion: {ingress.VERSION}\nSHA256: wrong\n'
    (tmp_path/'Packages').write_text(index)
    (tmp_path/'InRelease').write_text('fake signed metadata without the expected index digest')
    monkeypatch.setattr(ingress,'run',lambda *args:'signature check simulated')
    with pytest.raises(ValueError,match='index mismatch'):ingress.verify_package(tmp_path)
    (tmp_path/'InRelease').write_text(f' {ingress.sha(index.encode())} {len(index.encode())} main/binary-amd64/Packages\n')
    (tmp_path/'caddy.deb').write_bytes(b'fake package')
    with pytest.raises(ValueError,match='package digest'):ingress.verify_package(tmp_path)


def test_only_reviewed_package_version_selected():
    for index in ('', 'Package: not-caddy\nVersion: '+ingress.VERSION,
                  'Package: caddy\nVersion: 0.0.0'):
        with pytest.raises(ValueError):ingress.package_record(index)


def test_ingress_service_has_no_access_to_application_state_or_management_socket():
    here=Path(ingress.__file__).parent
    unit=(here/ingress.SERVICE).read_text()
    assert 'DynamicUser=yes' in unit and 'ProtectHome=yes' in unit
    assert 'NoNewPrivileges=yes' in unit and 'CAP_NET_ADMIN' not in unit
    assert 'StateDirectory=polymarket-demo-https\n' in unit
    assert '/var/lib/polymarket-demo-v2' not in unit and '/etc/polymarket-demo-v2' not in unit
    assert 'AF_INET6' not in unit
