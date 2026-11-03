import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import smtplib

import pytest
from pydantic import ValidationError

from demo_v2.notifications import (Notifications, NotificationSettings, MailConfig,
    SETTINGS_KEY, STATE_KEY, QUEUE_KEY, load, save, periodic_boundary, daily_boundary, LOCAL)
from demo_v2.health import HealthRegistry, ComponentHealth, HealthState
from demo_v2.domain.trading import ExitReason
from test_api import app_for, login, PREFIX
from test_app_isolation import running_client
from conftest import open_position

UTC=timezone.utc


def service(repository,**kwargs):
    now=[datetime(2026,11,3,10,5,tzinfo=UTC)]
    health=HealthRegistry()
    health.record(ComponentHealth('database',HealthState.READY))
    sent=[]
    worker=Notifications(repository,health,MailConfig(enabled=True),transport=sent.append,clock=lambda:now[0],**kwargs)
    return worker,now,health,sent


def enable(worker,**kwargs):
    settings=NotificationSettings(enabled=True,recipients=('one@example.com','two@example.com'),**kwargs)
    worker.update(settings,worker.view()['revision'])
    return settings


def queue(worker):
    with worker.database.connect() as conn:return load(conn,QUEUE_KEY,[])


@pytest.mark.parametrize('recipient',['a@example.com\r\nBcc:x@example.org','a@example.com,b@example.com','Name <a@example.com>','@example.com','a..b@example.com','a@-example.com','a@example..com'])
def test_mailboxes_reject_lists_and_headers(recipient):
    with pytest.raises(ValidationError):NotificationSettings(recipients=(recipient,))


def test_settings_validation_defaults_and_secret_separation(repository):
    worker,_,_,_=service(repository)
    initial=worker.view()
    assert not initial['settings']['enabled'] and not initial['settings']['recipients']
    assert initial['sender']=='demo@dvirtechnologies.com'
    for fields in ({'enabled':True},{'daily_time':'25:01'},{'periodic_hours':2},
                   {'events':['TEST']},{'recipients':['a@example.com','A@example.com']},
                   {'recipients':['a@example.com']*11}):
        with pytest.raises(ValidationError):NotificationSettings(**fields)
    settings=enable(worker,periodic_hours=6)
    assert worker.view()['settings']['periodic_hours']==6
    with pytest.raises(ValueError):worker.update(settings,0)
    config=MailConfig.from_env({'DEMO_EMAIL_ENABLED':'true','DEMO_SMTP_USERNAME':'fake-user','DEMO_SMTP_PASSWORD':'fake-password','SMTP_HOST':'live-must-not-read'})
    assert config.host=='smtp-relay.gmail.com'
    assert 'fake-password' not in repr(config) and 'fake-user' not in repr(config)
    with pytest.raises(ValueError):MailConfig(security='NONE')


def test_trade_events_opt_in_durable_dedup_no_backfill_and_recipients(repository,rule):
    worker,now,_,sent=service(repository)
    old=open_position(repository,rule,1)
    enable(worker,events=('POSITION_OPENED','TAKE_PROFIT','STOP_LOSS','MARKET_RESOLUTION'))
    worker.collect();assert queue(worker)==[]
    current=open_position(repository,rule,2)
    worker.collect();worker.collect()
    assert len(queue(worker))==2
    assert {q['event'] for q in queue(worker)}=={'POSITION_OPENED'}
    assert all('ETH test' in q['body'] for q in queue(worker))
    repo=repository
    repo.close_position(current['id'],price=__import__('decimal').Decimal('0.96'),sell_fee=__import__('decimal').Decimal('0.068'),reason=ExitReason.TAKE_PROFIT,occurred_at=datetime.now(UTC),provenance={})
    worker.collect();assert len(queue(worker))==4
    async def run():
        for _ in range(4):await worker.deliver_once()
    asyncio.run(run())
    assert len(sent)==4 and all(q['status']=='SENT' for q in queue(worker))
    restarted=Notifications(repository,worker.health,worker.config,transport=sent.append,clock=worker.clock)
    restarted.collect();asyncio.run(restarted.deliver_once())
    assert len(sent)==4
    assert old['id'] not in '\n'.join(q['body'] for q in queue(worker))


