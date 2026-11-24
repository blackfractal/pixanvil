import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pixanvil_app.server import make_app
from pixanvil_app.workflows import fit_canvas
from test_app import Fake, HEADERS
from test_video import VideoFake, video_info


class AnimateFake(VideoFake):
    uploads=[]
    def info(self):
        info=video_info()
        info.update(json.loads((Path(__file__).parent/'fixtures/image_tool_nodes.json').read_text()))
        return info
    def upload(self,name,data):
        self.uploads.append((name,hashlib.sha256(data).hexdigest()))
        return 'pixanvil/'+name


def picture(size=(1402,1122),kind='JPEG'):
    stream=io.BytesIO(); Image.new('RGB',size,(60,120,180)).save(stream,format=kind); return stream.getvalue()


@pytest.fixture
def setup(tmp_path):
    Fake.submitted=[]; Fake.download_fail=False; Fake.submission_lost=False; AnimateFake.uploads=[]
    app=make_app(tmp_path,worker=False,client_factory=AnimateFake)
    with TestClient(app) as client:
        response=client.post('/api/assets/import',headers=HEADERS,files={'file':('wizard boy.jpg',picture())})
        assert response.status_code==201,response.text
        yield app,client,response.json()


def animate(client,source,**values):
    return client.post('/api/jobs',headers=HEADERS,json={'prompt':'he casts a spell','workflow':'minimax-h3-video',
        'operation':'img2vid','source_asset_id':source['id'],**values})


def test_import_saves_a_png_asset_once(setup):
    app,client,asset=setup
    assert (asset['width'],asset['height'])==(1402,1122) and asset['path'].endswith('.png')
    assert asset['recipe']['operation']=='import' and asset['recipe']['prompt']=='wizard boy'
    with Image.open(Path(asset['root'])/asset['path']) as saved: assert saved.format=='PNG'
    assert client.get(f'/api/assets/{asset["id"]}/thumbnail').headers['content-type']=='image/jpeg'
    again=client.post('/api/assets/import',headers=HEADERS,files={'file':('other name.jpg',picture())})
    assert again.json()['id']==asset['id'] and app.state.store.assets()['total']==1


def test_import_keeps_png_bytes_and_rejects_other_files(setup):
    app,client,_=setup
    data=picture((64,64),'PNG')
    asset=client.post('/api/assets/import',headers=HEADERS,files={'file':('a.png',data)}).json()
    assert (Path(asset['root'])/asset['path']).read_bytes()==data
    assert client.post('/api/assets/import',headers=HEADERS,files={'file':('a.txt',b'not an image')}).status_code==422
    assert app.state.store.assets()['total']==2


def test_canvas_follows_the_source_shape():
    assert fit_canvas((1402,1122),500000)==(768,640)
    assert fit_canvas((1122,1402),500000)==(640,768)
    assert fit_canvas((8000,500),500000)==(1536,256)


def test_animate_starts_the_clip_on_the_source_image(setup):
    app,client,source=setup
    response=animate(client,source)
    assert response.status_code==201,response.text
    job=response.json()['jobs'][0]; recipe,graph=job['recipe'],job['graph']
    assert (recipe['width'],recipe['height'],recipe['frames'])==(768,640,124)
    assert (recipe['operation'],recipe['parent_asset_id'],recipe['source_fit'])==('img2vid',source['id'],'center-crop')
    original=(Path(source['root'])/source['path']).read_bytes()
    assert recipe['source_sha256']==hashlib.sha256(original).hexdigest()
    assert graph['start-image']['inputs']['image']=='pixanvil/'+recipe['input_name']
    assert graph['start-size']['inputs']|{'image':0}=={'image':0,'upscale_method':'lanczos','width':768,'height':640,'crop':'center'}
    assert graph['cond']['inputs']['first_frame']==['start-size',0]
    assert (graph['cond']['inputs']['width'],graph['cond']['inputs']['height'])==(768,640)
    app.state.engine.tick(); app.state.engine.tick()
    assert AnimateFake.uploads==[(recipe['input_name'],recipe['source_sha256'])]
    assert client.get('/api/jobs/'+job['id']).json()['state']=='complete'
    video=client.get('/api/assets').json()['items'][0]
    assert video['path'].endswith('.mp4') and video['recipe']['parent_asset_id']==source['id']


def test_animate_accepts_a_chosen_canvas_and_text_to_video_has_no_start_image(setup):
    app,client,source=setup
    job=animate(client,source,width=832,height=448).json()['jobs'][0]
    assert (job['recipe']['width'],job['graph']['start-size']['inputs']['height'])==(832,448)
    plain=client.post('/api/jobs',headers=HEADERS,json={'prompt':'a wizard','workflow':'minimax-h3-video'}).json()['jobs'][0]
    assert 'first_frame' not in plain['graph']['cond']['inputs'] and 'start-image' not in plain['graph']


@pytest.mark.parametrize('values',[{'workflow':'sulphur-video'},{'workflow':'sdxl'},{'source_asset_id':'missing'},{'source_asset_id':None}])
def test_animate_rejections(setup,values):
    app,client,source=setup
    assert animate(client,source,**values).status_code==422
    assert not app.state.store.jobs()


def test_a_video_cannot_be_the_start_image(setup):
    app,client,source=setup
    animate(client,source)
    app.state.engine.tick(); app.state.engine.tick()
    video=client.get('/api/assets').json()['items'][0]
    response=animate(client,video)
    assert response.status_code==422 and 'still image' in response.json()['detail']


EDIT_MODEL='qwen_image_edit_2511_fp8mixed.safetensors'


