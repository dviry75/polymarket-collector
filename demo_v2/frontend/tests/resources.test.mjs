import test from 'node:test';
import assert from 'node:assert/strict';
import {formatBytes,resourceState,resourcesHTML} from '../src/resources.js';
const now=Date.parse('2026-11-03T12:00:00Z');
const resource={state:'AVAILABLE',sampled_at:new Date(now).toISOString(),process:{cpu_percent:25,rss_bytes:64*1024*1024},host:{cpu_percent:10,ram_percent:30,ram_used_bytes:1024,ram_total_bytes:8192},network:{received_bytes_per_second:1024,sent_bytes_per_second:2048},disk:{percent:10},demo_storage:{total_bytes:4096}};
test('resource units are bytes and KiB/s, missing values are not zero',()=>{
  assert.equal(formatBytes(1024,true),'1 KiB/s');assert.equal(formatBytes(2**30),'1 GiB');
  assert.equal(formatBytes(0,true),'0 B/s');
  for(const value of [null,undefined,-1,NaN,Infinity,'1024'])assert.equal(formatBytes(value),'—');
});
test('sample expiry, unavailable data, invalid/future timestamp and failed health are explicit',()=>{
  assert.equal(resourceState(resource,now),'AVAILABLE');
  assert.equal(resourceState({...resource,state:'PARTIAL'},now),'PARTIAL');
  assert.equal(resourceState(resource,now+15001),'STALE');
  assert.equal(resourceState({...resource,sampled_at:'bad'},now),'STALE');
  assert.equal(resourceState(resource,now-1),'STALE');
  assert.equal(resourceState(resource,now,true),'UNAVAILABLE');
  assert.equal(resourceState(null,now),'UNAVAILABLE');
});
test('health cards show backend versus host scope, escape numeric inputs and hide stale values',()=>{
  const html=resourcesHTML(resource,now);
  assert.equal((html.match(/data-resource="/g)||[]).length,4);
  for(const text of ['25.0%','64 MiB','1 KiB/s','2 KiB/s','השרת כולו','תהליך DEMO','זו אינה מדידה נפרדת'])assert(html.includes(text));
  assert(!resourcesHTML(resource,now+15001).includes('64 MiB'));
  const malformed={...resource,process:{cpu_percent:'<img src=x onerror=alert(1)>',rss_bytes:'<script>'}};
  assert(!resourcesHTML(malformed,now).includes('<script>'));
  assert(!resourcesHTML(malformed,now).includes('onerror'));
});
