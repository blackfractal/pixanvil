import asyncio
import json
import os
import re
import subprocess
import threading
import time
import hashlib
import io
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Literal
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, field_validator

from .config import Config, ROOT
from .store import Store
from . import history
from .comfy import Comfy
from .engine import Engine
from .workflows import catalog, compile_recipe
from .media import import_image, inspect_image, last_frame_files, safe_path, save_thumbnail
from . import branches, sequence
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


class Seen(BaseModel):
    ids: list[str] = Field(default_factory=list,max_length=1000)
    all: bool = False


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


class BranchLabel(BaseModel):
    label: str = Field(max_length=40)


class BranchExport(BaseModel):
    items: list[str] = Field(min_length=1, max_length=2000)


class PairChoice(BaseModel):
    # The pair is the one this finished clip belongs to.
    clip_id: str = Field(max_length=200)
    preferred: bool = False
    label_mode: Literal['stats','custom'] | None = None
    label: str | None = Field(default=None, max_length=60)


class Chain(BaseModel):
    clips: list[str] = Field(min_length=1, max_length=200)


class ChainChoices(BaseModel):
    choices: dict[str,Literal['copy','overwrite','skip']]


class GroupBox(BaseModel):
    left: float
    top: float
    right: float
    bottom: float


class GroupNew(BaseModel):
    name: str = Field(default='', max_length=80)
    members: list[str] = Field(min_length=1, max_length=500)
    box: GroupBox | None = None