def test_health_debounce_no_repeat_and_stable_recovery(repository):
    worker,now,health,_=service(repository)
    enable(worker,events=('SYSTEM_FAULT','SYSTEM_RECOVERY'))
    health.record(ComponentHealth('market_websocket',HealthState.NOT_READY,'SOCKET_DISCONNECTED'))
    worker.collect();now[0]+=timedelta(seconds=29);worker.collect();assert not queue(worker)
    now[0]+=timedelta(seconds=1);worker.collect();worker.collect()
    assert [q['event'] for q in queue(worker)]==['SYSTEM_FAULT']*2
    health.record(ComponentHealth('market_websocket',HealthState.READY))
    worker.collect();now[0]+=timedelta(seconds=9);worker.collect();assert len(queue(worker))==2
    now[0]+=timedelta(seconds=1);worker.collect();worker.collect()
    assert [q['event'] for q in queue(worker)][-2:]==['SYSTEM_RECOVERY']*2


@pytest.mark.parametrize('hours',[1,3,6])
def test_periodic_summary_is_configurable_no_initial_backfill_and_once(repository,hours):
    worker,now,_,_=service(repository)
    enable(worker,events=('PERIODIC_SUMMARY',),periodic_hours=hours)
    worker.collect();assert not queue(worker)
    now[0]=periodic_boundary(now[0],hours)+timedelta(hours=hours,seconds=1)
    worker.collect();worker.collect()
    assert len(queue(worker))==2 and all(q['event']=='PERIODIC_SUMMARY' for q in queue(worker))
    assert all('לא היו פתיחות' in q['body'] for q in queue(worker))


def test_daily_report_uses_local_schedule_and_dst_calendar_day(repository):
    worker,now,_,_=service(repository)
    now[0]=datetime(2026,11,3,5,59,tzinfo=UTC)
    enable(worker,events=('DAILY_SUMMARY',),daily_time='08:00')
    worker.collect();assert not queue(worker)
    now[0]+=timedelta(minutes=1);worker.collect();worker.collect()
    assert len(queue(worker))==2
    assert '02/11/2026 08:00' in queue(worker)[0]['body']
    end=daily_boundary(datetime(2026,10,25,10,tzinfo=UTC),'08:00')
    start=(end.astimezone(LOCAL)-timedelta(days=1)).astimezone(UTC)
    assert (end-start).total_seconds()==25*3600


def test_disabling_or_removing_recipients_cancels_pending_and_reset_cancels_research(repository,rule):
    worker,_,_,sent=service(repository)
    enable(worker,events=('POSITION_OPENED',))
    open_position(repository,rule,1);worker.collect();assert len(queue(worker))==2
    worker.update(NotificationSettings(),worker.view()['revision'])
    asyncio.run(worker.deliver_once());assert not sent
    assert all(q['status']=='CANCELED' for q in queue(worker))
    enable(worker,events=('POSITION_OPENED',))
    open_position(repository,rule,2);worker.collect();repository.reset()
    assert all(q['status']=='CANCELED' for q in queue(worker))
    assert worker.view()['settings']['enabled']


def test_queue_bounds_expiry_and_no_secret_payload_in_status(repository):
    worker,now,_,_=service(repository)
    enable(worker)
    for _ in range(252):
        now[0]+=timedelta(minutes=1)
        worker.queue_test()
    assert len(queue(worker))==500 and worker.view()['queue']['dropped']==4
    assert 'body' not in worker.view()['deliveries'][0]
    now[0]+=timedelta(hours=25);worker.collect()
    assert all(q['status']=='EXPIRED' for q in queue(worker))


@pytest.mark.parametrize('error,expected',[(smtplib.SMTPAuthenticationError(535,b'fake-private'), 'FAILED'),
    (smtplib.SMTPServerDisconnected('fake-secret'),'UNKNOWN'),(ConnectionRefusedError('fake'),'PENDING')])
def test_delivery_failure_classification_does_not_leak_secrets(repository,error,expected):
    worker,_,_,_=service(repository);enable(worker);worker.queue_test()
    def fail(item):raise error
    worker.transport=fail
    asyncio.run(worker.deliver_once())
    assert queue(worker)[0]['status']==expected
    assert 'fake' not in json.dumps(worker.view())


