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
    assert all(f"עסקת DEMO #{current['serial_number']}" in q['body'] for q in queue(worker))
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



def test_slow_notification_collection_does_not_pause_event_loop(repository,monkeypatch):
    import threading
    worker,_,_,_=service(repository)
    started=threading.Event();release=threading.Event()
    def blocked():
        started.set()
        assert release.wait(3)
    monkeypatch.setattr(worker,'collect',blocked)
    async def run():
        task=asyncio.create_task(worker.run())
        try:
            for _ in range(200):
                if started.is_set():break
                await asyncio.sleep(.005)
            assert started.is_set() and not task.done()
        finally:
            release.set()
            worker.stop.set()
            await asyncio.wait_for(task,2)
    asyncio.run(run())

def test_per_recipient_routing_legacy_defaults_and_all_event_categories(repository):
    from demo_v2.notifications import EVENTS
    worker,_,_,_=service(repository)
    first=tuple(list(EVENTS)[::2]);second=tuple(list(EVENTS)[1::2])
    enable(worker,events=(),recipient_events={'ONE@example.com':first,'two@example.com':second})
    settings=NotificationSettings.model_validate(worker.view()['settings'])
    assert settings.recipient_events['one@example.com']==first
    with worker.database.transaction() as conn:
        state={};items=[]
        for event in EVENTS:worker.enqueue(items,state,settings,event,event,event,'fake body')
        assert len(items)==len(EVENTS)
        for item in items:
            assert item['event'] in (first if item['recipient']=='one@example.com' else second)
    # Old settings without recipient_events retain the exact old recipients/events.
    with worker.database.transaction() as conn:
        save(conn,SETTINGS_KEY,{'revision':7,'settings':{'enabled':True,'recipients':['one@example.com','two@example.com'],'events':['SYSTEM_FAULT']}})
    view=worker.view()
    assert view['revision']==7 and view['settings']['recipient_events']=={}
    with worker.database.connect() as conn:
        assert 'recipient_events' not in load(conn,SETTINGS_KEY,{})['settings']  # GET does not mutate old settings.
    settings=NotificationSettings.model_validate(view['settings'])
    items=[];worker.enqueue(items,{},settings,'SYSTEM_FAULT','fault','fault','fake')
    assert {q['recipient'] for q in items}=={'one@example.com','two@example.com'}


@pytest.mark.parametrize('fields',[
    {'recipient_events':{'other@example.com':['SYSTEM_FAULT']}},
    {'recipient_events':{'one@example.com':['TEST']}},
    {'recipient_events':{'one@example.com':['SYSTEM_FAULT','SYSTEM_FAULT']}},
    {'recipient_events':{'one@example.com':[],'ONE@example.com':['SYSTEM_FAULT']}},
    {'recipient_events':{'one@example.com':[],'two@example.com':[]}},
])
def test_per_recipient_validation_rejects_unknown_duplicate_and_empty_active_subscriptions(fields):
    with pytest.raises(ValidationError):
        NotificationSettings(enabled=True,recipients=('one@example.com','two@example.com'),**fields)


def test_recipient_trade_routing_override_disable_and_test_send(repository,rule):
    worker,_,_,sent=service(repository)
    enable(worker,events=('DAILY_SUMMARY',),recipient_events={
        'one@example.com':('POSITION_OPENED',),'two@example.com':()})
    open_position(repository,rule,1);worker.collect()
    assert [(q['recipient'],q['event']) for q in queue(worker)]==[('one@example.com','POSITION_OPENED')]
    asyncio.run(worker.deliver_once());assert len(sent)==1
    # Explicit test is labeled as such and authorized to all saved recipients.
    worker.queue_test()
    assert {q['recipient'] for q in queue(worker) if q['event']=='TEST'}=={'one@example.com','two@example.com'}
    # Preference changes cancel old pending sends, and unsubscribed recipients stay excluded.
    enable(worker,events=('DAILY_SUMMARY',),recipient_events={
        'one@example.com':(),'two@example.com':('POSITION_OPENED',)})
    open_position(repository,rule,2);worker.collect()
    pending=[q for q in queue(worker) if q['status']=='PENDING']
    assert [(q['recipient'],q['event']) for q in pending]==[('two@example.com','POSITION_OPENED')]


@pytest.mark.parametrize('event',['DAILY_SUMMARY','PERIODIC_SUMMARY','CPU_HIGH'])
def test_collect_uses_effective_recipient_union_not_global_defaults(repository,event):
    worker,now,_,_=service(repository)
    enable(worker,events=(),recipient_events={'one@example.com':(event,),'two@example.com':()})
    if event=='CPU_HIGH':
        resources=FakeResources(now);resources.values['cpu']=99;worker.resources=resources
        worker.collect();resource_tick(worker,now,62)
    else:
        worker.collect();now[0]+=timedelta(days=1);worker.collect()
    assert [(q['recipient'],q['event']) for q in queue(worker)]==[('one@example.com',event)]


def test_delivery_checks_recipient_specific_subscription_even_for_existing_queue(repository):
    worker,_,_,sent=service(repository)
    settings=enable(worker,events=('SYSTEM_FAULT',))
    with worker.database.transaction() as conn:
        items=[];state={}
        worker.enqueue(items,state,settings,'SYSTEM_FAULT','old','fault','fake')
        save(conn,QUEUE_KEY,items)
        settings=NotificationSettings(enabled=True,recipients=settings.recipients,events=(),
            recipient_events={'one@example.com':(),'two@example.com':('SYSTEM_FAULT',)})
        save(conn,SETTINGS_KEY,{'revision':2,'settings':settings.model_dump(mode='json')})
    asyncio.run(worker.deliver_once());assert queue(worker)[0]['status']=='CANCELED' and not sent
    asyncio.run(worker.deliver_once());assert len(sent)==1 and sent[0]['recipient']=='two@example.com'
