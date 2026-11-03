"""DEMO-only opt-in mail, durable bounded queue and independent background worker.

Uses the existing DEMO metadata table; no schema or LIVE dependency. SMTP never
runs in the decision path. An uncertain SMTP result is not automatically resent.
"""
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate
import hashlib
import json
import math
import re
import smtplib
import ssl
from typing import Annotated, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from .domain.trading import FrozenModel, decimal, text
from .persistence.trade_serials import number as trade_serial_number


LOCAL = ZoneInfo('Asia/Jerusalem')
UTC = timezone.utc
EVENTS = {
    'SYSTEM_FAULT': 'תקלה מתמשכת במערכת',
    'SYSTEM_RECOVERY': 'התאוששות מתקלה',
    'DAILY_SUMMARY': 'סיכום יומי',
    'PERIODIC_SUMMARY': 'דוח מפורט תקופתי',
    'NETWORK_HIGH': 'תעבורת רשת גבוהה בשרת',
    'CPU_HIGH': 'עומס CPU גבוה בשרת',
    'RAM_HIGH': 'ניצול RAM גבוה בשרת',
    'DISK_HIGH': 'דיסק כמעט מלא',
    'POSITION_OPENED': 'פתיחת עסקה מדומה',
    'TAKE_PROFIT': 'סגירה ביעד רווח',
    'STOP_LOSS': 'סגירה בעצירת הפסד',
    'MARKET_RESOLUTION': 'סגירה בהכרעת שוק',
}
Event = Literal['SYSTEM_FAULT','SYSTEM_RECOVERY','DAILY_SUMMARY','PERIODIC_SUMMARY',
                'POSITION_OPENED','TAKE_PROFIT','STOP_LOSS','MARKET_RESOLUTION',
                'NETWORK_HIGH','CPU_HIGH','RAM_HIGH','DISK_HIGH']
SETTINGS_KEY = 'notification_settings'
STATE_KEY = 'notification_state'
QUEUE_KEY = 'notification_queue'
MAX_QUEUE = 500
TERMINAL = {'SENT','FAILED','UNKNOWN','CANCELED','EXPIRED'}


def mailbox(value):
    """Bare ASCII mailbox only; reject display names, lists and header injection."""
    value = value.strip()
    if len(value)>254 or not re.fullmatch(r'[A-Za-z0-9_+%.-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}', value):
        raise ValueError('כתובת מייל לא תקינה')
    local, domain = value.rsplit('@',1)
    if len(local)>64 or local.startswith('.') or local.endswith('.') or '..' in value or any(p.startswith('-') or p.endswith('-') for p in domain.split('.')):
        raise ValueError('כתובת מייל לא תקינה')
    return local+'@'+domain.lower()


