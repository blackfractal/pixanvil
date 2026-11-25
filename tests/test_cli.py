import json
import socket
import subprocess
import sys
import threading
import time

import httpx
import uvicorn
from pixanvil_app.config import ROOT
from pixanvil_app.server import make_app
from test_app import Fake


def test_terminal_prompt_appears_in_gallery(tmp_path):
    Fake.submitted=[]; Fake.download_fail=False; Fake.submission_lost=False
    app=make_app(tmp_path,client_factory=Fake)
    sock=socket.socket(); sock.bind(('127.0.0.1',0))
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',ws='none'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline: time.sleep(.05)
        assert server.started
        result=subprocess.run([sys.executable,str(ROOT/'pixanvil.py'),'generate','a cinematic water knight',
                               '--workflow','sdxl','--seed','1234567890123456789','--port',str(port),
                               '--no-browser','--timeout','20'],capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stdout+result.stderr
        assert 'Complete' in result.stdout and 'Saved:' in result.stdout
        with httpx.Client(trust_env=False) as client:
            assets=client.get(f'http://127.0.0.1:{port}/api/assets').json()['items']
            assert len(assets)==1
            assert assets[0]['recipe']['prompt']=='a cinematic water knight'
            assert assets[0]['recipe']['seed']=='1234567890123456789'
            assert client.get(f'http://127.0.0.1:{port}/api/assets/{assets[0]["id"]}/image').headers['content-type']=='image/png'
    finally:
        server.should_exit=True; thread.join(timeout=10); sock.close()


def test_stop_ends_a_background_server(tmp_path):
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]; sock.close()
    env={**__import__('os').environ,'PIXANVIL_STATE':str(tmp_path)}
    server=subprocess.Popen([sys.executable,str(ROOT/'pixanvil.py'),'serve','--port',str(port)],
                            env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                if httpx.get(f'http://127.0.0.1:{port}/api/health',trust_env=False).status_code==200: break
            except httpx.ConnectError: time.sleep(.2)
        stop=[sys.executable,str(ROOT/'pixanvil.py'),'stop','--port',str(port)]
        result=subprocess.run(stop,env=env,capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stdout+result.stderr
        assert 'PixAnvil stopped.' in result.stdout
        assert server.wait(timeout=10)==0
        again=subprocess.run(stop,env=env,capture_output=True,text=True,timeout=30)
        assert again.returncode==0 and 'not running' in again.stdout
    finally:
        if server.poll() is None: server.kill()


def test_shutdown_needs_a_server_that_can_stop_itself(tmp_path):
    from fastapi.testclient import TestClient
    from test_app import HEADERS
    app=make_app(tmp_path,worker=False,client_factory=Fake)
    with TestClient(app) as client:
        assert client.post('/api/shutdown',headers=HEADERS).status_code==409
        stopped=[]; app.state.shutdown=lambda:stopped.append(True)
        assert client.post('/api/shutdown').status_code==403
        assert client.post('/api/shutdown',headers=HEADERS).json()=={'stopping':True,'active':0}
        assert stopped==[True]
