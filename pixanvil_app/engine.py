import json
import threading
import time
from .comfy import Comfy
from .media import collect, collect_video, safe_path
import hashlib


def execution_error(status):
    """A readable account of a failed ComfyUI run: which node failed and the exception, then the last traceback lines.

    ComfyUI's report also holds the node's inputs (whole image tensors), so keeping the end of the raw
    report used to lose the exception message itself."""
    for kind, data in status.get('messages', []):
        if kind == 'execution_error':
            where = f'{data.get("node_type")} (node {data.get("node_id")})'
            traceback = ''.join(data.get('traceback') or [])[-1500:]
            return f'ComfyUI execution failed in {where}: {data.get("exception_type")}: {data.get("exception_message", "").strip()}\n{traceback}'
    return 'ComfyUI execution failed: '+json.dumps(status.get('messages', []))[-3000:]


class Engine:
    def __init__(self, store, config, client_factory=Comfy):
        self.store, self.config, self.client_factory = store, config, client_factory
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.monitor = None
        self.monitor_job = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='pixanvil-jobs')
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=5)
        if self.monitor: self.monitor.close()

    def with_progress(self, job):
        monitor = self.monitor
        if job and job['id']==self.monitor_job and monitor and job['state'] in ('submitting','running'):
            return {**job, 'progress': monitor.snapshot()}
        return {**job, 'progress': None} if job else None

    def run(self):
        while not self.stop.is_set():
            try: self.tick()
            except Exception:
                import logging
                logging.exception('Job coordinator error')
            self.stop.wait(2)

    def tick(self):
        with self.lock:
            active=self.store.active()
            if self.monitor and not any(j['id']==self.monitor_job and j['state'] in ('queued','submitting','running','unknown') for j in active):
                self.monitor.close()
                self.monitor = None
                self.monitor_job = None
            # Only dispatch when all preceding owned jobs have been reconciled.
            for job in active:
                if job['state']!='queued':
                    self.advance(job)
                    return
            if active: self.advance(active[0])

    def advance(self, job):
        client=self.client_factory(job['recipe']['comfy_url'])
        try:
            if job['state']=='queued':
                # Check connectivity before marking a submission ambiguous.
                client.health()
                # A job can carry a source image and, for video, an end image.
                for name_key,hash_key in (('input_name','source_sha256'),('end_input_name','end_sha256')):
                    name=job['recipe'].get(name_key)
                    if not name: continue
                    path=safe_path(self.config.state/'inputs',name)
                    if not path.is_file():
                        self.store.update_job(job['id'],state='failed',error='Saved source input is missing; restore it or submit from the gallery again')
                        return
                    payload=path.read_bytes()
                    if hashlib.sha256(payload).hexdigest()!=job['recipe'][hash_key]:
                        self.store.update_job(job['id'],state='failed',error='Saved source input changed; render was not submitted')
                        return
                    client.upload(name,payload)
                self.watch(client, job)
                self.store.update_job(job['id'],state='submitting',error=None)
                try:
                    result=client.submit(job['graph'],job['id'],job['recipe']['workflow'])
                except Exception as exc:
                    # A 4xx validation response is definitive; a timeout/5xx is not.
                    status=getattr(getattr(exc,'response',None),'status_code',0)
                    self.store.update_job(job['id'],state='failed' if 400<=status<500 else 'unknown',
                                          error=f'Submission could not be confirmed: {exc}')
                    return
                if 'prompt_id' not in result:
                    self.store.update_job(job['id'],state='failed',error=json.dumps(result))
                    return
                self.store.update_job(job['id'],state='running',prompt_id=result['prompt_id'],error=None)
                if self.monitor: self.monitor.bind(result['prompt_id'])
                return
            self.watch(client, job)
            if not job['prompt_id']:
                prompt_id=client.reconcile(job['id'])
                if prompt_id:
                    self.store.update_job(job['id'],state='running',prompt_id=prompt_id,error=None)
                    if self.monitor: self.monitor.bind(prompt_id)
                else:
                    self.store.update_job(job['id'],state='unknown',error='Submission unresolved. No automatic retry: use Abandon to release this local job, then submit again if appropriate.')
                return
            history=client.history(job['prompt_id'])
            if not history:
                queue=client.queue()
                present=any(e[1]==job['prompt_id'] for e in queue.get('queue_running',[])+queue.get('queue_pending',[]))
                # A render can finish between the two requests: gone from the queue, now in history.
                if not present: history=client.history(job['prompt_id'])
            if not history:
                self.store.update_job(job['id'],state='running' if present else 'unknown',
                    error=None if present else 'Job absent from backend queue/history. It may have been cleared or ComfyUI restarted; use Abandon to release it.')
                return
            status=history.get('status',{})
            failed=status.get('status_str')=='error'
            complete=status.get('completed',False)
            if not complete and not failed: return
            outputs=[]
            for node_id, output in history.get('outputs',{}).items():
                for item in output.get('images',[]):
                    if item.get('type')=='output': outputs.append(item)
            # SaveVideo reports its MP4 under "images"; the stills beside it are the poster (first) frame
            # and, for clips made since Branches, the last frame.
            videos=[item for item in outputs if item.get('filename','').lower().endswith('.mp4')]
            stills=[item for item in outputs if item not in videos]
            last=next((item for item in stills if item.get('filename','').startswith('PixAnvil_last')),None)
            poster=next((item for item in stills if item is not last),None)
            if videos:
                outputs=videos
                if not poster and not failed:
                    self.store.update_job(job['id'],state='failed',error='Video workflow completed without a poster frame')
                    return
            saved=[]
            self.store.update_job(job['id'],state='collecting',error=None)
            for index, output in enumerate(outputs):
                asset_id=f'{job["id"]}-{index}'
                if not self.store.asset(asset_id):
                    if videos: collect_video(self.store,job,client.download(output),client.download(poster),index,
                                             client.download(last) if last else None)
                    else: collect(self.store,job,client.download(output),index)
                saved.append(asset_id)
                self.store.update_job(job['id'],outputs=saved)
            if failed:
                self.store.update_job(job['id'],state='failed',error=execution_error(status))
            elif not saved:
                self.store.update_job(job['id'],state='failed',error='Workflow completed without saved outputs')
            else:
                self.store.update_job(job['id'],state='complete',outputs=saved,error=None)
        except Exception as exc:
            # Preserve phase/prompt_id so downloads and interrupted sessions resume.
            self.store.update_job(job['id'],error=str(exc))

    def watch(self, client, job):
        if self.monitor_job==job['id'] or not hasattr(client,'watch'): return
        if self.monitor: self.monitor.close()
        self.monitor = client.watch(job)
        self.monitor_job = job['id']
