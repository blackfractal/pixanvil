"""Run the real prompt-to-gallery acceptance test once ComfyUI is available.

No simulated output. Run from repo: python scripts/live_acceptance.py
Optional --wait waits up to the chosen number of seconds for ComfyUI startup.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pixanvil import ensure_server, request
from pixanvil_app.config import state_directory


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--wait',type=int,default=0)
    parser.add_argument('--workflow',default='flux-dev',choices=['flux-dev','flux-schnell','sdxl'])
    parser.add_argument('--port',type=int,default=8790)
    args=parser.parse_args()
    base=f'http://127.0.0.1:{args.port}'
    ensure_server(base,args.port)
    deadline=time.monotonic()+args.wait
    while True:
        connection=request(base,'/api/connection')
        if connection['state']=='ready': break
        if time.monotonic()>=deadline:
            raise RuntimeError('ComfyUI not ready. Live acceptance is NOT passed. '+connection.get('error',''))
        time.sleep(10)
    prompt='Create a high quality image of a young, handsome teenage water knight casting a powerful water spell. Sleeveless armor, cinematic lighting'
    before={a['id'] for a in request(base,'/api/assets?limit=100')['items']}
    result=subprocess.run([sys.executable,str(ROOT/'pixanvil.py'),'generate',prompt,
        '--workflow',args.workflow,'--seed','20260917','--no-browser','--port',str(args.port)],cwd=ROOT)
    if result.returncode: raise RuntimeError('Live generation command failed; see app History')
    assets=request(base,'/api/assets?limit=100')['items']
    generated=[a for a in assets if a['id'] not in before and a['recipe']['prompt']==prompt]
    if not generated: raise RuntimeError('CLI returned success but no new image was found in Gallery')
    from PIL import Image
    for asset in generated:
        path=Path(asset['root'])/asset['path']
        with Image.open(path) as image:
            image.verify()
        job=request(base,'/api/jobs/'+asset['job_id'])
        if job['state']!='complete' or not job['prompt_id']:
            raise RuntimeError('No confirmed ComfyUI execution record')
    report={'passed':True,'backend':connection['url'],'workflow':args.workflow,'assets':[a['id'] for a in generated],
            'paths':[str(Path(a['root'])/a['path']) for a in generated],'tested_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
    (state_directory()/'live-acceptance.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    try: main()
    except Exception as exc: print(str(exc),file=sys.stderr); sys.exit(1)