class EditFake(AnimateFake):
    def info(self):
        info=super().info()
        info['UNETLoader']['input']['required']['unet_name'][0].append(EDIT_MODEL)
        info['VAELoader']['input']['required']['vae_name'][0].append('qwen_image_vae.safetensors')
        return info
    def history(self,prompt_id): return Fake.history(self,prompt_id)
    def download(self,output): return picture((1104,944),'PNG')


@pytest.fixture
def edit_setup(tmp_path):
    Fake.submitted=[]; Fake.download_fail=False; Fake.submission_lost=False; AnimateFake.uploads=[]
    app=make_app(tmp_path,worker=False,client_factory=EditFake)
    with TestClient(app) as client:
        source=client.post('/api/assets/import',headers=HEADERS,files={'file':('wizard boy.jpg',picture())}).json()
        yield app,client,source


def edit(client,source,**values):
    return client.post('/api/jobs',headers=HEADERS,json={'prompt':'make the cloak red','workflow':'qwen-image-edit',
        'operation':'edit','source_asset_id':source['id'],**values})


def test_edit_changes_a_copy_of_the_source_image(edit_setup):
    app,client,source=edit_setup
    response=edit(client,source,quality='balanced')
    assert response.status_code==201,response.text
    job=response.json()['jobs'][0]; recipe,graph=job['recipe'],job['graph']
    assert (recipe['model'],recipe['steps'],recipe['cfg'],recipe['parent_asset_id'])==(EDIT_MODEL,20,4,source['id'])
    assert graph['input']['inputs']['image']=='pixanvil/'+recipe['input_name']
    assert graph['positive']['inputs']['prompt']=='make the cloak red' and graph['negative']['inputs']['prompt']==''
    assert EDIT_MODEL not in next(c for c in client.get('/api/workflows').json()['items'] if c['id']=='flux-dev')['models']
    app.state.engine.tick(); app.state.engine.tick()
    assert AnimateFake.uploads==[(recipe['input_name'],recipe['source_sha256'])]
    result=client.get('/api/assets').json()['items'][0]
    # The source stays in the library; the result records its own size.
    assert (result['width'],result['height'])==(1104,944) and app.state.store.assets()['total']==2


@pytest.mark.parametrize('values',[{'operation':'txt2img','source_asset_id':None},{'operation':'refine'},
                                   {'workflow':'sdxl'},{'source_asset_id':None}])
def test_edit_rejections(edit_setup,values):
    app,client,source=edit_setup
    assert edit(client,source,**values).status_code==422
    assert not app.state.store.jobs()


def test_first_frame_of_a_clip_becomes_an_image_linked_to_it(setup):
    app,client,source=setup
    animate(client,source)
    app.state.engine.tick(); app.state.engine.tick()
    video=client.get('/api/assets').json()['items'][0]
    response=client.post(f'/api/assets/{video["id"]}/first-frame',headers=HEADERS)
    assert response.status_code==201,response.text
    frame=response.json()
    assert (frame['recipe']['operation'],frame['recipe']['parent_asset_id'],frame['recipe']['prompt'])==('frame',video['id'],'he casts a spell')
    assert frame['path'].endswith('.png') and (frame['width'],frame['height'])==(64,64)
    assert client.post(f'/api/assets/{video["id"]}/first-frame',headers=HEADERS).json()['id']==frame['id']
    assert client.post(f'/api/assets/{source["id"]}/first-frame',headers=HEADERS).status_code==404
    # The frame is a still image, so it can start a new clip.
    assert animate(client,frame).status_code==201


def test_clip_can_take_an_end_image_alone_or_with_a_start_image(setup):
    app,client,start=setup
    end=client.post('/api/assets/import',headers=HEADERS,files={'file':('end.png',picture((640,800),'PNG'))}).json()
    both=animate(client,start,end_asset_id=end['id'])
    assert both.status_code==201,both.text
    job=both.json()['jobs'][0]; recipe,graph=job['recipe'],job['graph']
    assert graph['cond']['inputs']['first_frame']==['start-size',0] and graph['cond']['inputs']['last_frame']==['end-size',0]
    assert graph['end-image']['inputs']['image']=='pixanvil/'+recipe['end_input_name']!=graph['start-image']['inputs']['image']
    # The canvas follows the start image; the end image is cropped to it.
    assert (recipe['width'],recipe['height'],graph['end-size']['inputs']['width'])==(768,640,768)
    app.state.engine.tick(); app.state.engine.tick()
    assert sorted(AnimateFake.uploads)==sorted([(recipe['input_name'],recipe['source_sha256']),(recipe['end_input_name'],recipe['end_sha256'])])
    assert client.get('/api/jobs/'+job['id']).json()['state']=='complete'
    alone=animate(client,start,source_asset_id=None,end_asset_id=end['id'])
    assert alone.status_code==201,alone.text
    job=alone.json()['jobs'][0]; recipe,graph=job['recipe'],job['graph']
    assert 'first_frame' not in graph['cond']['inputs'] and graph['cond']['inputs']['last_frame']==['end-size',0]
    assert (recipe['width'],recipe['height'],recipe['parent_asset_id'])==(640,768,end['id'])


@pytest.mark.parametrize('values',[{'operation':'txt2img','source_asset_id':None},{'end_asset_id':'missing'},
                                   {'workflow':'sdxl','operation':'refine'}])
def test_end_image_rejections(setup,values):
    app,client,start=setup
    end=client.post('/api/assets/import',headers=HEADERS,files={'file':('end.png',picture((640,800),'PNG'))}).json()
    assert animate(client,start,**{'end_asset_id':end['id'],**values}).status_code==422
    assert not app.state.store.jobs()
