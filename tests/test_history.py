import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pixanvil_app import history
from pixanvil_app.server import make_app
from test_app import Fake, HEADERS
from test_branches import BranchFake, add_image, clip, image, real_video, tree
from test_image_to_video import AnimateFake


@pytest.fixture
def two(tmp_path):
    """Two separate PixAnvils, each with its own state and library folder."""
    Fake.submitted=[]; Fake.download_fail=False; Fake.submission_lost=False; AnimateFake.uploads=[]
    apps=[make_app(tmp_path/name,worker=False,client_factory=BranchFake) for name in ('a','b')]
    with TestClient(apps[0]) as a, TestClient(apps[1]) as b:
        yield (apps[0],a),(apps[1],b)


def finish(client,started):
    assert started.status_code==202,started.text
    task_id=started.json()['id']
    for _ in range(500):
        task=client.get('/api/library/tasks/'+task_id).json()
        if task['finished']: return task
        time.sleep(.01)
    raise AssertionError('The task did not finish')


def export(client):
    task=finish(client,client.post('/api/library/export',headers=HEADERS))
    assert task['stage']=='Ready to download' and task['error'] is None,task
    assert task['files_done']==task['files_total']
    response=client.get(f'/api/library/tasks/{task["id"]}/file')
    assert response.status_code==200 and response.headers['content-type']=='application/zip'
    # It can be downloaded again, in case the browser's download failed.
    assert client.get(f'/api/library/tasks/{task["id"]}/file').content==response.content
    return response.content


def import_zip(client,data):
    return finish(client,client.post('/api/library/import',headers=HEADERS,files={'file':('history.zip',data,'application/zip')}))


def everything(client):
    items=client.get('/api/assets',params={'limit':100}).json()['items']
    return sorted((a['id'],a['created'],a['favorite'],a['recipe'].get('prompt')) for a in items)


def drawn(client,item_id):
    """What Branches shows for a tree, without anything tied to where the files live."""
    found=tree(client,item_id)
    return {'name':found['name'],
            'nodes':sorted((n['id'],n['label'],n['kind']) for n in found['nodes']),
            'clips':sorted((c['id'],c.get('take'),c.get('preferred'),c['pair_label']) for c in found['clips']),
            'positions':found['positions'],'chains':[(c['name'],c['steps']) for c in found['chains']]}


def test_history_round_trips_into_an_empty_pixanvil(two):
    (app,a),(other,b)=two
    start,end=add_image(a,(20,60,120),'start'),add_image(a,(90,30,10),'end')
    first,second=clip(app,a,start,end=end),clip(app,a,start,end=end)
    forward=clip(app,a,start)
    for made in (first,forward):
        real_video(Path(made['root'])/made['path'])
    frame=a.post(f'/api/branches/nodes/{forward["id"]}:last/real',headers=HEADERS).json()
    a.put('/api/branches/labels/'+start['id'],headers=HEADERS,json={'label':'A1'})
    a.put(f'/api/branches/{start["id"]}/name',headers=HEADERS,json={'name':'The long shot'})
    a.put('/api/branches/layout',headers=HEADERS,json={'positions':{start['id']:{'x':10,'y':20}}})
    a.put('/api/branches/pairs',headers=HEADERS,json={'clip_id':second['id'],'preferred':True})
    a.post('/api/branches/chains',headers=HEADERS,json={'name':'Run','clips':[first['id']]})
    joined=a.post('/api/branches/sequence',headers=HEADERS,json={'clips':[forward['id']]})
    assert joined.status_code==201,joined.text
    a.put('/api/assets/'+start['id'],headers=HEADERS,json={'favorite':True})
    a.post('/api/presets',headers=HEADERS,json={'name':'Knight','data':{'prompt':'a water knight','workflow':'sdxl'}})
    queued=a.post('/api/jobs',headers=HEADERS,json={'prompt':'still waiting','workflow':'sdxl'}).json()['jobs'][0]
    before=everything(a)
    data=export(a)
    names=zipfile.ZipFile(io.BytesIO(data)).namelist()
    manifest=json.loads(zipfile.ZipFile(io.BytesIO(data)).read('manifest.json'))
    assert manifest['format']==1 and manifest['missing']==[] and manifest['settings']['library']==app.state.config.data['library']
    assert any(n.endswith('.last.png') for n in names) and any(n.startswith('library/Sequences/') for n in names)
    assert any(n.startswith('inputs/') for n in names)
    settings=b.get('/api/settings').json()

    result=import_zip(b,data)
    assert result['stage']=='Done' and result['error'] is None,result
    added=result['result']['added']
    assert (added['jobs'],added['assets'],added['presets'])==(len(a.get('/api/jobs').json()['items']),len(before),1)
    assert result['result']['settings']['library']==app.state.config.data['library']
    # Everything is back in the same order, with the same times and favorites, from the new library folder.
    assert everything(b)==before
    library=Path(other.state.config.data['library'])
    for asset in b.get('/api/assets',params={'limit':100}).json()['items']:
        assert Path(asset['root'])==library.resolve()
        assert b.get(f'/api/assets/{asset["id"]}/file').status_code==200
    assert drawn(b,start['id'])==drawn(a,start['id'])
    assert {p['name'] for p in b.get('/api/presets').json()['items']}=={'Knight'}
    # The queued job comes back cancelled, and is never sent to ComfyUI.
    job=b.get('/api/jobs/'+queued['id']).json()
    assert job['state']=='cancelled' and 'Imported' in job['error']
    assert other.state.store.active()==[]
    # Import leaves this PixAnvil's settings alone.
    assert b.get('/api/settings').json()==settings
    # A second import of the same zip adds nothing.
    again=import_zip(b,data)['result']
    assert set(again['added'].values())=={0} and again['skipped']['assets']==len(before)


