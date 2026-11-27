import copy
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from pixanvil_app.server import make_app, Generation
from pixanvil_app.workflows import packages, compile_recipe, catalog
from pixanvil_app.media import inspect_image, safe_path
from pixanvil_app.comfy import Comfy

HEADERS={'X-PixAnvil':'local'}


def object_info():
    info={}
    for package in packages().values():
        for node in package['graph'].values():
            info[node['class_type']]={'input':{'required':{k:['STRING',{}] for k in node['inputs']}}}
    def choices(node,key,values): info[node]['input']['required'][key]=[values]
    choices('CheckpointLoaderSimple','ckpt_name',['sd_xl_base_1.0.safetensors'])
    choices('UNETLoader','unet_name',['flux1-dev.safetensors','flux1-schnell.safetensors'])
    choices('DualCLIPLoader','clip_name1',['t5xxl_fp16.safetensors','clip_l.safetensors'])
    choices('DualCLIPLoader','clip_name2',['t5xxl_fp16.safetensors','clip_l.safetensors'])
    choices('VAELoader','vae_name',['ae.safetensors'])
    return info


def png():
    image=Image.new('RGB',(64,64),(60,120,180))
    stream=io.BytesIO(); image.save(stream,format='PNG'); return stream.getvalue()


class Fake:
    submitted=[]
    download_fail=False
    submission_lost=False
    def __init__(self,url): pass
    def health(self): return {'system':{'comfyui_version':'test'},'devices':[]}
    def info(self): return object_info()
    def submit(self,graph,job_id,workflow):
        self.submitted.append(job_id)
        if self.submission_lost: raise TimeoutError('response lost')
        return {'prompt_id':'remote-'+job_id}
    def reconcile(self,job_id): return 'remote-'+job_id if job_id in self.submitted else None
    def history(self,prompt_id): return {'status':{'completed':True,'status_str':'success'},'outputs':{'7':{'images':[{'filename':'out.png','type':'output','subfolder':''}]}}}
    def queue(self): return {'queue_running':[],'queue_pending':[]}
    def download(self,output):
        if self.download_fail: raise OSError('connection dropped during collection')
        return png()


@pytest.fixture
def app(tmp_path):
    Fake.submitted=[]; Fake.download_fail=False; Fake.submission_lost=False
    return make_app(tmp_path,worker=False,client_factory=Fake)


def submit(app,**values):
    with TestClient(app) as client:
        response=client.post('/api/jobs',json={'prompt':'a water knight','workflow':'sdxl',**values},headers=HEADERS)
        assert response.status_code==201,response.text
        return response.json()['jobs']


def test_prompt_to_gallery_and_metadata_roundtrip(app):
    job=submit(app,seed='18446744073709551615')[0]
    app.state.engine.tick(); app.state.engine.tick()
    with TestClient(app) as client:
        completed=client.get('/api/jobs/'+job['id']).json()
        assert completed['state']=='complete'
        asset=client.get('/api/assets').json()['items'][0]
        image=client.get('/api/assets/'+asset['id']+'/image')
        assert image.status_code==200
        metadata=inspect_image(image.content)
        assert metadata['recipe']['seed']=='18446744073709551615'
        assert metadata['recipe']['prompt']=='a water knight'
        assert json.loads(metadata['raw']['prompt'])==job['graph']
        assert (Path(asset['root'])/asset['path']).with_suffix('.json').exists()
    reopened=make_app(app.state.config.state,worker=False,client_factory=Fake)
    assert reopened.state.store.assets()['total']==1
    assert reopened.state.store.job(job['id'])['state']=='complete'


def test_collection_retry_does_not_regenerate(app):
    job=submit(app)[0]; app.state.engine.tick()
    Fake.download_fail=True; app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='collecting'
    Fake.download_fail=False; app.state.engine.tick(); app.state.engine.tick()
    assert Fake.submitted==[job['id']]
    assert app.state.store.assets()['total']==1


def test_ambiguous_submission_reconciles_without_duplicate(app):
    job=submit(app)[0]; Fake.submission_lost=True
    app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='unknown'
    app.state.engine.tick(); app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='complete'
    assert Fake.submitted==[job['id']]


