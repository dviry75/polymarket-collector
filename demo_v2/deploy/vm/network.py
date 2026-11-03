"""Root-owned, dedicated VM network initializer; never flushes other tables."""
import ipaddress
import json
from pathlib import Path
import pwd
import subprocess
import sys

if __package__:
    from .firewall import BRIDGE, TAP, GATEWAY, TABLE, render
else:
    from firewall import BRIDGE, TAP, GATEWAY, TABLE, render

CONFIG = Path('/etc/polymarket-demo-vm/network.json')
RUNTIME = Path('/run/polymarket-demo-vm-network')


def run(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True).stdout


def main():
    config=json.loads(CONFIG.read_text())
    uplink=config['uplink']
    routes=json.loads(run('ip','-j','route','show','default'))
    if len(routes)!=1 or routes[0]['dev']!=uplink:
        raise SystemExit('Default uplink changed; network review required before VM start')
    uid=pwd.getpwnam('polymarket-demo-vm').pw_uid
    links={v['ifname']:v for v in json.loads(run('ip','-j','-d','link','show'))}
    if BRIDGE in links:
        if links[BRIDGE].get('linkinfo',{}).get('info_kind')!='bridge':
            raise SystemExit('Unexpected existing interface')
        attached=[v['ifname'] for v in json.loads(run('ip','-j','link','show','master',BRIDGE))]
        if any(name!=TAP for name in attached):
            raise SystemExit('Physical/foreign interface attached to VM bridge; refusing start')
    if TAP in links:
        info=links[TAP].get('linkinfo',{})
        data=info.get('info_data',{})
        owner=data.get('user')
        owner_uid=int(owner) if owner is not None and str(owner).isdigit() else pwd.getpwnam(str(owner)).pw_uid if owner is not None else -1
        if info.get('info_kind')!='tun' or data.get('type')!='tap' or owner_uid!=uid:
            raise SystemExit('Unexpected TAP owner')
        master=links[TAP].get('master')
        if master and master not in (BRIDGE,links.get(BRIDGE,{}).get('ifindex')):
            raise SystemExit('TAP is attached to a foreign network')
    script=render(uplink,config['home_public_ipv4'])
    existing=subprocess.run(['nft','list','table','inet',TABLE],capture_output=True).returncode==0
    if existing:script=f'delete table inet {TABLE}\n'+script
    temp=RUNTIME/'rules.nft'
    temp.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp.write_text(script);temp.chmod(0o600)
    run('nft','-c','-f',str(temp));run('nft','-f',str(temp))
    # Firewall is active before the bridge/TAP or IP forwarding is enabled.
    if BRIDGE not in links:run('ip','link','add',BRIDGE,'type','bridge')
    run('sysctl','-w',f'net.ipv6.conf.{BRIDGE}.disable_ipv6=1')
    run('ip','addr','replace',GATEWAY+'/30','dev',BRIDGE)
    run('ip','link','set',BRIDGE,'up')
    if TAP not in links:run('ip','tuntap','add','dev',TAP,'mode','tap','user',str(uid))
    run('ip','link','set',TAP,'master',BRIDGE)
    run('ip','link','set',TAP,'up')
    run('sysctl','-w','net.ipv4.ip_forward=1')
    print('Dedicated VM firewall and network applied; no incoming forwarding enabled')


if __name__=='__main__':main()
