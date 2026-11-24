import copy
import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pixanvil_app.server import make_app, Generation
from pixanvil_app.comfy import Comfy, choices
from pixanvil_app.workflows import compile_recipe
from pixanvil_app.media import inspect_image
from comfy_nodes.pixanvil_noise import blend_weights
from test_app import Fake, HEADERS, object_info


class ImageFake(Fake):
    uploads=[]
    upload_fail=False
    def info(self):
        info=object_info()
        info.update(json.loads((Path(__file__).parent/'fixtures/image_tool_nodes.json').read_text()))
        info['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0].append('dreamshaperXL_alpha2Xl10.safetensors')
        return info
    def upload(self,name,data):
        self.uploads.append((name,hashlib.sha256(data).hexdigest()))
        if self.upload_fail: raise TimeoutError('upload disconnected')
        return 'pixanvil/'+name
    def download(self,output):
        stream=io.BytesIO();Image.new('RGB',(512,512),(60,120,180)).save(stream,format='PNG');return stream.getvalue()


@pytest.fixture
def setup(tmp_path):
    ImageFake.submitted=[];ImageFake.submission_lost=False;ImageFake.uploads=[];ImageFake.upload_fail=False
    app=make_app(tmp_path,worker=False,client_factory=ImageFake)
    with TestClient(app) as client:
        first=client.post('/api/jobs',headers=HEADERS,json={'prompt':'water knight','workflow':'sdxl','seed':'18446744073709551615','width':512,'height':512}).json()['jobs'][0]
        app.state.engine.tick();app.state.engine.tick()
        source=client.get('/api/assets').json()['items'][0]
        yield app,client,source


def create(client,source,**kwargs):
    return client.post('/api/jobs',headers=HEADERS,json={
        'prompt':'water knight','workflow':'sdxl','source_asset_id':source['id'],**kwargs})


def test_variation_batch_keeps_base_seed_and_increments_only_variation_seed(setup):
    app,client,source=setup
    response=create(client,source,operation='variation',count=2,variation_seed='18446744073709551615',variation_strength=0)
    assert response.status_code==201,response.text
    jobs=response.json()['jobs']
    assert [j['recipe']['seed'] for j in jobs]==['18446744073709551615']*2
    assert [j['recipe']['variation_seed'] for j in jobs]==['18446744073709551615','0']
    assert jobs[0]['recipe']['width']==512
    assert jobs[0]['graph']['var-noise']['inputs']['strength']==0
    assert jobs[0]['graph']['5']['class_type']=='SamplerCustomAdvanced'
    assert jobs[0]['recipe']['parent_asset_id']==source['id']


def test_refinement_upload_retries_before_submission_and_keeps_snapshot(setup):
    app,client,source=setup
    response=create(client,source,operation='refine',denoise=.2,model='dreamshaperXL_alpha2Xl10.safetensors')
    assert response.status_code==201,response.text
    job=response.json()['jobs'][0];recipe=job['recipe']
    original=(Path(source['root'])/source['path']).read_bytes()
    assert job['graph']['5']['inputs']['denoise']==.2
    assert job['graph']['input-encode']['inputs']['vae']==['1',2]
    assert recipe['source_sha256']==hashlib.sha256(original).hexdigest()
    # Input remains valid if the managed original is moved after queuing.
    path=Path(source['root'])/source['path'];path.rename(path.with_suffix('.moved'))
    ImageFake.upload_fail=True;app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='queued'
    assert job['id'] not in ImageFake.submitted
    ImageFake.upload_fail=False;app.state.engine.tick();app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='complete'
    assert ImageFake.submitted.count(job['id'])==1
    assert ImageFake.uploads[-1]==ImageFake.uploads[-2]
    child=client.get('/api/assets/'+job['id']+'-0').json()
    metadata=inspect_image((Path(child['root'])/child['path']).read_bytes())
    assert metadata['recipe']['parent_asset_id']==source['id']


def test_corrupt_staged_source_never_submits(setup):
    app,client,source=setup
    job=create(client,source,operation='refine').json()['jobs'][0]
    (app.state.config.state/'inputs'/job['recipe']['input_name']).write_bytes(b'changed')
    app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='failed'
    assert job['id'] not in ImageFake.submitted


@pytest.mark.parametrize('factor',[2,4])
def test_upscale_output_dimensions_and_model_choices(setup,factor):
    app,client,source=setup
    response=create(client,source,operation='upscale',scale=factor)
    assert response.status_code==201,response.text
    job=response.json()['jobs'][0]
    assert job['recipe']['width']==512*factor
    assert job['graph']['size']['inputs']['width']==512*factor
    assert job['graph']['model']['inputs']['model_name']=='RealESRGAN_x4plus.pth'
    assert job['recipe']['refinement'] is None
    assert choices(ImageFake('').info(),'UpscaleModelLoader','model_name')==['RealESRGAN_x4plus.pth']


def test_second_model_pass_uses_target_encoder_and_only_final_output():
    data=Generation(prompt='water knight',workflow='flux-dev',refinement={
        'workflow':'sdxl','model':'dreamshaperXL_alpha2Xl10.safetensors','denoise':.2}).model_dump()
    recipe,graph=compile_recipe(data,ImageFake('').info())
    assert graph['ref-encode']['inputs']=={'pixels':['9',0],'vae':['ref-1',2]}
    assert graph['ref-5']['inputs']['latent_image']==['ref-encode',0]
    assert graph['ref-5']['inputs']['denoise']==.2
    assert len([n for n in graph.values() if n['class_type']=='SaveImage'])==1
    assert recipe['refinement']['seed']==recipe['seed']
    assert recipe['refinement']['model']=='dreamshaperXL_alpha2Xl10.safetensors'


@pytest.mark.parametrize('values',[
    {'operation':'refine','source_asset_id':'missing'},
    {'operation':'variation','variation_strength':1.1},
    {'operation':'refine','denoise':0},
    {'operation':'refine','workflow':'flux-schnell'},
    {'operation':'upscale','count':2},
    {'operation':'upscale','upscale_model':'missing.pth'},
    {'operation':'upscale','scale':3},
    {'operation':'variation','variation_seed':'18446744073709551616'},
])
def test_invalid_operations_rejected_before_queueing(setup,values):
    app,client,source=setup
    response=create(client,source,**values)
    assert response.status_code==422,response.text
    assert len(app.state.store.jobs())==1


def test_missing_variation_node_is_capability_gated(setup,monkeypatch):
    app,client,source=setup
    info=ImageFake('').info();del info['PixAnvilVariationNoise']
    # Fresh app avoids the existing valid capability cache.
    monkeypatch.setattr(ImageFake,'info',lambda self:info)
    other=make_app(app.state.config.state,worker=False,client_factory=ImageFake)
    with TestClient(other) as c:
        assert not c.get('/api/image-tools').json()['variation']['available']
        assert create(c,source,operation='variation').status_code==422


def test_upload_http_contract(monkeypatch):
    def request(self,method,path,**kwargs):
        assert method=='POST' and path=='/upload/image'
        assert kwargs['data']=={'subfolder':'pixanvil','type':'input','overwrite':'false'}
        assert kwargs['files']['image']==('digest.png',b'png','image/png')
        return {'name':'digest.png','subfolder':'pixanvil','type':'input'}
    monkeypatch.setattr(Comfy,'request',request)
    assert Comfy('http://localhost').upload('digest.png',b'png')=='pixanvil/digest.png'
    monkeypatch.setattr(Comfy,'request',lambda *a,**k:{'name':'renamed.png','subfolder':'pixanvil','type':'input'})
    with pytest.raises(ValueError,match='unexpected input path'):Comfy('http://localhost').upload('digest.png',b'png')


def test_noise_blend_endpoints_and_continuity():
    assert blend_weights(0,0)==(1,0)
    assert blend_weights(0,1)==(0,1)
    a,b=blend_weights(0,.5)
    assert a==pytest.approx(b)
    assert a*a+b*b==pytest.approx(1)
    assert blend_weights(1,.25)==(.75,.25)
