"""Read-only telemetry, interval arithmetic, degradation and authenticated API."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from demo_v2.resources import ResourceMonitor
from test_api import app_for, login, PREFIX
from test_app_isolation import running_client


def proc_files(root, *, cpu='cpu 100 0 100 800 0 0 0 0 50 0', rx=1000, tx=2000, interface='eth0'):
    (root/'self').mkdir(parents=True,exist_ok=True)
    (root/'net').mkdir(exist_ok=True)
    (root/'stat').write_text(cpu+'\ncpu0 100 0 100 800\n')
    (root/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 600 kB\nSwapTotal: 100 kB\nSwapFree: 75 kB\n')
    (root/'self/statm').write_text('1000 50 0 0 0 0 0')
    (root/'net/dev').write_text('header\nheader\n'+
        f'lo: 999999 0 0 0 0 0 0 0 999999 0 0 0 0 0 0 0\n{interface}: {rx} 0 0 0 0 0 0 0 {tx} 0 0 0 0 0 0 0\n')


def test_interval_cpu_network_units_and_current_memory_without_guest_double_count(tmp_path,monkeypatch):
    proc=tmp_path/'proc'
    proc_files(proc)
    data=tmp_path/'state';data.mkdir()
    for suffix,size in [('',100),('-wal',30),('-shm',10)]:
        (data/('demo_v2.sqlite3'+suffix)).write_bytes(b'x'*size)
    before={path.name:path.read_bytes() for path in data.iterdir()}
    time=[10.0]; cpu=[1.0]
    monitor=ResourceMonitor(data,proc_root=proc,clock=lambda:time[0],cpu_clock=lambda:cpu[0])
    monkeypatch.setattr('demo_v2.resources.shutil.disk_usage',lambda _:SimpleNamespace(total=1000,used=400,free=600))
    monitor.sample()
    first=monitor.snapshot()
    assert first['state']=='AVAILABLE'
    assert first['process']['cpu_percent'] is None
    assert first['network']['received_bytes_per_second'] is None
    assert first['network']['received_bytes']==1000  # loopback excluded
    assert first['host']['ram_used_bytes']==400*1024 and first['host']['swap_used_bytes']==25*1024
    assert first['demo_storage']['total_bytes']==140
    assert first['disk']['percent']==40
    assert first['process']['rss_bytes']==50*__import__('os').sysconf('SC_PAGE_SIZE')
    time[0]=12.0;cpu[0]=1.5
    proc_files(proc,cpu='cpu 150 0 150 900 0 0 0 0 100 0',rx=3048,tx=6096)
    monitor.sample()
    second=monitor.snapshot()
    assert second['process']['cpu_percent']==25
    assert second['host']['cpu_percent']==50  # guest must not be added a second time
    assert second['network']['received_bytes_per_second']==1024
    assert second['network']['sent_bytes_per_second']==2048
    assert first['process']['cpu_percent'] is None  # prior snapshot unchanged
    assert before=={path.name:path.read_bytes() for path in data.iterdir()}


def test_network_counter_reset_and_interface_change_rebaseline_instead_of_negative_rates(tmp_path):
    proc_files(tmp_path)
    time=[10.0]
    monitor=ResourceMonitor(tmp_path,proc_root=tmp_path,clock=lambda:time[0])
    monitor.sample();time[0]+=2
    proc_files(tmp_path,rx=1,tx=1)
    monitor.sample()
    assert monitor.snapshot()['network']['received_bytes_per_second'] is None
    time[0]+=2;proc_files(tmp_path,rx=1,tx=1,interface='eth1');monitor.sample()
    assert monitor.snapshot()['network']['sent_bytes_per_second'] is None
    time[0]+=2;monitor.sample()
    assert monitor.snapshot()['network']['sent_bytes_per_second']==0


def test_missing_partial_and_malformed_proc_never_fabricates_zero_or_writes(tmp_path):
    monitor=ResourceMonitor(tmp_path/'absent',proc_root=tmp_path/'absent')
    monitor.sample()
    snapshot=monitor.snapshot()
    assert snapshot['state']=='UNAVAILABLE' and len(snapshot['unavailable'])==6
    assert snapshot['process']['rss_bytes'] is None and not (tmp_path/'absent').exists()
    proc_files(tmp_path)
    monitor=ResourceMonitor(tmp_path,proc_root=tmp_path)
    (tmp_path/'net/dev').write_text('malformed')
    (tmp_path/'self/statm').write_text('')
    monitor.sample()
    assert monitor.snapshot()['state']=='PARTIAL'
    assert monitor.snapshot()['network']=={}
    assert set(monitor.snapshot()['unavailable'])=={'network','process_memory'}
    assert monitor.snapshot()['host']['ram_total_bytes']==1024000


def test_resource_worker_refreshes_in_background_and_closes_without_affecting_readiness(tmp_path):
    proc_files(tmp_path)
    monitor=ResourceMonitor(tmp_path,proc_root=tmp_path,interval=.001)
    async def run():
        await monitor.start()
        first=monitor.snapshot()['sampled_at']
        for _ in range(100):
            await asyncio.sleep(.001)
            if monitor.snapshot()['sampled_at']!=first: break
        assert monitor.snapshot()['sampled_at']!=first
        await monitor.close()
        assert monitor._task.done()
    asyncio.run(run())


def test_resource_api_requires_session_is_cached_and_unavailable_telemetry_does_not_change_health(tmp_path):
    app=app_for(tmp_path)
    async def run():
        async with running_client(app) as client:
            assert (await client.get(PREFIX+'/health')).status_code==401
            assert 'resources' not in (await client.get('/health')).json()
            await login(client)
            app.state.resources._snapshot={'state':'UNAVAILABLE','sampled_at':None,'unavailable':['network']}
            snapshot=app.state.health.snapshot()
            # An HTTP health read cannot perform an extra sampler or wait for I/O.
            app.state.resources.sample=lambda: (_ for _ in ()).throw(AssertionError('sample on request'))
            response=await client.get(PREFIX+'/health')
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
            payload=response.json()
            assert payload['resources']['state']=='UNAVAILABLE'
            assert payload['state']==snapshot.state.value
            assert not any('resource' in c['component'] for c in payload['components'])
            assert 'resources' in (await client.get('/openapi.json')).json()['components']['schemas']['HealthView']['properties']
        assert app.state.resources._task.done()
    asyncio.run(run())


def test_decision_process_cpu_rss_aggregation_and_restart_rebaseline(tmp_path):
    import os
    proc_files(tmp_path)
    child=tmp_path/'123';child.mkdir()
    fields=['0']*22
    fields[11]='100';fields[12]='50';fields[19]='999'
    (child/'stat').write_text('123 (demo owner) '+' '.join(fields))
    (child/'statm').write_text('400 20')
    stamp=[10.];cpu=[1.]
    monitor=ResourceMonitor(tmp_path,proc_root=tmp_path,decision_pid=lambda:123,clock=lambda:stamp[0],cpu_clock=lambda:cpu[0])
    monitor.sample()
    assert monitor.snapshot()['process']['rss_bytes']==70*os.sysconf('SC_PAGE_SIZE')
    assert monitor.snapshot()['process']['scope']=='DEMO_SERVICE_PROCESSES'
    stamp[0]+=2;cpu[0]+=.5;fields[11]=str(100+os.sysconf('SC_CLK_TCK'))
    (child/'stat').write_text('123 (demo owner) '+' '.join(fields));monitor.sample()
    assert monitor.snapshot()['process']['cpu_percent']==75
    fields[19]='1000';fields[11]='1'
    (child/'stat').write_text('123 (demo owner) '+' '.join(fields));stamp[0]+=2;monitor.sample()
    assert monitor.snapshot()['process']['cpu_percent'] is None