def test_import_merges_and_keeps_local_choices(two):
    (app,a),(other,b)=two
    shared=add_image(a,(20,60,120),'shared')
    a.put('/api/assets/'+shared['id'],headers=HEADERS,json={'favorite':True})
    add_image(a,(90,30,10),'only in a')
    local=add_image(b,(10,90,10),'only in b')
    assert add_image(b,(20,60,120),'shared')['id']==shared['id']
    result=import_zip(b,export(a))['result']
    assert (result['added']['assets'],result['skipped']['assets'])==(1,1)
    found={a['id']:a for a in b.get('/api/assets').json()['items']}
    assert len(found)==3 and local['id'] in found
    # The shared image keeps this PixAnvil's choice, not the export's favorite.
    assert found[shared['id']]['favorite']==0


def test_export_and_import_are_refused_while_a_job_renders(two):
    (app,a),(other,b)=two
    job=a.post('/api/jobs',headers=HEADERS,json={'prompt':'busy','workflow':'sdxl'}).json()['jobs'][0]
    app.state.store.update_job(job['id'],state='running')
    assert a.post('/api/library/export',headers=HEADERS).status_code==409
    assert a.post('/api/library/import',headers=HEADERS,files={'file':('h.zip',b'x','application/zip')}).status_code==409


def zip_of(entries):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        for name,data in entries.items():
            archive.writestr(name,data if isinstance(data,(bytes,str)) else json.dumps(data))
    return stream.getvalue()


MANIFEST={'format':1}
ASSET={'id':'import-x','job_id':None,'created':'2026-01-01T00:00:00+00:00','path':'Imports/import-x.png',
       'thumbnail':'.thumbnails/import-x.jpg','recipe':{'prompt':'x'},'width':640,'height':512,'favorite':0,'rating':0,
       'files':['Imports/import-x.png','.thumbnails/import-x.jpg']}


@pytest.mark.parametrize('entries,message',[
    ({'manifest.json':MANIFEST,'library/../evil.png':b'x'},'unsafe entry'),
    ({'manifest.json':MANIFEST,'/abs.png':b'x'},'unsafe entry'),
    ({'jobs.json':[]},'no manifest'),
    ({'manifest.json':{'format':99}},'format 99'),
    ({'manifest.json':MANIFEST,'jobs.json':[],'assets.json':[ASSET],'presets.json':[],'branches.json':{},
      'library/Imports/import-x.png':image((1,2,3))},'missing from it'),
    ({'manifest.json':MANIFEST,'stray.txt':'x'},'does not know'),
])
def test_bad_zips_are_refused_before_anything_is_written(two,entries,message):
    (other,b),_=two
    library=Path(other.state.config.data['library'])
    before=sorted(library.rglob('*')) if library.exists() else []
    task=import_zip(b,zip_of(entries))
    assert task['stage']=='Failed' and message in task['error'],task
    assert (sorted(library.rglob('*')) if library.exists() else [])==before
    assert b.get('/api/assets').json()['items']==[]