class GroupChange(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    collapsed: bool | None = None
    ports: dict[str, str] | None = None
    box: GroupBox | None = None
    clear_box: bool = False


class GroupMembers(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=500)
    remove: list[str] = Field(default_factory=list, max_length=500)


class FrameAt(BaseModel):
    frame: int = Field(ge=0)


class Split(BaseModel):
    clip_id: str = Field(min_length=1, max_length=200)
    # Any frame number is accepted here, so the route can explain why the first and last cannot be split.
    frame: int = Field(ge=0)


class SavedChain(Chain):
    name: str = Field(default='', max_length=80)


def make_app(state=None, worker=True, client_factory=Comfy):
    config=Config(state)
    store=Store(config.state/'pixanvil.sqlite3')
    # History zips being built or uploaded live here; any left behind by a crash are removed at start.
    exports=config.state/'exports'; exports.mkdir(exist_ok=True)
    for leftover in [*exports.glob('history-*.zip'),*exports.glob('import-*.zip')]: leftover.unlink(missing_ok=True)
    library_tasks={}
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

    def start_service(restart=False):
        """Start ComfyUI on SPARK over SSH, or restart it, then wait until it answers. Starting does nothing when
        it is already up; restarting is for a ComfyUI that is up but failing (see the Spark notes on bad GPU state)."""
        try:
            if not restart:
                try:
                    client_factory(config.data['comfy_url']).health()
                    startup.update(state='ready',message='ComfyUI is already ready'); return
                except Exception: pass
            remote='docker restart comfyui' if restart else 'python3 ~/ai/scripts/spark_services.py comfyui'
            command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',config.data['ssh_host'],remote]
            flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            proc=subprocess.run(command,capture_output=True,text=True,timeout=600,creationflags=flags)
            if proc.returncode: raise RuntimeError((proc.stderr or proc.stdout)[-3000:])
            deadline=time.monotonic()+240
            while time.monotonic()<deadline:
                try:
                    client_factory(config.data['comfy_url']).health()
                    startup.update(state='ready',message='ComfyUI is ready'); return
                except Exception: time.sleep(3)
            raise RuntimeError(('Restart' if restart else 'Start')+' command completed, but ComfyUI is not yet reachable. Check the SPARK service logs.')
        except Exception as exc:
            startup.update(state='failed',message=str(exc))
        finally: start_lock.release()

    @app.post('/api/connection/start')
    def start():
        if start_lock.acquire(blocking=False):
            startup.update(state='starting',message='Starting ComfyUI over SSH; first startup may take several minutes.')
            threading.Thread(target=start_service,daemon=True).start()
        return startup.copy()

    @app.post('/api/connection/restart')
    def restart():
        """Restart ComfyUI on SPARK. Refused while a job is on ComfyUI, which a restart would lose; queued jobs
        wait, because jobs are only sent once ComfyUI answers."""
        if any(j['state'] in busy_states for j in store.active()):
            raise HTTPException(409,'A job is rendering on ComfyUI, and restarting would lose it. Wait for it to finish, or abandon it in History first.')
        if not start_lock.acquire(blocking=False):
            raise HTTPException(409,'ComfyUI is already starting or restarting.')
        startup.update(state='starting',message='Restarting ComfyUI on SPARK; this usually takes a minute or two.')
        threading.Thread(target=start_service,args=(True,),daemon=True).start()
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
            # Everything one submission makes is a batch, which the gallery can show as one stack.
            if len(prepared)>1:
                batch=uuid.uuid4().hex
                for recipe,_ in prepared: recipe['batch']=batch
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
            if found['recipe'].get('batch'): recipe['batch']=found['recipe']['batch']
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

    def refuse_during_history_task():
        """Deleting while an export or import reads or writes the library could leave it with records whose files
        are gone, so deletions wait until it finishes."""
        if any(not t.finished for t in library_tasks.values()):
            raise HTTPException(409,'A history export or import is running. Wait for it to finish, or cancel it, before deleting.')

    def remove_job(job):
        """Delete a job, every output it produced, and source snapshots no other job needs."""
        refuse_during_history_task()
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
            refuse_during_history_task()
            # A split's image is the split: while its take exists, it is deleted only as Remove split is, when nothing
            # else uses it, whichever button asks.
            if isinstance(branches.frame_of(asset),int) and store.asset(asset['recipe'].get('parent_asset_id') or ''):
                reasons=split_reasons(asset_id)
                if reasons: raise HTTPException(409,'This image is a split in Branches and is in use: '+'; '.join(reasons)+'. Delete those first.')
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
            refuse_during_history_task()
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

    # ---- History export and import (pixanvil_app/history.py): background tasks the page polls for progress. ----
    def start_library_task(task,work):
        if any(j['state'] in busy_states for j in store.active()):
            raise HTTPException(409,'Some jobs may still be rendering on ComfyUI. Wait for them to finish, or abandon them first.')
        if any(not t.finished for t in library_tasks.values()):
            raise HTTPException(409,'A history export or import is already running.')
        library_tasks[task.id]=task
        def go():
            try: work()
            except history.Cancelled: task.update(stage='Cancelled',error='Cancelled')
            except Exception as exc: task.update(stage='Failed',error=str(exc))
            finally: task.update(finished=True)
        threading.Thread(target=go,daemon=True).start()
        return task.public()

    def library_task(task_id):
        task=library_tasks.get(task_id)
        if not task: raise HTTPException(404,'No such export or import')
        return task

    @app.post('/api/library/export',status_code=202)
    def export_library():
        """Start building a zip of the whole history; download it from the task once it is ready."""
        task=history.Task('export')
        path=exports/f'history-{task.id}.zip'
        def work():
            # The previous export's zip is kept for downloading again only until a new one is made.
            for old in library_tasks.values():
                if old is not task and old.file: old.file.unlink(missing_ok=True); old.file=None
            history.export_history(task,store,config.data,asset_files,saved_inputs(),path)
        return start_library_task(task,work)

    async def receive_zip(file,task):
        path=exports/f'import-{task.id}.zip'
        with path.open('wb') as out:
            while chunk:=await file.read(1<<20): out.write(chunk)
        return path

    @app.post('/api/library/verify',status_code=202)
    async def verify_library(file:UploadFile):
        """Check a history zip without importing it: every check an import makes, plus reading every file to confirm
        it is intact. The result says what importing it here would add."""
        task=history.Task('verify')
        path=await receive_zip(file,task)
        def work():
            try: history.import_history(task,store,config.data['library'],config.state/'inputs',path,engine.lock,verify=True)
            finally: path.unlink(missing_ok=True)
        try: return start_library_task(task,work)
        except HTTPException:
            path.unlink(missing_ok=True); raise

    @app.post('/api/library/import',status_code=202)
    async def import_library(file:UploadFile,chains:Literal['ask','copy','overwrite','skip']='ask'):
        """Merge an uploaded history zip into this library. Anything already present is skipped. A saved chain that
        is here with different steps or name is decided by `chains`; with 'ask' the task waits for choices."""
        task=history.Task('import')
        path=await receive_zip(file,task)
        def work():
            try: history.import_history(task,store,config.data['library'],config.state/'inputs',path,engine.lock,chains)
            finally: path.unlink(missing_ok=True)
        try: return start_library_task(task,work)
        except HTTPException:
            path.unlink(missing_ok=True); raise

    @app.get('/api/library/tasks/{task_id}')
    def library_task_status(task_id:str): return library_task(task_id).public()

    @app.post('/api/library/tasks/{task_id}/choices')
    def choose_chains(task_id:str,value:ChainChoices):
        """The person's choice for each conflicting saved chain, for an import waiting on them."""
        task=library_task(task_id)
        if not task.conflicts: raise HTTPException(409,'This import is not waiting for choices')
        if set(value.choices)!={c['id'] for c in task.conflicts}: raise HTTPException(422,'Choose for every conflicting chain')
        # Cleared here, not only by the import, so the reply never asks again.
        task.choices=dict(value.choices); task.update(conflicts=None,stage='Checking the zip'); task.chosen.set()
        return task.public()

    @app.post('/api/library/tasks/{task_id}/cancel')
    def cancel_library_task(task_id:str):
        task=library_task(task_id); task.cancel_requested=True
        return task.public()

    @app.get('/api/library/tasks/{task_id}/file')
    def library_task_file(task_id:str):
        """The finished export. It can be downloaded again until the next export, or until PixAnvil restarts."""
        task=library_task(task_id)
        if not task.file or not task.file.is_file(): raise HTTPException(404,'This export is no longer available; export again')
        return FileResponse(task.file,media_type='application/zip',filename=f'pixanvil-history-{datetime.now().date().isoformat()}.zip')

    @app.get('/api/assets')
    def assets(q:str='',model:str='',offset:int=0,limit:int=60,favorites:bool=False,media:Literal['','image','video']='',
               group:Literal['','batch','takes']=''):
        return store.assets(q,model,max(0,offset),max(1,min(limit,100)),favorites,media,group)

    @app.get('/api/assets/unseen')
    def unseen(): return store.unseen_counts()

    @app.post('/api/assets/seen')
    def seen(value:Seen):
        """Mark results as viewed: the ones listed, or all of them."""
        store.mark_seen(None if value.all else value.ids)
        return store.unseen_counts()

    @app.get('/api/assets/group')
    def asset_group(kind:Literal['batch','takes'],key:str):
        """Every item in one gallery stack. A take set comes in take order with the preferred take marked, as in
        Branches."""
        items=store.group_members(kind,key)
        if kind=='takes' and items:
            _,trees=branch_graph()
            marks={c['id']:c for t in trees for c in t['clips'] if not c['pending']}
            for item in items:
                mark=marks.get(item['id'],{})
                item.update(take=mark.get('take'),preferred=bool(mark.get('preferred')))
            items.sort(key=lambda a:(a['take'] or 0,a['created']))
        return {'items':items}

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
        return import_image(store,config.data['library'],image.read_bytes(),image.name,asset_id=branches.frame_asset_id(clip['id'],which),operation='frame',frame=which,
                            prompt=clip['recipe']['prompt'],resolved_prompt=clip['recipe']['prompt'],parent_asset_id=clip['id'])

    @app.post('/api/assets/{asset_id}/first-frame',status_code=201)
    def first_frame(asset_id:str): return save_frame(video_asset(asset_id),'first')

    def branch_graph():
        def has_last_frame(clip): return clip_frame_files(clip,'last')[0].is_file()
        nodes,clips=branches.build(store.all_assets(),store.active(),store.branch_added(),has_last_frame)
        numbers,highest=store.take_numbers()
        new=branches.mark_takes(nodes,clips,store.branch_pairs(),numbers,highest)
        if new: store.add_take_numbers(new)
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
        clip=video_asset(clip_id)
        if clip['recipe'].get('operation')=='sequence': raise HTTPException(422,'A joined chain is not part of a tree; its clips already are')
        asset=save_frame(clip,which)
        store.move_branch_position(node_id,asset['id'])
        return asset

    def split_reasons(split_id):
        """Why a split cannot be removed yet (see branches.split_users); empty when it can."""
        nodes,trees=branch_graph()
        clips=[c|{'source_key':branches.label_key(nodes[c['source']]),'target_key':branches.label_key(nodes[c['target']])}
               for t in trees for c in t['clips']]
        return branches.split_users(split_id,clips,store.branch_chains())

    probed={}
    def probe_clip(clip):
        """sequence.probe of a clip's file, remembered until the file changes (a tree reads every clip it draws)."""
        path=safe_path(clip['root'],clip['path'])
        stat=path.stat()
        key=(str(path),stat.st_mtime_ns,stat.st_size)
        if key not in probed: probed[key]=sequence.probe(path)
        return probed[key]

    def clip_frames(clip):
        """A clip's frame rate and frame count, read from the file: a recipe's can differ from what the model wrote."""
        try: info=probe_clip(clip)
        except (ValueError,OSError,subprocess.TimeoutExpired) as exc: raise HTTPException(422,'Cannot read this clip: '+str(exc))
        return info,round(info.seconds*info.fps)

    def file_fps(clip_id,fallback):
        """The frame rate of a clip's file, or `fallback` (the recipe's) when the file cannot be read."""
        clip=store.asset(clip_id)
        try: return probe_clip(clip).fps if clip else fallback
        except (ValueError,OSError,subprocess.TimeoutExpired): return fallback

    def frame_png(clip,frame):
        """Frame number `frame` of a clip (from 0), as PNG bytes."""
        folder=config.state/'exports'; folder.mkdir(exist_ok=True)
        handle,temp=tempfile.mkstemp(suffix='.png',dir=folder); os.close(handle)
        temp=Path(temp)
        try:
            sequence.frame_at(safe_path(clip['root'],clip['path']),frame,temp)
            return temp.read_bytes()
        except (RuntimeError,ValueError,OSError,subprocess.TimeoutExpired) as exc:
            raise HTTPException(500,'Could not take the frame from the clip: '+str(exc))
        finally:
            temp.unlink(missing_ok=True)

    def group_keys(node_ids):
        """The image keys of nodes picked on the page, checked: in one tree, and images that can be grouped."""
        nodes,trees=branch_graph()
        tree=branches.find_tree(trees,node_ids[0]) if node_ids else None
        if not tree or any(n not in tree['nodes'] for n in node_ids): raise HTTPException(422,'Group images from one tree')
        keys=[branches.label_key(nodes[n]) for n in node_ids]
        if None in keys: raise HTTPException(422,'An image still rendering cannot be grouped yet')
        return keys

    def taken_message(taken):
        names=sorted(set(taken.values()))
        return f'{len(taken)} of these images {"is" if len(taken)==1 else "are"} already in the group{"s" if len(names)>1 else ""} {", ".join(chr(39)+n+chr(39) for n in names)}; an image can be in one group'

    @app.post('/api/branches/groups',status_code=201)
    def make_group(value:GroupNew):
        """Group images: drawn as a box around them, or collapsed into one block with ports. Nothing is changed."""
        keys=group_keys(value.members)
        group_id='group-'+uuid.uuid4().hex
        try: store.create_branch_group(group_id,value.name.strip() or 'Group',keys,value.box.model_dump() if value.box else None)
        except ValueError as exc: raise HTTPException(409,taken_message(exc.args[0]))
        return store.branch_group(group_id)

    @app.put('/api/branches/groups/{group_id}')
    def change_group(group_id:str,value:GroupChange):
        """Rename a group, collapse or expand it, or name its ports."""
        if not store.branch_group(group_id): raise HTTPException(404,'Group not found')
        store.update_branch_group(group_id,None if value.name is None else value.name.strip() or 'Group',value.collapsed,
                                  None if value.ports is None else {k:v.strip()[:40] for k,v in value.ports.items() if v.strip()},
                                  value.box.model_dump() if value.box else None,value.clear_box)
        return store.branch_group(group_id)

    @app.put('/api/branches/groups/{group_id}/members')
    def change_group_members(group_id:str,value:GroupMembers):
        """Images joining or leaving a group, as dragged into or out of its box. A group left empty is removed."""
        if not store.branch_group(group_id): raise HTTPException(404,'Group not found')
        add=group_keys(value.add) if value.add else []
        remove=group_keys(value.remove) if value.remove else []
        try: kept=store.change_group_members(group_id,add,remove)
        except ValueError as exc: raise HTTPException(409,taken_message(exc.args[0]))
        return store.branch_group(group_id) if kept else {'removed':group_id}

    @app.delete('/api/branches/groups/{group_id}')
    def ungroup(group_id:str):
        if not store.branch_group(group_id): raise HTTPException(404,'Group not found')
        store.delete_branch_group(group_id)
        return {'ok':True}

    @app.post('/api/branches/splits',status_code=201)
    def make_split(value:Split):
        """Split a take at a frame: the frame is saved as an image, and Branches draws the take's two pieces through
        it as well as the whole take. Splitting at the same frame again returns the same image."""
        clip=video_asset(value.clip_id)
        if clip['recipe'].get('operation')=='sequence': raise HTTPException(422,'A joined chain is not part of a tree; split one of its clips')
        if not branches.find_tree(branch_graph()[1],clip['id']): raise HTTPException(404,'This clip is not in Branches')
        split_id=branches.split_asset_id(clip['id'],value.frame)
        if found:=store.asset(split_id): return found
        info,total=clip_frames(clip)
        # The first and last frames are the clip's own ends, so a split goes between them.
        if not 1<=value.frame<=total-2: raise HTTPException(422,f'Choose a frame between 1 and {total-2}; the first and last are the clip\'s own ends')
        prompt=clip['recipe'].get('prompt','')
        return import_image(store,config.data['library'],frame_png(clip,value.frame),f'{clip["id"]}-frame{value.frame}.png',asset_id=split_id,
                            operation='frame',frame=value.frame,clip_frames=total,fps=info.fps,
                            prompt=prompt,resolved_prompt=prompt,parent_asset_id=clip['id'])

    @app.get('/api/assets/{asset_id}/frames')
    def clip_frame_info(asset_id:str):
        """A clip's frame rate and frame count as read from its file, so frame numbers on the page match the file's."""
        info,total=clip_frames(video_asset(asset_id))
        return {'fps':info.fps,'frames':total}

    @app.post('/api/assets/{asset_id}/frames',status_code=201)
    def save_still(asset_id:str,value:FrameAt):
        """Save one frame of a clip to the gallery as an image of its own. It is linked to the clip (View source) but is
        not a split, so it changes nothing in Branches. Saving the same frame again returns the same image."""
        clip=video_asset(asset_id)
        still_id=f'still-{clip["id"]}-{value.frame}'
        if found:=store.asset(still_id): return found
        _,total=clip_frames(clip)
        if value.frame>=total: raise HTTPException(422,f'This clip has frames 0 to {total-1}')
        prompt=clip['recipe'].get('prompt','')
        return import_image(store,config.data['library'],frame_png(clip,value.frame),f'{clip["id"]}-frame{value.frame}.png',asset_id=still_id,
                            operation='still',frame_number=value.frame,prompt=prompt,resolved_prompt=prompt,parent_asset_id=clip['id'])

    @app.delete('/api/branches/splits/{split_id}')
    def remove_split(split_id:str):
        """Remove split: delete the split's image, which removes its two pieces. Refused while anything else uses it."""
        asset=store.asset(split_id)
        if not asset or not isinstance(branches.frame_of(asset),int): raise HTTPException(404,'Split not found')
        reasons=split_reasons(split_id)
        if reasons: raise HTTPException(409,'This split is in use: '+'; '.join(reasons)+'.')
        return delete_asset(split_id)

    @app.get('/api/branches/frames/{clip_id}/{which}')
    def clip_frame(clip_id:str,which:Literal['first','last'],thumbnail:bool=False):
        image,thumb=clip_frame_files(video_asset(clip_id),which)
        path=thumb if thumbnail else image
        if not path.is_file(): raise HTTPException(404,f'This clip has no saved {which} frame')
        return FileResponse(path,media_type='image/jpeg' if thumbnail else 'image/png')

    @app.put('/api/branches/{tree_id}/name')
    def name_tree(tree_id:str,value:TreeName):
        nodes,trees=branch_graph()
        tree=branches.find_tree(trees,tree_id)
        if not tree: raise HTTPException(404,'Not in Branches')
        # Written where the name is read from, so a rename sticks whichever node currently holds the name.
        store.set_branch_name(branches.name_key(tree,nodes,store.branch_names()),value.name.strip())
        return {'ok':True}

    @app.put('/api/branches/labels/{image_id}')
    def label_node(image_id:str,value:BranchLabel):
        """Name a node, such as A1. The label is used on the graph and in exported file names."""
        nodes,_=branch_graph()
        if not any(branches.label_key(n)==image_id for n in nodes.values()): raise HTTPException(404,'Not in Branches')
        store.set_branch_label(image_id,value.label.strip())
        return {'ok':True}

    @app.put('/api/branches/pairs')
    def choose_for_pair(value:PairChoice):
        """Make a take the preferred one between its two images, or set how their connection is labelled."""
        nodes,trees=branch_graph()
        clip=next((c for t in trees for c in t['clips'] if c['id']==value.clip_id),None)
        if not clip or clip['pending']: raise HTTPException(404,'Not a finished clip in Branches')
        keys=branches.pair_keys(nodes,clip)
        store.set_branch_pair(*keys,preferred_clip=clip['id'] if value.preferred else None,
                              label_mode=value.label_mode,label=None if value.label is None else value.label.strip())
        return {'ok':True}

    def node_picture(node):
        """The full-size PNG behind an image node or a clip frame not yet saved."""
        if node['kind']=='image':
            asset=store.asset(node['asset_id'])
            return safe_path(asset['root'],asset['path'])
        return clip_frame_files(video_asset(node['clip_id']),node['frame'])[0]

    def asset_path(asset_id):
        asset=store.asset(asset_id)
        return safe_path(asset['root'],asset['path'])

    def clip_piece(clip):
        """What a Branches clip plays, for sequence.render: its whole file, or a piece's frame range of its take's."""
        piece=clip.get('piece')
        if not piece: return sequence.Piece(asset_path(clip['id']))
        return sequence.Piece(asset_path(piece['clip']),piece['start_frame'],piece['end_frame'])

    def tree_title(tree,nodes):
        names=store.branch_names()
        return names.get(branches.name_key(tree,nodes,names)) or tree['root']['prompt'][:80] or 'Untitled tree'

    @app.post('/api/branches/export')
    def export_selected(value:BranchExport):
        """A zip of the selected images and clips, named after node labels, with a graph.json that links them."""
        nodes,trees=branch_graph()
        tree=next(filter(None,(branches.find_tree(trees,item) for item in value.items)),None)
        if not tree: raise HTTPException(404,'Not in Branches')
        images,clips,graph=branches.plan_export(tree,nodes,value.items,store.branch_labels())
        if not images and not clips: raise HTTPException(422,'Nothing selected can be exported: select images with a picture, or finished clips')
        name=tree_title(tree,nodes)
        graph={'tree':{'id':tree['id'],'name':name},'exported':datetime.now(timezone.utc).isoformat()}|graph
        folder=config.state/'exports'; folder.mkdir(exist_ok=True)
        handle,temp=tempfile.mkstemp(suffix='.zip',dir=folder); os.close(handle)
        temp=Path(temp)
        try:
            # Pictures and clips are already compressed, so they are stored as they are.
            with zipfile.ZipFile(temp,'w',zipfile.ZIP_STORED) as archive:
                for node,file in images:
                    archive.write(node_picture(node),f'images/{file}.png')
                for clip,file in clips:
                    if not clip.get('piece'):
                        archive.write(asset_path(clip['id']),f'clips/{file}.mp4'); continue
                    # A piece is cut from its take into a file of its own.
                    cut=temp.with_name(temp.stem+'-piece.mp4')
                    try:
                        sequence.render([clip_piece(clip)],cut)
                        archive.write(cut,f'clips/{file}.mp4')
                    finally:
                        cut.unlink(missing_ok=True)
                archive.writestr('graph.json',json.dumps(graph,indent=2),compress_type=zipfile.ZIP_DEFLATED)
        except Exception:
            temp.unlink(missing_ok=True); raise
        return FileResponse(temp,media_type='application/zip',filename=branches.file_name(name)+'.zip',
                            background=BackgroundTask(temp.unlink,missing_ok=True))

    def saved_chain(chain_id,value):
        """Check a chain from the builder and save it under this id, new or not."""
        nodes,trees=branch_graph()
        tree=branches.find_tree(trees,value.clips[0])
        if not tree: raise HTTPException(404,'Not in Branches')
        problem=branches.chain_errors(tree,value.clips)
        if problem: raise HTTPException(422,problem)
        store.save_branch_chain(chain_id,value.name.strip() or 'Untitled chain',branches.chain_steps(tree,nodes,value.clips))
        return store.branch_chain(chain_id)

    @app.post('/api/branches/chains',status_code=201)
    def save_chain(value:SavedChain): return saved_chain('chain-'+uuid.uuid4().hex,value)

    @app.put('/api/branches/chains/{chain_id}')
    def update_chain(chain_id:str,value:SavedChain):
        if not store.branch_chain(chain_id): raise HTTPException(404,'Saved chain not found')
        return saved_chain(chain_id,value)

    @app.delete('/api/branches/chains/{chain_id}')
    def delete_chain(chain_id:str):
        if not store.branch_chain(chain_id): raise HTTPException(404,'Saved chain not found')
        store.delete_branch_chain(chain_id)
        return {'ok':True}

    @app.post('/api/branches/sequence',status_code=201)
    def export_chain(value:Chain):
        """Join a chain of clips into one video, saved to the gallery as a clip of its own."""
        nodes,trees=branch_graph()
        tree=branches.find_tree(trees,value.clips[0])
        if not tree: raise HTTPException(404,'Not in Branches')
        problem=branches.chain_errors(tree,value.clips)
        if problem: raise HTTPException(422,problem)
        root=Path(config.data['library']).resolve()
        asset_id='sequence-'+uuid.uuid4().hex
        folder=root/'Sequences'/datetime.now(timezone.utc).date().isoformat(); folder.mkdir(parents=True,exist_ok=True)
        path=folder/(asset_id+'.mp4')
        thumb=root/'.thumbnails'/(asset_id+'.jpg'); thumb.parent.mkdir(parents=True,exist_ok=True)
        try:
            in_tree={c['id']:c for c in tree['clips']}
            width,height,fps,seconds=sequence.render([clip_piece(in_tree[c]) for c in value.clips],path)
            sequence.first_frame(path,path.with_suffix('.png'))
            with Image.open(path.with_suffix('.png')) as poster:
                poster.load(); save_thumbnail(poster,thumb)
        except (RuntimeError,ValueError,OSError,subprocess.TimeoutExpired) as exc:
            for file in (path,path.with_suffix('.png'),thumb): file.unlink(missing_ok=True)
            raise HTTPException(500,'Could not join the clips: '+str(exc))
        prompt=f'{tree_title(tree,nodes)} · chain of {len(value.clips)} clips'
        recipe={'operation':'sequence','workflow':'sequence','media':'video','prompt':prompt,'resolved_prompt':prompt,
                'negative':'','model':'','seed':'','steps':'','cfg':'','clips':value.clips,'tree':tree['id'],
                'chain':branches.chain_steps(tree,nodes,value.clips),
                'width':width,'height':height,'fps':fps,'frames':round(seconds*fps)}
        store.add_asset({'id':asset_id,'job_id':None,'root':str(root),'path':str(path.relative_to(root)),
                         'thumbnail':str(thumb.relative_to(root)),'recipe':recipe,'width':width,'height':height})
        return store.asset(asset_id)

    @app.get('/api/branches/{item_id}')
    def branch_tree(item_id:str):
        """The tree holding a node or clip, with everything needed to draw it."""
        nodes,trees=branch_graph()
        tree=branches.find_tree(trees,item_id)
        if not tree: raise HTTPException(404,'Not in Branches')
        positions,labels=store.branch_positions(),store.branch_labels()
        defaults=branches.keyframe_names(tree,nodes)
        # Anything using a split is in its tree, so the tree's clips are enough to say why it cannot be removed.
        keyed=[c|{'source_key':branches.label_key(nodes[c['source']]),'target_key':branches.label_key(nodes[c['target']])} for c in tree['clips']]
        chains=store.branch_chains()
        # Frame numbers on the page (the player, the spark, Split here) use the file's frame rate, as splits do.
        clips=[c|{'fps':file_fps(c['id'],c.get('fps'))} if not c.get('pending') and not c.get('piece') else c for c in tree['clips']]
        def drawn(node):
            key=branches.label_key(node)
            extra={'split':node['split']|{'in_use':branches.split_users(key,keyed,chains)}} if node.get('split') else {}
            return node|{'label_key':key,'label':labels.get(key,''),'default_label':defaults[node['id']]}|extra
        return branches.summary(tree,nodes,store.branch_names())|{
            'nodes':[drawn(nodes[n]) for n in tree['nodes']],'clips':clips,
            'chains':branches.tree_chains(tree,nodes,store.branch_chains()),
            'groups':branches.tree_groups(tree,nodes,store.branch_groups()),
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