def test_queued_cancel_never_submits(app):
    job=submit(app)[0]
    with TestClient(app) as client:
        assert client.post('/api/jobs/'+job['id']+'/cancel',headers=HEADERS).status_code==200
    app.state.engine.tick()
    assert not Fake.submitted


def test_running_cancel_refused(app):
    job=submit(app)[0]; app.state.engine.tick()
    with TestClient(app) as client:
        assert client.post('/api/jobs/'+job['id']+'/cancel',headers=HEADERS).status_code==409


@pytest.mark.parametrize('values',[{'seed':'18446744073709551616'},{'width':513},{'count':99},{'prompt':'   '},{'quality':'magic'}])
def test_invalid_requests(app,values):
    with TestClient(app) as client:
        assert client.post('/api/jobs',json={'prompt':'test',**values},headers=HEADERS).status_code==422
    assert not app.state.store.jobs()


def test_batch_has_distinct_exact_seeds(app):
    jobs=submit(app,count=3,seed='9007199254740993')
    assert [j['recipe']['seed'] for j in jobs]==['9007199254740993','9007199254740994','9007199254740995']


def test_missing_dependencies_visible():
    info=object_info(); del info['DualCLIPLoader']
    entries=catalog(info)
    assert not next(x for x in entries if x['id']=='flux-dev')['available']
    assert next(x for x in entries if x['id']=='sdxl')['available']


def test_emphasis_compiles_and_unsupported_rejected():
    data=Generation(prompt='a powerful water spell',workflow='sdxl',emphasis=[{'text':'water spell','weight':1.4}]).model_dump()
    recipe,graph=compile_recipe(data,object_info())
    assert '(water spell:1.4)' in graph['2']['inputs']['text']
    assert recipe['prompt']=='a powerful water spell'
    data['workflow']='flux-dev'
    with pytest.raises(ValueError,match='emphasis'): compile_recipe(data,object_info())


def test_a1111_metadata_and_unknown_fields():
    im=Image.new('RGB',(8,8)); meta=PngImagePlugin.PngInfo()
    meta.add_text('parameters','water knight\ncinematic\nNegative prompt: blur\nSteps: 30, Seed: 42, CFG scale: 6, Size: 1024x1024, Model: "a,b", Mystery: retained')
    b=io.BytesIO(); im.save(b,format='PNG',pnginfo=meta)
    parsed=inspect_image(b.getvalue())
    assert parsed['recipe']['prompt']=='water knight\ncinematic'
    assert parsed['recipe']['model']=='a,b'
    assert parsed['fields']['Mystery']=='retained'
    assert inspect_image(png())['format']=='none'


def test_preset_update_does_not_mutate_history(app):
    job=submit(app)[0]
    with TestClient(app) as client:
        value={'name':'Water','data':{'prompt':'first'}}
        preset=client.post('/api/presets',json=value,headers=HEADERS).json()
        value.update(id=preset['id'],data={'prompt':'changed'})
        assert client.post('/api/presets',json=value,headers=HEADERS).status_code==200
        assert client.get('/api/presets').json()['items'][0]['data']['prompt']=='changed'
    assert app.state.store.job(job['id'])['recipe']['prompt']=='a water knight'


def test_local_browser_boundary(app):
    with TestClient(app) as client:
        assert client.post('/api/jobs',json={'prompt':'test'}).status_code==403
        assert client.post('/api/jobs',json={'prompt':'test'},headers={**HEADERS,'Origin':'https://evil.example'}).status_code==403
        assert client.get('/api/settings',headers={'Host':'evil.example'}).status_code==403
        assert client.get('/api/assets').status_code==200


def test_path_escape_rejected(tmp_path):
    with pytest.raises(ValueError): safe_path(tmp_path,'../outside.png')


def test_only_one_coordinator_per_state_directory(tmp_path):
    from pixanvil_app.locking import InstanceLock
    first=InstanceLock(tmp_path/'server.lock')
    try:
        with pytest.raises(RuntimeError,match='already running'):
            InstanceLock(tmp_path/'server.lock')
    finally: first.close()
    replacement=InstanceLock(tmp_path/'server.lock')
    replacement.close()


