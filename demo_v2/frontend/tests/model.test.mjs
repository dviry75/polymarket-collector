import test from 'node:test';
import assert from 'node:assert/strict';
import {freshRule,mapRule,rulePayload,validateRule,mapTrade,cohortQuery,jerusalemDayStart,formatDecimal,formatMoney,compareDecimal,marketState,healthState} from '../src/model.js';

test('decimal display rounds exactly without losing huge values or raw precision',()=>{
  const raw='9007199254740993.739999999999';
  assert.equal(formatDecimal(raw,2),'9007199254740993.74');
  assert.equal(formatMoney('-3.70000001',true),'−$3.70');
  assert.equal(formatMoney('0.005',true),'+$0.01');
  assert.equal(formatMoney(null),'—');
  assert.equal(compareDecimal('0.73999999999999999999','0.74'),-1);
  assert.equal(formatDecimal('NaN'),'—');assert.equal(raw,'9007199254740993.739999999999');
});
test('complete rule payload roundtrip preserves decimal strings, shared windows and cross-midnight weekdays',()=>{
  const r={...freshRule(),name:'  exact name  ',markets:['BTC 5m','HYPE 1h','DOGE 15m'],entry:'0.740000000000000001',shares:'5.0000000000000001',frequency:2,tpOn:true,tp:'1',slOn:true,sl:'0',
    schedule:[{day:0,start:'22:00:15',end:'02:00:30'},{day:1,start:'08:00',end:'11:00'}],windows:[{start:0,end:60},{start:120,end:240}],conditions:[{measure:'Percentage',direction:'Negative',op:'Between',min:'0.2',max:'0.2'},{measure:'Price Amount',direction:'Either / Absolute',op:'Less Than',min:'100.00000001',max:''}]};
  assert.deepEqual(validateRule(r),[]);
  const payload=rulePayload(r);
  assert.deepEqual(payload.markets,['BTC-5m','HYPE-1h','DOGE-15m']);
  assert.equal(payload.weekly_schedule[0].weekday,6);assert.equal(payload.weekly_schedule[1].weekday,0);
  assert.equal(payload.take_profit,'1');assert.equal(payload.stop_loss,'0');assert.equal(payload.gross_shares,r.shares);
  const view=mapRule({id:'r1',current_version_id:'v2',spec:payload,trade_count:12,wallet:'-1.2',net_pnl:'2.5'});
  assert.equal(view.versionId,'v2');assert.equal(view.schedule[0].day,0);assert.equal(view.wallet,'-1.2');assert.equal(view.pnl,'2.5');
  assert.deepEqual(rulePayload(view),payload);
});
test('rule validation rejects invalid boundaries, keeps TP=1 SL=0 and equal BETWEEN bounds',()=>{
  const r={...freshRule(),name:'test'};
  assert.deepEqual(validateRule(r),[]);
  for(const patch of [{entry:'1'},{entry:'0'},{shares:'4.99999'},{frequency:1.1},{markets:[]},{windows:[{start:20,end:10}]},{schedule:[{day:1,start:'12:00',end:'12:00'}]},{conditions:[{measure:'Percentage',direction:'Positive',op:'Between',min:'0.3',max:'0.2'}]}])assert(validateRule({...r,...patch}).length);
});
test('shared trade/KPI cohort uses API keys and inclusive Jerusalem calendar days',()=>{
  const query=cohortQuery({rule:'r1',asset:'BTC',tf:'5m',side:'DOWN',status:'CLOSED',from:'2026-01-02',to:'2026-01-02',measure:'amount',min:'-0.00000001',max:'10'});
  assert.deepEqual(query,{rule_id:'r1',asset:'BTC',timeframe:'5m',side:'DOWN',status:'CLOSED',movement_min:'-0.00000001',movement_max:'10',from_date:'2026-01-01T22:00:00.000Z',to_date:'2026-01-02T22:00:00.000Z',movement_measurement:'ABSOLUTE'});
  assert.equal(cohortQuery({market:'HYPE 1h',asset:'BTC'}).asset,'HYPE');
});
test('Jerusalem ranges use IANA DST and work when browser timezone differs',()=>{
  assert.equal(jerusalemDayStart('2026-07-01'),'2026-06-30T21:00:00.000Z');
  assert.equal(jerusalemDayStart('2026-03-27'),'2026-03-26T22:00:00.000Z');
  assert.equal(cohortQuery({from:'2026-03-27',to:'2026-03-27'}).to_date,'2026-03-27T21:00:00.000Z');
  assert.equal(jerusalemDayStart('2026-10-25'),'2026-10-24T21:00:00.000Z');
  assert.equal(cohortQuery({to:'2026-10-25'}).to_date,'2026-10-25T22:00:00.000Z');
  assert.throws(()=>jerusalemDayStart('2026-02-30'));
});
test('trade mapping retains raw gross/net/fees and missing underlying is unavailable',()=>{
  const row={id:'p1',rule_id:'r1',asset:'ETH',timeframe:'1h',entry_price:'0.74',gross_shares:'5',net_shares:'4.99',buy_fee:'0.01',sell_fee:null,total_fees:'0.01',net_pnl:null,entry_provenance:{},status:'OPEN'};
  const value=mapTrade(row,[{id:'r1',name:'<img onerror=alert(1)>'}]);
  assert.equal(value.raw,row);assert.equal(value.entry,'0.74');assert.equal(value.underlyingOpen,null);assert.equal(value.pnl,null);
  const detail=mapTrade({...row,asset:undefined,timeframe:undefined,market:{definition_id:'ETH-1h'},rule_snapshot:{name:'Entry-time name'}});
  assert.equal(detail.asset,'ETH');assert.equal(detail.tf,'1h');assert.equal(detail.ruleName,'Entry-time name');
});
test('backend market freshness can expire locally; no fresh/listening inference from quotes',()=>{
  const now=Date.now();const snapshot={fresh:true,listening:true,observed_at:new Date(now).toISOString()};
  assert.equal(marketState(snapshot,now),'FRESH');assert.equal(marketState(snapshot,now+5001),'STALE');
  assert.equal(marketState({...snapshot,listening:false},now),'NOT_LISTENING');
  assert.equal(marketState({...snapshot,fresh:false},now),'STALE');assert.equal(marketState(null),'UNAVAILABLE');
});
test('health preserves all server states and missing state never becomes READY',()=>{
  for(const state of ['READY','DEGRADED','NOT_READY'])assert.equal(healthState({state}),state);
  assert.equal(healthState({status:200}),'UNKNOWN');assert.equal(healthState(null),'UNKNOWN');
});
