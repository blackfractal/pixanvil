import copy
import json
import secrets
from pathlib import Path
from .comfy import choices
from .config import ROOT


def packages():
    result = {p.stem:json.loads(p.read_text('utf-8')) for p in sorted((ROOT/'workflows').glob('*.json'))}
    # Schnell uses the same topology with its own tested sampling profile.
    schnell = copy.deepcopy(result['flux-dev'])
    schnell.update(id='flux-schnell', name='FLUX Schnell · Draft',
                   description='Four-step drafts for quick iteration. Separate model from Dev.',
                   model_contains=['flux1-schnell'])
    schnell['profiles'] = {key:{'steps':4,'cfg':0} for key in ('fast','balanced','quality','maximum')}
    result['flux-schnell'] = schnell
    return result


def validate_graph(graph, info):
    errors = []
    for node_id, node in graph.items():
        definition = info.get(node['class_type'])
        if not definition:
            errors.append('Missing node: '+node['class_type']); continue
        required = definition.get('input',{}).get('required',{})
        for name, spec in required.items():
            if name not in node['inputs']:
                errors.append(f'{node["class_type"]}: missing input {name}'); continue
            value = node['inputs'][name]
            if isinstance(value,list):
                if len(value)!=2 or value[0] not in graph:
                    errors.append(f'Invalid connection: {node_id}.{name}')
            else:
                options = choices(info,node['class_type'],name)
                # Input images are staged after compilation, before remote submission.
                staged = node['class_type']=='LoadImage' and name=='image' and isinstance(value,str) and value.startswith('pixanvil/')
                if options and value not in options and not staged:
                    errors.append(f'{node["class_type"]}: unavailable {name} = {value}')
    return sorted(set(errors))


def catalog(info):
    results = []
    for package in packages().values():
        node, field = package['model_source']
        # Hidden entries are download-cache files (.cache/huggingface/...), not models.
        models = [m for m in choices(info,node,field)
                  if not m.startswith('.') and any(s.lower() in m.lower() for s in package['model_contains'])]
        graph = copy.deepcopy(package['graph'])
        for n, key in package['bindings']['model']:
            graph[n]['inputs'][key] = models[0] if models else ''
        errors = validate_graph(graph,info)
        if not models:
            errors.insert(0,'No compatible installed model')
        results.append({k:v for k,v in package.items() if k not in ('graph','bindings','model_contains','model_source')} |
                       {'models':models,'available':not errors,'issues':errors})
    return results


def fit_canvas(size, pixels):
    """A canvas with the source's aspect ratio at about `pixels`, in the multiples of 64 the API accepts."""
    ratio = size[0]/size[1]
    def side(value): return min(1536,max(256,round(value/64)*64))
    return side((pixels*ratio)**.5), side((pixels/ratio)**.5)


