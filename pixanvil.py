"""Run `python pixanvil.py generate "your prompt"` or `python pixanvil.py serve`."""
import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
import httpx
from pixanvil_app.config import ROOT, state_directory


def request(base,path,method='GET',**kwargs):
    with httpx.Client(timeout=45,trust_env=False) as client:
        response=client.request(method,base+path,headers={'X-PixAnvil':'local'},**kwargs)
        if response.is_error:
            try: detail=response.json().get('detail',response.text)
            except ValueError: detail=response.text
            raise RuntimeError(str(detail))
        return response.json()


def ensure_server(base,port):
    try:
        if request(base,'/api/health').get('app')=='pixanvil': return
        raise RuntimeError('Port belongs to another application')
    except httpx.ConnectError: pass
    state=state_directory(); state.mkdir(parents=True,exist_ok=True)
    flags=(subprocess.CREATE_NO_WINDOW|subprocess.DETACHED_PROCESS) if os.name=='nt' else 0
    with (state/'server.log').open('ab') as log:
        subprocess.Popen([sys.executable,str(ROOT/'pixanvil.py'),'serve','--port',str(port)],
                         cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags,
                         start_new_session=os.name!='nt')
    for _ in range(60):
        time.sleep(.5)
        try:
            if request(base,'/api/health').get('app')=='pixanvil': return
        except httpx.ConnectError: pass
    raise RuntimeError('Server did not start. See '+str(state/'server.log'))


def stop_server(base):
    try:
        if request(base,'/api/health').get('app')!='pixanvil': raise RuntimeError('Port belongs to another application')
    except httpx.ConnectError:
        print('PixAnvil is not running.'); return 0
    active=request(base,'/api/shutdown','POST')['active']
    if active: print(f'{active} job(s) are still active. They are saved and resume when PixAnvil starts again.')
    for _ in range(40):
        time.sleep(.5)
        try: request(base,'/api/health')
        except httpx.ConnectError:
            print('PixAnvil stopped.'); return 0
    raise RuntimeError('The server was asked to stop but is still answering. See '+str(state_directory()/'server.log'))


def import_file(base,path):
    path=Path(path)
    with path.open('rb') as handle:
        return request(base,'/api/assets/import','POST',files={'file':(path.name,handle)})


def follow_task(base,task):
    """Print a history export or import's stage and progress until it finishes; returns the finished task."""
    shown=''
    while not task['finished']:
        time.sleep(.5)
        task=request(base,f'/api/library/tasks/{task["id"]}')
        total=task['bytes_total'] or task['files_total']
        done=task['bytes_done'] if task['bytes_total'] else task['files_done']
        line=task['stage']+(f' {100*done//total}%' if total else '')
        if line!=shown: print(line,flush=True); shown=line
    if task['error']: raise RuntimeError(task['error'])
    return task


def export_history(base,path):
    task=follow_task(base,request(base,'/api/library/export','POST'))
    with httpx.Client(timeout=None,trust_env=False) as client, client.stream('GET',f'{base}/api/library/tasks/{task["id"]}/file',headers={'X-PixAnvil':'local'}) as response:
        response.raise_for_status()
        part=Path(str(path)+'.part')
        with part.open('wb') as out:
            for chunk in response.iter_bytes(1<<20): out.write(chunk)
        part.replace(path)
    counts=task['result']
    print(f'Saved {path}: {counts["jobs"]} jobs, {counts["assets"]} gallery items, {counts["presets"]} presets.')
    if counts['missing']: print('Missing from disk, not included: '+', '.join(counts['missing']))
    return 0


def import_history(base,path,chains):
    path=Path(path)
    print('Uploading',flush=True)
    with path.open('rb') as handle, httpx.Client(timeout=None,trust_env=False) as client:
        response=client.post(base+'/api/library/import',params={'chains':chains},headers={'X-PixAnvil':'local'},files={'file':(path.name,handle,'application/zip')})
        if response.is_error: raise RuntimeError(response.json().get('detail',response.text))
    result=follow_task(base,response.json())['result']
    added,skipped=result['added'],result['skipped']
    print(f'Added {added["jobs"]} jobs, {added["assets"]} gallery items, {added["presets"]} presets, {added["inputs"]} source images.')
    print(f'Already here, skipped: {skipped["jobs"]} jobs, {skipped["assets"]} gallery items, {skipped["presets"]} presets.')
    chosen=result['chains']
    if any(chosen.values()): print(f'Saved chains that differed from yours: {chosen["copy"]} kept as copies, {chosen["overwrite"]} overwritten, {chosen["skip"]} skipped.')
    if result['unavailable']: print(f'{len(result["unavailable"])} gallery items were missing from the export and were not added.')
    return 0



