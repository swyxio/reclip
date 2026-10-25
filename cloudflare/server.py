import json, os, re, signal, subprocess, threading, tempfile, shutil, time, requests
from types import SimpleNamespace
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)
state = {'status':'idle'}
lock = threading.Lock()
STRATEGIES = {'default','browser','mobile','referer','impersonate','cloudflare','auto'}
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

def options(data):
    strategy = data.get('strategy', 'default')
    if strategy not in STRATEGIES: raise ValueError('Unknown strategy')
    return ['default','browser','mobile','referer','cloudflare','impersonate'] if strategy == 'auto' else [strategy]

def flags(strategy, url, custom_headers):
    from urllib.parse import urlparse
    flags = []
    if strategy in ('browser','referer','cloudflare'):
        flags += ['--user-agent', UA]
    if strategy == 'mobile':
        flags += ['--user-agent', 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1']
    if strategy in ('referer','cloudflare'):
        p = urlparse(url); flags += ['--referer', f'{p.scheme}://{p.netloc}/']
    if strategy in ('impersonate','cloudflare'): flags += ['--impersonate','chrome']
    if strategy == 'cloudflare': flags += ['--extractor-args','generic:impersonate=chrome']
    for line in str(custom_headers or '').splitlines():
        if not line.strip(): continue
        if ':' not in line: raise ValueError('Invalid header')
        name,value=line.split(':',1)
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",name.strip()) or len(value)>4096: raise ValueError('Invalid header')
        flags += ['--add-headers',f'{name.strip()}:{value.strip()}']
    return flags

def run_command(command, timeout, workdir=None):
    # Keep subprocess output off the container's memory while enforcing disk headroom.
    with tempfile.TemporaryFile(mode='w+t') as out, tempfile.TemporaryFile(mode='w+t') as err:
        process=subprocess.Popen(command,stdout=out,stderr=err,start_new_session=True)
        deadline=time.monotonic()+timeout
        failure=None
        while process.poll() is None:
            if time.monotonic() >= deadline: failure='Download timed out'; break
            if workdir and shutil.disk_usage(workdir).free < 512*1024*1024: failure='Container disk is full; choose a smaller quality'; break
            time.sleep(1)
        if failure:
            os.killpg(process.pid,signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: os.killpg(process.pid,signal.SIGKILL); process.wait()
            raise RuntimeError(failure)
        out.seek(0); err.seek(0)
        return SimpleNamespace(returncode=process.returncode,stdout=out.read(),stderr=err.read())

def execute(data, extra, timeout, workdir=None):
    attempts=[]
    for strategy in options(data):
        command = ['yt-dlp','--ignore-config','--no-playlist','--js-runtimes','node','--no-progress','--concurrent-fragments','4'] + flags(strategy,data['url'],data.get('headers')) + extra + ['--',data['url']]
        result = run_command(command,timeout,workdir)
        if result.returncode == 0: return result, strategy
        errors = [s.strip() for s in result.stderr.splitlines() if s.strip()]
        tail=errors[-1][-500:] if errors else 'yt-dlp failed'
        tail=tail.replace(data['url'],'[URL]')
        for line in str(data.get('headers') or '').splitlines():
            if ':' in line:
                value=line.split(':',1)[1].strip()
                if value: tail=tail.replace(value,'[redacted]')
        attempts.append(strategy+': '+tail)
    raise RuntimeError('All strategies failed. '+' | '.join(attempts))

@app.post('/info')
def info():
    data=request.get_json()
    try:
        result,strategy=execute(data,['-j'],60)
        info=json.loads(result.stdout); heights={}
        for f in info.get('formats',[]):
            h=f.get('height')
            if isinstance(h, int) and 0 < h <= 16384 and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',str(f.get('format_id',''))) and f.get('vcodec','none')!='none' and (h not in heights or (f.get('tbr') or 0) > (heights[h].get('tbr') or 0)): heights[h]=f
        return jsonify(title=info.get('title',''),thumbnail=info.get('thumbnail',''),duration=info.get('duration'),uploader=info.get('uploader',''),formats=[{'id':f['format_id'],'label':str(h)+'p','height':h} for h,f in sorted(heights.items(),reverse=True)],strategy=strategy)
    except Exception as e: return jsonify(error=str(e)),400

def work(data):
    global state
    job=data['job_id']; directory=tempfile.mkdtemp(prefix='reclip-')
    try:
        fmt=data.get('format','video'); fmt_id=data.get('format_id')
        if fmt not in ('audio','video'): raise ValueError('Invalid format')
        if fmt_id and not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',str(fmt_id)): raise ValueError('Invalid format ID')
        args=['--max-filesize','4G','-o',directory+'/video.%(ext)s']
        args += ['-x','--audio-format','mp3'] if fmt=='audio' else ['-f',f'{fmt_id}+bestaudio/best' if fmt_id else 'bestvideo+bestaudio/best','--merge-output-format','mp4']
        result,strategy=execute(data,args,7200,directory)
        files=[p for p in Path(directory).iterdir() if p.suffix in ('.mp4','.mp3','.mkv','.webm')]
        if len(files)!=1: raise RuntimeError('Download did not produce exactly one file')
        file=files[0]; size=file.stat().st_size
        if size > 4_000_000_000: raise ValueError('File exceeds the 4 GB output limit')
        probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(file)],capture_output=True,text=True,timeout=60)
        if probe.returncode: raise RuntimeError('Output file failed media validation')
        title=re.sub(r'[\\/:*?"<>|\x00-\x1f]','',data.get('title','')).strip()[:100] or 'video'
        filename=title+file.suffix
        base='http://reclip-storage.internal/'+job
        create=requests.post(base+'?action=create',timeout=60); create.raise_for_status(); upload=create.json()['uploadId']; parts=[]
        try:
            with file.open('rb') as stream:
                n=0
                while chunk:=stream.read(8*1024*1024):
                    n+=1
                    r=requests.put(base,params={'action':'part','uploadId':upload,'partNumber':n},data=chunk,timeout=120); r.raise_for_status(); parts.append(r.json())
            r=requests.post(base,params={'action':'complete','uploadId':upload},json={'parts':parts,'filename':filename,'duration':probe.stdout.strip(),'size':size,'strategy':strategy},timeout=120); r.raise_for_status()
        except Exception:
            requests.delete(base,params={'uploadId':upload},timeout=30); raise
        state={'status':'done','filename':filename,'strategy':strategy,'size':size,'duration':probe.stdout.strip()}
    except Exception as e: state={'status':'error','error':str(e)[:1500]}
    finally: shutil.rmtree(directory,ignore_errors=True)

@app.post('/start')
def start():
    global state
    data=request.get_json()
    with lock:
        if state['status']=='idle':
            state={'status':'downloading','job_id':data['job_id']}
            threading.Thread(target=work,args=(data,),daemon=True).start()
        elif state.get('job_id') not in (None,data['job_id']): return jsonify(error='Container already in use'),409
    return jsonify(state)

@app.get('/status')
def status(): return jsonify(state)

if __name__=='__main__': app.run(host='0.0.0.0',port=8080,threaded=True)