def test_interrupted_send_not_retried_and_disabled_transport_never_called(repository):
    worker,_,_,sent=service(repository);enable(worker);worker.queue_test()
    with worker.database.transaction() as conn:
        data=load(conn,QUEUE_KEY,[]);data[0]['status']='SENDING';save(conn,QUEUE_KEY,data)
    async def recover():
        await worker.start();await worker.close()
    asyncio.run(recover());assert queue(worker)[0]['status']=='UNKNOWN' and not sent
    disabled=Notifications(repository,worker.health,MailConfig(),transport=lambda _:pytest.fail('SMTP disabled'))
    asyncio.run(disabled.deliver_once())
    with pytest.raises(ValueError):disabled.queue_test()


def test_connection_refusal_retries_are_bounded(repository):
    worker,now,_,_=service(repository)
    worker.update(NotificationSettings(enabled=True,recipients=('one@example.com',)),0)
    worker.queue_test()
    calls=[]
    def fail(item):calls.append(item['id']);raise ConnectionRefusedError()
    worker.transport=fail
    async def run():
        for attempt in range(3):
            await worker.deliver_once()
            now[0]+=timedelta(minutes=3)
        await worker.deliver_once()
    asyncio.run(run())
    assert len(calls)==3 and queue(worker)[0]['status']=='FAILED'


def test_report_financials_and_names_use_saved_trade_snapshot(repository,rule):
    from decimal import Decimal
    worker,now,_,_=service(repository)
    position=open_position(repository,rule)
    stamp=datetime.now(UTC)
    repository.close_position(position['id'],price=Decimal('0.96'),sell_fee=Decimal('0.068'),
        reason=ExitReason.TAKE_PROFIT,occurred_at=stamp,provenance={})
    repository.update_rule(rule['id'],rule['spec'].model_copy(update={'name':'Changed later'}))
    with repository.database.connect() as conn:
        body=worker.summary(conn,stamp-timedelta(minutes=5),stamp+timedelta(minutes=1))
    assert 'נפתחו: 1 | נסגרו: 1' in body
    assert '1.032' in body and 'עמלות פעולות בתקופה: 0.068' in body
    assert 'ETH test' in body and 'Changed later' not in body
    assert 'מצב המערכת כעת' in body and position['id'] in body


def test_smtp_uses_tls_separate_recipients_and_does_not_retry_quit(monkeypatch):
    calls=[]
    class FakeSMTP:
        def __init__(self,*args,**kwargs):
            assert kwargs['local_hostname']=='dvirtechnologies.com'
            calls.append(('connect',args))
        def ehlo(self):calls.append('ehlo')
        def starttls(self,context):assert context.check_hostname;calls.append('tls')
        def send_message(self,message,**kwargs):
            calls.append(('send',message,kwargs))
        def quit(self):raise smtplib.SMTPServerDisconnected('after accepted')
        def close(self):pass
    monkeypatch.setattr(smtplib,'SMTP',FakeSMTP)
    MailConfig(enabled=True).send({'recipient':'one@example.com','subject':'בדיקה','body':'טקסט','id':'abc'})
    sent=next(c for c in calls if isinstance(c,tuple) and c[0]=='send')
    assert sent[1]['Date']
    assert sent[1]['To']=='one@example.com' and not sent[1]['Bcc']
    assert sent[2]['to_addrs']==['one@example.com']
    assert calls.index('tls')<calls.index(sent)


def test_notification_api_requires_session_csrf_and_revision(tmp_path):
    app=app_for(tmp_path)
    async def run():
        async with running_client(app) as client:
            assert (await client.get(PREFIX+'/notifications')).status_code==401
            await login(client)
            first=(await client.get(PREFIX+'/notifications')).json()
            payload={'settings':first['settings'],'expected_revision':0}
            csrf=client.headers.pop('X-Demo-CSRF')
            assert (await client.put(PREFIX+'/notifications',json=payload)).status_code==403
            client.headers['X-Demo-CSRF']=csrf
            assert (await client.put(PREFIX+'/notifications',json=payload)).status_code==200
            assert (await client.put(PREFIX+'/notifications',json=payload)).status_code==409
            assert (await client.post(PREFIX+'/notifications/test')).status_code==409
            payload['expected_revision']=1
            payload['settings']['recipients']=['x@example.com\nBcc:y@example.com']
            assert (await client.put(PREFIX+'/notifications',json=payload)).status_code==422
    asyncio.run(run())

