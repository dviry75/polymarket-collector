// Local development/preview only. Same-origin API proxy preserves cookie and CSRF semantics.
import {createServer,request} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL(process.argv.includes('--built')?'../build/':'../',import.meta.url));
const target=new URL(process.env.DEMO_BACKEND_TARGET||'http://127.0.0.1:8000');
if(target.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(target.hostname)||target.username||target.password)throw new Error('Development backend must be loopback HTTP');
const server=createServer(async(req,res)=>{
  const path=new URL(req.url,'http://localhost').pathname;
  if(path.startsWith('/demo/api/v1/')){
    const proxy=request(new URL(req.url,target),{method:req.method,headers:{...req.headers,host:target.host}},upstream=>{
      res.writeHead(upstream.statusCode,upstream.headers);upstream.pipe(res);
    });
    proxy.on('error',()=>{if(!res.headersSent)res.writeHead(502,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({detail:{code:'DEMO_BACKEND_UNAVAILABLE'}}));});
    proxy.setTimeout(15000,()=>proxy.destroy());req.pipe(proxy);return;
  }
  if(!path.startsWith('/demo/')){res.writeHead(302,{Location:'/demo/'});res.end();return;}
  const file=resolve(root,decodeURIComponent(path.slice(6)||'index.html'));
  if(!file.startsWith(resolve(root)+'/')||file.includes('/node_modules/')||!['.html','.js','.css'].includes(extname(file))){res.writeHead(404);res.end();return;}
  try{
    let body=await readFile(file);
    if(file.endsWith('/src/config.js')&&process.env.DEMO_API_BASE)body=Buffer.from('export const API_BASE = '+JSON.stringify(process.env.DEMO_API_BASE)+';\n');
    res.writeHead(200,{'Content-Type':({'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'})[extname(file)],'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(body);
  }catch{res.writeHead(404);res.end();}
});
server.listen(Number(process.env.DEMO_FRONTEND_PORT||5173),'127.0.0.1',()=>console.log('DEMO local frontend on loopback port '+server.address().port));
for(const signal of ['SIGTERM','SIGINT'])process.on(signal,()=>server.close(()=>process.exit(0)));
