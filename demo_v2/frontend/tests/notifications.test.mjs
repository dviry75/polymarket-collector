import test from 'node:test';
import assert from 'node:assert/strict';
import {notificationStatus} from '../src/notifications.js';
import {createClient} from '../src/api/client.js';

test('notification delivery UI escapes remote strings and separates provider acceptance from inbox delivery',()=>{
  const html=notificationStatus({sender:'demo@example.com',transport:{state:'CONFIGURED'},queue:{pending:1,dropped:0},catalog:[],deliveries:[{recipient:'<img src=x onerror=alert(1)>',event:'<script>',status:'UNKNOWN',attempts:1}]});
  assert(!html.includes('<img'));assert(!html.includes('<script>'));assert(html.includes('&lt;img'));
  assert(html.includes('לא נשלח שוב אוטומטית'));assert(html.includes('מסירה לנמען דורשת בדיקת מייל'));
});

test('notification settings and test send use authenticated versioned API and CSRF',async()=>{
  const calls=[];
  const api=createClient('/demo/api/v1',async(url,options)=>{calls.push({url,options});return new Response(JSON.stringify({user:'fake',csrf_token:'fake-test-csrf'}));});
  await assert.rejects(api.testNotifications(),e=>e.status===403);
  await api.current();await api.notifications();
  const settings={enabled:true,recipients:['one@example.com'],events:['PERIODIC_SUMMARY'],daily_time:'08:00',periodic_hours:6};
  await api.saveNotifications(settings,4);await api.testNotifications();
  assert.equal(calls[1].url,'/demo/api/v1/notifications');
  assert.deepEqual(JSON.parse(calls[2].options.body),{settings,expected_revision:4});
  assert.equal(calls[2].options.headers.get('X-Demo-CSRF'),'fake-test-csrf');
  assert.equal(calls[3].url,'/demo/api/v1/notifications/test');assert.equal(calls[3].options.method,'POST');
});
