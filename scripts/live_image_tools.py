"""Real SPARK acceptance for image tools; saves actual outputs in the gallery."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pixanvil import request
from pixanvil_app.server import Generation
from pixanvil_app.config import state_directory
from PIL import Image, ImageChops, ImageStat


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source')
    args=parser.parse_args()
    base='http://127.0.0.1:8790'
    assets=request(base,'/api/assets?limit=100')['items']
    source=next(a for a in assets if a['id']==args.source) if args.source else next(
        a for a in assets if a['recipe']['workflow']=='sdxl' and a['recipe'].get('operation','txt2img')=='txt2img')
    fields=Generation.model_fields
    original={k:v for k,v in source['recipe'].items() if k in fields}
    original.update(source_asset_id=source['id'],count=1)
    source_path=Path(source['root'])/source['path']
    report={'source':source['id'],'backend':request(base,'/api/connection')['url'],'checks':[]}

    def run(name,values,expected_size):
        started=time.monotonic()
        job=request(base,'/api/jobs','POST',json=values)['jobs'][0]
        print(name,job['id'],flush=True)
        deadline=time.monotonic()+1800
        last=None
        while time.monotonic()<deadline:
            job=request(base,'/api/jobs/'+job['id'])
            current=(job['state'],job.get('progress'))
            if current!=last: print(current,flush=True);last=current
            if job['state']=='complete': break
            if job['state'] in ('failed','cancelled','abandoned','unknown'): raise RuntimeError(job.get('error') or job['state'])
            time.sleep(2)
        else: raise RuntimeError('Render wait expired; job may still be running')
        asset=request(base,'/api/assets/'+job['outputs'][0])
        path=Path(asset['root'])/asset['path']
        with Image.open(path) as image:
            assert image.size==expected_size,(name,image.size,expected_size)
            image.verify()
        if values.get('source_asset_id'): assert asset['recipe']['parent_asset_id']==source['id']
        check={'name':name,'job_id':job['id'],'prompt_id':job['prompt_id'],'asset_id':asset['id'],
               'path':str(path),'seconds':round(time.monotonic()-started,1),'dimensions':expected_size}
        if name.startswith('variation'):
            with Image.open(source_path) as first,Image.open(path) as second:
                check['mean_pixel_difference']=sum(ImageStat.Stat(ImageChops.difference(first.convert('RGB'),second.convert('RGB'))).mean)/3
        report['checks'].append(check)
        (state_directory()/'image-tools-live.json').write_text(json.dumps(report,indent=2))
        print('PASSED',name,check,flush=True)

    size=(source['width'],source['height'])
    run('variation-zero',{**original,'operation':'variation','variation_seed':'20260918','variation_strength':0},size)
    run('variation-015',{**original,'operation':'variation','variation_seed':'20260918','variation_strength':.15},size)
    run('refine',{**original,'operation':'refine','model':'dreamshaperXL_alpha2Xl10.safetensors','denoise':.25,'steps':16},size)
    run('upscale-2x',{**original,'operation':'upscale','scale':2},(size[0]*2,size[1]*2))
    run('second-model-pass',{'prompt':original['prompt'],'workflow':'sdxl','model':'sd_xl_base_1.0.safetensors',
        'seed':'20260918','width':512,'height':512,'steps':8,'refinement':{
        'workflow':'sdxl','model':'dreamshaperXL_alpha2Xl10.safetensors','denoise':.25,'steps':8}},(512,512))
    report['passed']=True
    (state_directory()/'image-tools-live.json').write_text(json.dumps(report,indent=2))
    print('All live image-tool checks passed',flush=True)


if __name__=='__main__': main()
