import test from 'node:test';
import assert from 'node:assert/strict';
import {createClient,ApiError,errorMessage} from '../src/api/client.js';
const json=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});

test('cookie auth/current and logout use in-memory CSRF, clear state after logout',async()=>{
  const calls=[];
  const client=createClient('/demo/api/v1',async(url,options)=>{calls.push({url,options});return json(url.endsWith('/logout')?{logged_out:true}:{user:'test',csrf_token:'fake-csrf'});});
  await client.login('test','fake-test-password');await client.current();await client.logout();
  assert.equal(calls[0].options.credentials,'include');assert.equal(calls[0].options.headers.get('X-Demo-CSRF'),null);
  assert.equal(calls[2].options.headers.get('X-Demo-CSRF'),'fake-csrf');assert.equal(calls[2].url,'/demo/api/v1/auth/logout');
  await assert.rejects(client.createRule({}),e=>e.status===403);
});
test('expired session notifies application, clears CSRF; invalid login does not expire other state',async()=>{
  let status=200,notified=0;const client=createClient(undefined,async()=>json(status===200?{user:'test',csrf_token:'fake'}:{detail:{code:'EXPIRED'}},status),()=>notified++);
  await client.current();status=401;
  await assert.rejects(client.rules(),e=>e.status===401);assert.equal(notified,1);
  await assert.rejects(client.login('test','bad'),e=>e.status===401);assert.equal(notified,1);
  await assert.rejects(client.reset('RESET_TRADING_DATA'),e=>e.code==='DEMO_CSRF_REQUIRED');
});
test('query strings preserve decimals and trade pagination is separate from KPI cohort',async()=>{
  const calls=[];const client=createClient('/demo/api/v1',async(url)=>{calls.push(url);return json({items:[]});});
  const cohort={asset:'ETH',timeframe:'1h',movement_min:'-0.00000001',from_date:'2026-01-01T22:00:00.000Z'};
  await client.trades({...cohort,limit:'10',offset:'20'});await client.kpi(cohort);
  const trades=new URL(calls[0],'http://test'),kpi=new URL(calls[1],'http://test');
  trades.searchParams.delete('limit');trades.searchParams.delete('offset');assert.equal(trades.search,kpi.search);
  assert.equal(kpi.searchParams.get('movement_min'),'-0.00000001');
});
test('rule mutation routes/version compare and reset confirmation contracts are exact',async()=>{
  const calls=[];const client=createClient('/demo/api/v1',async(url,options)=>{calls.push({url,options});return json({user:'test',csrf_token:'fake'});});await client.current();
  await client.updateRule('rule/special',{entry_trigger:'0.74000000000000001'},'version-1');
  assert.equal(calls[1].url,'/demo/api/v1/rules/rule%2Fspecial');
  assert.deepEqual(JSON.parse(calls[1].options.body),{spec:{entry_trigger:'0.74000000000000001'},expected_version_id:'version-1'});
  for(const action of ['enable','disable','duplicate','archive'])await client.ruleAction('r1',action);
  await client.reset('RESET_TRADING_DATA');await client.reset('FACTORY_RESET_DEMO');
  assert.equal(calls.at(-2).url,'/demo/api/v1/maintenance/reset-trading-data');assert.equal(JSON.parse(calls.at(-1).options.body).confirmation,'FACTORY_RESET_DEMO');
});
test('API error, invalid JSON, network failure and cancellation never return sample data',async()=>{
  for(const [transport,code,status] of [
    [async()=>json({detail:{code:'DEMO_VALIDATION_ERROR'}},422),'DEMO_VALIDATION_ERROR',422],
    [async()=>new Response('<html>down</html>',{status:502}),'INVALID_API_RESPONSE',502],
    [async()=>{throw new TypeError('offline');},'NETWORK_ERROR',0]
  ]){const client=createClient(undefined,transport);await assert.rejects(client.rules(),error=>error instanceof ApiError&&error.code===code&&error.status===status&&errorMessage(error).length>0);}
  const controller=new AbortController();controller.abort();const client=createClient(undefined,async(_,options)=>{assert(options.signal.aborted);throw new Error('aborted');});
  await assert.rejects(client.trades({},controller.signal),e=>e.code==='REQUEST_ABORTED');
});
test('only public HTTP API bases accepted, no credentials or query secrets',()=>{
  for(const base of ['javascript:alert(1)','https://user:pass@host/api','//host/api','/demo/api/v1?token=secret'])assert.throws(()=>createClient(base));
  assert(createClient('https://demo.example/demo/api/v1'));
});