class FakeResources:
    def __init__(self,now):self.now,self.values=now,dict(cpu=20,ram=30,disk=40,rx=0,tx=0)
    def snapshot(self):
        v=self.values
        return {'state':'PARTIAL','sampled_at':self.now[0],
                'host':{'cpu_percent':v['cpu'],'ram_percent':v['ram']},
                'disk':{'percent':v['disk']},
                'network':{'received_bytes_per_second':v['rx'],'sent_bytes_per_second':v['tx']}}


def resource_tick(worker,now,seconds):
    for _ in range(seconds//2):
        now[0]+=timedelta(seconds=2)
        worker.collect()


@pytest.mark.parametrize('event,values',[
    ('NETWORK_HIGH',{'rx':6*1048576,'tx':4*1048576}),
    ('CPU_HIGH',{'cpu':90}),('RAM_HIGH',{'ram':90}),('DISK_HIGH',{'disk':90})])
def test_resource_opt_in_sustained_threshold_once_and_rearm(repository,event,values):
    worker,now,_,_=service(repository)
    resources=FakeResources(now);resources.values.update(values);worker.resources=resources
    enable(worker,events=(event,));worker.collect()
    resource_tick(worker,now,58);assert not queue(worker)
    resource_tick(worker,now,2);assert [q['event'] for q in queue(worker)]==[event]*2
    assert 'שרת כולו' in queue(worker)[0]['body']
    resource_tick(worker,now,120);assert len(queue(worker))==2
    resources.values.update(cpu=75,ram=75,disk=75,rx=8*1048576,tx=0)
    resource_tick(worker,now,130);assert len(queue(worker))==2  # Hysteresis: not low enough.
    resources.values.update(cpu=1,ram=1,disk=1,rx=0,tx=0)
    resource_tick(worker,now,122)
    resources.values.update(values);resource_tick(worker,now,62)
    assert len(queue(worker))==4


def test_resources_missing_stale_opt_out_and_settings_reset(repository):
    worker,now,_,_=service(repository)
    resources=FakeResources(now);resources.values['cpu']=99;worker.resources=resources
    enable(worker,events=('SYSTEM_FAULT',))
    worker.collect();resource_tick(worker,now,70);assert not queue(worker)
    enable(worker,events=('CPU_HIGH',));worker.collect();resource_tick(worker,now,30)
    resources.values['cpu']=None
    resource_tick(worker,now,40)
    resources.values['cpu']=99
    resource_tick(worker,now,30);assert not queue(worker)
    resource_tick(worker,now,32);assert len(queue(worker))==2
    # A saved change restarts observation and cancels pending old-preference mail.
    enable(worker,events=('CPU_HIGH',),cpu_percent=100)
    resource_tick(worker,now,62);assert all(q['status']=='CANCELED' for q in queue(worker))
    enable(worker,events=('NETWORK_HIGH',))
    frozen=resources.snapshot();frozen['network'].update(received_bytes_per_second=100*1048576)
    resources.snapshot=lambda:frozen
    resource_tick(worker,now,90)
    assert len(queue(worker))==2  # A repeated/stale high sample cannot prove a sustained spike.


def test_resource_short_spike_gap_and_restart_never_count_as_continuity(repository):
    worker,now,_,_=service(repository);resources=FakeResources(now);worker.resources=resources
    enable(worker,events=('CPU_HIGH',));resources.values['cpu']=99
    worker.collect();resource_tick(worker,now,30)
    resources.values['cpu']=10;resource_tick(worker,now,2)
    resources.values['cpu']=99;resource_tick(worker,now,30);assert not queue(worker)
    now[0]+=timedelta(minutes=5);worker.collect()
    resource_tick(worker,now,58);assert not queue(worker)
    async def restart():
        # Recover persistent queue/state without running the background loop.
        original=worker.run
        async def idle():await worker.stop.wait()
        worker.run=idle
        await worker.start();await worker.close();worker.run=original
    asyncio.run(restart())
    resource_tick(worker,now,4);assert not queue(worker)
    resource_tick(worker,now,60);assert len(queue(worker))==2
    worker.stop=asyncio.Event()
    asyncio.run(restart());resource_tick(worker,now,70)
    assert len(queue(worker))==2  # An already alerted incident survives restart.


@pytest.mark.parametrize('fields',[{'cpu_percent':101},{'disk_percent':0},
    {'network_mib_per_second':float('nan')},{'ram_percent':float('inf')},
    {'resource_duration_seconds':9},{'resource_duration_seconds':3601}])
def test_resource_threshold_validation(fields):
    with pytest.raises(ValidationError):NotificationSettings(**fields)
