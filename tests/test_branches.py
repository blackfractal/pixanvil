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


def test_gallery_listing_can_be_limited_to_images_or_clips(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    video=clip(app,client,start)
    ids=lambda media:[a['id'] for a in client.get('/api/assets?media='+media).json()['items']]
    assert ids('image')==[start['id']] and ids('video')==[video['id']]
    assert sorted(ids(''))==sorted([start['id'],video['id']])
    assert client.get('/api/assets?media=sound').status_code==422


def test_deleting_a_joining_frame_keeps_the_chain_in_one_tree(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    first=clip(app,client,start)
    frame=client.post(f'/api/branches/nodes/{first["id"]}:last/real',headers=HEADERS).json()
    second=clip(app,client,frame)
    client.delete('/api/assets/'+frame['id'],headers=HEADERS)
    [only]=client.get('/api/branches').json()['items']
    links={c['id']:(c['source'],c['target']) for c in tree(client,start['id'])['clips']}
    assert links[first['id']]==(start['id'],first['id']+':last') and links[second['id']][0]==first['id']+':last'
    assert only['clips']==2
    # Saving the frame again restores the same image identity, so the chain is whole again.
    again=client.post(f'/api/branches/nodes/{first["id"]}:last/real',headers=HEADERS).json()
    assert again['id']==frame['id']
    links={c['id']:(c['source'],c['target']) for c in tree(client,start['id'])['clips']}
    assert links[first['id']][1]==frame['id'] and links[second['id']][0]==frame['id']


def test_a_tree_keeps_its_name_when_its_root_image_is_deleted(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    video=clip(app,client,start)
    client.put(f'/api/branches/{start["id"]}/name',headers=HEADERS,json={'name':'Wizard duel'})
    client.delete('/api/assets/'+start['id'],headers=HEADERS)
    [listed]=client.get('/api/branches').json()['items']
    assert listed['name']=='Wizard duel' and listed['id']=='missing:'+start['id']
    # Renaming the tree in that state still sticks.
    client.put(f'/api/branches/{listed["id"]}/name',headers=HEADERS,json={'name':'Wizard duel, part two'})
    assert tree(client,video['id'])['name']=='Wizard duel, part two'


def label(client,image_id,text):
    response=client.put('/api/branches/labels/'+image_id,headers=HEADERS,json={'label':text})
    assert response.status_code==200,response.text


def test_labels_follow_a_frame_when_it_is_saved_and_when_it_is_deleted(setup):
    app,client=setup
    start=add_image(client,(20,60,120))
    video=clip(app,client,start)
    label(client,start['id'],'A1')
    virtual=next(n for n in tree(client,start['id'])['nodes'] if n['kind']=='virtual')
    label(client,virtual['label_key'],'B1')
    labels=lambda:{n['id']:n['label'] for n in tree(client,video['id'])['nodes']}
    assert labels()=={start['id']:'A1',video['id']+':last':'B1'}
    # Without a typed label, an image is named by the order the tree's images were made.
    defaults=lambda:{n['id']:n['default_label'] for n in tree(client,video['id'])['nodes']}
    later=clip(app,client,start)
    assert defaults()=={start['id']:'Keyframe 1',video['id']+':last':'Keyframe 2',later['id']+':last':'Keyframe 3'}
    client.delete('/api/assets/'+later['id'],headers=HEADERS)
    frame=client.post(f'/api/branches/nodes/{video["id"]}:last/real',headers=HEADERS).json()
    assert labels()=={start['id']:'A1',frame['id']:'B1'}
    client.delete('/api/assets/'+start['id'],headers=HEADERS)
    assert labels()=={'missing:'+start['id']:'A1',frame['id']:'B1'}
    label(client,frame['id'],'  ')
    assert labels()[frame['id']]==''
    assert client.put('/api/branches/labels/nonsense',headers=HEADERS,json={'label':'X'}).status_code==404
    assert client.put('/api/branches/labels/'+frame['id'],headers=HEADERS,json={'label':'X'*41}).status_code==422


def test_export_selected_names_files_after_labels(setup):
    import json, zipfile
    app,client=setup
    a1,b1=add_image(client,(20,60,120),'a1'),add_image(client,(90,30,10),'b1')
    first,second=clip(app,client,a1,end=b1),clip(app,client,a1,end=b1)
    forward=clip(app,client,b1)
    label(client,a1['id'],'A1'); label(client,b1['id'],'B/1')
    client.delete('/api/assets/'+forward['id'],headers=HEADERS)
    forward=clip(app,client,b1)
    virtual=forward['id']+':last'
    items=[a1['id'],b1['id'],virtual,first['id'],second['id'],forward['id']]
    response=client.post('/api/branches/export',headers=HEADERS,json={'items':items})
    assert response.status_code==200,response.text
    assert response.headers['content-type']=='application/zip'
    archive=zipfile.ZipFile(io.BytesIO(response.content))
    names=sorted(archive.namelist())
    frame_name='Keyframe 3'  # unlabelled: named by the order the tree's images were made
    assert names==sorted(['graph.json','images/A1.png','images/B_1.png',f'images/{frame_name}.png',
                          'clips/A1-B_1.mp4','clips/A1-B_1-take2.mp4',f'clips/B_1-{frame_name}.mp4'])
    assert archive.read('images/A1.png')==(Path(a1['root'])/a1['path']).read_bytes()
    assert archive.read(f'images/{frame_name}.png')==image((200,40,40))
    assert archive.read('clips/A1-B_1.mp4')==(Path(first['root'])/first['path']).read_bytes()
    graph=json.loads(archive.read('graph.json'))
    assert graph['tree']['id']==a1['id'] and graph['skipped']==[]
    assert {(c['from_label'],c['to_label'],c['file']) for c in graph['clips']}>={('A1','B/1','clips/A1-B_1.mp4')}
    # Only what was selected is exported, and nothing exportable is refused.
    only=zipfile.ZipFile(io.BytesIO(client.post('/api/branches/export',headers=HEADERS,json={'items':[second['id']]}).content))
    assert sorted(only.namelist())==['clips/A1-B_1-take2.mp4','graph.json']
    client.delete('/api/assets/'+a1['id'],headers=HEADERS)
    refused=client.post('/api/branches/export',headers=HEADERS,json={'items':['missing:'+a1['id']]})
    assert refused.status_code==422
    assert list((app.state.config.state/'exports').iterdir())==[]


def real_video(path,seconds=1,sound=True,size='160x120'):
    """Replace a simulated clip with a real MP4 of `seconds` at 24 fps."""
    from pixanvil_app import sequence
    audio=['-f','lavfi','-i',f'sine=frequency=440:duration={seconds}','-c:a','aac'] if sound else []
    sequence.ffmpeg('-y','-f','lavfi','-i',f'testsrc=size={size}:rate=24:duration={seconds}',*audio,
                    '-c:v','libx264','-pix_fmt','yuv420p','-shortest',path)


def test_export_chain_joins_clips_and_drops_the_repeated_frame(setup):
    from pixanvil_app import sequence
    app,client=setup
    start=add_image(client,(20,60,120))
    first=clip(app,client,start)
    frame=client.post(f'/api/branches/nodes/{first["id"]}:last/real',headers=HEADERS).json()
    second=clip(app,client,frame)
    # One clip with sound, one without and at another size: the join fits and pads both.
    real_video(Path(first['root'])/first['path'])
    real_video(Path(second['root'])/second['path'],sound=False,size='120x120')
    response=client.post('/api/branches/sequence',headers=HEADERS,json={'clips':[first['id'],second['id']]})
    assert response.status_code==201,response.text
    joined=response.json()
    recipe=joined['recipe']
    assert (recipe['operation'],recipe['media'],recipe['clips'])==('sequence','video',[first['id'],second['id']])
    # The steps are kept with the images they join, so the chain can be opened in the builder again.
    assert recipe['chain']==[{'clip':first['id'],'start_key':start['id'],'end_key':frame['id']},
                             {'clip':second['id'],'start_key':frame['id'],'end_key':f"frame-{second['id']}-last"}]
    assert (joined['width'],joined['height'],recipe['frames'])==(160,120,47)
    made=sequence.probe(Path(joined['root'])/joined['path'])
    assert (made.width,made.height,made.audio)==(160,120,True) and abs(made.seconds-47/24)<0.1
    assert client.get(f'/api/assets/{joined["id"]}/thumbnail').status_code==200
    # The joined video is a gallery clip, not a step in the tree.
    assert {c['id'] for c in tree(client,start['id'])['clips']}=={first['id'],second['id']}
    # Nor can it be added through its frames: its clips are already in the tree.
    assert client.post(f'/api/branches/nodes/{joined["id"]}:first/real',headers=HEADERS).status_code==422
    out_of_order=client.post('/api/branches/sequence',headers=HEADERS,json={'clips':[second['id'],first['id']]})
    assert out_of_order.status_code==422 and 'previous one ends' in out_of_order.json()['detail']
    assert client.delete('/api/assets/'+joined['id'],headers=HEADERS).status_code==200
    assert not (Path(joined['root'])/joined['path']).exists()


def test_takes_between_two_images_are_numbered_with_one_preferred(setup):
    app,client=setup
    a,b=add_image(client,(20,60,120),'a'),add_image(client,(90,30,10),'b')
    takes=[clip(app,client,a,end=b) for _ in range(3)]
    queued=client.post('/api/jobs',headers=HEADERS,json={'prompt':'one more','workflow':'minimax-h3-video',
        'operation':'img2vid','source_asset_id':a['id'],'end_asset_id':b['id']}).json()['jobs'][0]
    def state():
        found=tree(client,a['id'])
        return found,{c['id']:(c.get('take'),c.get('takes'),c.get('preferred')) for c in found['clips']}
    found,marks=state()
    assert [marks[t['id']] for t in takes]==[(1,3,True),(2,3,False),(3,3,False)]
    assert marks['job:'+queued['id']]==(None,None,None)
    assert (found['connections'],sum(not c['pending'] for c in found['clips']),found['pending'])==(1,3,1)
    # Choosing another take, and a custom label, persist.
    pick=lambda body:client.put('/api/branches/pairs',headers=HEADERS,json=body)
    assert pick({'clip_id':takes[2]['id'],'preferred':True}).status_code==200
    assert pick({'clip_id':takes[0]['id'],'label_mode':'custom','label':' Leap '}).status_code==200
    found,marks=state()
    assert [marks[t['id']][2] for t in takes]==[False,False,True]
    assert {(c['pair_label_mode'],c['pair_label']) for c in found['clips']}=={('custom','Leap')}
    # Export names the preferred take plainly and the others by their take number.
    import zipfile
    archive=zipfile.ZipFile(io.BytesIO(client.post('/api/branches/export',headers=HEADERS,json={'items':[t['id'] for t in takes]}).content))
    a_name,b_name='Keyframe 1','Keyframe 2'
    assert sorted(n for n in archive.namelist() if n.startswith('clips/'))==sorted(
        [f'clips/{a_name}-{b_name}.mp4',f'clips/{a_name}-{b_name}-take1.mp4',f'clips/{a_name}-{b_name}-take2.mp4'])
    # Deleting the preferred take makes the lowest-numbered remaining one preferred.
    client.delete('/api/assets/'+takes[2]['id'],headers=HEADERS)
    found,marks=state()
    assert [marks[t['id']] for t in takes[:2]]==[(1,2,True),(2,2,False)]
    # Numbers are kept: deleting take 1 leaves take 2 as take 2, and new takes never reuse 1 or 3. Running the
    # worker for another clip also finishes the queued job, which becomes take 4.
    client.delete('/api/assets/'+takes[0]['id'],headers=HEADERS)
    later=clip(app,client,a,end=b)
    found,marks=state()
    finished=next(c for c in found['clips'] if c.get('take')==4)
    assert finished['prompt']=='one more'
    assert (marks[takes[1]['id']],marks[later['id']])==((2,3,True),(5,3,False))
    # Only finished clips can be chosen.
    assert pick({'clip_id':'job:'+queued['id'],'preferred':True}).status_code==404
    assert pick({'clip_id':takes[0]['id'],'label_mode':'fancy'}).status_code==422


def test_saved_chains_keep_their_steps_and_images(setup):
    app,client=setup
    a,b=add_image(client,(20,60,120),'a'),add_image(client,(90,30,10),'b')
    there,back,again=clip(app,client,a,end=b),clip(app,client,b,end=a),clip(app,client,a,end=b)
    steps=[there['id'],back['id'],again['id']]
    response=client.post('/api/branches/chains',headers=HEADERS,json={'name':' Ping pong ','clips':steps})
    assert response.status_code==201,response.text
    saved=response.json()
    assert saved['name']=='Ping pong'
    assert saved['steps']==[{'clip':there['id'],'start_key':a['id'],'end_key':b['id']},
                            {'clip':back['id'],'start_key':b['id'],'end_key':a['id']},
                            {'clip':again['id'],'start_key':a['id'],'end_key':b['id']}]
    assert [c['id'] for c in tree(client,a['id'])['chains']]==[saved['id']]
    # Re-saving replaces the steps and name; a blank name becomes Untitled chain.
    updated=client.put('/api/branches/chains/'+saved['id'],headers=HEADERS,json={'name':'','clips':steps[:2]})
    assert updated.status_code==200 and updated.json()['name']=='Untitled chain' and len(updated.json()['steps'])==2
    # A chain that does not join up is refused, as are unknown chains.
    assert client.post('/api/branches/chains',headers=HEADERS,json={'clips':[there['id'],there['id']]}).status_code==422
    assert client.put('/api/branches/chains/nonsense',headers=HEADERS,json={'clips':steps}).status_code==404
    # Deleting a take keeps the chain listed with the tree: its images are still there.
    client.delete('/api/assets/'+there['id'],headers=HEADERS)
    assert [c['id'] for c in tree(client,a['id'])['chains']]==[saved['id']]
    assert client.delete('/api/branches/chains/'+saved['id'],headers=HEADERS).status_code==200
    assert tree(client,a['id'])['chains']==[]
    assert client.delete('/api/branches/chains/'+saved['id'],headers=HEADERS).status_code==404


def test_gallery_groups_batches_and_take_sets(setup):
    app,client=setup
    a,b=add_image(client,(20,60,120),'a'),add_image(client,(90,30,10),'b')
    submit=lambda **values:client.post('/api/jobs',headers=HEADERS,json={'prompt':'three knights','workflow':'sdxl',**values}).json()['jobs']
    batch,alone=submit(count=3),submit(prompt='one knight')
    assert len({j['recipe']['batch'] for j in batch})==1 and 'batch' not in alone[0]['recipe']
    # Editing a queued job keeps it in its batch.
    edited=client.put('/api/jobs/'+batch[1]['id'],headers=HEADERS,json={'prompt':'three knights, edited','workflow':'sdxl'}).json()
    assert edited['recipe']['batch']==batch[0]['recipe']['batch']
    for _ in range(12):
        app.state.engine.tick()
    first,second=clip(app,client,a,end=b),clip(app,client,a,end=b)
    clip(app,client,a)
    grouped=lambda group,**q:client.get('/api/assets',params={'group':group,**q}).json()
    # Batches: the three from one submission are one stack; everything else, imports included, stands alone.
    found=grouped('batch')
    assert sorted(i['group']['count'] for i in found['items'])==[1]*6+[3] and found['total']==7
    stack=next(i for i in found['items'] if i['group']['count']==3)
    members=client.get('/api/assets/group',params={'kind':'batch','key':stack['group']['key']}).json()['items']
    assert {m['job_id'] for m in members}=={j['id'] for j in batch}
    # A filter lists a stack when any of its items match, with all of its items counted.
    assert [i['group']['count'] for i in grouped('batch',q='edited')['items']]==[3]
    # Take sets: clips between the same two images are one stack, covered by the preferred take.
    takes=next(i for i in grouped('takes')['items'] if i['group']['count']==2)
    assert takes['group']['key']==f"{a['id']}>{b['id']}" and takes['id']==first['id']
    client.put('/api/branches/pairs',headers=HEADERS,json={'clip_id':second['id'],'preferred':True})
    assert next(i for i in grouped('takes')['items'] if i['group']['count']==2)['id']==second['id']
    members=client.get('/api/assets/group',params={'kind':'takes','key':takes['group']['key']}).json()['items']
    assert [(m['id'],m['take'],m['preferred']) for m in members]==[(first['id'],1,False),(second['id'],2,True)]
