import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pixanvil_app.server import make_app, Generation
from pixanvil_app.workflows import compile_recipe, catalog
from test_app import Fake, HEADERS, object_info, png

MODEL='sulphur_distil-Q6_K.gguf'
# Synthetic fixture bytes with an MP4 signature; not a playable clip.
MP4=b'\x00\x00\x00\x18ftypmp42'+b'\x00'*32


H3_MODEL='minimax_h3_fl2va_pruned_int8_convrot.safetensors'


def video_info():
    info=object_info()
    info['UNETLoader']['input']['required']['unet_name']=[['flux1-dev.safetensors','flux1-schnell.safetensors',H3_MODEL]]
    info['VAELoader']['input']['required']['vae_name']=[['ae.safetensors',
        'minimax_h3_video_vae_int8_convrot.safetensors','minimax_h3_audio_vae_fp32.safetensors']]
    # ComfyUI lists Hugging Face download-cache files beside the real model.
    info['LTX2_SM_Model']['input']['required']['gguf']=['COMBO',{'options':[
        'none','.cache/huggingface/download/'+MODEL+'.metadata','gemma-3-12b-it-qat-Q4_0.gguf',MODEL]}]
    return info


class VideoFake(Fake):
    def info(self): return video_info()
    def history(self,prompt_id):
        return {'status':{'completed':True,'status_str':'success'},'outputs':{
            'save':{'images':[{'filename':'PixAnvil_00001_.mp4','subfolder':'video','type':'output'}],'animated':[True]},
            'poster':{'images':[{'filename':'PixAnvil_poster_00001_.png','subfolder':'','type':'output'}]}}}
    def download(self,output): return MP4 if output['filename'].endswith('.mp4') else png()


@pytest.fixture
def app(tmp_path):
    Fake.submitted=[]; Fake.download_fail=False; Fake.submission_lost=False
    return make_app(tmp_path,worker=False,client_factory=VideoFake)


def post(app,**values):
    with TestClient(app) as client:
        return client.post('/api/jobs',json={'prompt':'a wizard casts a spell','workflow':'sulphur-video',**values},headers=HEADERS)


def test_video_recipe_uses_package_defaults_and_bindings():
    data=Generation(prompt='a wizard casts a spell',negative='blurry',workflow='sulphur-video').model_dump()
    recipe,graph=compile_recipe(data,video_info())
    assert (recipe['media'],recipe['model'])==('video',MODEL)
    assert (recipe['width'],recipe['height'],recipe['frames'],recipe['fps'])==(768,512,97,24)
    assert 0<=int(recipe['seed'])<=2147483647
    assert graph['model']['inputs']['gguf']==MODEL
    assert graph['encode']['inputs']['prompt']=='a wizard casts a spell'
    assert graph['encode']['inputs']['negative_prompt']=='blurry'
    assert graph['latents']['inputs']['num_frames']==97
    assert graph['video']['inputs']['fps']==graph['latents']['inputs']['frame_rate']==24
    assert graph['sampler']['inputs']['seed']==int(recipe['seed'])


def test_h3_recipe_uses_its_own_defaults_and_frame_rule():
    data=Generation(prompt='a wizard casts a spell',workflow='minimax-h3-video').model_dump()
    recipe,graph=compile_recipe(data,video_info())
    assert (recipe['model'],recipe['width'],recipe['height'],recipe['frames'],recipe['fps'])==(H3_MODEL,832,448,124,24)
    assert graph['unet']['inputs']['unet_name']==H3_MODEL
    assert graph['cond']['inputs']['length']==124 and graph['cond']['inputs']['prompt']=='a wizard casts a spell'
    assert graph['noise']['inputs']['noise_seed']==int(recipe['seed'])
    # 97 frames is valid for Sulphur (8n+1) and invalid for H3 (17n+5).
    with pytest.raises(ValueError,match='multiple of 17 plus 5'): compile_recipe({**data,'frames':97},video_info())
    with pytest.raises(ValueError,match='negative'): compile_recipe({**data,'negative':'blurry'},video_info())


def test_h3_model_is_not_offered_to_flux():
    entries={c['id']:c for c in catalog(video_info())}
    assert entries['minimax-h3-video']['models']==[H3_MODEL]
    assert H3_MODEL not in entries['flux-dev']['models']
    assert entries['minimax-h3-video']['durations'][1]=={'seconds':5,'frames':124}