def verify_history(base,path):
    path=Path(path)
    print('Uploading',flush=True)
    with path.open('rb') as handle, httpx.Client(timeout=None,trust_env=False) as client:
        response=client.post(base+'/api/library/verify',headers={'X-PixAnvil':'local'},files={'file':(path.name,handle,'application/zip')})
        if response.is_error: raise RuntimeError(response.json().get('detail',response.text))
    result=follow_task(base,response.json())['result']
    counts,would=result['counts'],result['would_add']
    print(f'Intact: every file was read. {counts.get("jobs",0)} jobs, {counts.get("assets",0)} gallery items, {counts.get("presets",0)} presets.')
    if result['missing']: print('Incomplete: missing from disk when it was made: '+', '.join(result['missing']))
    print(f'Importing here would add {would["jobs"]} jobs, {would["assets"]} gallery items, {would["presets"]} presets, {would["inputs"]} source images.')
    return 1 if result['missing'] else 0


def main():
    parser=argparse.ArgumentParser(description='PixAnvil — local images, made on SPARK')
    sub=parser.add_subparsers(dest='command')
    serve=sub.add_parser('serve',help='Run the gallery server in this terminal')
    serve.add_argument('--port',type=int,default=8790)
    serve.add_argument('--open',action='store_true')
    gen=sub.add_parser('generate',help='Generate an image or video and open its gallery (starts app if needed)')
    gen.add_argument('prompt',nargs='?',help='Omit to enter a prompt interactively')
    gen.add_argument('--workflow',choices=['flux-dev','flux-schnell','sdxl','qwen-image-edit','sulphur-video','minimax-h3-video'],default='flux-dev')
    gen.add_argument('--model',default='')
    gen.add_argument('--quality',choices=['fast','balanced','quality','maximum'],default='quality')
    gen.add_argument('--seed',default='-1')
    gen.add_argument('--negative',default='')
    gen.add_argument('--width',type=int,help='Default: the workflow canvas (1024 for images, 768 for video)')
    gen.add_argument('--height',type=int,help='Default: the workflow canvas (1024 for images, 512 for video)')
    gen.add_argument('--frames',type=int,help='Video length in frames at 24 fps. Sulphur: 8n+1 (default 97). MiniMax H3: 17n+5 (default 124)')
    gen.add_argument('--steps',type=int); gen.add_argument('--count',type=int,default=1)
    gen.add_argument('--port',type=int,default=8790)
    gen.add_argument('--no-browser',action='store_true')
    gen.add_argument('--start-comfy',action='store_true',help='Start stopped ComfyUI over configured SSH')
    gen.add_argument('--timeout',type=int,default=1800)
    gen.add_argument('--source',help='Source image, as a gallery asset id or an image file (imported first): the start image of a video workflow, or the image qwen-image-edit changes')
    gen.add_argument('--end',help='End image for a video workflow that accepts one (MiniMax H3): a gallery asset id or an image file')
    gen.add_argument('--json',action='store_true',help='Print one JSON result on stdout; progress goes to stderr')
    imp=sub.add_parser('import',help='Add an outside image file to the gallery')
    imp.add_argument('file')
    imp.add_argument('--port',type=int,default=8790)
    imp.add_argument('--json',action='store_true',help='Print the whole asset record instead of its id')
    exp=sub.add_parser('export',help='Save the whole history (jobs, gallery, presets, Branches, files) to a zip')
    exp.add_argument('file'); exp.add_argument('--port',type=int,default=8790)
    imh=sub.add_parser('import-history',help='Merge a history zip into this library; anything already present is skipped')
    imh.add_argument('file'); imh.add_argument('--port',type=int,default=8790)
    ver=sub.add_parser('verify-history',help='Check a history zip without importing it: every file is read to confirm it is intact')
    ver.add_argument('file'); ver.add_argument('--port',type=int,default=8790)
    imh.add_argument('--chains',choices=['copy','overwrite','skip'],default='copy',help='A saved chain that is here with different steps or name: keep both (copy, the default), replace yours (overwrite), or keep yours (skip)')
    for name,description in (('open',None),('status',None),('start-comfy',None),('stop','Stop the running PixAnvil server')):
        cmd=sub.add_parser(name,help=description)
        cmd.add_argument('--port',type=int,default=8790)
    args=parser.parse_args()
    if not args.command: parser.print_help(); return 0
    base=f'http://127.0.0.1:{args.port}'
    if args.command=='serve':
        import uvicorn
        from pixanvil_app.server import make_app
        if args.open:
            import threading
            threading.Timer(1.5,lambda:webbrowser.open(base)).start()
        app=make_app()
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,ws='none'))
        # Lets `pixanvil.py stop` end a server that runs in the background with no window.
        app.state.shutdown=lambda:setattr(server,'should_exit',True)
        server.run()
        return 0
    if args.command=='stop': return stop_server(base)
    ensure_server(base,args.port)
    if args.command=='open': webbrowser.open(base); print(base); return 0
    if args.command=='status': print(json.dumps(request(base,'/api/connection'),indent=2)); return 0
    if args.command=='start-comfy': print(request(base,'/api/connection/start','POST')); return 0
    if args.command=='export': return export_history(base,args.file)
    if args.command=='import-history': return import_history(base,args.file,args.chains)
    if args.command=='verify-history': return verify_history(base,args.file)
    if args.command=='import':
        asset=import_file(base,args.file)
        print(json.dumps(asset,indent=2) if args.json else f'Imported: {asset["id"]}\nSaved: {Path(asset["root"])/asset["path"]}')
        return 0
    def say(text): print(text,file=sys.stderr if args.json else sys.stdout,flush=True)
    prompt=args.prompt or input('Your prompt > ').strip()
    connection=request(base,'/api/connection')
    if connection['state']!='ready':
        if not args.start_comfy:
            raise RuntimeError('ComfyUI is unavailable. Use --start-comfy or the gallery Start ComfyUI button.')
        request(base,'/api/connection/start','POST')
        say('Starting ComfyUI…')
        for _ in range(180):
            time.sleep(5); connection=request(base,'/api/connection')
            if connection['state']=='ready': break
            if connection['startup']['state']=='failed': raise RuntimeError(connection['startup']['message'])
        else: raise RuntimeError('ComfyUI startup timed out')
    payload={k:getattr(args,k) for k in ('workflow','model','quality','seed','negative','width','height','frames','steps','count')}
    payload['prompt']=prompt
    def asset_id(value): return import_file(base,value)['id'] if Path(value).is_file() else value
    if args.source or args.end:
        payload['operation']='img2vid' if args.workflow.endswith('-video') else 'edit'
        if args.source: payload['source_asset_id']=asset_id(args.source)
        if args.end: payload['end_asset_id']=asset_id(args.end)
    jobs=request(base,'/api/jobs','POST',json=payload)['jobs']
    say(f'Gallery: {base}\nQueued {len(jobs)} job(s).')
    if not args.no_browser: webbrowser.open(base)
    deadline=time.monotonic()+args.timeout
    previous={}
    from pixanvil_app.progress import terminal_progress
    previous_progress={}
    while time.monotonic()<deadline:
        all_done=True
        for item in jobs:
            job=request(base,'/api/jobs/'+item['id'])
            state=job['state']
            if previous.get(job['id'])!=state:
                say(f'{job["id"][:8]}  {state}  seed {job["recipe"]["seed"]}')
                previous[job['id']]=state
            progress=terminal_progress(job.get('progress'))
            if progress and previous_progress.get(job['id'])!=progress:
                say(f'{job["id"][:8]}  {progress}')
                previous_progress[job['id']]=progress
            if state in ('failed','cancelled','abandoned','unknown'):
                raise RuntimeError(job.get('error') or state)
            if state!='complete': all_done=False
        if all_done:
            assets=request(base,'/api/assets?limit=100')['items']
            ids={j['id'] for j in jobs}
            saved=[asset for asset in assets if asset['job_id'] in ids]
            for asset in saved: say('Saved: '+str(Path(asset['root'])/asset['path']))
            say('Complete — saved to the gallery.')
            if args.json:
                print(json.dumps({'state':'complete','gallery':base,'assets':[
                    {'id':a['id'],'job_id':a['job_id'],'file':str(Path(a['root'])/a['path']),'media':a['recipe']['media'],
                     'workflow':a['recipe']['workflow'],'seed':a['recipe']['seed'],'width':a['width'],'height':a['height']}
                    for a in saved]},indent=2))
            return 0
        time.sleep(2)
    raise RuntimeError('Stopped waiting; the durable job continues. Check Gallery / History.')


if __name__=='__main__':
    try: sys.exit(main())
    except KeyboardInterrupt: print('\nStopped waiting. Submitted jobs remain in history.'); sys.exit(130)
    except Exception as exc: print('PixAnvil: '+str(exc),file=sys.stderr); sys.exit(1)
