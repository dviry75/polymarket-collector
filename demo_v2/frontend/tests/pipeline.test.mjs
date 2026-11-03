import test from 'node:test';
import assert from 'node:assert/strict';
import {pipelineHTML} from '../src/pipeline.js';
test('pipeline distinguishes unavailable, fenced and healthy streams without unsafe markup',()=>{
  assert.equal(pipelineHTML(null),'');assert.equal(pipelineHTML({},true),'');
  const html=pipelineHTML({owner_alive:false,loop_lag_seconds:.02,streams:{'5m':{depth:4,capacity:1024,published:10,delivered:6,oldest_seconds:.1,reason:'<script>bad</script>'}}});
  assert.match(html,/20.0 ms/);assert.match(html,/4 \/ 1,024/);assert.match(html,/אינו זמין/);
  assert.ok(!html.includes('<script>'));assert.match(html,/&lt;script&gt;/);
});
