"""Unprivileged host controller: verify the fresh guest boundary using pinned SSH."""
import argparse
import concurrent.futures
import json
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time

GUEST='172.31.250.2'
GATEWAY='172.31.250.1'
PRIVATE=Path('/home/dvir/Documents/Codex/2026-10-29/new-chat/work/demo-vm-private')


def command():
    key=Path('/etc/polymarket-demo-vm/guest-host-key.pub').read_text().split()
    if key[0]!='ssh-ed25519' or len(key)<2:raise RuntimeError('Unexpected pinned host key')
    known=PRIVATE/'known_hosts'
    known.write_text('polymarket-demo-vm '+' '.join(key[:2])+'\n');known.chmod(0o600)
    return ['ssh','-i',str(PRIVATE/'id_ed25519'),'-o','BatchMode=yes',
        '-o','IdentitiesOnly=yes','-o','ConnectTimeout=5','-o','StrictHostKeyChecking=yes',
        '-o','HostKeyAlias=polymarket-demo-vm','-o','UserKnownHostsFile='+str(known),
        'demoadmin@'+GUEST]


def public_probe(host):
    """Safe per-stage evidence; never return payloads, addresses or exception text."""
    detail={'dns':False,'tcp':False,'tls':False}
    stage='dns'
    deadline=time.monotonic()+15
    try:
        addresses=socket.getaddrinfo(host,443,socket.AF_INET,socket.SOCK_STREAM)
        if not addresses:raise socket.gaierror()
        detail['dns']=True;stage='tcp'
        # Try every resolved IPv4, just as create_connection would normally do.
        failure=None
        for family,kind,protocol,_,address in addresses:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            raw=socket.socket(family,kind,protocol);raw.settimeout(min(5,remaining))
            try:raw.connect(address)
            except OSError as exc:raw.close();failure=exc;continue
            with raw:
                detail['tcp']=True;stage='tls'
                with ssl.create_default_context().wrap_socket(raw,server_hostname=host):
                    detail['tls']=True
            break
        else:raise failure or OSError()
    except OSError as exc:
        detail.update(failed_stage=stage,error_type=type(exc).__name__)
    return detail


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--wait',type=int,default=300)
    parser.add_argument('--report',type=Path);args=parser.parse_args()
    ssh=command();deadline=time.monotonic()+args.wait
    while True:
        p=subprocess.run(ssh+['true'],capture_output=True,text=True)
        if p.returncode==0:break
        if time.monotonic()>deadline:
            raise SystemExit('Guest SSH was not ready with its pinned host key; inspect the dedicated VM service/serial log')
        time.sleep(3)
    # Probes are done as root inside the fresh guest, without modifying its firewall.
    sockets=[];stop=threading.Event()
    try:
        for addr in ('192.168.33.24',GATEWAY):
            s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            s.bind((addr,52990));s.listen();s.settimeout(.2);sockets.append(s)
        def serve(s):
            while not stop.is_set():
                try:
                    c,_=s.accept();c.sendall(b'CANARY');c.close()
                except socket.timeout:pass
                except OSError:
                    if stop.is_set():break
                    raise
        threads=[threading.Thread(target=serve,args=(s,),daemon=True) for s in sockets]
        for t in threads:t.start()
        for addr in ('192.168.33.24',GATEWAY):
            with socket.create_connection((addr,52990),timeout=2) as c:assert c.recv(20)==b'CANARY'
        import inspect
        code='import json,os,socket,ssl,time\n'+inspect.getsource(public_probe)+'''
assert os.geteuid()==0
results={"guest_root_probe":True}
for name,host,port in [("home_router_blocked","192.168.33.1",80),("host_lan_blocked","192.168.33.24",52990),("host_bridge_blocked","172.31.250.1",52990),("host_api_not_exposed","172.31.250.1",8010)]:
 try:
  s=socket.create_connection((host,port),timeout=2);s.close();results[name]=False
 except OSError:results[name]=True
diagnostics={}
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
 jobs={name:pool.submit(public_probe,host) for name,host in [("gamma_dns_tls","gamma-api.polymarket.com"),("binance_dns_tls","api.binance.com")]}
 for name,job in jobs.items():
  detail=job.result();diagnostics[name]=detail;results[name]=detail['tls']
print(json.dumps({'checks':results,'public_connectivity':diagnostics}))
assert all(results.values()),"Guest network validation failed"
'''
        code='import concurrent.futures\n'+code
        p=subprocess.run(ssh+['sudo','-n','python3','-'],input=code,capture_output=True,text=True,timeout=70)
        result=json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {'checks':{'status':'NO_GUEST_RESULT'}}
        report={'status':'PASS' if p.returncode==0 else 'FAIL',**result,'production_data_used':False,'public_forwarding_enabled':False}
        if args.report:args.report.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
        if p.returncode:raise SystemExit('VM boundary checks did not all pass; do not expose the VM')
    finally:
        stop.set()
        for s in sockets:s.close()


if __name__=='__main__':main()