def test_settings_change_preserves_old_assets(app,tmp_path):
    first=submit(app)[0]; app.state.engine.tick(); app.state.engine.tick()
    original=app.state.store.assets()['items'][0]
    with TestClient(app) as client:
        new=tmp_path/'new-library'
        result=client.put('/api/settings',headers=HEADERS,json={
            'library':str(new),'comfy_url':'http://spark-86e6.local:8188','ssh_host':'exordium@spark-86e6.local'})
        assert result.status_code==200,result.text
        assert client.get('/api/assets/'+original['id']+'/image').status_code==200
    second=submit(app)[0]; app.state.engine.tick(); app.state.engine.tick()
    assert app.state.store.job(second['id'])['recipe']['library']==str(new)
    assert app.state.store.job(first['id'])['recipe']['library']!=str(new)


def test_settings_cannot_change_during_active_job(app,tmp_path):
    submit(app)
    with TestClient(app) as client:
        result=client.put('/api/settings',headers=HEADERS,json={
            'library':str(tmp_path/'elsewhere'),'comfy_url':'http://example:8188','ssh_host':'user@host'})
        assert result.status_code==409


def test_start_healthy_service_does_not_run_ssh(app,monkeypatch):
    import time
    def fail(*args,**kwargs): raise AssertionError('SSH must not run for healthy service')
    monkeypatch.setattr('pixanvil_app.server.subprocess.run',fail)
    with TestClient(app) as client:
        client.post('/api/connection/start',headers=HEADERS)
        for _ in range(30):
            state=client.get('/api/connection').json()['startup']['state']
            if state!='starting': break
            time.sleep(.01)
        assert state=='ready'


def test_backend_failure_keeps_gallery_available(app,monkeypatch):
    job=submit(app)[0]; app.state.engine.tick(); app.state.engine.tick()
    def fail(self): raise ConnectionError('offline')
    monkeypatch.setattr(Fake,'health',fail)
    with TestClient(app) as client:
        assert client.get('/api/connection').json()['state']=='unavailable'
        assert client.get('/api/assets').json()['total']==1
        assert client.get('/api/jobs/'+job['id']).json()['state']=='complete'


def test_partial_download_retry_keeps_one_record_per_output(app,monkeypatch):
    job=submit(app)[0]; app.state.engine.tick()
    monkeypatch.setattr(Fake,'history',lambda self,p:{'status':{'completed':True,'status_str':'success'},
        'outputs':{'7':{'images':[{'filename':'one.png','type':'output'},{'filename':'two.png','type':'output'}]}}})
    calls=[]
    def download(self,out):
        calls.append(out['filename'])
        if len(calls)==2: raise OSError('lost second output')
        return png()
    monkeypatch.setattr(Fake,'download',download)
    app.state.engine.tick()
    assert app.state.store.assets()['total']==1
    app.state.engine.tick()
    assert app.state.store.assets()['total']==2
    assert calls==['one.png','two.png','two.png']
    assert app.state.store.job(job['id'])['state']=='complete'


def test_real_http_adapter_to_gallery(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            route=self.path.split('?')[0]
            if route=='/ws':
                self.send_error(404); return
            data={'/system_stats':{},'/object_info':object_info(),'/history/http-job':Fake('').history('http-job')}
            if route=='/history/http-job': data[route]={'http-job':data[route]}
            body=png() if route=='/view' else json.dumps(data[route]).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert payload['prompt']['5']['inputs']['steps']==32
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"prompt_id":"http-job"}')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        app=make_app(tmp_path,worker=False)
        app.state.config.save({'comfy_url':f'http://127.0.0.1:{server.server_port}'})
        job=submit(app)[0]
        app.state.engine.tick(); app.state.engine.tick()
        assert app.state.store.job(job['id'])['state']=='complete'
        assert app.state.store.assets()['total']==1
    finally:
        app.state.engine.close()
        server.shutdown(); server.server_close(); thread.join()


