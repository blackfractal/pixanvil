import asyncio
import json
import os
import re
import subprocess
import threading
import time
import hashlib
import io
from typing import Literal
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import Config, ROOT
from .store import Store
from .comfy import Comfy
from .engine import Engine
from .workflows import catalog, compile_recipe
from .media import import_image, inspect_image, last_frame_files, safe_path
from . import branches
from .locking import InstanceLock
from .image_tools import capabilities, compile_upscale, seed as resolve_seed


MEDIA_TYPES={'.png':'image/png','.jpg':'image/jpeg','.mp4':'video/mp4'}


class Emphasis(BaseModel):
    text: str = Field(min_length=1,max_length=200)
    weight: float = Field(ge=0.1,le=2)


class Refinement(BaseModel):
    workflow: Literal['sdxl','flux-dev'] = 'sdxl'
    model: str = ''
    denoise: float = Field(default=.25,gt=0,le=1)
    steps: int = Field(default=20,ge=1,le=80)
    cfg: float | None = Field(default=None,ge=0,le=20)


class Generation(BaseModel):
    prompt: str = Field(min_length=1,max_length=12000)
    negative: str = Field(default='',max_length=12000)
    workflow: str = 'flux-dev'
    model: str = ''
    quality: str = 'quality'
    # None = the workflow package's default canvas / clip length.
    width: int | None = Field(default=None,ge=256,le=1536)
    height: int | None = Field(default=None,ge=256,le=1536)
    frames: int | None = Field(default=None,ge=1,le=1000)
    fps: int | None = Field(default=None,ge=8,le=60)
    seed: str = '-1'
    steps: int | None = Field(default=None,ge=1,le=80)
    cfg: float | None = Field(default=None,ge=0,le=20)
    count: int = Field(default=1,ge=1,le=8)
    emphasis: list[Emphasis] = Field(default_factory=list,max_length=12)
    operation: Literal['txt2img','variation','refine','upscale','img2vid','edit'] = 'txt2img'
    source_asset_id: str | None = Field(default=None,max_length=100)
    end_asset_id: str | None = Field(default=None,max_length=100)
    variation_seed: str = '-1'
    variation_strength: float = Field(default=.15,ge=0,le=1)
    denoise: float = Field(default=.25,gt=0,le=1)
    scale: Literal[2,4] = 2
    upscale_model: str = ''
    refinement: Refinement | None = None

    @field_validator('width','height')
    @classmethod
    def dimensions(cls,v):
        if v is not None and v%64: raise ValueError('Dimensions must be multiples of 64')
        return v

    @field_validator('prompt')
    @classmethod
    def prompt_nonempty(cls,v):
        if not v.strip(): raise ValueError('Enter a prompt')
        return v

    @field_validator('quality')
    @classmethod
    def profile(cls,v):
        if v not in ('fast','balanced','quality','maximum'): raise ValueError('Invalid quality profile')
        return v

    @field_validator('seed','variation_seed')
    @classmethod
    def seed_range(cls,v):
        if v in ('','-1','random'): return v
        if not v.isdecimal() or not 0<=int(v)<2**64: raise ValueError('Seed must be an unsigned 64-bit integer or -1')
        return str(int(v))


class Settings(BaseModel):
    comfy_url: str
    library: str
    ssh_host: str


class Preset(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    data: Generation
    id: str | None = None


class Mark(BaseModel):
    favorite: bool = False
    rating: int = Field(default=0,ge=0,le=5)


class Position(BaseModel):
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)


class Layout(BaseModel):
    # None forgets a node's position, so automatic placement takes it again.
    positions: dict[str, Position | None] = Field(max_length=5000)


class TreeName(BaseModel):
    name: str = Field(max_length=100)


class BranchImage(BaseModel):
    asset_id: str = Field(max_length=100)


