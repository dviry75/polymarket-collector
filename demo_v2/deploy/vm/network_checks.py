"""Exercise actual network initialization/idempotency in an empty private namespace."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

if __package__:
    from . import network
else:
    import network


def run(*args):return subprocess.run(args,check=True,capture_output=True,text=True).stdout


def main():
    links=json.loads(run('ip','-j','link','show'))
    if {v['ifname'] for v in links}!={'lo'}:raise SystemExit('Refusing nonempty network namespace')
    run('ip','link','set','lo','up')
    run('ip','link','add','uplink0','type','veth','peer','name','synthetic-peer')
    run('ip','addr','add','192.168.33.24/24','dev','uplink0')
    run('ip','link','set','uplink0','up')
    run('ip','route','add','default','via','192.168.33.1')
    with tempfile.TemporaryDirectory(prefix='demo-network-test-') as tmp:
        network.CONFIG=Path(tmp)/'network.json';network.RUNTIME=Path(tmp)/'run'
        network.CONFIG.write_text(json.dumps({'uplink':'uplink0','home_public_ipv4':'9.9.9.10'}))
        with patch.object(network.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=os.getuid())):
            network.main()
            members=json.loads(run('ip','-j','link','show','master',network.BRIDGE))
            assert [v['ifname'] for v in members]==[network.TAP]
            assert Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()=='1'
            assert Path('/proc/sys/net/ipv6/conf/'+network.BRIDGE+'/disable_ipv6').read_text().strip()=='1'
            network.main()  # no duplicate address/TAP/table or unsafe replacement
            run('ip','link','set','synthetic-peer','master',network.BRIDGE)
            try:network.main()
            except SystemExit as error:assert 'foreign interface' in str(error)
            else:raise AssertionError('Foreign bridge member was accepted')
            print(json.dumps({'status':'PASS','tests':['actual initializer creates guarded private bridge/TAP','initializer is idempotent','foreign bridge attachment fails closed'],'count':3,'test_namespace_only':True}))


if __name__=='__main__':main()
