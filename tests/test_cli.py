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
