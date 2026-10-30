"""Explicit test-only real nginx/TLS/ASGI smoke; every listener/state file is temporary.
Supply an extracted nginx binary and mime.types; never installs or starts a service.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import resource
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile

from argon2 import PasswordHasher
import httpx
import uvicorn
from demo_v2.app import create_app
from demo_v2.config import DemoConfig

ROOT=Path(__file__).resolve().parents[2]
DEPLOY=ROOT/'demo_v2/deploy'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


async def verify(nginx,mime_types,output):
    report={'mode':'TEMP_LOOPBACK_NGINX_TLS_DEMO_DB','checks':[]}
    with tempfile.TemporaryDirectory(prefix='demo-deploy-smoke-') as folder:
        root=Path(folder)
        config=DemoConfig(data_dir=root/'state',login_username='deployment-test',
            login_password_hash=PasswordHasher(time_cost=1,memory_cost=8192).hash('only-fake-test-password'),
            session_secret=secrets.token_urlsafe(48),session_cookie_secure=True,public_data_enabled=False)
        app=create_app(config)
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
        backend_port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        task=asyncio.create_task(server.serve(sockets=[listener]))
        child=None
        try:
            for _ in range(1000):
                if server.started:break
                if task.done():await task
                await asyncio.sleep(.005)
            assert server.started
            build=ROOT/'demo_v2/frontend/build'
            assert (build/'index.html').exists(),'Build the production frontend first'
            shutil.copytree(build,root/'frontend/demo')
            cert,key=root/'test-cert.pem',root/'test-key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                '-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1,DNS:localhost',
                '-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            tls_port,http_port=free_port(),free_port()
            snippet=(DEPLOY/'nginx-demo-api.conf').read_text().replace('127.0.0.1:8010',f'127.0.0.1:{backend_port}')
            (root/'api.conf').write_text(snippet)
            def render(name):
                text=(DEPLOY/name).read_text().replace('/etc/nginx/snippets/polymarket-demo-v2-api.conf',str(root/'api.conf'))
                text=text.replace('/opt/polymarket-demo-v2/frontend',str(root/'frontend'))
                text=text.replace('/var/log/nginx/polymarket-demo-v2.access.log',str(root/'access.log'))
                text=text.replace('/var/log/nginx/polymarket-demo-v2.error.log',str(root/'error.log'))
                text=text.replace('/etc/letsencrypt/live/demo-poly.dvirtechnologies.com/fullchain.pem',str(cert))
                text=text.replace('/etc/letsencrypt/live/demo-poly.dvirtechnologies.com/privkey.pem',str(key))
                return text.replace('listen 80;',f'listen 127.0.0.1:{http_port};').replace('listen 443 ssl;',f'listen 127.0.0.1:{tls_port} ssl;').replace('listen 127.0.0.1:8080;',f'listen 127.0.0.1:{http_port};')
            def wrapper(profile):
                return f'daemon off; master_process off; pid {root}/nginx.pid; error_log {root}/main.log;\nevents {{worker_connections 128;}}\nhttp {{include {mime_types}; client_body_temp_path {root}/client; proxy_temp_path {root}/proxy; fastcgi_temp_path {root}/fastcgi; uwsgi_temp_path {root}/uwsgi; scgi_temp_path {root}/scgi; access_log off;\n'+render(profile)+'\n}\n'
            conf=root/'nginx.conf'
            for profile in ['nginx-demo-poly.conf','nginx-demo-tunnel.conf.example']:
                conf.write_text(wrapper(profile))
                result=subprocess.run([str(nginx),'-t','-p',str(root),'-c',str(conf)],capture_output=True,text=True)
                assert result.returncode==0,result.stderr
                report['checks'].append(profile+' syntax')
            conf.write_text(wrapper('nginx-demo-poly.conf'))
            child=subprocess.Popen([str(nginx),'-p',str(root),'-c',str(conf)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            context=ssl.create_default_context(cafile=str(cert))
            async with httpx.AsyncClient(base_url=f'https://127.0.0.1:{tls_port}',verify=context,trust_env=False,
                    headers={'Host':'demo-poly.dvirtechnologies.com'},timeout=5,follow_redirects=False) as client:
                for _ in range(200):
                    try:
                        root_response=await client.get('/')
                        break
                    except httpx.ConnectError:await asyncio.sleep(.01)
                else:raise AssertionError('Temporary nginx did not start')
                assert root_response.status_code==302 and root_response.headers['location'].endswith('/demo/')
                report['checks'].append('HTTPS root redirects /demo/')
                for path,kind in [('/demo/','text/html'),('/demo/src/app.js','javascript'),('/demo/src/styles.css','text/css')]:
                    response=await client.get(path)
                    assert response.status_code==200 and kind in response.headers['content-type']
                    assert 'no-store' in response.headers['cache-control']
                    report['checks'].append(path+' production asset/MIME/no-cache')
                assert '/demo/api/v1' in (await client.get('/demo/src/config.js')).text
                for path in ['/demo/missing.js','/demo/api/v2/missing','/openapi.json','/health']:
                    assert (await client.get(path)).status_code==404
                    report['checks'].append(path+' stays 404')
                api='/demo/api/v1'
                assert (await client.get(api+'/trades')).status_code==401
                login=await client.post(api+'/auth/login',json={'username':'deployment-test','password':'only-fake-test-password'})
                assert login.status_code==200
                header=login.headers['set-cookie']
                assert all(flag in header for flag in ('Secure','HttpOnly','SameSite=strict','Path=/demo/'))
                assert 'Domain=' not in header and login.headers['cache-control']=='no-store'
                csrf=login.json()['csrf_token']
                assert (await client.get(api+'/auth/current')).json()['user']=='deployment-test'
                report['checks'].append('real TLS host-only Secure cookie/current session')
                assert (await client.post(api+'/rules',json={})).status_code==403
                client.headers['X-Demo-CSRF']=csrf
                rule=await client.post(api+'/rules',json={'name':'TLS test only','markets':['BTC-5m'],'entry_trigger':'0.74','gross_shares':'5'})
                assert rule.status_code==201
                assert (await client.get(api+'/rules')).json()['items'][0]['id']==rule.json()['id']
                report['checks'].append('API prefix unchanged/auth/CSRF/rule write')
                assert (await client.post(api+'/maintenance/factory-reset',json={'confirmation':'wrong'})).status_code==409
                report['checks'].append('reset wrong confirmation refused')
                health=await client.get(api+'/health')
                assert health.json()['state']!='READY' and not health.json()['entries_allowed']
                assert not any(name.lower().startswith('access-control-allow') for name in health.headers)
                report['checks'].append('real blocked health/no CORS')
                assert (await client.post(api+'/auth/logout')).status_code==200
                assert (await client.get(api+'/auth/current')).status_code==401
                report['checks'].append('logout/revocation through proxy')
            async with httpx.AsyncClient(trust_env=False,timeout=5,follow_redirects=False) as client:
                response=await client.get(f'http://127.0.0.1:{http_port}/demo/',headers={'Host':'demo-poly.dvirtechnologies.com'})
                assert response.status_code==308 and response.headers['location']=='https://demo-poly.dvirtechnologies.com/demo/'
                assert (await client.get(f'http://127.0.0.1:{backend_port}/health')).json()=={'status':'alive','mode':'SIMULATION','trading_enabled':False}
                assert (await client.get(f'http://127.0.0.1:{backend_port}/ready')).status_code==503
                report['checks'].append('HTTP redirects HTTPS/internal liveness != readiness')
            resident=next(int(line.split()[1]) for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:'))
            report.update(result='PASS',public_provider_calls=0,resident_rss_kib=resident,max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        finally:
            if child is not None:
                child.terminate()
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.wait()
            server.should_exit=True
            await asyncio.wait_for(task,10)
            listener.close()
            report['processes_stopped']=task.done() and (child is None or child.poll() is not None)
            Path(output).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'result':report.get('result'),'checks':len(report['checks']),'processes_stopped':report['processes_stopped']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nginx',required=True,type=Path)
    parser.add_argument('--mime-types',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    asyncio.run(verify(args.nginx.resolve(),args.mime_types.resolve(),args.output))
