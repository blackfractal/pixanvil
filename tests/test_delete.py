from pathlib import Path

from fastapi.testclient import TestClient

from test_app import HEADERS, app, png, submit  # noqa: F401 -- app is a fixture
from test_image_tools import create, setup  # noqa: F401 -- setup is a fixture


def files_of(asset):
    path=Path(asset['root'])/asset['path']
    return [path,path.with_suffix('.json'),Path(asset['root'])/asset['thumbnail']]


def finish(app,job):
    app.state.engine.tick(); app.state.engine.tick()
    return app.state.store.job(job['id'])


def test_deleting_an_output_removes_its_job_and_files(app):
    job=submit(app)[0]
    assert finish(app,job)['state']=='complete'
    with TestClient(app) as client:
        asset=client.get('/api/assets').json()['items'][0]
        assert all(f.is_file() for f in files_of(asset))
        response=client.delete('/api/assets/'+asset['id'],headers=HEADERS)
        assert response.status_code==200,response.text
        assert response.json()=={'deleted_job':job['id'],'deleted_assets':[asset['id']],'leftover_files':[]}
        assert client.get('/api/jobs/'+job['id']).status_code==404
        assert client.get('/api/assets').json()['total']==0
    assert not any(f.exists() for f in files_of(asset))


def test_deleting_a_job_removes_its_outputs_and_files(app):
    keep,remove=submit(app,count=2)
    finish(app,keep); finish(app,remove)
    with TestClient(app) as client:
        removed=client.get(f'/api/assets/{remove["id"]}-0').json()
        response=client.delete('/api/jobs/'+remove['id'],headers=HEADERS)
        assert response.status_code==200,response.text
        assert response.json()['deleted_assets']==[removed['id']]
        assert [a['id'] for a in client.get('/api/assets').json()['items']]==[keep['id']+'-0']
        assert [j['id'] for j in client.get('/api/jobs').json()['items']]==[keep['id']]
    assert not any(f.exists() for f in files_of(removed))


def test_queued_job_can_be_deleted_before_submission(app):
    job=submit(app)[0]
    with TestClient(app) as client:
        assert client.delete('/api/jobs/'+job['id'],headers=HEADERS).json()['deleted_assets']==[]
        assert client.get('/api/jobs').json()['items']==[]
    app.state.engine.tick()
    assert app.state.store.job(job['id']) is None


def test_jobs_that_may_still_be_running_cannot_be_deleted(app):
    job=submit(app)[0]; app.state.engine.tick()
    assert app.state.store.job(job['id'])['state']=='running'
    with TestClient(app) as client:
        response=client.delete('/api/jobs/'+job['id'],headers=HEADERS)
        assert response.status_code==409
        assert client.delete('/api/jobs/missing',headers=HEADERS).status_code==404
        assert client.delete('/api/assets/missing',headers=HEADERS).status_code==404
    assert app.state.store.job(job['id'])['state']=='running'


def test_source_snapshot_is_removed_with_the_last_job_using_it(setup):
    app,client,source=setup
    first,second=create(client,source,operation='refine',count=2).json()['jobs']
    snapshot=app.state.config.state/'inputs'/first['recipe']['input_name']
    assert snapshot.is_file() and second['recipe']['input_name']==first['recipe']['input_name']
    client.delete('/api/jobs/'+first['id'],headers=HEADERS)
    assert snapshot.is_file()
    client.delete('/api/jobs/'+second['id'],headers=HEADERS)
    assert not snapshot.exists()
    # The image the refinements started from is untouched.
    assert client.get('/api/assets/'+source['id']).status_code==200


def test_deleting_an_import_removes_only_that_image(app):
    with TestClient(app) as client:
        imported=client.post('/api/assets/import',files={'file':('photo.png',png(),'image/png')},headers=HEADERS).json()
        response=client.delete('/api/assets/'+imported['id'],headers=HEADERS).json()
        assert response=={'deleted_job':None,'deleted_assets':[imported['id']],'leftover_files':[]}
        assert client.get('/api/assets').json()['total']==0
    assert not (Path(imported['root'])/imported['path']).exists()
    assert not (Path(imported['root'])/imported['thumbnail']).exists()