def test_empty_negative_prompt_falls_back_to_package_default():
    recipe,graph=compile_recipe(Generation(prompt='a wizard',workflow='sulphur-video').model_dump(),video_info())
    assert 'blurry' in recipe['negative'] and graph['encode']['inputs']['negative_prompt']==recipe['negative']


def test_download_cache_entries_are_not_offered_as_models():
    entry=next(c for c in catalog(video_info()) if c['id']=='sulphur-video')
    assert entry['available'] and entry['models']==[MODEL] and entry['media']=='video'


def test_image_recipe_keeps_image_defaults():
    recipe,_=compile_recipe(Generation(prompt='a knight',workflow='sdxl').model_dump(),object_info())
    assert (recipe['width'],recipe['height'],recipe['media'])==(1024,1024,'image')
    assert recipe['frames'] is None and recipe['fps'] is None


@pytest.mark.parametrize('values',[
    {'frames':100},{'frames':17},{'seed':'2147483648'},
    {'refinement':{'workflow':'sdxl'}},{'operation':'refine','source_asset_id':'missing'}])
def test_invalid_video_requests(app,values):
    assert post(app,**values).status_code==422
    assert not app.state.store.jobs()


def test_video_batch_seeds_stay_in_range(app):
    response=post(app,count=3)
    assert response.status_code==201,response.text
    seeds=[int(j['recipe']['seed']) for j in response.json()['jobs']]
    assert seeds==[seeds[0],seeds[0]+1,seeds[0]+2] and seeds[-1]<=2147483647


def test_video_to_gallery(app):
    job=post(app,frames=49).json()['jobs'][0]
    app.state.engine.tick(); app.state.engine.tick()
    with TestClient(app) as client:
        assert client.get('/api/jobs/'+job['id']).json()['state']=='complete'
        asset=client.get('/api/assets').json()['items'][0]
        assert asset['path'].endswith('.mp4') and (asset['width'],asset['height'])==(64,64)
        assert asset['recipe']['frames']==49
        video=client.get('/api/assets/'+asset['id']+'/file')
        assert video.headers['content-type']=='video/mp4' and video.content==MP4
        assert video.headers['content-disposition']==f'inline; filename="{asset["id"]}.mp4"'
        assert client.get('/static/app.js').headers['cache-control']=='no-cache'
        page=client.get('/').text
        assert '/static/app.js?v=' in page and '/static/style.css?v=' in page
        partial=client.get('/api/assets/'+asset['id']+'/file',headers={'Range':'bytes=4-7'})
        assert partial.status_code==206 and partial.content==b'ftyp'
        thumbnail=client.get('/api/assets/'+asset['id']+'/thumbnail')
        assert thumbnail.headers['content-type']=='image/jpeg'
    sidecar=json.loads((Path(asset['root'])/asset['path']).with_suffix('.json').read_text('utf-8'))
    assert sidecar['recipe']['workflow']=='sulphur-video' and sidecar['api_graph']==job['graph']
    assert app.state.store.assets()['total']==1


def test_video_without_poster_fails_clearly(app,monkeypatch):
    monkeypatch.setattr(VideoFake,'history',lambda self,p:{'status':{'completed':True,'status_str':'success'},
        'outputs':{'save':{'images':[{'filename':'clip.mp4','type':'output'}]}}})
    job=post(app).json()['jobs'][0]
    app.state.engine.tick(); app.state.engine.tick()
    found=app.state.store.job(job['id'])
    assert found['state']=='failed' and 'poster' in found['error']


def test_non_mp4_video_output_is_not_saved(app,monkeypatch):
    monkeypatch.setattr(VideoFake,'download',lambda self,output:png())
    job=post(app).json()['jobs'][0]
    app.state.engine.tick(); app.state.engine.tick()
    found=app.state.store.job(job['id'])
    assert found['state']=='collecting' and 'MP4' in found['error']
    assert app.state.store.assets()['total']==0


def test_render_finishing_between_history_and_queue_checks_is_not_lost(app,monkeypatch):
    job=post(app).json()['jobs'][0]
    app.state.engine.tick()
    finished=VideoFake.history
    calls=[]
    def history(self,prompt_id):
        calls.append(prompt_id)
        return None if len(calls)==1 else finished(self,prompt_id)
    monkeypatch.setattr(VideoFake,'history',history)
    app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='complete'
    assert len(calls)==2