def make_app(state=None, worker=True, client_factory=Comfy):
    config=Config(state)
    store=Store(config.state/'pixanvil.sqlite3')
    engine=Engine(store,config,client_factory)
    startup={'state':'idle','message':''}
    start_lock=threading.Lock()
    cached={'info':None,'time':0,'url':None}

    @asynccontextmanager
    async def lifespan(app):
        instance_lock=InstanceLock(config.state/'server.lock') if worker else None
        try:
            if worker: engine.start()
            yield
        finally:
            engine.close()
            if instance_lock: instance_lock.close()

    app=FastAPI(title='PixAnvil',lifespan=lifespan)
    app.state.store=store; app.state.engine=engine; app.state.config=config

    @app.middleware('http')
    async def local_only(request:Request, call_next):
        hostname=request.url.hostname
        if hostname not in ('localhost','127.0.0.1','testserver'):
            return JSONResponse({'detail':'Localhost only'},status_code=403)
        origin=request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail':'Foreign browser origin blocked'},status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS') and request.headers.get('x-pixanvil')!='local':
            return JSONResponse({'detail':'Missing local application header'},status_code=403)
        response=await call_next(request)
        # Revalidate the page and its scripts on every load so an open tab never runs a stale frontend.
        if request.url.path=='/' or request.url.path.startswith('/static/'):
            response.headers['Cache-Control']='no-cache'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        return response

    def info():
        url=config.data['comfy_url']
        if cached['info'] is None or cached['url']!=url or time.monotonic()-cached['time']>30:
            cached.update(info=client_factory(url).info(),url=url,time=time.monotonic())
        return cached['info']

    @app.get('/api/health')
    def health(): return {'app':'pixanvil','version':'0.1.0','library':config.data['library']}

    @app.post('/api/shutdown')
    def shutdown():
        """Ask the server to exit. Queued and unfinished jobs are durable and resume on the next start."""
        stop=getattr(app.state,'shutdown',None)
        if not stop: raise HTTPException(409,'This server was not started by pixanvil.py serve, so it cannot stop itself')
        stop()
        return {'stopping':True,'active':len(store.active())}

    @app.get('/api/settings')
    def get_settings(): return config.public()

    @app.put('/api/settings')
    def settings(value:Settings):
        parsed=urlparse(value.comfy_url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise HTTPException(422,'Use a plain ComfyUI http(s) host and port URL')
        if not re.fullmatch(r'[\w.-]+@[\w.-]+',value.ssh_host):
            raise HTTPException(422,'SSH target must be user@hostname')
        root=Path(value.library).expanduser()
        if not root.is_absolute(): raise HTTPException(422,'Library needs an absolute Windows path')
        with engine.lock:
            if startup['state']=='starting': raise HTTPException(409,'Wait for ComfyUI startup before changing connection settings')
            if store.active(): raise HTTPException(409,'Finish or abandon active jobs before changing settings')
            try:
                root.mkdir(parents=True,exist_ok=True)
                import tempfile
                with tempfile.TemporaryFile(dir=root): pass
            except OSError as exc: raise HTTPException(422,str(exc))
            config.save({'comfy_url':value.comfy_url.rstrip('/'),'ssh_host':value.ssh_host,'library':str(root.resolve())})
        return config.public()

    @app.get('/api/connection')
    def connection():
        try:
            stats=client_factory(config.data['comfy_url']).health()
            return {'state':'ready','url':config.data['comfy_url'],'stats':stats,'startup':startup.copy()}
        except Exception as exc:
            return {'state':'starting' if startup['state']=='starting' else 'unavailable','url':config.data['comfy_url'],'error':str(exc),'startup':startup.copy()}

    @app.get('/api/discover')
    def discover(): return config.discovery()

    def start_service():
        try:
            try:
                client_factory(config.data['comfy_url']).health()
                startup.update(state='ready',message='ComfyUI is already ready'); return
            except Exception: pass
            command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',config.data['ssh_host'],
                     'python3 ~/ai/scripts/spark_services.py comfyui']
            flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            proc=subprocess.run(command,capture_output=True,text=True,timeout=600,creationflags=flags)
            if proc.returncode: raise RuntimeError((proc.stderr or proc.stdout)[-3000:])
            deadline=time.monotonic()+240
            while time.monotonic()<deadline:
                try:
                    client_factory(config.data['comfy_url']).health()
                    startup.update(state='ready',message='ComfyUI is ready'); return
                except Exception: time.sleep(3)
            raise RuntimeError('Start command completed, but ComfyUI is not yet reachable. Check the SPARK service logs.')
        except Exception as exc:
            startup.update(state='failed',message=str(exc))
        finally: start_lock.release()

    @app.post('/api/connection/start')
    def start():
        if start_lock.acquire(blocking=False):
            startup.update(state='starting',message='Starting ComfyUI over SSH; first startup may take several minutes.')
            threading.Thread(target=start_service,daemon=True).start()
        return startup.copy()

    @app.get('/api/workflows')
    def workflows():
        try: return {'items':catalog(info())}
        except Exception as exc: raise HTTPException(503,'ComfyUI unavailable: '+str(exc))

    @app.get('/api/image-tools')
    def image_tools():
        try: return capabilities(info())
        except Exception as exc: raise HTTPException(503,'ComfyUI unavailable: '+str(exc))

    def prepare(value:Generation):
        """Compile a request into one (recipe, graph) per output, plus the source snapshots the jobs read.

        Raises HTTPException: 422 for a request that cannot run, 503 when ComfyUI cannot be reached."""
        try:
            data=value.model_dump()
            count=data.pop('count')
            data.update(library=config.data['library'],comfy_url=config.data['comfy_url'])
            definitions=info()
            source=None
            end=None
            snapshots={}
            def stage(asset,name_key,hash_key):
                if asset['recipe'].get('media')=='video': raise ValueError('The source must be a still image, not a video')
                path=safe_path(asset['root'],asset['path'])
                if path.stat().st_size>80*1024*1024: raise ValueError('Source exceeds 80 MB')
                payload=path.read_bytes()
                from PIL import Image
                with Image.open(io.BytesIO(payload)) as image:
                    if image.format!='PNG' or image.size!=(asset['width'],asset['height']):
                        raise ValueError('Source file no longer matches the saved image')
                    image.verify()
                digest=hashlib.sha256(payload).hexdigest()
                data.update({name_key:digest+'.png',hash_key:digest})
                snapshots[digest+'.png']=payload
            if data['end_asset_id']:
                if data['operation']!='img2vid': raise ValueError('An end image needs a video workflow')
                end=store.asset(data['end_asset_id'])
                if not end: raise ValueError('Choose an end image from the gallery')
            if data['operation']!='txt2img':
                source=store.asset(data['source_asset_id']) if data['source_asset_id'] else None
                if not source and not end: raise ValueError('Choose a source image from the gallery')
                data['parent_asset_id']=(source or end)['id']
                if data['operation'] in ('img2vid','edit'):
                    data['source_size']=[(source or end)['width'],(source or end)['height']]
                if data['operation']=='variation':
                    original=source['recipe']
                    if original.get('operation','txt2img') not in ('txt2img','variation'):
                        raise ValueError('Seed variations need a generated image or a seed variation; use Refine for edited/upscaled images')
                    data.update(seed=original['seed'],workflow=original['workflow'],model=original['model'],
                                width=source['width'],height=source['height'])
                    data['variation_seed']=resolve_seed(data['variation_seed'])
                else:
                    if source: stage(source,'input_name','source_sha256')
                    if end: stage(end,'end_input_name','end_sha256')
            elif data['source_asset_id']:
                raise ValueError('Source image requires an image operation')
            if data['operation']=='upscale':
                if data['refinement']: raise ValueError('Upscale first, then refine the saved result')
                if count!=1: raise ValueError('Upscaling produces one image at a time')
                recipe,graph=compile_upscale(data,source,definitions)
            else:
                recipe,graph=compile_recipe(data,definitions)
            try:
                stats=client_factory(config.data['comfy_url']).health()
                data['environment']={'system':stats.get('system',{}),'devices':stats.get('devices',[])}
            except Exception:
                data['environment']={'note':'Runtime version information unavailable at submission'}
            prepared=[]
            base=int(recipe['seed'])
            for i in range(count):
                item={**data,'seed':str((base+i)%(2**64))}
                if data['operation']=='variation':
                    item.update(seed=str(base),variation_seed=str((int(data['variation_seed'])+i)%(2**64)))
                rec,g=compile_upscale(item,source,definitions) if data['operation']=='upscale' else compile_recipe(item,definitions)
                prepared.append((rec,g))
            return prepared,snapshots
        except ValueError as exc: raise HTTPException(422,str(exc))
        except Exception as exc: raise HTTPException(503,str(exc))

    def save_snapshots(snapshots):
        for name,payload in snapshots.items():
            inputs=config.state/'inputs'; inputs.mkdir(exist_ok=True)
            target=inputs/name
            temporary=target.with_suffix('.part')
            temporary.write_bytes(payload); temporary.replace(target)

    def release_inputs(recipe):
        """Delete the source snapshots a recipe used once no remaining job needs them. Returns the ones that could not be removed."""
        leftover=[]
        for key in ('input_name','end_input_name'):
            name=recipe.get(key)
            if name and not store.input_in_use(name):
                try: safe_path(config.state/'inputs',name).unlink(missing_ok=True)
                except OSError as exc: leftover.append(str(exc.filename))
        return leftover

    @app.post('/api/jobs',status_code=201)
    def submit(value:Generation):
        with engine.lock:
            prepared,snapshots=prepare(value)
            save_snapshots(snapshots)
            return {'jobs':[store.create_job(rec,g) for rec,g in prepared]}

    @app.put('/api/jobs/{job_id}')
    def edit_job(job_id:str,value:Generation):
        """Replace a queued job's settings. The job keeps its place in the queue.

        The coordinator takes the same lock before it sends a job to ComfyUI, so an edit either
        lands before submission or is refused because the job has already started."""
        if value.count!=1: raise HTTPException(422,'A queued job makes one output. Submit a new job for more.')
        with engine.lock:
            found=store.job(job_id)
            if not found: raise HTTPException(404,'Job not found')
            if found['state']!='queued': raise HTTPException(409,'This job has already started, so its settings can no longer change.')
            [(recipe,graph)],snapshots=prepare(value)
            save_snapshots(snapshots)
            store.update_job(job_id,recipe=recipe,graph=graph)
            release_inputs(found['recipe'])
            return store.job(job_id)

    @app.get('/api/jobs')
    def jobs(): return {'items':[engine.with_progress(j) for j in store.jobs()]}

    @app.get('/api/jobs/{job_id}')
    def job(job_id:str):
        found=store.job(job_id)
        if not found: raise HTTPException(404,'Job not found')
        return engine.with_progress(found)

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(job_id:str):
        with engine.lock:
            found=store.job(job_id)
            if not found: raise HTTPException(404,'Job not found')
            if found['state']!='queued': raise HTTPException(409,'Only locally queued jobs can be cancelled safely. Running ComfyUI work is shared.')
            store.update_job(job_id,state='cancelled',error=None)
        return store.job(job_id)

    @app.post('/api/jobs/{job_id}/abandon')
    def abandon(job_id:str):
        with engine.lock:
            found=store.job(job_id)
            if not found or found['state']!='unknown': raise HTTPException(409,'Only unresolved jobs can be abandoned')
            store.update_job(job_id,state='abandoned',error='Locally abandoned. Any remote execution is unaffected.')
        return store.job(job_id)

    # Jobs in these states may still be executing on ComfyUI, so their records and files must stay.
    busy_states=('submitting','running','collecting','unknown')

    def asset_files(asset):
        """Every file an asset owns in the library."""
        path=safe_path(asset['root'],asset['path'])
        # A generated output keeps its recipe in a JSON sidecar; a clip also keeps its full-size first frame.
        files=[path,safe_path(asset['root'],asset['thumbnail']),path.with_suffix('.json')]
        if path.suffix.lower()=='.mp4':
            # A clip also keeps its full-size first frame, and its last frame with a thumbnail.
            files+=[path.with_suffix('.png'),*last_frame_files(path,safe_path(asset['root'],asset['thumbnail']))]
        return files

    def remove_asset_files(asset):
        """Delete an asset's files from the library. Returns the ones that could not be removed."""
        leftover=[]
        for file in asset_files(asset):
            try: file.unlink(missing_ok=True)
            except OSError: leftover.append(str(file))
        return leftover

    def saved_inputs():
        """Source-image snapshots kept for jobs that read an image."""
        folder=config.state/'inputs'
        return [f for f in folder.iterdir() if f.is_file()] if folder.is_dir() else []

    def remove_job(job):
        """Delete a job, every output it produced, and source snapshots no other job needs."""
        if job['state'] in busy_states:
            raise HTTPException(409,'This job may still be running on ComfyUI. Wait for it to finish, or abandon it first.')
        removed=store.delete_job(job['id'])
        leftover=[f for asset in removed for f in remove_asset_files(asset)]+release_inputs(job['recipe'])
        return {'deleted_job':job['id'],'deleted_assets':[a['id'] for a in removed],'leftover_files':leftover}

    @app.delete('/api/jobs/{job_id}')
    def delete_job(job_id:str):
        with engine.lock:
            found=store.job(job_id)
            if not found: raise HTTPException(404,'Job not found')
            return remove_job(found)

    @app.delete('/api/assets/{asset_id}')
    def delete_asset(asset_id:str):
        with engine.lock:
            asset=store.asset(asset_id)
            if not asset: raise HTTPException(404,'Image not found')
            # A generated output and its job are one record: deleting either removes both.
            job=store.job(asset['job_id']) if asset['job_id'] else None
            if job: return remove_job(job)
            store.delete_asset(asset_id)
            return {'deleted_job':None,'deleted_assets':[asset_id],'leftover_files':remove_asset_files(asset)}

    @app.get('/api/library/summary')
    def library_summary():
        """What deleting the whole library would remove, so the user can review it first."""
        assets=store.all_assets()
        inputs=saved_inputs()
        files=[f for a in assets for f in asset_files(a)]+inputs
        return {'jobs':store.job_count(),
                'generated':sum(1 for a in assets if a['job_id']),
                'imported':sum(1 for a in assets if not a['job_id']),
                'inputs':len(inputs),
                'bytes':sum(f.stat().st_size for f in files if f.is_file()),
                'busy':sum(1 for j in store.active() if j['state'] in busy_states)}

    @app.delete('/api/library')
    def clear_library():
        """Delete every job, gallery entry, library file and source snapshot. Presets and settings stay."""
        with engine.lock:
            if any(j['state'] in busy_states for j in store.active()):
                raise HTTPException(409,'Some jobs may still be running on ComfyUI. Wait for them to finish, or abandon them first.')
            jobs=store.job_count()
            removed=store.clear_history()
            leftover=[f for asset in removed for f in remove_asset_files(asset)]
            inputs=saved_inputs()
            for file in inputs:
                try: file.unlink()
                except OSError: leftover.append(str(file))
        return {'deleted_jobs':jobs,'deleted_assets':len(removed),'deleted_inputs':len(inputs),'leftover_files':leftover}

    @app.get('/api/assets')
    def assets(q:str='',model:str='',offset:int=0,limit:int=60,favorites:bool=False,media:Literal['','image','video']=''):
        return store.assets(q,model,max(0,offset),max(1,min(limit,100)),favorites,media)

    @app.get('/api/assets/{asset_id}')
    def asset_detail(asset_id:str):
        asset=store.asset(asset_id)
        if not asset: raise HTTPException(404,'Image not found')
        return asset

    def asset_file(asset_id,thumbnail=False):
        asset=store.asset(asset_id)
        if not asset: raise HTTPException(404,'Image not found')
        path=safe_path(asset['root'],asset['thumbnail'] if thumbnail else asset['path'])
        if not path.is_file(): raise HTTPException(404,'Image file is missing from the library')
        # The filename gives "Save as" the right name and extension whatever the URL ends in.
        return FileResponse(path,media_type=MEDIA_TYPES[path.suffix.lower()],filename=path.name,content_disposition_type='inline')

    # /file serves the original of any asset; /image is the older name for the same thing.
    @app.get('/api/assets/{asset_id}/file')
    @app.get('/api/assets/{asset_id}/image')
    def original(asset_id:str): return asset_file(asset_id)

    @app.get('/api/assets/{asset_id}/thumbnail')
    def thumbnail(asset_id:str): return asset_file(asset_id,True)

    @app.post('/api/assets/import',status_code=201)
    async def import_asset(file:UploadFile):
        data=await file.read(40*1024*1024+1)
        if len(data)>40*1024*1024: raise HTTPException(413,'Image exceeds 40 MB')
        try: return import_image(store,config.data['library'],data,file.filename)
        except Exception as exc: raise HTTPException(422,'Cannot import this file as an image: '+str(exc))

    def video_asset(asset_id):
        asset=store.asset(asset_id)
        if not asset or asset['recipe'].get('media')!='video': raise HTTPException(404,'Video not found')
        return asset

    def clip_frame_files(clip,which):
        """A clip's full-size first or last frame, and that frame's thumbnail."""
        path=safe_path(clip['root'],clip['path'])
        thumbnail=safe_path(clip['root'],clip['thumbnail'])
        return (path.with_suffix('.png'),thumbnail) if which=='first' else last_frame_files(path,thumbnail)

    def save_frame(clip,which):
        """Add a clip's first or last frame to the gallery as an image linked to the clip."""
        image,_=clip_frame_files(clip,which)
        if not image.is_file():
            raise HTTPException(404,f'This clip was saved before {which} frames were kept; generate it again to use its {which} frame')
        # Keyed by clip and frame, not content: identical frames from two clips are two nodes with their own parents.
        return import_image(store,config.data['library'],image.read_bytes(),image.name,asset_id=f'frame-{clip["id"]}-{which}',operation='frame',frame=which,
                            prompt=clip['recipe']['prompt'],resolved_prompt=clip['recipe']['prompt'],parent_asset_id=clip['id'])

    @app.post('/api/assets/{asset_id}/first-frame',status_code=201)
    def first_frame(asset_id:str): return save_frame(video_asset(asset_id),'first')

    def branch_graph():
        def has_last_frame(clip): return clip_frame_files(clip,'last')[0].is_file()
        nodes,clips=branches.build(store.all_assets(),store.active(),store.branch_added(),has_last_frame)
        return nodes,branches.trees(nodes,clips)

    @app.get('/api/branches')
    def branch_trees():
        nodes,trees=branch_graph()
        names=store.branch_names()
        items=[branches.summary(t,nodes,names) for t in trees]
        return {'items':sorted(items,key=lambda t:t['updated'],reverse=True)}

    @app.put('/api/branches/layout')
    def branch_layout(value:Layout):
        store.set_branch_positions({k:v.model_dump() if v else None for k,v in value.positions.items()})
        return {'ok':True}

    @app.post('/api/branches/images',status_code=201)
    def add_branch_image(value:BranchImage):
        asset=store.asset(value.asset_id)
        if not asset: raise HTTPException(404,'Image not found')
        if asset['recipe'].get('media')=='video': raise HTTPException(422,'Add a clip to Branches through its first frame')
        store.add_branch_image(asset['id'])
        return {'tree':branches.find_tree(branch_graph()[1],asset['id'])['id']}

    @app.delete('/api/branches/images/{asset_id}')
    def remove_branch_image(asset_id:str):
        store.remove_branch_image(asset_id); return {'ok':True}

    @app.post('/api/branches/nodes/{node_id}/real',status_code=201)
    def make_node_real(node_id:str):
        """Save a virtual node -- a clip's first or last frame -- as a gallery image, keeping its place."""
        clip_id,separator,which=node_id.rpartition(':')
        if not separator or which not in ('first','last'): raise HTTPException(422,'Only a clip frame can be made into an image')
        asset=save_frame(video_asset(clip_id),which)
        store.move_branch_position(node_id,asset['id'])
        return asset

    @app.get('/api/branches/frames/{clip_id}/{which}')
    def clip_frame(clip_id:str,which:Literal['first','last'],thumbnail:bool=False):
        image,thumb=clip_frame_files(video_asset(clip_id),which)
        path=thumb if thumbnail else image
        if not path.is_file(): raise HTTPException(404,f'This clip has no saved {which} frame')
        return FileResponse(path,media_type='image/jpeg' if thumbnail else 'image/png')

    @app.put('/api/branches/{root_id}/name')
    def name_tree(root_id:str,value:TreeName):
        store.set_branch_name(root_id,value.name.strip()); return {'ok':True}

    @app.get('/api/branches/{item_id}')
    def branch_tree(item_id:str):
        """The tree holding a node or clip, with everything needed to draw it."""
        nodes,trees=branch_graph()
        tree=branches.find_tree(trees,item_id)
        if not tree: raise HTTPException(404,'Not in Branches')
        positions=store.branch_positions()
        return branches.summary(tree,nodes,store.branch_names())|{
            'nodes':[nodes[n] for n in tree['nodes']],'clips':tree['clips'],
            'positions':{n:positions[n] for n in tree['nodes'] if n in positions}}

    @app.put('/api/assets/{asset_id}')
    def mark(asset_id:str,value:Mark):
        if not store.asset(asset_id): raise HTTPException(404,'Image not found')
        store.mark_asset(asset_id,value.favorite,value.rating)
        return store.asset(asset_id)

    @app.get('/api/presets')
    def presets(): return {'items':store.presets()}

    @app.post('/api/presets')
    def save_preset(value:Preset):
        return {'id':store.save_preset(value.name,value.data.model_dump(),value.id)}

    @app.delete('/api/presets/{preset_id}')
    def delete_preset(preset_id:str):
        store.delete_preset(preset_id); return {'ok':True}

    @app.post('/api/inspect')
    async def inspect(file:UploadFile):
        data=await file.read(40*1024*1024+1)
        if len(data)>40*1024*1024: raise HTTPException(413,'Image exceeds 40 MB')
        try: return inspect_image(data)
        except Exception as exc: raise HTTPException(422,'Cannot read image metadata: '+str(exc))

    @app.get('/')
    def index():
        # Version the asset URLs by modification time: a browser that cached an older
        # script without revalidation rules would otherwise keep running it after an update.
        html=(ROOT/'frontend'/'index.html').read_text('utf-8')
        for name in ('app.js','style.css'):
            version=int((ROOT/'frontend'/name).stat().st_mtime)
            html=html.replace(f'/static/{name}"',f'/static/{name}?v={version}"')
        return HTMLResponse(html)

    app.mount('/static',StaticFiles(directory=ROOT/'frontend'),name='static')
    return app
