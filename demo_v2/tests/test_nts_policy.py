"""Validate the narrow repair without touching the real host firewall."""
from pathlib import Path

import pytest

from demo_v2.deploy.vm import firewall, repair_nts


def test_repair_accepts_only_public_nts_port_and_comments():
    new = Path(firewall.__file__).read_text()
    old = new.replace(repair_nts.NEW_PORTS, repair_nts.OLD_PORTS)
    repair_nts.validate_change(old, new)
    with pytest.raises(ValueError, match='unrelated'):
        repair_nts.validate_change(old, new.replace('counter name blocked_private drop', 'accept'))
    with pytest.raises(ValueError, match='unrelated'):
        repair_nts.validate_change(old, new.replace('policy drop;', 'policy accept;'))
    with pytest.raises(ValueError, match='Unexpected'):
        repair_nts.validate_change(new, new)


def test_atomic_public_module_replacement_is_readable_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / 'firewall.py'
    target.write_bytes(b'old policy')
    repair_nts.replace_public_file(target, b'new policy')
    assert target.read_bytes() == b'new policy'
    assert target.stat().st_mode & 0o777 == 0o644
    assert list(tmp_path.iterdir()) == [target]


def test_failed_policy_application_restores_renderer_and_reapplies_old_guards(tmp_path):
    target = tmp_path / 'firewall.py'
    target.write_bytes(b'old policy')
    calls = []
    def initialize(*args):
        calls.append(target.read_bytes())
        if len(calls) == 1:
            raise RuntimeError('synthetic nft validation failure')
    with pytest.raises(RuntimeError, match='synthetic'):
        repair_nts.apply_policy(target, tmp_path / 'network.py', b'old policy', b'new policy', initialize)
    assert calls == [b'new policy', b'old policy']
    assert target.read_bytes() == b'old policy'