def test_a_missing_file_is_reported_and_its_item_left_out(two):
    (app,a),(other,b)=two
    kept,lost=add_image(a,(20,60,120),'kept'),add_image(a,(90,30,10),'lost')
    (Path(lost['root'])/lost['path']).unlink()
    data=export(a)
    manifest=json.loads(zipfile.ZipFile(io.BytesIO(data)).read('manifest.json'))
    assert manifest['missing']==[lost['path'].replace('\\','/')]
    result=import_zip(b,data)['result']
    assert result['unavailable']==[lost['id']]
    assert [x['id'] for x in b.get('/api/assets').json()['items']]==[kept['id']]


def test_a_cancelled_import_leaves_nothing_behind(two,tmp_path):
    (app,a),(other,b)=two
    for n in range(3):
        add_image(a,(20*n,60,120),f'image{n}')
    path=tmp_path/'history.zip'; path.write_bytes(export(a))

    class CancelAfterOne(history.Task):
        calls=0
        def check(self):
            self.calls+=1
            if self.calls>2: raise history.Cancelled()
    store,config=other.state.store,other.state.config
    library=Path(config.data['library'])
    with pytest.raises(history.Cancelled):
        history.import_history(CancelAfterOne('import'),store,library,config.state/'inputs',path,other.state.engine.lock)
    assert [f for f in library.rglob('*') if f.is_file()]==[]
    assert store.all_assets()==[]


def chain_setup(app,client):
    """A saved chain, exported, then edited here: re-importing the export now conflicts with it."""
    start,end=add_image(client,(20,60,120),'start'),add_image(client,(90,30,10),'end')
    there,back=clip(app,client,start,end=end),clip(app,client,end,end=start)
    chain=client.post('/api/branches/chains',headers=HEADERS,json={'name':'Run','clips':[there['id'],back['id']]}).json()
    data=export(client)
    client.put('/api/branches/chains/'+chain['id'],headers=HEADERS,json={'name':'Run, edited','clips':[there['id']]})
    return chain,start,data


def saved_chains(client,item_id):
    return sorted((c['name'],len(c['steps'])) for c in tree(client,item_id)['chains'])


def start_import(client,data,**params):
    response=client.post('/api/library/import',headers=HEADERS,params=params,files={'file':('h.zip',data,'application/zip')})
    assert response.status_code==202,response.text
    return response.json()['id']


def wait_for(client,task_id,stage):
    for _ in range(500):
        task=client.get('/api/library/tasks/'+task_id).json()
        if task['stage']==stage or task['finished']: return task
        time.sleep(.01)
    raise AssertionError('The task never reached '+stage)


@pytest.mark.parametrize('choice,expected',[
    ('copy',[('Run (imported)',2),('Run, edited',1)]),
    ('overwrite',[('Run',2)]),
    ('skip',[('Run, edited',1)]),
])
def test_an_import_asks_what_to_do_with_a_changed_chain(two,choice,expected):
    (app,a),_=two
    chain,start,data=chain_setup(app,a)
    task_id=start_import(a,data)
    waiting=wait_for(a,task_id,'Waiting for your choices')
    assert waiting['stage']=='Waiting for your choices' and not waiting['finished']
    [conflict]=waiting['conflicts']
    assert (conflict['id'],conflict['local']['name'],conflict['local']['steps'],conflict['incoming']['name'],conflict['incoming']['steps'])==(chain['id'],'Run, edited',1,'Run',2)
    # Every conflicting chain needs a choice.
    assert a.post(f'/api/library/tasks/{task_id}/choices',headers=HEADERS,json={'choices':{}}).status_code==422
    assert a.post(f'/api/library/tasks/{task_id}/choices',headers=HEADERS,json={'choices':{chain['id']:choice}}).status_code==200
    done=wait_for(a,task_id,'Done')
    assert done['stage']=='Done' and done['result']['chains'][choice]==1,done
    assert saved_chains(a,start['id'])==expected


def test_a_chain_choice_can_be_made_for_all_up_front(two):
    (app,a),_=two
    chain,start,data=chain_setup(app,a)
    done=finish(a,a.post('/api/library/import',headers=HEADERS,params={'chains':'copy'},files={'file':('h.zip',data,'application/zip')}))
    assert done['stage']=='Done' and saved_chains(a,start['id'])==[('Run (imported)',2),('Run, edited',1)]


def test_cancelling_while_choosing_adds_nothing(two):
    (app,a),_=two
    chain,start,data=chain_setup(app,a)
    task_id=start_import(a,data)
    wait_for(a,task_id,'Waiting for your choices')
    a.post(f'/api/library/tasks/{task_id}/cancel',headers=HEADERS)
    assert wait_for(a,task_id,'Cancelled')['stage']=='Cancelled'
    assert saved_chains(a,start['id'])==[('Run, edited',1)]


