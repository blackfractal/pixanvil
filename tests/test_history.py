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
    # Sent once: the temporary zip is gone after the download.
    assert client.get(f'/api/library/tasks/{task["id"]}/file').status_code==404
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
    ({'manifest.json':MANIFEST,'assets.json':[ASSET],'library/Imports/import-x.png':image((1,2,3))},'missing from it'),
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