class NotificationSettings(FrozenModel):
    enabled: bool = False
    recipients: Annotated[tuple[str,...],Field(max_length=10)] = ()
    events: tuple[Event,...] = ('SYSTEM_FAULT','SYSTEM_RECOVERY','DAILY_SUMMARY','PERIODIC_SUMMARY')
    recipient_events: dict[str,tuple[Event,...]] = Field(default_factory=dict,max_length=10)
    daily_time: str = '08:00'
    periodic_hours: Literal[1,3,6] = 3
    network_mib_per_second: Annotated[float,Field(ge=0.01,le=100000,allow_inf_nan=False)] = 10
    cpu_percent: Annotated[float,Field(ge=1,le=100,allow_inf_nan=False)] = 90
    ram_percent: Annotated[float,Field(ge=1,le=100,allow_inf_nan=False)] = 90
    disk_percent: Annotated[float,Field(ge=1,le=100,allow_inf_nan=False)] = 90
    resource_duration_seconds: Annotated[int,Field(ge=10,le=3600)] = 60

    @field_validator('recipients')
    @classmethod
    def addresses(cls, values):
        cleaned=tuple(mailbox(v) for v in values)
        if len({v.lower() for v in cleaned})!=len(cleaned):
            raise ValueError('כתובת נמען כפולה')
        return cleaned

    @field_validator('recipient_events')
    @classmethod
    def subscriptions(cls, values):
        cleaned={}
        for address,events in values.items():
            key=mailbox(address).lower()
            if key in cleaned:raise ValueError('בחירת התראות כפולה לנמען')
            if len(set(events))!=len(events):raise ValueError('סוג התראה כפול לנמען')
            cleaned[key]=events
        return cleaned

    def events_for(self,recipient):
        return self.recipient_events.get(recipient.lower(),self.events)

    def subscribed(self,event):
        return any(event in self.events_for(recipient) for recipient in self.recipients)

    def allows(self,recipient,event):
        return recipient.lower() in {r.lower() for r in self.recipients} and event in self.events_for(recipient)

    @field_validator('daily_time')
    @classmethod
    def schedule(cls, value):
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):
            raise ValueError('נדרשת שעה בפורמט HH:mm')
        return value

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.events))!=len(self.events):
            raise ValueError('סוג התראה כפול')
        if set(self.recipient_events)-{r.lower() for r in self.recipients}:
            raise ValueError('בחירת התראות לכתובת שאינה ברשימת הנמענים')
        if self.enabled and (not self.recipients or not any(self.events_for(r) for r in self.recipients)):
            raise ValueError('להפעלה יש לבחור לפחות נמען אחד וסוג התראה אחד')
        return self


@dataclass(frozen=True)
class MailConfig:
    enabled: bool = False
    host: str = 'smtp-relay.gmail.com'
    port: int = 587
    sender: str = 'demo@dvirtechnologies.com'
    username: str = field(default='',repr=False)
    password: str = field(default='',repr=False)
    security: str = 'STARTTLS'

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',self.host) or not 1<=self.port<=65535:
            raise ValueError('Invalid DEMO mail host/port')
        mailbox(self.sender)
        if self.security not in ('STARTTLS','SSL'):
            raise ValueError('DEMO email requires STARTTLS or SSL')
        if bool(self.username)!=bool(self.password):
            raise ValueError('DEMO mail username and password must be supplied together')

    @classmethod
    def from_env(cls, env):
        return cls(enabled=env.get('DEMO_EMAIL_ENABLED','false').lower()=='true',
            host=env.get('DEMO_SMTP_HOST','smtp-relay.gmail.com'),
            port=int(env.get('DEMO_SMTP_PORT','587')),
            sender=env.get('DEMO_EMAIL_FROM','demo@dvirtechnologies.com'),
            username=env.get('DEMO_SMTP_USERNAME',''),password=env.get('DEMO_SMTP_PASSWORD',''),
            security=env.get('DEMO_SMTP_SECURITY','STARTTLS'))

    def send(self, item):
        message=EmailMessage()
        message['From']=self.sender
        message['To']=item['recipient']  # One recipient per envelope; no address leakage.
        message['Subject']='[Polymarket DEMO] '+item['subject']
        message['Date']=formatdate(localtime=False)
        message['Message-ID']='<'+item['id']+'@'+self.sender.rsplit('@',1)[1]+'>'
        message.set_content(item['body'])
        context=ssl.create_default_context()
        client=None
        try:
            if self.security=='SSL':
                client=smtplib.SMTP_SSL(self.host,self.port,timeout=10,context=context,local_hostname=self.sender.rsplit('@',1)[1])
            else:
                client=smtplib.SMTP(self.host,self.port,timeout=10,local_hostname=self.sender.rsplit('@',1)[1])
                client.ehlo();client.starttls(context=context);client.ehlo()
            if self.username:
                client.login(self.username,self.password)
            client.send_message(message,from_addr=self.sender,to_addrs=[item['recipient']])
        finally:
            # A failed QUIT after accepted DATA must never cause a duplicate retry.
            if client is not None:
                with suppress(Exception):client.quit()
                with suppress(Exception):client.close()


