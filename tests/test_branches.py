import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pixanvil_app.server import make_app
from test_app import Fake, HEADERS
from test_image_to_video import AnimateFake


def image(color):
    stream=io.BytesIO(); Image.new('RGB',(640,512),color).save(stream,format='PNG'); return stream.getvalue()


class BranchFake(AnimateFake):
    """A video workflow that reports a poster and a last frame, as the packages now do."""
    def history(self,prompt_id):
        history=super().history(prompt_id)
        history['outputs']['last']={'images':[{'filename':'PixAnvil_last_00001_.png','subfolder':'','type':'output'}]}
        return history
    def download(self,output):
        if output['filename'].startswith('PixAnvil_last'): return image((200,40,40))
        return super().download(output)


@pytest.fixture
def setup(tmp_path):
    Fake.submitted=[]; Fake.download_fail=False; Fake.submission_lost=False; AnimateFake.uploads=[]
    app=make_app(tmp_path,worker=False,client_factory=BranchFake)
    with TestClient(app) as client:
        yield app,client


def add_image(client,color,name='start'):
    response=client.post('/api/assets/import',headers=HEADERS,files={'file':(name+'.png',image(color))})
    assert response.status_code==201,response.text
    return response.json()


def clip(app,client,source=None,end=None,**values):
    """Queue an H3 clip and run it to completion; returns its gallery asset."""
    response=client.post('/api/jobs',headers=HEADERS,json={'prompt':'the spell grows','workflow':'minimax-h3-video',
        'operation':'img2vid' if source or end else 'txt2img','source_asset_id':source and source['id'],
        'end_asset_id':end and end['id'],**values})
    assert response.status_code==201,response.text
    job=response.json()['jobs'][0]
    for _ in range(4):
        app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='complete'
    return client.get(f'/api/assets/{job["id"]}-0').json()


def tree(client,item_id):
    response=client.get('/api/branches/'+item_id)
    assert response.status_code==200,response.text
    return response.json()


def test_clips_save_their_last_frame_and_delete_it_with_them(setup):
    app,client=setup
    video=clip(app,client,add_image(client,(20,60,120)))
    path=Path(video['root'])/video['path']
    last,thumbnail=path.with_suffix('.last.png'),(Path(video['root'])/video['thumbnail']).with_suffix('.last.jpg')
    assert last.read_bytes()==image((200,40,40)) and thumbnail.is_file()
    frame=client.get(f'/api/branches/frames/{video["id"]}/last')
    assert frame.headers['content-type']=='image/png' and frame.content==image((200,40,40))
    assert client.get(f'/api/branches/frames/{video["id"]}/last?thumbnail=true').headers['content-type']=='image/jpeg'
    client.delete('/api/assets/'+video['id'],headers=HEADERS)
    assert not last.exists() and not thumbnail.exists()


