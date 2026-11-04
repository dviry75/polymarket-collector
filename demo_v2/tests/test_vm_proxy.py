"""Relay convergence and auth gates; synthetic local HTTP/nginx only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import build_opener,ProxyHandler

import pytest

from demo_v2.deploy.vm import migrate


def test_relay_waits_for_new_config_and_transient_upstream_failure(monkeypatch,tmp_path):
    calls=[]
    results=iter((migrate.ProxyPending('old workers'),HTTPError('http://fake',502,'fake',{},None),None))
    def probe(*args):
        calls.append(args);result=next(results)
        if result:raise result
    monkeypatch.setattr(migrate,'proxy_probe',probe)
    monkeypatch.setattr(migrate.time,'sleep',lambda _:None)
    migrate.proxy_verify(tmp_path)
    assert len(calls)==3


def test_persistent_bad_gateway_still_fails_closed(monkeypatch,tmp_path):
    def failed(*args):raise HTTPError('http://fake',502,'fake',{},None)
    monkeypatch.setattr(migrate,'proxy_probe',failed)
    with pytest.raises(RuntimeError,match='before timeout') as error:migrate.proxy_verify(tmp_path,timeout=0)
    assert isinstance(error.value.__cause__,HTTPError)


@pytest.mark.parametrize('message',('Host proxy bypassed authentication','Host proxy serves another frontend'))
def test_relay_never_retries_away_auth_or_artifact_violation(monkeypatch,tmp_path,message):
    calls=[]
    def failed(*args):calls.append(args);raise RuntimeError(message)
    monkeypatch.setattr(migrate,'proxy_probe',failed)
    with pytest.raises(RuntimeError,match=message):migrate.proxy_verify(tmp_path)
    assert len(calls)==1


@contextmanager
def backend(api_status):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(api_status if '/api/' in self.path else 200)
            self.end_headers();self.wfile.write(b'identical fake frontend')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server.server_address[1]
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


@pytest.mark.skipif(shutil.which('nginx') is None,reason='nginx unavailable for synthetic reload regression')
def test_real_nginx_reload_converges_despite_identical_old_frontend(tmp_path,monkeypatch):
    with backend(502) as old_port,backend(401) as new_port:
        with socket.socket() as reserve:reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
        conf=tmp_path/'nginx.conf'
        def configuration(backend_port,marker):
            return (f'pid {tmp_path}/nginx.pid;\nerror_log {tmp_path}/error.log;\n'
                'events { worker_connections 64; }\nhttp { access_log off; server {\n'
                f'listen 127.0.0.1:{port};\n'+marker+
                f'location / {{ proxy_pass http://127.0.0.1:{backend_port}; }}\n}} }}\n')
        conf.write_text(configuration(old_port,''))
        nginx=subprocess.Popen(['nginx','-p',str(tmp_path),'-c',str(conf),'-g','daemon off;'],
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        opener=build_opener(ProxyHandler({}));base='http://127.0.0.1:'+str(port)
        timer=None
        try:
            deadline=time.monotonic()+5
            while True:
                try:
                    with opener.open(base+'/demo/',timeout=1) as response:assert response.read()==b'identical fake frontend'
                    break
                except OSError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.05)
            with pytest.raises(HTTPError) as original:opener.open(base+'/demo/api/v1/health',timeout=1)
            assert original.value.code==502
            stage=tmp_path/'stage'
            for name in ('index.html','src/app.js','src/pipeline.js'):
                file=stage/'served-check'/name;file.parent.mkdir(parents=True,exist_ok=True)
                file.write_bytes(b'identical fake frontend')
            probe=migrate.proxy_probe
            with pytest.raises(migrate.ProxyPending):probe(stage,base)
            def reload():
                conf.write_text(configuration(new_port,'add_header X-Demo-Runtime-Target polymarket-demo-vm always;\n'))
                nginx.send_signal(signal.SIGHUP)
            timer=threading.Timer(.3,reload);timer.start()
            monkeypatch.setattr(migrate,'proxy_probe',lambda stage,_base:probe(stage,base))
            migrate.proxy_verify(stage,timeout=5)
        finally:
            if timer:timer.cancel();timer.join(timeout=2)
            nginx.terminate();nginx.wait(timeout=5)