def test_an_unchanged_chain_is_not_a_conflict(two):
    (app,a),(other,b)=two
    chain,start,data=chain_setup(app,a)
    assert import_zip(b,data)['stage']=='Done'
    # Importing into B again: B's chain is identical to the export's, so nothing is asked.
    again=import_zip(b,data)
    assert again['stage']=='Done' and set(again['result']['chains'].values())=={0}


def test_deleting_waits_while_an_export_runs(two,monkeypatch):
    import threading
    (app,a),_=two
    asset=add_image(a,(20,60,120),'kept')
    release=threading.Event()
    real=history.export_history
    def slow(task,*args):
        release.wait(10); real(task,*args)
    monkeypatch.setattr(history,'export_history',slow)
    started=a.post('/api/library/export',headers=HEADERS)
    try:
        assert a.delete('/api/library',headers=HEADERS).status_code==409
        assert a.delete('/api/assets/'+asset['id'],headers=HEADERS).status_code==409
    finally:
        release.set()
    task=finish(a,started)
    assert task['stage']=='Ready to download' and task['result']['complete'] is True
    assert a.delete('/api/assets/'+asset['id'],headers=HEADERS).status_code==200


def test_a_different_file_at_the_destination_is_set_aside(two):
    (app,a),(other,b)=two
    asset=add_image(a,(20,60,120),'picture')
    data=export(a)
    original=(Path(asset['root'])/asset['path']).read_bytes()
    library=Path(other.state.config.data['library'])
    target=library/asset['path']; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(b'half a file')
    thumb=library/asset['thumbnail']; thumb.parent.mkdir(parents=True,exist_ok=True)
    thumb.write_bytes(zipfile.ZipFile(io.BytesIO(data)).read('library/'+asset['thumbnail'].replace('\\','/')))
    result=import_zip(b,data)['result']
    assert target.read_bytes()==original
    assert (target.parent/(target.name+'.before-import')).read_bytes()==b'half a file'
    # The identical thumbnail was left alone; only the different file is reported.
    assert [r['file'] for r in result['replaced']]==['library/'+asset['path'].replace('\\','/')]


def test_verify_checks_a_backup_without_importing_it(two):
    (app,a),(other,b)=two
    asset=add_image(a,(20,60,120),'picture')
    data=export(a)
    verified=finish(b,b.post('/api/library/verify',headers=HEADERS,files={'file':('h.zip',data,'application/zip')}))
    assert verified['stage']=='Done' and verified['result']['verified'] is True,verified
    assert verified['result']['would_add']['assets']==1
    assert b.get('/api/assets').json()['items']==[]
    # A damaged archive is found: flip a byte inside the stored image.
    name='library/'+asset['path'].replace('\\','/')
    info=zipfile.ZipFile(io.BytesIO(data)).getinfo(name)
    offset=info.header_offset+30+len(info.filename.encode())+len(info.extra)+10
    damaged=bytearray(data); damaged[offset]^=0xFF
    task=finish(b,b.post('/api/library/verify',headers=HEADERS,files={'file':('h.zip',bytes(damaged),'application/zip')}))
    assert task['stage']=='Failed' and 'damaged' in task['error'],task


def test_a_new_export_replaces_the_previous_zip(two):
    (app,a),_=two
    add_image(a,(20,60,120),'picture')
    first=finish(a,a.post('/api/library/export',headers=HEADERS))
    second=finish(a,a.post('/api/library/export',headers=HEADERS))
    assert a.get(f'/api/library/tasks/{first["id"]}/file').status_code==404
    assert a.get(f'/api/library/tasks/{second["id"]}/file').status_code==200


def rewritten(data,drop=(),change=None):
    """The export `data` with record files left out (`drop`) or replaced (`change`: {name: bytes})."""
    out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out,'w') as target:
        for info in source.infolist():
            if info.filename in drop: continue
            target.writestr(info,(change or {}).get(info.filename,source.read(info)))
    return out.getvalue()


def test_verify_refuses_an_archive_with_records_missing(two):
    """Found by review: an archive without assets.json, or whose records disagree with its manifest, must not pass."""
    import json
    (app,a),(other,b)=two
    add_image(a,(20,60,120),'picture')
    data=export(a)
    verify=lambda d:finish(b,b.post('/api/library/verify',headers=HEADERS,files={'file':('h.zip',d,'application/zip')}))
    task=verify(rewritten(data,drop=('assets.json',)))
    assert task['stage']=='Failed' and 'missing assets.json' in task['error'],task
    task=verify(rewritten(data,change={'assets.json':b'[]'}))
    assert task['stage']=='Failed' and 'lists 1 assets but assets.json holds 0' in task['error'],task
    # Import makes the same checks before writing anything.
    task=finish(b,b.post('/api/library/import',headers=HEADERS,files={'file':('h.zip',rewritten(data,drop=('assets.json',)),'application/zip')}))
    assert task['stage']=='Failed' and b.get('/api/assets').json()['items']==[]


