import { Container, ContainerProxy } from '@cloudflare/containers';
import { WorkflowEntrypoint } from 'cloudflare:workers';
export { ContainerProxy };

export class Downloader extends Container {
  defaultPort = 8080;
  sleepAfter = '3h';
}
Downloader.outboundByHost = {
  'reclip-storage.internal': async (request, env, ctx) => {
    const url = new URL(request.url);
    const id = url.pathname.slice(1);
    if (!/^[0-9a-f-]{36}$/.test(id) || env.DOWNLOADERS.idFromName(id).toString() !== ctx.containerId) return new Response('Forbidden', {status:403});
    const key = `files/${id}`;
    const action = url.searchParams.get('action');
    const uploadId = url.searchParams.get('uploadId');
    if (request.method === 'POST' && action === 'create') {
      const upload = await env.FILES.createMultipartUpload(key);
      return Response.json({uploadId: upload.uploadId});
    }
    if (!uploadId) return new Response('Missing upload ID', {status:400});
    const upload = env.FILES.resumeMultipartUpload(key, uploadId);
    if (request.method === 'PUT' && action === 'part') {
      const partNumber = Number(url.searchParams.get('partNumber'));
      if (!Number.isInteger(partNumber) || partNumber < 1 || partNumber > 500) return new Response('Invalid part', {status:400});
      return Response.json(await upload.uploadPart(partNumber, request.body));
    }
    if (request.method === 'POST' && action === 'complete') {
      const {parts, ...metadata} = await request.json();
      await upload.complete(parts);
      await env.FILES.put(`receipts/${id}`, JSON.stringify(metadata));
      return Response.json({ok:true});
    }
    if (request.method === 'DELETE') { await upload.abort(); return Response.json({ok:true}); }
    return new Response('Not found', {status:404});
  }
};

export class DownloadJob extends WorkflowEntrypoint {
  async run(event, step) {
    const input = event.payload;
    const stub = this.env.DOWNLOADERS.getByName(input.job_id);
    try {
      const already = await step.do('check-receipt', async () => {
        const receipt = await this.env.FILES.get(`receipts/${input.job_id}`);
        return receipt ? await receipt.json() : null;
      });
      if (already) return {status:'done', ...already};
      await step.do('start-download', {retries:{limit:3,delay:'15 seconds',backoff:'exponential'},timeout:'3 minutes'}, async () => {
        const saved = await this.env.FILES.get(`inputs/${input.job_id}`);
        if (!saved) {
          if (await this.env.FILES.head(`receipts/${input.job_id}`)) return;
          const status = await stub.fetch('http://container/status');
          const existing = status.ok ? await status.json() : null;
          if (existing && existing.status !== 'idle') return;
          throw new Error('Job input is no longer available; please retry');
        }
        const payload = await saved.text();
        const r = await stub.fetch('http://container/start', {method:'POST',headers:{'content-type':'application/json'},body:payload});
        if (!r.ok) throw new Error(`Container could not start (${r.status})`);
        await this.env.FILES.delete(`inputs/${input.job_id}`);
      });
      for (let i=0; i<720; i++) {
        await step.sleep(`wait-${i}`, '10 seconds');
        const result = await step.do(`check-${i}`, {retries:{limit:3,delay:'10 seconds',backoff:'exponential'}}, async () => {
          const receipt = await this.env.FILES.get(`receipts/${input.job_id}`);
          if (receipt) return {status:'done', ...await receipt.json()};
          const r = await stub.fetch('http://container/status');
          if (!r.ok) throw new Error(`Container status unavailable (${r.status})`);
          return await r.json();
        });
        if (result.status === 'done' || result.status === 'error') return result;
        if (result.status === 'idle') return {status:'error',error:'Download container stopped before completion; please retry.'};
      }
      return {status:'error',error:'Download exceeded the two-hour limit'};
    } catch (error) { return {status:'error',error:String(error.message || error).slice(0,1500)}; }
    finally { await step.do('stop-container', {retries:{limit:2,delay:'10 seconds',backoff:'exponential'}}, async () => { await stub.destroy(); }); }
  }
}

