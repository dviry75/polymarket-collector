"""Read-only, bounded Linux resource telemetry; never a trading/readiness gate.

Host NIC counters are NOT per-process traffic or a home-router bandwidth meter.
No privileged commands, environment inspection, DB queries or network probes.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import time


class ResourceMonitor:
    def __init__(self, data_dir, *, proc_root=Path('/proc'), interval=2.0,
                 clock=time.monotonic, cpu_clock=time.process_time,
                 now=lambda: datetime.now(timezone.utc), decision_pid=None):
        self.data_dir, self.proc_root = Path(data_dir), Path(proc_root)
        self.interval, self.clock, self.cpu_clock, self.now = interval, clock, cpu_clock, now
        self._cpu_previous = self._network_previous = None
        self.decision_pid=decision_pid
        self._child_cpu=0.0
        self._child_identity=None
        self._task = None
        self._snapshot = self._empty()

    @staticmethod
    def _empty():
        return dict(state='UNAVAILABLE', sampled_at=None, interval_seconds=None,
                    process={}, host={}, disk={}, demo_storage={}, network={}, unavailable=[])

    def snapshot(self):
        return self._snapshot

    def _cpu(self):
        fields = (self.proc_root/'stat').read_text().splitlines()[0].split()
        if fields[0] != 'cpu' or len(fields) < 5:
            raise ValueError('CPU counters unavailable')
        # guest/guest_nice are already included in user/nice, never add twice.
        values = [int(value) for value in fields[1:9]]
        total, idle = sum(values), values[3] + (values[4] if len(values) > 4 else 0)
        current = (self.clock(), self.cpu_clock()+self._child_cpu, total, idle)
        process_percent = host_percent = elapsed = None
        previous = self._cpu_previous
        if previous:
            elapsed = current[0]-previous[0]
            delta = total-previous[2]
            idle_delta = idle-previous[3]
            if elapsed > 0 and current[1] >= previous[1]:
                process_percent = round((current[1]-previous[1])/elapsed*100, 1)
            if delta > 0 and 0 <= idle_delta <= delta:
                host_percent = round((delta-idle_delta)/delta*100, 1)
        self._cpu_previous = current
        return process_percent, host_percent, elapsed

    def _memory(self):
        values = {}
        for line in (self.proc_root/'meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            values[key] = int(value.split()[0])*1024
        total, available = values['MemTotal'], values['MemAvailable']
        if total <= 0 or not 0 <= available <= total:
            raise ValueError('Memory counters unavailable')
        return dict(ram_total_bytes=total, ram_available_bytes=available,
                    ram_used_bytes=total-available,
                    ram_percent=round((total-available)/total*100, 1),
                    swap_total_bytes=values.get('SwapTotal'),
                    swap_used_bytes=(values['SwapTotal']-values['SwapFree']
                                     if 'SwapTotal' in values and 'SwapFree' in values else None))

    def _network(self):
        counters = {}
        for line in (self.proc_root/'net/dev').read_text().splitlines()[2:]:
            name, fields = line.split(':', 1)
            name, fields = name.strip(), fields.split()
            if name != 'lo':
                counters[name] = (int(fields[0]), int(fields[8]))
        if not counters:
            raise ValueError('Network counters unavailable')
        stamp = self.clock()
        received = sum(value[0] for value in counters.values())
        sent = sum(value[1] for value in counters.values())
        rx_rate = tx_rate = elapsed = None
        previous = self._network_previous
        if previous:
            elapsed = stamp-previous[0]
            if (elapsed > 0 and counters.keys() == previous[1].keys()
                and all(all(a >= b for a,b in zip(value, previous[1][name]))
                        for name,value in counters.items())):
                rx_rate = round((received-sum(value[0] for value in previous[1].values()))/elapsed, 1)
                tx_rate = round((sent-sum(value[1] for value in previous[1].values()))/elapsed, 1)
        self._network_previous = (stamp, counters)
        return dict(scope='HOST_NON_LOOPBACK_INTERFACES', interface_count=len(counters),
                    received_bytes=received, sent_bytes=sent,
                    received_bytes_per_second=rx_rate, sent_bytes_per_second=tx_rate,
                    interval_seconds=elapsed)

    def _storage(self):
        if not self.data_dir.is_dir():
            raise FileNotFoundError("DEMO data directory unavailable")
        sizes = {}
        for name, suffix in (('database_bytes',''), ('wal_bytes','-wal'), ('shm_bytes','-shm')):
            try:
                sizes[name] = (self.data_dir/('demo_v2.sqlite3'+suffix)).stat().st_size
            except FileNotFoundError:
                sizes[name] = 0
        return {**sizes, 'total_bytes':sum(sizes.values())}

    def _decision_process(self):
        if self.decision_pid is None:
            return None
        pid=self.decision_pid()
        if not isinstance(pid,int) or pid<=0:
            raise ValueError("Decision process unavailable")
        # Only kernel resource counters; never process environments or files.
        fields=(self.proc_root/str(pid)/"stat").read_text().rsplit(")",1)[1].split()
        identity=(pid,fields[19])  # pid + kernel process start time
        if self._child_identity!=identity:
            self._cpu_previous=None
            self._child_identity=identity
        self._child_cpu=(int(fields[11])+int(fields[12]))/os.sysconf("SC_CLK_TCK")
        pages=[int(v) for v in (self.proc_root/str(pid)/"statm").read_text().split()[:2]]
        page_size=os.sysconf("SC_PAGE_SIZE")
        return {"pid":pid,"rss_bytes":pages[1]*page_size,"virtual_memory_bytes":pages[0]*page_size}

    def sample(self):
        result = self._empty()
        result['sampled_at'] = self.now()
        result['process'] = dict(scope='DEMO_BACKEND_PROCESS', cpu_percent=None, rss_bytes=None,
                                 virtual_memory_bytes=None)
        result['host'] = dict(logical_cpus=os.cpu_count(), cpu_percent=None)
        child=None
        try:
            child=self._decision_process()
        except (OSError,ValueError,IndexError):
            self._cpu_previous=None
            self._child_cpu=0.0
            self._child_identity=None
            result['unavailable'].append('decision_process')
        if self.decision_pid is not None:
            result['process']['scope']='DEMO_SERVICE_PROCESSES'
            result['process']['aggregation']='SUM_OF_PROCESS_RSS_INCLUDES_SHARED_PAGES'
            result['decision_process']=child
        try:
            process, host, elapsed = self._cpu()
            result['process']['cpu_percent'] = process
            result['host']['cpu_percent'] = host
            result['interval_seconds'] = elapsed
        except (OSError, ValueError, IndexError):
            self._cpu_previous = None
            result['unavailable'].append('cpu')
        try:
            result['host'].update(self._memory())
        except (OSError, ValueError, KeyError, IndexError):
            result['unavailable'].append('host_memory')
        try:
            pages = [int(value) for value in (self.proc_root/'self/statm').read_text().split()[:2]]
            page_size = os.sysconf('SC_PAGE_SIZE')
            result['process'].update(virtual_memory_bytes=pages[0]*page_size+(child['virtual_memory_bytes'] if child else 0),
                rss_bytes=pages[1]*page_size+(child['rss_bytes'] if child else 0))
        except (OSError, ValueError, IndexError):
            result['unavailable'].append('process_memory')
        if self.decision_pid is not None and child is None:
            result['process'].update(rss_bytes=None,virtual_memory_bytes=None,cpu_percent=None)
        try:
            usage = shutil.disk_usage(self.data_dir)
            result['disk'] = dict(scope='DEMO_DATA_FILESYSTEM', total_bytes=usage.total,
                                  used_bytes=usage.used, available_bytes=usage.free,
                                  percent=round(usage.used/usage.total*100, 1))
        except (OSError, ValueError, ZeroDivisionError):
            result['unavailable'].append('disk')
        try:
            result['demo_storage'] = self._storage()
        except OSError:
            result['unavailable'].append('demo_storage')
        try:
            result['network'] = self._network()
        except (OSError, ValueError, IndexError):
            self._network_previous = None
            result['unavailable'].append('network')
        result['state'] = ('AVAILABLE' if not result['unavailable'] else
                           'UNAVAILABLE' if len(result['unavailable']) == (7 if self.decision_pid is not None else 6) else 'PARTIAL')
        # Atomic replacement: request handlers only read the last complete sample.
        self._snapshot = result

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            await asyncio.to_thread(self.sample)

    async def start(self):
        await asyncio.to_thread(self.sample)
        self._task = asyncio.create_task(self._run(), name='demo-resource-telemetry')

    async def close(self):
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
