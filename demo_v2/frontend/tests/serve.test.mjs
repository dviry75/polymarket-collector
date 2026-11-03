import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer,request} from 'node:http';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,mkdir,copyFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

function get(port,path,headers={}){
  return new Promise((resolve,reject)=>{
    const req=request({hostname:'127.0.0.1',port,path,headers,timeout:3000},res=>{
      let body='';res.setEncoding('utf8');res.on('data',v=>body+=v);
      res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body}));
    });
    req.on('timeout',()=>req.destroy(new Error('request timeout')));
    req.on('error',reject);req.end();
  });
}

async function preview(t,built){
  const root=await mkdtemp(join(tmpdir(),'demo-preview-test-'));
  t.after(()=>rm(root,{recursive:true,force:true}));
  for(const dir of ['scripts','src','build/src'])await mkdir(join(root,dir),{recursive:true});
  await copyFile(new URL('../scripts/serve.mjs',import.meta.url),join(root,'scripts/serve.mjs'));
  const contentRoot=built?join(root,'build'):root;
  await writeFile(join(contentRoot,'index.html'),'<h1>temporary preview</h1>');
  await writeFile(join(contentRoot,'src/config.js'),'export const API_BASE="default";');
  await writeFile(join(contentRoot,'literal%.js'),'export const percent=true;');
  const backend=createServer((req,res)=>{res.writeHead(201,{'Content-Type':'application/json'});res.end(JSON.stringify({url:req.url,csrf:req.headers['x-demo-csrf'],cookie:req.headers.cookie}));});
  backend.listen(0,'127.0.0.1');await once(backend,'listening');
  t.after(()=>new Promise(resolve=>backend.close(resolve)));
  const child=spawn(process.execPath,[join(root,'scripts/serve.mjs'),...(built?['--built']:[])],{
    env:{DEMO_FRONTEND_PORT:'0',DEMO_BACKEND_TARGET:`http://127.0.0.1:${backend.address().port}`,DEMO_API_BASE:'/demo/api/v1'},
    stdio:['ignore','pipe','pipe']
  });
  let stderr='';child.stderr.on('data',chunk=>stderr+=chunk);
  t.after(async()=>{if(child.exitCode===null&&child.signalCode===null){const exit=once(child,'exit');child.kill('SIGTERM');await exit;}assert.equal(stderr,'');});
  const port=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{child.kill('SIGKILL');reject(new Error('preview startup timeout'));},5000);
    const onError=error=>{clearTimeout(timer);reject(error);};
    child.once('error',onError);child.once('exit',code=>onError(new Error('preview exited: '+code+' '+stderr)));
    let output='';child.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/loopback port (\d+)/);if(match){clearTimeout(timer);resolve(Number(match[1]));}});
  });
  return {port,child};
}

for(const built of [false,true])test(`malformed requests stay contained in ${built?'built':'source'} preview`,{timeout:20000},async t=>{
  const {port,child}=await preview(t,built);
  for(const path of ['/demo/%','/demo/%2','/demo/%GG','/demo/%C0%AF','/demo/%ED%A0%80','/demo/src/%','http://[::1/demo/']){
    const bad=await get(port,path);
    assert.equal(bad.status,400,path);assert.equal(bad.headers['cache-control'],'no-store');
    const good=await get(port,'/demo/');assert.equal(good.status,200);assert(good.body.includes('temporary preview'));
    assert.equal(child.exitCode,null);
  }
  assert.equal((await get(port,'/demo/index.html?q=%')).status,200);
  assert.equal((await get(port,'/demo/literal%25.js')).status,200);
  assert.equal((await get(port,'/demo/literal%2525.js')).status,404);
  for(const path of ['/demo/missing.js','/demo/%2e%2e%2fsecret.js','/demo/node_modules/x.js','/demo/private.env'])assert.equal((await get(port,path)).status,404,path);
  const redirect=await get(port,'/');assert.equal(redirect.status,302);assert.equal(redirect.headers.location,'/demo/');
  const config=await get(port,'/demo/src/config.js');assert.equal(config.status,200);assert(config.body.includes('/demo/api/v1'));
  assert.match(config.headers['content-type'],/javascript/);assert.equal(config.headers['x-content-type-options'],'nosniff');
  const path='/demo/api/v1/rules/rule%2Fspecial?q=%';
  const proxy=await get(port,path,{'X-Demo-CSRF':'fake-csrf',Cookie:'demo_v2_session=fake-test-cookie'});
  assert.equal(proxy.status,201);assert.deepEqual(JSON.parse(proxy.body),{url:path,csrf:'fake-csrf',cookie:'demo_v2_session=fake-test-cookie'});
});