def test_splits_pieces_and_their_chains_round_trip(two):
    (app,a),(other,b)=two
    from test_branches import real_video
    start,end=add_image(a,(20,60,120),'start'),add_image(a,(90,30,10),'end')
    take=clip(app,a,start,end=end)
    real_video(Path(take['root'])/take['path'],seconds=2)
    split=a.post('/api/branches/splits',headers=HEADERS,json={'clip_id':take['id'],'frame':20}).json()
    second=clip(app,a,start,end=split)
    a.put('/api/branches/pairs',headers=HEADERS,json={'clip_id':second['id'],'preferred':True})
    a.post('/api/branches/chains',headers=HEADERS,json={'name':'Rejoined','clips':[f'piece:{split["id"]}:before',f'piece:{split["id"]}:after']})
    done=import_zip(b,export(a))
    assert done['stage']=='Done',done
    assert drawn(b,start['id'])==drawn(a,start['id'])


def group_history_setup(app,client):
    """a -> x -> c and a -> y, with x and y in group 'Middle'."""
    a,c=add_image(client,(20,60,120),'a'),add_image(client,(90,30,10),'c')
    x,y=add_image(client,(10,140,40),'x'),add_image(client,(140,140,40),'y')
    clip(app,client,a,end=x); clip(app,client,x,end=c); clip(app,client,a,end=y)
    group=client.post('/api/branches/groups',headers=HEADERS,json={'name':'Middle','members':[x['id'],y['id']]}).json()
    return a,c,x,y,group


def test_groups_round_trip(two):
    (app,a),(other,b)=two
    start,c,x,y,group=group_history_setup(app,a)
    a.put('/api/branches/groups/'+group['id'],headers=HEADERS,json={'collapsed':True,'ports':{start['id']:'In'}})
    done=import_zip(b,export(a))
    assert done['stage']=='Done',done
    assert tree(b,start['id'])['groups']==tree(a,start['id'])['groups']


@pytest.mark.parametrize('choice,expected',[('overwrite',('Middle',2)),('skip',('Middle, edited',1))])
def test_an_import_asks_about_a_changed_group(two,choice,expected):
    (app,a),_=two
    start,c,x,y,group=group_history_setup(app,a)
    data=export(a)
    a.put('/api/branches/groups/'+group['id'],headers=HEADERS,json={'name':'Middle, edited'})
    a.put(f'/api/branches/groups/{group["id"]}/members',headers=HEADERS,json={'remove':[y['id']]})
    task_id=start_import(a,data)
    waiting=wait_for(a,task_id,'Waiting for your choices')
    [conflict]=waiting['conflicts']
    assert (conflict['kind'],conflict['local']['name'],conflict['local']['steps'],conflict['incoming']['steps'])==('group','Middle, edited',1,2)
    assert a.post(f'/api/library/tasks/{task_id}/choices',headers=HEADERS,json={'choices':{group['id']:choice}}).status_code==200
    done=wait_for(a,task_id,'Done')
    assert done['stage']=='Done' and done['result']['groups'][choice]==1,done
    [now]=tree(a,start['id'])['groups']
    assert (now['name'],len(now['members']))==expected


def test_an_imported_member_already_in_another_group_stays_there(two):
    (app,a),(other,b)=two
    start,c,x,y,group=group_history_setup(app,a)
    data=export(a)
    # b gets the same images first, with y in a group of its own, then imports a's history.
    done=import_zip(b,data)
    b.delete('/api/branches/groups/'+group['id'],headers=HEADERS)
    mine=b.post('/api/branches/groups',headers=HEADERS,json={'name':'Alt ending','members':[y['id']]}).json()
    done=import_zip(b,data)
    assert done['stage']=='Done',done
    assert "Group 'Middle' from the export: 1 image was already in your group 'Alt ending', so it stayed there." in done['result']['group_notes']
    groups={g['name']:sorted(g['members']) for g in tree(b,start['id'])['groups']}
    assert groups=={'Alt ending':[y['id']],'Middle':[x['id']]}