def test_a_clip_connects_its_start_image_to_a_virtual_last_frame(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    video=clip(app,client,start)
    [listed]=client.get('/api/branches').json()['items']
    assert (listed['id'],listed['clips'],listed['nodes'],listed['seconds'])==(start['id'],1,2,5.2)
    found=tree(client,video['id'])
    assert found['id']==start['id']
    nodes={n['id']:n for n in found['nodes']}
    assert nodes[start['id']]['kind']=='image'
    end=nodes[video['id']+':last']
    assert end['kind']=='virtual' and end['available'] and end['image']==f'/api/branches/frames/{video["id"]}/last'
    assert [(c['source'],c['target'],c['pending']) for c in found['clips']]==[(start['id'],video['id']+':last',False)]


def test_making_a_virtual_node_real_keeps_its_place(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    video=clip(app,client,start)
    virtual=video['id']+':last'
    client.put('/api/branches/layout',headers=HEADERS,json={'positions':{virtual:{'x':300,'y':40}}})
    response=client.post(f'/api/branches/nodes/{virtual}/real',headers=HEADERS)
    assert response.status_code==201,response.text
    frame=response.json()
    assert (frame['recipe']['operation'],frame['recipe']['frame'],frame['recipe']['parent_asset_id'])==('frame','last',video['id'])
    found=tree(client,start['id'])
    assert [(c['source'],c['target']) for c in found['clips']]==[(start['id'],frame['id'])]
    assert found['positions']=={frame['id']:{'x':300,'y':40}}
    assert client.post('/api/branches/nodes/nonsense/real',headers=HEADERS).status_code==422


def test_a_backward_clip_ends_on_a_node_and_starts_from_its_first_frame(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    forward=clip(app,client,start)
    # Queued: drawn as a pending connection into the start image.
    queued=client.post('/api/jobs',headers=HEADERS,json={'prompt':'what came before','workflow':'minimax-h3-video',
        'operation':'img2vid','end_asset_id':start['id']}).json()['jobs'][0]
    pending=[c for c in tree(client,start['id'])['clips'] if c['pending']]
    assert [(c['source'],c['target'],c['state']) for c in pending]==[(f'job:{queued["id"]}:start',start['id'],'queued')]
    for _ in range(4): app.state.engine.tick()
    backward=client.get(f'/api/assets/{queued["id"]}-0').json()
    found=tree(client,start['id'])
    links={c['id']:(c['source'],c['target']) for c in found['clips']}
    assert links=={forward['id']:(start['id'],forward['id']+':last'),backward['id']:(backward['id']+':first',start['id'])}
    first=next(n for n in found['nodes'] if n['id']==backward['id']+':first')
    assert first['kind']=='virtual' and first['available']


def test_a_clip_ending_on_another_tree_merges_them_under_the_older_name(setup):
    app,client=setup
    older,newer=add_image(client,(20,60,120),'older'),add_image(client,(90,30,10),'newer')
    clip(app,client,older); clip(app,client,newer)
    assert len(client.get('/api/branches').json()['items'])==2
    client.put(f'/api/branches/{older["id"]}/name',headers=HEADERS,json={'name':'Wizard duel'})
    clip(app,client,older,end=newer)
    [merged]=client.get('/api/branches').json()['items']
    assert (merged['id'],merged['name'],merged['clips'])==(older['id'],'Wizard duel',3)


def test_text_to_video_joins_branches_once_its_first_frame_is_saved(setup):
    app,client=setup
    video=clip(app,client)
    assert client.get('/api/branches').json()['items']==[]
    assert client.get('/api/branches/'+video['id']).status_code==404
    frame=client.post(f'/api/branches/nodes/{video["id"]}:first/real',headers=HEADERS).json()
    assert frame['recipe']['frame']=='first'
    found=tree(client,video['id'])
    assert found['id']==frame['id']
    assert [(c['source'],c['target']) for c in found['clips']]==[(frame['id'],video['id']+':last')]


def test_images_can_be_added_by_hand_before_they_have_clips(setup):
    app,client=setup
    picture=add_image(client,(20,60,120))
    response=client.post('/api/branches/images',headers=HEADERS,json={'asset_id':picture['id']})
    assert response.status_code==201 and response.json()=={'tree':picture['id']}
    [listed]=client.get('/api/branches').json()['items']
    assert (listed['id'],listed['nodes'],listed['clips'])==(picture['id'],1,0)
    assert tree(client,picture['id'])['nodes'][0]['added'] is True
    client.delete('/api/branches/images/'+picture['id'],headers=HEADERS)
    assert client.get('/api/branches').json()['items']==[]
    assert client.post('/api/branches/images',headers=HEADERS,json={'asset_id':'missing'}).status_code==404


def test_layout_saves_forgets_and_follows_deletes(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    video=clip(app,client,start)
    virtual=video['id']+':last'
    client.put('/api/branches/layout',headers=HEADERS,json={'positions':{start['id']:{'x':0,'y':0},virtual:{'x':240,'y':0}}})
    assert tree(client,start['id'])['positions']=={start['id']:{'x':0,'y':0},virtual:{'x':240,'y':0}}
    client.put('/api/branches/layout',headers=HEADERS,json={'positions':{start['id']:None}})
    assert tree(client,start['id'])['positions']=={virtual:{'x':240,'y':0}}
    client.delete('/api/assets/'+video['id'],headers=HEADERS)
    assert app.state.store.branch_positions()=={}
    assert client.put('/api/branches/layout',headers=HEADERS,json={'positions':{'a':{'x':'nan','y':0}}}).status_code==422


def test_start_over_clears_branches(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    clip(app,client,start)
    client.put(f'/api/branches/{start["id"]}/name',headers=HEADERS,json={'name':'Wizard duel'})
    client.put('/api/branches/layout',headers=HEADERS,json={'positions':{start['id']:{'x':0,'y':0}}})
    client.post('/api/branches/images',headers=HEADERS,json={'asset_id':start['id']})
    assert client.delete('/api/library',headers=HEADERS).status_code==200
    store=app.state.store
    assert (store.branch_positions(),store.branch_names(),store.branch_added())==({},{},[])


def test_identical_frames_from_two_clips_stay_two_nodes(setup):
    # The simulated workflow returns the same last-frame bytes for every clip.
    app,client=setup
    start=add_image(client,(20,60,120))
    first,second=clip(app,client,start),clip(app,client,start)
    frames=[client.post(f'/api/branches/nodes/{c["id"]}:last/real',headers=HEADERS).json() for c in (first,second)]
    assert frames[0]['id']!=frames[1]['id']
    assert [f['recipe']['parent_asset_id'] for f in frames]==[first['id'],second['id']]
    targets={c['id']:c['target'] for c in tree(client,start['id'])['clips']}
    assert targets=={first['id']:frames[0]['id'],second['id']:frames[1]['id']}
    # Saving the same frame again returns the image already saved.
    again=client.post(f'/api/branches/nodes/{first["id"]}:last/real',headers=HEADERS).json()
    assert again['id']==frames[0]['id']


def test_deleting_an_image_leaves_a_placeholder_and_keeps_its_clips(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    forward=clip(app,client,start)
    backward=clip(app,client,end=start)
    client.delete('/api/assets/'+start['id'],headers=HEADERS)
    found=tree(client,forward['id'])
    missing=next(n for n in found['nodes'] if n['kind']=='missing')
    assert missing['id']=='missing:'+start['id'] and not missing['available']
    links={c['id']:(c['source'],c['target']) for c in found['clips']}
    assert links=={forward['id']:(missing['id'],forward['id']+':last'),backward['id']:(backward['id']+':first',missing['id'])}
    assert [t['clips'] for t in client.get('/api/branches').json()['items']]==[2]
