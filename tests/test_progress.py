import json
import threading
import time

import pytest
from websockets.sync.server import serve

from pixanvil_app.progress import Progress, terminal_progress


def job(prompt_id='our-prompt'):
    return {'id': 'our-job', 'prompt_id': prompt_id,
            'graph': {'1': {'class_type': 'KSampler'}, '2': {'class_type': 'VAEDecode'}}}


def event(kind='progress', **data):
    return {'type': kind, 'data': {'prompt_id': 'our-prompt', 'node': '1', 'value': 12, 'max': 28, **data}}


def test_progress_filters_other_clients_and_validates_counts():
    progress=Progress('http://localhost:8188',job())
    progress.handle(event())
    expected=progress.snapshot()
    assert expected['percent']==43
    for message in [event(prompt_id='someone-else'),event(node='unknown'),
                    event(value=-1),event(max=0),event(value=29),event(value=float('nan')),
                    {'type':'progress','data':None}, []]:
        progress.handle(message)
        assert progress.snapshot()==expected


def test_events_before_submit_response_are_bound_to_correct_prompt():
    progress=Progress('http://localhost:8188',job(None))
    progress.handle(event(prompt_id='unrelated'))
    progress.handle(event())
    assert 'value' not in progress.snapshot()
    progress.bind('our-prompt')
    assert progress.snapshot()['value']==12


def test_stage_change_resets_percentage_and_does_not_claim_completion():
    progress=Progress('http://localhost:8188',job())
    progress.handle(event(value=28))
    progress.handle(event('executing',node='2'))
    assert progress.snapshot()['stage']=='Decoding image'
    assert 'percent' not in progress.snapshot()
    progress.handle(event('executing',node=None))
    assert progress.snapshot()['stage']=='Finishing render'
    assert 'percent' not in progress.snapshot()


def test_terminal_bar_and_unavailable_status():
    progress=Progress('https://localhost:8188/comfy',job())
    assert progress.url=='wss://localhost:8188/comfy/ws?clientId=pixanvil-our-job'
    progress.handle(event())
    assert terminal_progress(progress.snapshot())=='Sampling [########------------] 12/28 (43%)'
    assert 'unavailable' in terminal_progress({'connected':False})
    assert terminal_progress(None)==''


def test_real_websocket_transport_reconnects_without_stale_percent():
    release=threading.Event()
    connections=[]
    def handler(ws):
        connections.append(ws.request.path)
        ws.send(b'preview bytes are not progress')
        ws.send('not JSON')
        ws.send(json.dumps(event(prompt_id='unrelated',value=25)))
        ws.send(json.dumps(event(value=7)))
        release.wait(3)
    with serve(handler,'127.0.0.1',0) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        progress=Progress(f'http://127.0.0.1:{server.socket.getsockname()[1]}',job()).start()
        try:
            deadline=time.monotonic()+3
            while progress.snapshot().get('value')!=7 and time.monotonic()<deadline: time.sleep(.01)
            assert progress.snapshot()['value']==7
            assert connections==['/ws?clientId=pixanvil-our-job']
            release.set()
            deadline=time.monotonic()+1
            while progress.snapshot()['connected'] and time.monotonic()<deadline: time.sleep(.01)
            assert progress.snapshot()['connected'] is False
            assert 'percent' not in progress.snapshot()
            deadline=time.monotonic()+4
            while len(connections)<2 and time.monotonic()<deadline: time.sleep(.01)
            assert len(connections)>=2
        finally:
            release.set();progress.close();server.shutdown();thread.join(timeout=2)
        assert not progress.thread.is_alive()