function validInput(data) {
  try {
    const u=new URL(data.url);
    if (!['http:','https:'].includes(u.protocol) || u.username || u.password || data.url.length>2048) return false;
    if (typeof data.headers !== 'undefined' && (typeof data.headers !== 'string' || data.headers.length > 12000 || data.headers.split('\n').length>16)) return false;
    return true;
  } catch {return false;}
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const auth = request.headers.get('Authorization');
    if (!env.PREVIEW_TOKEN || (auth !== `Bearer ${env.PREVIEW_TOKEN}` && auth !== `Basic ${btoa('reclip:' + env.PREVIEW_TOKEN)}`)) {
      return new Response('Authentication required', {status:401, headers:{'WWW-Authenticate':'Basic realm="ReClip preview", charset="UTF-8"','Cache-Control':'no-store'}});
    }
    if (request.method === 'POST' && ['/api/info','/api/download'].includes(url.pathname)) {
      let input;
      try { input = await request.json(); } catch { return Response.json({error:'Invalid JSON'},{status:400}); }
      if (!validInput(input)) return Response.json({error:'Invalid URL or request headers'},{status:400});
      if (url.pathname === '/api/info') {
        const stub=env.DOWNLOADERS.getByName(`info-${crypto.randomUUID()}`);
        try {
          const r = await stub.fetch('http://container/info',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(input)});
          return Response.json(await r.json(), {status:r.status});
        } finally { await stub.destroy(); }
      }
      const id=crypto.randomUUID();
      await env.FILES.put(`inputs/${id}`, JSON.stringify({...input,job_id:id}));
      await env.JOBS.create({id,params:{job_id:id}});
      return Response.json({job_id:id,strategy:input.strategy || 'default'});
    }
    const match=url.pathname.match(/^\/api\/(status|file)\/([0-9a-f-]{36})$/);
    if (match) {
      const id=match[2];
      if (match[1] === 'status') {
        try {
          const status=await (await env.JOBS.get(id)).status();
          if (status.status === 'complete') return Response.json(status.output);
          if (status.status === 'errored' || status.status === 'terminated') return Response.json({status:'error',error:'Download job failed'});
          return Response.json({status:'downloading'});
        } catch { return Response.json({error:'Job not found'},{status:404}); }
      }
      const receipt=await env.FILES.get(`receipts/${id}`);
      if (!receipt) return Response.json({error:'File not ready'},{status:404});
      const meta=await receipt.json();
      const size=Number(meta.size);
      if (!Number.isSafeInteger(size) || size < 1) return Response.json({error:'Invalid file metadata'},{status:500});
      let range;
      const rangeHeader=request.headers.get('range');
      if (rangeHeader) {
        const m=rangeHeader.match(/^bytes=(\d*)-(\d*)$/);
        if (!m || (!m[1] && !m[2])) return new Response(null,{status:416,headers:{'content-range':`bytes */${size}`}});
        let start, end;
        if (!m[1]) { const suffix=Number(m[2]); start=Math.max(0,size-suffix); end=size-1; }
        else { start=Number(m[1]); end=m[2] ? Math.min(Number(m[2]),size-1) : size-1; }
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= size || start > end) return new Response(null,{status:416,headers:{'content-range':`bytes */${size}`}});
        range={offset:start,length:end-start+1};
      }
      const file=await env.FILES.get(`files/${id}`,range ? {range} : undefined);
      if (!file) return Response.json({error:'File expired'},{status:404});
      const name=String(meta.filename || 'video').replace(/[\r\n"\\]/g,'_');
      const headers={'content-type':'application/octet-stream','content-length':String(range ? range.length : file.size),'content-disposition':`attachment; filename="${name.replace(/[^\x20-\x7e]/g,'_')}"; filename*=UTF-8''${encodeURIComponent(name)}`,'cache-control':'private, no-store','accept-ranges':'bytes','etag':file.httpEtag};
      if (range) headers['content-range']=`bytes ${range.offset}-${range.offset+range.length-1}/${size}`;
      return new Response(file.body,{status:range ? 206 : 200,headers});
    }
    return env.ASSETS.fetch(request);
  }
};