def load(conn,key,default):
    row=conn.execute('SELECT value FROM demo_v2_meta WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default


def save(conn,key,value):
    conn.execute('INSERT OR REPLACE INTO demo_v2_meta (key,value) VALUES (?,?)',
                 (key,json.dumps(value,ensure_ascii=False,separators=(',',':'))))


def latest_ledger(conn):
    row=conn.execute('SELECT rowid,id FROM demo_v2_ledger ORDER BY rowid DESC LIMIT 1').fetchone()
    return [row[0],row[1]] if row else [0,'']


def periodic_boundary(now,hours):
    local=now.astimezone(LOCAL)
    return local.replace(hour=(local.hour//hours)*hours,minute=0,second=0,microsecond=0).astimezone(UTC)


def daily_boundary(now,clock):
    h,m=map(int,clock.split(':'))
    local=now.astimezone(LOCAL)
    end=local.replace(hour=h,minute=m,second=0,microsecond=0)
    if local<end:end-=timedelta(days=1)
    return end.astimezone(UTC)


def reset_trading_notifications(conn):
    """Cancel queued research content in the same transaction as a data reset."""
    queue=[item for item in load(conn,QUEUE_KEY,[])
           if item['event'] in ('TEST','SYSTEM_FAULT','SYSTEM_RECOVERY','NETWORK_HIGH','CPU_HIGH','RAM_HIGH','DISK_HIGH')]
    save(conn,QUEUE_KEY,queue)
    state=load(conn,STATE_KEY,{})
    state['cursor']=[0,'']
    save(conn,STATE_KEY,state)


class Notifications:
    def __init__(self,repository,health,config,*,transport=None,resources=None,clock=lambda:datetime.now(UTC)):
        self.repository,self.database,self.health=repository,repository.database,health
        self.config,self.transport,self.clock=config,transport or config.send,clock
        self.resources=resources
        self.task=None
        self.stop=asyncio.Event()
        self.runtime_error=''
        self.send_lock=asyncio.Lock()

    def settings(self,conn):
        record=load(conn,SETTINGS_KEY,{'revision':0,'settings':NotificationSettings().model_dump(mode='json')})
        return record,NotificationSettings.model_validate(record['settings'])

    def view(self):
        with self.database.connect() as conn:
            record,settings=self.settings(conn)
            queue=load(conn,QUEUE_KEY,[])
            state=load(conn,STATE_KEY,{})
        return {**record,'settings':settings.model_dump(mode='json'),'sender':self.config.sender,'timezone':'Asia/Jerusalem',
            'transport':{'state':'CONFIGURED' if self.config.enabled else 'DISABLED',
                         'reason':'' if self.config.enabled else 'DEMO_EMAIL_ENABLED_FALSE'},
            'catalog':[{'code':key,'label':value} for key,value in EVENTS.items()],
            'queue':{'pending':sum(q['status'] in ('PENDING','SENDING') for q in queue),
                     'dropped':state.get('dropped',0),'worker_error':self.runtime_error},
            'deliveries':[{k:v for k,v in item.items() if k in ('id','recipient','event','status','attempts','created_at','sent_at','error')}
                          for item in queue[-30:]][::-1]}

    def update(self,settings,expected_revision):
        now=self.clock()
        with self.database.transaction() as conn:
            record,_=self.settings(conn)
            if record['revision']!=expected_revision:
                raise ValueError('הגדרות ההתראות השתנו; יש לרענן')
            save(conn,SETTINGS_KEY,{'revision':record['revision']+1,'settings':settings.model_dump(mode='json')})
            # Preferences apply from this point; never backfill old trades/reports.
            state=load(conn,STATE_KEY,{})
            state.update(cursor=latest_ledger(conn),health={},resources={},
                periodic_at=periodic_boundary(now,settings.periodic_hours).isoformat(),
                daily_at=daily_boundary(now,settings.daily_time).isoformat())
            save(conn,STATE_KEY,state)
            queue=load(conn,QUEUE_KEY,[])
            for q in queue:
                if q['status']=='PENDING':q['status']='CANCELED'
            save(conn,QUEUE_KEY,queue)
        return self.view()

    def enqueue(self,queue,state,settings,event,event_id,subject,body,*,test=False):
        if not settings.enabled or (not settings.subscribed(event) and not test):return
        now=self.clock().isoformat()
        for recipient in settings.recipients:
            if not test and not settings.allows(recipient,event):continue
            key=hashlib.sha256((event_id+'\0'+recipient.lower()).encode()).hexdigest()
            if any(q['id']==key for q in queue):continue
            while len(queue)>=MAX_QUEUE:
                terminal=next((i for i,q in enumerate(queue) if q['status'] in TERMINAL),None)
                if terminal is None:break
                queue.pop(terminal)
            if len(queue)>=MAX_QUEUE:
                state['dropped']=state.get('dropped',0)+1
                continue
            queue.append({'id':key,'recipient':recipient,'event':event,'subject':subject,
                'body':body[:20000]+'\n\nPolymarket DEMO — מסחר מדומה בלבד. כל השעות לפי Asia/Jerusalem.',
                'created_at':now,'next_at':now,'attempts':0,'status':'PENDING','error':''})

    def queue_test(self):
        if not self.config.enabled:raise ValueError('שליחת המיילים עדיין לא הוגדרה בשרת DEMO')
        with self.database.transaction() as conn:
            _,settings=self.settings(conn)
            if not settings.enabled:raise ValueError('יש לשמור נמענים ולהפעיל את ההתראות לפני בדיקה')
            state=load(conn,STATE_KEY,{})
            now=self.clock()
            last=state.get('last_test')
            if last and (now-datetime.fromisoformat(last)).total_seconds()<60:
                raise ValueError('יש להמתין דקה בין הודעות בדיקה')
            queue=load(conn,QUEUE_KEY,[])
            self.enqueue(queue,state,settings,'TEST',uuid4().hex,'בדיקת התראות DEMO',
                         'זוהי הודעת בדיקה שנשלחה בעקבות בקשה מפורשת מתוך הגדרות DEMO.',test=True)
            state['last_test']=now.isoformat()
            save(conn,STATE_KEY,state);save(conn,QUEUE_KEY,queue)
        return self.view()

    def trade_text(self,conn,position):
        rule=conn.execute('SELECT snapshot_json FROM demo_v2_rule_versions WHERE id=?',(position['rule_version_id'],)).fetchone()
        rule_name=json.loads(rule[0])['name'] if rule else position['rule_id']
        market=conn.execute('SELECT definition_id FROM demo_v2_instances WHERE id=?',(position['instance_id'],)).fetchone()
        def stamp(value):return datetime.fromisoformat(value).astimezone(LOCAL).strftime('%d/%m/%Y %H:%M:%S') if value else '—'
        return '\n'.join((f"עסקת DEMO #{trade_serial_number(conn,position['id'])} | מזהה: {position['id']}",f"חוק: {rule_name}",
            f"שוק: {market[0] if market else position['instance_id']} | צד: {position['side']}",
            f"כניסה: {stamp(position['entry_at'])} | מחיר {position['entry_price_text']} | מניות {position['net_shares_text']}",
            f"סגירה: {stamp(position['exit_at'])} | מחיר {position['exit_price_text'] or '—'} | סיבה {EVENTS.get(position['exit_reason'],'פתוחה')}",
            f"רווח/הפסד נקי: {position['net_pnl_text'] or '—'} | עמלת קנייה: {position['buy_fee_text']} | עמלת מכירה: {position['sell_fee_text'] or '0'}"))

    def summary(self,conn,start,end):
        rows=[dict(r) for r in conn.execute('''SELECT * FROM demo_v2_positions
            WHERE (entry_at>=? AND entry_at<?) OR (exit_at>=? AND exit_at<?)
            ORDER BY entry_at,id''',(start.isoformat(),end.isoformat(),start.isoformat(),end.isoformat()))]
        open_count=conn.execute("SELECT COUNT(*) FROM demo_v2_positions WHERE status='OPEN'").fetchone()[0]
        inside=lambda value:bool(value and start<=datetime.fromisoformat(value)<end)
        entered=[p for p in rows if inside(p['entry_at'])]
        closed=[p for p in rows if inside(p['exit_at'])]
        pnl=sum((decimal(p['net_pnl_text']) for p in closed),Decimal(0))
        fees=sum((decimal(p['buy_fee_text']) for p in entered),Decimal(0))+sum((decimal(p['sell_fee_text'] or '0') for p in closed),Decimal(0))
        details=[p for p in rows if inside(p['entry_at']) or inside(p['exit_at'])]
        lines=[f"תקופה: {start.astimezone(LOCAL):%d/%m/%Y %H:%M %z} עד {end.astimezone(LOCAL):%d/%m/%Y %H:%M %z} (סיום לא כולל)",
            f"נפתחו: {len(entered)} | נסגרו: {len(closed)} | פתוחות כעת: {open_count}",
            f"רווח/הפסד ממומש של הסגירות בתקופה: {text(pnl)} | עמלות פעולות בתקופה: {text(fees)}",
            'פירוט עסקאות (עד 30):']
        lines.extend(self.trade_text(conn,p) for p in details[:30])
        if len(details)>30:lines.append(f'ועוד {len(details)-30} עסקאות; הפירוט המלא בממשק DEMO.')
        if not details:lines.append('לא היו פתיחות או סגירות בתקופה.')
        snapshot=self.health.snapshot()
        lines.append('מצב המערכת כעת: '+snapshot.state.value)
        lines.extend(c.component+': '+c.state.value+(' ('+c.reason+')' if c.reason else '')
                     for c in snapshot.components if ':' not in c.component)
        return '\n\n'.join(lines)

    def collect(self):
        now=self.clock()
        with self.database.transaction() as conn:
            _,settings=self.settings(conn)
            state=load(conn,STATE_KEY,{})
            queue=load(conn,QUEUE_KEY,[])
            original_state=json.dumps(state,sort_keys=True)
            original_queue=json.dumps(queue,sort_keys=True)
            if not self.config.enabled or not settings.enabled:
                state.update(cursor=latest_ledger(conn),health={},resources={},
                    periodic_at=periodic_boundary(now,settings.periodic_hours).isoformat(),
                    daily_at=daily_boundary(now,settings.daily_time).isoformat())
            else:
                cursor=state.get('cursor')
                if cursor is None:cursor=latest_ledger(conn)
                if cursor[0]:
                    current=conn.execute('SELECT id FROM demo_v2_ledger WHERE rowid=?',(cursor[0],)).fetchone()
                    if not current or current[0]!=cursor[1]:cursor=[0,'']
                rows=conn.execute('SELECT rowid,* FROM demo_v2_ledger WHERE rowid>? ORDER BY rowid LIMIT 100',(cursor[0],)).fetchall()
                for row in rows:
                    p=conn.execute('SELECT * FROM demo_v2_positions WHERE id=?',(row['position_id'],)).fetchone()
                    if p:
                        event='POSITION_OPENED' if row['movement_type']=='ENTRY' else p['exit_reason']
                        if event in EVENTS:self.enqueue(queue,state,settings,event,'ledger:'+row['id'],EVENTS[event],self.trade_text(conn,dict(p)))
                    cursor=[row['rowid'],row['id']]
                state['cursor']=cursor
                self.collect_health(queue,state,settings,now)
                self.collect_resources(queue,state,settings,now)
                for kind,end,span in [('PERIODIC_SUMMARY',periodic_boundary(now,settings.periodic_hours),timedelta(hours=settings.periodic_hours)),
                                      ('DAILY_SUMMARY',daily_boundary(now,settings.daily_time),None)]:
                    key='periodic_at' if kind=='PERIODIC_SUMMARY' else 'daily_at'
                    previous=state.get(key)
                    if previous and end>datetime.fromisoformat(previous) and settings.subscribed(kind):
                        # Three/six-hour wall-clock blocks may span a DST change.
                        start=((end.astimezone(LOCAL)-span).astimezone(UTC) if settings.periodic_hours!=1 else end-span) if span else (end.astimezone(LOCAL)-timedelta(days=1)).astimezone(UTC)
                        self.enqueue(queue,state,settings,kind,kind+':'+end.isoformat(),EVENTS[kind],self.summary(conn,start,end))
                    state[key]=end.isoformat()
            for q in queue:
                if q['status']=='PENDING' and now-datetime.fromisoformat(q['created_at'])>timedelta(hours=24):q['status']='EXPIRED'
            if json.dumps(state,sort_keys=True)!=original_state:save(conn,STATE_KEY,state)
            if json.dumps(queue,sort_keys=True)!=original_queue:save(conn,QUEUE_KEY,queue)

    def collect_health(self,queue,state,settings,now):
        observed=state.setdefault('health',{})
        for component in self.health.snapshot().components:
            if ':' in component.component or component.component in ('auth',):continue
            key=component.component
            previous=observed.get(key)
            bad=component.state.value!='READY'
            if not previous:
                observed[key]={'bad':bad,'since':now.isoformat(),'notified':False}
                continue
            if previous['bad']!=bad:
                observed[key]={'bad':bad,'since':now.isoformat(),'notified':previous['notified']}
                previous=observed[key]
            age=(now-datetime.fromisoformat(previous['since'])).total_seconds()
            if bad and age>=30 and not previous['notified']:
                self.enqueue(queue,state,settings,'SYSTEM_FAULT','health:'+key+':'+previous['since'],
                    'תקלה: '+key,key+'\nמצב: '+component.state.value+'\nסיבה: '+component.reason+'\nהתקלה נמשכה לפחות 30 שניות.')
                previous['notified']=True
            if not bad and age>=10 and previous['notified']:
                self.enqueue(queue,state,settings,'SYSTEM_RECOVERY','recovery:'+key+':'+previous['since'],
                    'התאוששות: '+key,key+' חזר למצב תקין למשך לפחות 10 שניות.')
                previous['notified']=False

    def collect_resources(self,queue,state,settings,now):
        """Observe existing host telemetry only; missing/stale samples break continuity."""
        observed=state.setdefault('resources',{})
        snapshot=self.resources.snapshot() if self.resources else {}
        try:
            stamp=snapshot.get('sampled_at')
            if isinstance(stamp,str):stamp=datetime.fromisoformat(stamp)
            fresh=(snapshot.get('state') in ('AVAILABLE','PARTIAL') and stamp is not None
                   and 0<=(now-stamp).total_seconds()<=15)
        except (ValueError,TypeError):fresh=False
        if not fresh:
            for item in observed.values():item.update(since=None,recovery_since=None)
            return
        def number(value):
            return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0
        network=snapshot.get('network',{})
        rx,tx=network.get('received_bytes_per_second'),network.get('sent_bytes_per_second')
        values=[('NETWORK_HIGH',(rx+tx)/1048576 if number(rx) and number(tx) else None,settings.network_mib_per_second,'MiB/s (קבלה + שליחה)'),
                ('CPU_HIGH',snapshot.get('host',{}).get('cpu_percent'),settings.cpu_percent,'%'),
                ('RAM_HIGH',snapshot.get('host',{}).get('ram_percent'),settings.ram_percent,'%'),
                ('DISK_HIGH',snapshot.get('disk',{}).get('percent'),settings.disk_percent,'%')]
        for event,value,threshold,unit in values:
            if not settings.subscribed(event):
                observed.pop(event,None)
                continue
            item=observed.setdefault(event,{'notified':False,'since':None,'recovery_since':None})
            if not number(value):
                item.update(since=None,recovery_since=None)
                continue
            previous=item.get('last_sample')
            if previous and stamp.isoformat()==previous:continue
            if previous and (stamp-datetime.fromisoformat(previous)).total_seconds()>15:
                item.update(since=None,recovery_since=None)
            item['last_sample']=stamp.isoformat()
            if value>=threshold:
                item['recovery_since']=None
                if not item['since']:item['since']=stamp.isoformat()
                age=(stamp-datetime.fromisoformat(item['since'])).total_seconds()
                if age>=settings.resource_duration_seconds and not item['notified']:
                    self.enqueue(queue,state,settings,event,event+':'+item['since'],EVENTS[event],
                        f"{EVENTS[event]}\nמדידה: {value:.2f} {unit} | סף: {threshold:g} {unit}\nמשך החריגה: {age:.0f} שניות.\nהמדד מתייחס לשרת כולו; תעבורת הרשת כוללת תוכנות אחרות וממשקים וירטואליים, ללא loopback.")
                    item['notified']=True
            else:
                item['since']=None
                if value<threshold*0.8:
                    if not item['recovery_since']:item['recovery_since']=stamp.isoformat()
                    if (stamp-datetime.fromisoformat(item['recovery_since'])).total_seconds()>=120:
                        item['notified']=False
                else:item['recovery_since']=None

    async def deliver_once(self):
        if not self.config.enabled:return
        async with self.send_lock:
            now=self.clock()
            with self.database.transaction() as conn:
                _,settings=self.settings(conn)
                if not settings.enabled:return
                queue=load(conn,QUEUE_KEY,[])
                item=next((q for q in queue if q['status']=='PENDING' and datetime.fromisoformat(q['next_at'])<=now),None)
                if not item:return
                if item['recipient'].lower() not in {r.lower() for r in settings.recipients} or (item['event']!='TEST' and not settings.allows(item['recipient'],item['event'])):
                    item['status']='CANCELED';save(conn,QUEUE_KEY,queue);return
                item['status']='SENDING';item['attempts']+=1
                save(conn,QUEUE_KEY,queue)
                item=dict(item)
            status,error='SENT',''
            try:
                await asyncio.to_thread(self.transport,item)
            except (smtplib.SMTPAuthenticationError,smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused):
                status,error='FAILED','SMTP_REJECTED'
            except (smtplib.SMTPConnectError,ConnectionRefusedError):
                status,error=('PENDING' if item['attempts']<3 else 'FAILED'),'SMTP_CONNECT_FAILED'
            except Exception:
                # May have been accepted remotely. Do not guess or duplicate.
                status,error='UNKNOWN','SMTP_RESULT_UNCERTAIN'
            with self.database.transaction() as conn:
                queue=load(conn,QUEUE_KEY,[])
                saved=next((q for q in queue if q['id']==item['id']),None)
                if saved:
                    saved.update(status=status,error=error)
                    if status=='SENT':saved['sent_at']=self.clock().isoformat()
                    if status=='PENDING':saved['next_at']=(self.clock()+timedelta(seconds=60*item['attempts'])).isoformat()
                    save(conn,QUEUE_KEY,queue)

    async def start(self):
        with self.database.transaction() as conn:
            state=load(conn,STATE_KEY,{})
            # Keep incident deduplication, but never count downtime as continuity.
            for item in state.get('resources',{}).values():
                item.update(since=None,recovery_since=None,last_sample=None)
            save(conn,STATE_KEY,state)
            queue=load(conn,QUEUE_KEY,[])
            for q in queue:
                if q['status']=='SENDING':q.update(status='UNKNOWN',error='INTERRUPTED_SEND')
            save(conn,QUEUE_KEY,queue)
        self.task=asyncio.create_task(self.run(),name='demo-notifications')

    async def run(self):
        while not self.stop.is_set():
            try:
                self.collect()
                await self.deliver_once()
                self.runtime_error=''
            except asyncio.CancelledError:raise
            except Exception:self.runtime_error='NOTIFICATION_WORKER_ERROR'
            try:await asyncio.wait_for(self.stop.wait(),timeout=1)
            except asyncio.TimeoutError:pass

    async def close(self):
        self.stop.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