def test_live_progress_reaches_job_api_and_clears_after_collection(app,monkeypatch):
    from pixanvil_app.progress import Progress
    watchers=[]
    def watch(self,job):
        p=Progress('http://localhost:8188',job)
        p.handle({'type':'progress','data':{'prompt_id':'remote-'+job['id'],'node':'5','value':12,'max':32}})
        watchers.append(p)
        return p
    monkeypatch.setattr(Fake,'watch',watch,raising=False)
    item=submit(app)[0]
    app.state.engine.tick()
    with TestClient(app) as client:
        current=client.get('/api/jobs/'+item['id']).json()
        assert current['progress']['value']==12
        assert current['progress']['stage']=='Sampling'
        assert client.get('/api/jobs').json()['items'][0]['progress']==current['progress']
        app.state.engine.tick()
        assert client.get('/api/jobs/'+item['id']).json()['progress'] is None
    assert len(watchers)==1
    assert Fake.submitted==[item['id']]


def test_queued_job_settings_can_change_before_submission(app):
    job=submit(app,seed='7')[0]
    with TestClient(app) as client:
        response=client.put('/api/jobs/'+job['id'],json={'prompt':'a fire knight','workflow':'sdxl','seed':'42'},headers=HEADERS)
        assert response.status_code==200,response.text
        edited=response.json()
    assert edited['id']==job['id'] and edited['created']==job['created'] and edited['state']=='queued'
    assert edited['recipe']['prompt']=='a fire knight' and edited['recipe']['seed']=='42'
    assert edited['graph']!=job['graph']
    app.state.engine.tick()
    assert Fake.submitted==[job['id']]
    assert app.state.store.job(job['id'])['recipe']['prompt']=='a fire knight'


def test_started_job_settings_cannot_change(app):
    job=submit(app)[0]; app.state.engine.tick()
    with TestClient(app) as client:
        response=client.put('/api/jobs/'+job['id'],json={'prompt':'too late','workflow':'sdxl'},headers=HEADERS)
        assert response.status_code==409
        assert client.put('/api/jobs/missing',json={'prompt':'x','workflow':'sdxl'},headers=HEADERS).status_code==404
    assert app.state.store.job(job['id'])['recipe']['prompt']=='a water knight'


def test_queued_job_edit_makes_one_output(app):
    job=submit(app)[0]
    with TestClient(app) as client:
        assert client.put('/api/jobs/'+job['id'],json={'prompt':'x','workflow':'sdxl','count':2},headers=HEADERS).status_code==422


def test_comfyui_failure_keeps_the_node_and_exception(app,monkeypatch):
    # ComfyUI's report includes the failing node's inputs, which can be whole image tensors.
    report={'node_id':'negative','node_type':'TextEncodeQwenImageEditPlus','exception_type':'RuntimeError',
            'exception_message':'GET was unable to find an engine to execute this computation\n',
            'traceback':['  File "conv.py", line 712, in _conv_forward\n','    return F.conv3d(\n'],
            'current_inputs':{'image1':['tensor('+'0.1255, '*5000+')']}}
    monkeypatch.setattr(Fake,'history',lambda self,prompt_id:{'status':{'completed':False,'status_str':'error',
        'messages':[['execution_start',{}],['execution_error',report]]},'outputs':{}})
    job=submit(app)[0]
    app.state.engine.tick(); app.state.engine.tick()
    found=app.state.store.job(job['id'])
    assert found['state']=='failed'
    assert found['error'].startswith('ComfyUI execution failed in TextEncodeQwenImageEditPlus (node negative): '
                                     'RuntimeError: GET was unable to find an engine to execute this computation\n')
    assert 'F.conv3d' in found['error'] and 'tensor(' not in found['error']


def test_restart_runs_docker_restart_and_is_refused_while_a_job_renders(app,monkeypatch):
    import time
    ran=[]
    class Done: returncode=0; stdout=''; stderr=''
    def fake_run(command,**kwargs): ran.append(command[-1]); return Done()
    monkeypatch.setattr('pixanvil_app.server.subprocess.run',fake_run)
    with TestClient(app) as client:
        assert client.post('/api/connection/restart',headers=HEADERS).status_code==200
        for _ in range(100):
            if client.get('/api/connection').json()['startup']['state']!='starting': break
            time.sleep(.01)
        assert ran==['docker restart comfyui']
        job=submit(app)[0]
        app.state.store.update_job(job['id'],state='running')
        refused=client.post('/api/connection/restart',headers=HEADERS)
        assert refused.status_code==409 and 'rendering' in refused.json()['detail']
