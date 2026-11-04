"""Real packet tests in disposable namespaces. Never run in the host namespace.
Run: unshare --user --map-root-user --net python3 packet_checks.py
Only synthetic peers are used; this test namespace has no physical interface.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

if __package__:
    from .firewall import BRIDGE, GATEWAY, GUEST, TABLE, render
else:
    from firewall import BRIDGE, GATEWAY, GUEST, TABLE, render

SERVER = '''import socket,threading,time
def tcp(addr,port):
 s=socket.socket(socket.AF_INET6 if ':' in addr else socket.AF_INET);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 s.bind((addr,port));s.listen()
 while True:
  c,_=s.accept();c.sendall(b"OK");c.close()
def udp(addr,port):
 s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind((addr,port))
 while True:
  data,peer=s.recvfrom(100);s.sendto(b"OK",peer)
for addr,port in TCP:threading.Thread(target=tcp,args=(addr,port),daemon=True).start()
for addr,port in UDP:threading.Thread(target=udp,args=(addr,port),daemon=True).start()
open(READY,'w').write('ready')
while True:time.sleep(60)
'''


def run(*args, **kw):
    result = subprocess.run(args, capture_output=True, text=True, **kw)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr[-1200:]} {result.stdout[-400:]}')
    return result


def main():
    # Refuse the real host namespace or any namespace with existing network devices.
    interfaces = json.loads(run('ip', '-j', 'link', 'show').stdout)
    if {x['ifname'] for x in interfaces} != {'lo'}:
        raise SystemExit('Use a NEW disposable network namespace; refusing existing interfaces')
    children = []
    servers = []
    passed = []
    with tempfile.TemporaryDirectory(prefix='demo-fw-test-') as tmp:
        try:
            def peer():
                p = subprocess.Popen(['unshare','--net','sleep','600'])
                children.append(p)
                for _ in range(100):
                    if p.poll() is not None: raise RuntimeError('Namespace child failed')
                    if os.readlink(f'/proc/{p.pid}/ns/net') != os.readlink('/proc/self/ns/net'):break
                    time.sleep(.01)
                return p.pid
            guest, outside = peer(), peer()
            def inside(pid, *args):return run('nsenter','-t',str(pid),'-n',*args)
            for pid in (guest,outside):inside(pid,'ip','link','set','lo','up')
            run('ip','link','set','lo','up')
            run('ip','link','add',BRIDGE,'type','bridge')
            run('ip','addr','add',GATEWAY+'/30','dev',BRIDGE)
            run('ip','link','set',BRIDGE,'up')
            run('ip','link','add','vm-end','type','veth','peer','name','guest-end')
            run('ip','link','set','vm-end','master',BRIDGE)
            run('ip','link','set','vm-end','up')
            run('ip','link','set','guest-end','netns',str(guest))
            inside(guest,'ip','addr','add',GUEST+'/30','dev','guest-end')
            inside(guest,'ip','link','set','guest-end','up')
            inside(guest,'ip','route','add','default','via',GATEWAY)
            run('ip','-6','addr','add','fd00:de02::1/64','dev',BRIDGE,'nodad')
            inside(guest,'ip','-6','addr','add','fd00:de02::2/64','dev','guest-end','nodad')
            inside(guest,'ip','-6','route','add','default','via','fd00:de02::1')
            run('ip','link','add','uplink0','type','veth','peer','name','outside-end')
            run('ip','addr','add','192.168.33.24/24','dev','uplink0')
            run('ip','link','set','uplink0','up')
            run('ip','link','set','outside-end','netns',str(outside))
            inside(outside,'ip','addr','add','192.168.33.1/24','dev','outside-end')
            inside(outside,'ip','link','set','outside-end','up')
            inside(outside,'ip','addr','add','9.9.9.9/32','dev','lo')
            inside(outside,'ip','addr','add','9.9.9.10/32','dev','lo')
            run('ip','-6','addr','add','2001:db8:de02::1/64','dev','uplink0','nodad')
            inside(outside,'ip','-6','addr','add','2001:db8:de02::2/64','dev','outside-end','nodad')
            inside(outside,'ip','-6','addr','add','2001:4860:4860::8888/128','dev','lo','nodad')
            inside(outside,'ip','route','add','172.31.250.0/30','via','192.168.33.24')
            run('ip','route','add','default','via','192.168.33.1')
            run('ip','-6','route','add','default','via','2001:db8:de02::2')
            def server(pid, name, tcp, udp):
                ready=str(Path(tmp)/name)
                code='TCP='+repr(tcp)+'\nUDP='+repr(udp)+'\nREADY='+repr(ready)+'\n'+SERVER
                cmd=([sys.executable,'-c',code] if pid is None else ['nsenter','-t',str(pid),'-n',sys.executable,'-c',code])
                p=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                servers.append(p)
                for _ in range(100):
                    if Path(ready).exists():break
                    if p.poll() is not None:raise RuntimeError(p.stderr.read().decode())
                    time.sleep(.02)
                else:raise RuntimeError('Canary startup failed')
            server(None,'host',[('0.0.0.0',52990),('0.0.0.0',4460),(GATEWAY,53),('fd00:de02::1',52990)],[(GATEWAY,53)])
            server(outside,'outside',[('0.0.0.0',443),('0.0.0.0',80),('0.0.0.0',587),('0.0.0.0',4460),('0.0.0.0',22),('2001:4860:4860::8888',443)],[('9.9.9.9',123)])
            server(guest,'guest',[('0.0.0.0',22)],[])
            def probe(pid, addr, port, allowed, udp=False, source=None):
                code='''import socket,sys
s=socket.socket(socket.AF_INET6 if ':' in ADDR else socket.AF_INET,socket.SOCK_DGRAM if UDP else socket.SOCK_STREAM);s.settimeout(.6)
if SOURCE:s.bind((SOURCE,0))
try:
 s.connect((ADDR,PORT))
 if UDP:s.send(b'test')
 assert s.recv(10)==b'OK'
 ok=True
except (OSError,AssertionError):ok=False
assert ok==EXPECTED,(ADDR,PORT,ok,EXPECTED)
'''
                pre=f'ADDR={addr!r};PORT={port};UDP={udp!r};SOURCE={source!r};EXPECTED={allowed!r}\n'
                if pid is None:run(sys.executable,'-c',pre+code)
                else:inside(pid,sys.executable,'-c',pre+code)
            # Positive canaries establish that blocked targets actually listen.
            probe(None,'192.168.33.1',80,True)
            probe(None,'192.168.33.1',4460,True)
            probe(None,'192.168.33.24',4460,True)
            probe(None,GATEWAY,4460,True)
            probe(None,'192.168.33.24',52990,True)
            probe(None,GATEWAY,52990,True)
            probe(None,'fd00:de02::1',52990,True)
            probe(None,'2001:4860:4860::8888',443,True)
            rules=Path(tmp)/'rules.nft';rules.write_text(render('uplink0','9.9.9.10'))
            run('nft','-c','-f',str(rules));run('nft','-f',str(rules))
            Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
            Path('/proc/sys/net/ipv6/conf/all/forwarding').write_text('1')
            def check(name, *args, **kw):
                probe(*args,**kw);passed.append(name)
            check('public HTTPS egress',guest,'9.9.9.9',443,True)
            check('public authenticated time key exchange',guest,'9.9.9.9',4460,True)
            check('public NTP egress',guest,'9.9.9.9',123,True,udp=True)
            check('private router NTS blocked',guest,'192.168.33.1',4460,False)
            check('host LAN NTS blocked',guest,'192.168.33.24',4460,False)
            check('host bridge NTS blocked',guest,GATEWAY,4460,False)
            check('home public NTS hairpin blocked',guest,'9.9.9.10',4460,False)
            check('public HTTP egress',guest,'9.9.9.9',80,True)
            check('public SMTP STARTTLS egress',guest,'9.9.9.9',587,True)
            check('private SMTP remains blocked',guest,'192.168.33.1',587,False)
            check('public SSH egress blocked',guest,'9.9.9.9',22,False)
            check('private router blocked',guest,'192.168.33.1',80,False)
            check('host LAN address blocked',guest,'192.168.33.24',52990,False)
            check('host bridge non-DNS blocked',guest,GATEWAY,52990,False)
            check('router public hairpin blocked',guest,'9.9.9.10',443,False)
            check('DNS TCP relay permitted',guest,GATEWAY,53,True)
            check('DNS UDP relay permitted',guest,GATEWAY,53,True,udp=True)
            check('host initiated management replies permitted',None,GUEST,22,True)
            check('LAN initiated VM management blocked',outside,GUEST,22,False)
            check('guest IPv6 host access blocked',guest,'fd00:de02::1',52990,False)
            check('guest IPv6 forwarding blocked',guest,'2001:4860:4860::8888',443,False)
            inside(guest,'ip','addr','add','172.31.250.6/32','dev','guest-end')
            check('spoofed guest source blocked',guest,'9.9.9.9',443,False,source='172.31.250.6')
            counters=json.loads(run('nft','-j','list','counters','inet',TABLE).stdout)
            stats={v['counter']['name']:v['counter'].get('packets',0) for v in counters['nftables'] if 'counter' in v}
            assert all(stats.get(k,0)>0 for k in ('blocked_host','blocked_private','blocked_other','blocked_inbound')),stats
            # No other namespace tables/host state involved; all probes ran as namespace root.
            print(json.dumps({'status':'PASS','tests':passed,'count':len(passed),'drop_counters_observed':True,'test_namespace_only':True}))
        finally:
            for p in servers+children:
                if p.poll() is None:p.terminate()
            for p in servers+children:
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait()


if __name__=='__main__':main()
