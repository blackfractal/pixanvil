import hashlib
import io
import json
import re
from pathlib import Path
from PIL import Image, PngImagePlugin

Image.MAX_IMAGE_PIXELS = 40_000_000


def safe_path(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Path escapes library')
    return target


def locations(job, index, suffix):
    root = Path(job['recipe']['library']).resolve()
    folder = root / 'Generations' / job['created'][:10]
    folder.mkdir(parents=True, exist_ok=True)
    asset_id = f'{job["id"]}-{index}'
    thumb = root / '.thumbnails' / (asset_id+'.jpg')
    thumb.parent.mkdir(parents=True,exist_ok=True)
    return root, asset_id, folder / (asset_id+suffix), thumb


def save_thumbnail(source, thumb):
    preview=source.convert('RGB'); preview.thumbnail((600,600))
    temp_thumb=thumb.with_suffix('.jpg.part')
    preview.save(temp_thumb,format='JPEG',quality=88); temp_thumb.replace(thumb)


def register(store, job, asset_id, root, path, thumb, width, height):
    recipe = job['recipe']
    sidecar=path.with_suffix('.json')
    temp_json=sidecar.with_suffix('.json.part')
    temp_json.write_text(json.dumps({'asset_id':asset_id,'recipe':recipe,'api_graph':job['graph'],
                                    'sha256':hashlib.sha256(path.read_bytes()).hexdigest()},indent=2),encoding='utf-8')
    temp_json.replace(sidecar)
    store.add_asset({'id':asset_id,'job_id':job['id'],'root':str(root),'path':str(path.relative_to(root)),
                     'thumbnail':str(thumb.relative_to(root)),'recipe':recipe,'width':width,'height':height})
    return asset_id


def collect_video(store, job, data, poster, index):
    """Save an MP4 with a poster thumbnail. The recipe lives in the JSON sidecar, not in the file."""
    if data[4:8]!=b'ftyp':
        raise ValueError('Video output is not an MP4 file')
    root, asset_id, path, thumb = locations(job, index, '.mp4')
    temp=path.with_suffix('.mp4.part')
    temp.write_bytes(data); temp.replace(path)
    # The full-size first frame stays beside the clip so it can be edited and animated again.
    frame=path.with_suffix('.png.part')
    frame.write_bytes(poster); frame.replace(path.with_suffix('.png'))
    with Image.open(io.BytesIO(poster)) as source:
        source.load()
        save_thumbnail(source, thumb)
        width,height=source.size
    return register(store, job, asset_id, root, path, thumb, width, height)


def collect(store, job, data, index):
    root, asset_id, path, thumb = locations(job, index, '.png')
    recipe = job['recipe']
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        metadata = PngImagePlugin.PngInfo()
        for key,value in source.info.items():
            if isinstance(value,str) and len(value)<4_000_000:
                metadata.add_text(key,value)
        metadata.add_text('pixanvil',json.dumps({'asset_id':asset_id,'recipe':recipe}))
        metadata.add_text('prompt',json.dumps(job['graph']))
        metadata.add_text('parameters',f'{recipe["resolved_prompt"]}\nNegative prompt: {recipe.get("negative","")}\nSteps: {recipe["steps"]}, Sampler: {recipe["sampler"]}, CFG scale: {recipe["cfg"]}, Seed: {recipe["seed"]}, Size: {recipe["width"]}x{recipe["height"]}, Model: {recipe["model"]}')
        temp=path.with_suffix('.png.part')
        source.save(temp,format='PNG',pnginfo=metadata)
        temp.replace(path)
        save_thumbnail(source, thumb)
        width,height=source.size
    return register(store, job, asset_id, root, path, thumb, width, height)


def import_image(store, library, data, name, **extra):
    """Add an image file to the library. The same file imported twice is one asset.

    `extra` adds recipe fields, such as the clip a first frame was taken from."""
    digest=hashlib.sha256(data).hexdigest()
    asset_id='import-'+digest[:20]
    existing=store.asset(asset_id)
    if existing: return existing
    root=Path(library).resolve()
    folder=root/'Imports'; folder.mkdir(parents=True,exist_ok=True)
    thumb=root/'.thumbnails'/(asset_id+'.jpg'); thumb.parent.mkdir(parents=True,exist_ok=True)
    path=folder/(asset_id+'.png')
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        temp=path.with_suffix('.png.part')
        # A PNG is kept byte for byte so any generation settings inside it survive.
        if source.format=='PNG': temp.write_bytes(data)
        else: source.convert('RGB').save(temp,format='PNG')
        temp.replace(path)
        save_thumbnail(source, thumb)
        width,height=source.size
    title=Path(name or 'image').stem[:200] or 'image'
    recipe={'operation':'import','workflow':'import','media':'image','prompt':title,'resolved_prompt':title,
            'negative':'','model':'','seed':'0','steps':0,'cfg':0,'sampler':'none','scheduler':'none',
            'width':width,'height':height,'original_name':Path(name or '').name[:200],'source_sha256':digest} | extra
    store.add_asset({'id':asset_id,'job_id':None,'root':str(root),'path':str(path.relative_to(root)),
                     'thumbnail':str(thumb.relative_to(root)),'recipe':recipe,'width':width,'height':height})
    return store.asset(asset_id)


def inspect_image(data):
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        raw={k:v for k,v in im.info.items() if isinstance(v,str)}
        if 'pixanvil' in raw:
            parsed=json.loads(raw['pixanvil'])
            return {'format':'pixanvil','recipe':parsed['recipe'],'raw':raw}
        if 'parameters' in raw:
            text=raw['parameters']; fields={}
            lines=text.strip().splitlines()
            tail=lines[-1] if lines and re.search(r'Steps: \d+',lines[-1]) else ''
            body='\n'.join(lines[:-1]) if tail else text
            prompt,sep,negative=body.partition('\nNegative prompt: ')
            for match in re.finditer(r'(?:^|,\s*)([\w /-]+):\s*("(?:[^"\\]|\\.)*"|[^,]*)',tail):
                val=match.group(2)
                fields[match.group(1)]=json.loads(val) if val.startswith('"') else val.strip()
            recipe={'prompt':prompt,'negative':negative if sep else '', 'seed':fields.get('Seed','-1'),
                    'model':fields.get('Model','')}
            for src,dest in [('Steps','steps'),('CFG scale','cfg')]:
                if src in fields:
                    try: recipe[dest]=float(fields[src]) if dest=='cfg' else int(fields[src])
                    except ValueError: pass
            size=fields.get('Size','').split('x')
            if len(size)==2 and all(x.isdigit() for x in size):
                recipe.update(width=int(size[0]),height=int(size[1]))
            return {'format':'a1111','recipe':recipe,'fields':fields,'raw':raw,
                    'notice':'Settings recovered; model, sampler, and workflow compatibility must be checked.'}
        if 'prompt' in raw or 'workflow' in raw:
            return {'format':'comfyui','recipe':None,'raw':raw,
                    'notice':'Graph preserved. Arbitrary graphs are not automatically mapped to generation controls.'}
        return {'format':'none','recipe':None,'raw':raw,'notice':'No generation metadata found. Original seed and prompt cannot be recovered.'}