def compile_recipe(values, info, advanced=True):
    data = copy.deepcopy(values)
    package = packages().get(data['workflow'])
    if not package:
        raise ValueError('Unknown workflow')
    entry = next(c for c in catalog(info) if c['id']==data['workflow'])
    if not entry['available']:
        raise ValueError('; '.join(entry['issues']))
    data['model'] = data.get('model') or entry['models'][0]
    if data['model'] not in entry['models']:
        raise ValueError('Model is not compatible with this workflow')
    if data.get('negative') and not package['negative']:
        raise ValueError('This workflow does not support negative prompts')
    emphasis = data.get('emphasis',[])
    if emphasis and not package['emphasis']:
        raise ValueError('Phrase emphasis is not supported by this workflow; choose SDXL or remove weights')
    video = package.get('media')=='video'
    defaults = package.get('defaults',{})
    operation = data.get('operation','txt2img')
    start = package.get('start_image')
    if operation=='img2vid':
        if not start:
            raise ValueError('Choose a video workflow that accepts a start image')
        if not data.get('width') or not data.get('height'):
            data['width'],data['height'] = fit_canvas(data['source_size'],start['pixels'])
    source = package.get('source_image')
    if source and operation!='edit':
        raise ValueError('This workflow changes an existing image; open one in the Gallery and choose Edit')
    if operation=='edit':
        if not source:
            raise ValueError('Choose an image-editing workflow for Edit')
        # The edit keeps the source's shape; the saved asset records the exact output size.
        data['width'],data['height'] = data['source_size']
    for field in ('width','height'):
        data[field] = data.get(field) or defaults.get(field,1024)
    for field in ('frames','fps'):
        data[field] = (data.get(field) or defaults[field]) if video else None
    if video:
        # Each video model accepts its own frame counts (LTX: 8n+1, MiniMax H3: 17n+5).
        rule = package['frames']
        if not rule['min']<=data['frames']<=rule['max'] or (data['frames']-rule['offset'])%rule['step']:
            raise ValueError(f'Frame count must be a multiple of {rule["step"]} plus {rule["offset"]}, '
                             f'from {rule["min"]} to {rule["max"]}')
    # The LTX2 encoder node fails on an empty negative prompt, so its package supplies one.
    data['negative'] = data.get('negative') or defaults.get('negative','')
    if video and (operation not in ('txt2img','img2vid') or data.get('refinement')):
        raise ValueError('Video workflows create clips from a prompt or a start image; other image tools and second-model refinement are unavailable')
    data['media'] = package.get('media','image')
    resolved_prompt = data['prompt']
    for item in emphasis:
        phrase = item['text']
        if phrase not in resolved_prompt:
            raise ValueError('Emphasis phrase must occur in the prompt: '+phrase)
        escaped = phrase.replace('(',r'\(').replace(')',r'\)')
        resolved_prompt = resolved_prompt.replace(phrase,f'({escaped}:{item["weight"]:g})',1)
    profile = package['profiles'][data['quality']]
    data['steps'] = data.get('steps') or profile['steps']
    data['cfg'] = profile['cfg'] if data.get('cfg') is None else data['cfg']
    if package['id']=='flux-schnell' and (data['steps']!=4 or data['cfg']!=0):
        raise ValueError('Schnell template requires four steps and guidance 0')
    seed_max = package.get('seed_max')
    if data['seed'] in ('','-1','random'):
        # A limited range keeps headroom so consecutive batch seeds stay valid.
        data['seed'] = str(secrets.randbelow(seed_max-7) if seed_max else secrets.randbits(63))
    else:
        data['seed'] = str(int(data['seed']))
    if seed_max and int(data['seed'])>seed_max:
        raise ValueError(f'This workflow accepts seeds up to {seed_max}')
    data['schema_version'] = 1
    data['workflow_version'] = package['version']
    data['resolved_prompt'] = resolved_prompt
    data['sampler'] = package.get('sampler') or ('euler' if package['family']=='flux' else 'dpmpp_2m')
    data['scheduler'] = package.get('scheduler') or ('simple' if package['family']=='flux' else 'karras')
    graph = copy.deepcopy(package['graph'])
    for field, bindings in package['bindings'].items():
        value = resolved_prompt if field=='prompt' else data.get(field,'')
        if field=='seed': value=int(value)
        for node, key in bindings:
            graph[node]['inputs'][key]=value
    if operation=='edit':
        graph[source['node']]['inputs'][source['input']] = 'pixanvil/'+data['input_name']
    if operation=='img2vid':
        # The clip opens on the start image and closes on the end image, each center-cropped to the canvas.
        for key,field in (('start','input_name'),('end','end_input_name')):
            if not data.get(field): continue
            target = package.get(key+'_image')
            if not target:
                raise ValueError(f'This workflow does not accept {"a start" if key=="start" else "an end"} image')
            graph[key+'-image'] = {'class_type':'LoadImage','inputs':{'image':'pixanvil/'+data[field]}}
            graph[key+'-size'] = {'class_type':'ImageScale','inputs':{'image':[key+'-image',0],'upscale_method':'lanczos',
                                  'width':data['width'],'height':data['height'],'crop':'center'}}
            graph[target['node']]['inputs'][target['input']] = [key+'-size',0]
        data['source_fit'] = 'center-crop'
    errors=validate_graph(graph,info)
    if errors: raise ValueError('; '.join(errors))
    if advanced and not video:
        from .image_tools import extend_graph
        data, graph = extend_graph(data,graph,info)
    return data, graph
