"""Request ACPI shutdown over the private QMP socket; no guest credential access."""
import json
import os
from pathlib import Path
import socket
import sys
import time

def main():
    p=Path('/run/polymarket-demo-vm/qmp.sock')
    if p.exists():
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(10);connection.connect(str(p))
            stream=connection.makefile('rwb',buffering=0)
            def request(command):
                stream.write((json.dumps({'execute':command})+'\n').encode())
                while True:
                    obj=json.loads(stream.readline())
                    if 'return' in obj:return
                    if 'error' in obj:raise SystemExit('QMP shutdown request failed')
            json.loads(stream.readline())
            request('qmp_capabilities');request('system_powerdown')
            stream.close()
        # Let the guest stop its services and flush the virtual disk before systemd kills QEMU.
        for _ in range(100):
            pid=int(sys.argv[1]) if len(sys.argv)>1 and sys.argv[1].isdigit() else 0
            if pid>1:
                try:os.kill(pid,0)
                except ProcessLookupError:break
            elif not p.exists():break
            time.sleep(1)


if __name__=="__main__":main()
