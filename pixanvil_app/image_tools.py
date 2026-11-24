"""Compose local image operations around the versioned generation packages."""
import copy
import secrets
from .comfy import choices


def node(kind, **inputs): return {'class_type':kind, 'inputs':inputs}


def capabilities(info):
    needed = {
        'variation':['PixAnvilVariationNoise','SamplerCustomAdvanced','CFGGuider','KSamplerSelect','BasicScheduler'],
        'refine':['LoadImage','ImageScale','VAEEncode'],
        'upscale':['LoadImage','UpscaleModelLoader','ImageUpscaleWithModel','ImageScale'],
        'img2vid':['LoadImage','ImageScale'],
        'edit':['LoadImage','FluxKontextImageScale','TextEncodeQwenImageEditPlus'],
    }
    result={}
    for operation,names in needed.items():
        missing=[name for name in names if name not in info]
        result[operation]={'available':not missing,'issues':['Missing node: '+n for n in missing]}
    upscalers=choices(info,'UpscaleModelLoader','model_name')
    result['upscale']['models']=upscalers
    if not upscalers:
        result['upscale']['available']=False
        result['upscale']['issues'].append('No installed upscale model')
    return result


def seed(value):
    return str(secrets.randbits(63)) if value in ('','-1','random') else str(int(value))


def extend_graph(data, graph, info):
    from .workflows import compile_recipe, validate_graph
    operation=data.get('operation','txt2img')
    sampler_id=next(n for n,v in graph.items() if v['class_type']=='KSampler')
    output_id=next(n for n,v in graph.items() if v['class_type']=='SaveImage')
    decode_id=next(n for n,v in graph.items() if v['class_type']=='VAEDecode')
    sampler=graph[sampler_id]['inputs']
    if operation=='variation':
        data['variation_seed']=seed(data.get('variation_seed','-1'))
        graph['var-noise']=node('PixAnvilVariationNoise',seed=int(data['seed']),
                               variation_seed=int(data['variation_seed']),strength=data['variation_strength'])
        graph['var-guider']=node('CFGGuider',model=sampler['model'],positive=sampler['positive'],negative=sampler['negative'],cfg=sampler['cfg'])
        graph['var-sampler']=node('KSamplerSelect',sampler_name=sampler['sampler_name'])
        graph['var-schedule']=node('BasicScheduler',model=sampler['model'],scheduler=sampler['scheduler'],steps=sampler['steps'],denoise=1.0)
        graph[sampler_id]=node('SamplerCustomAdvanced',noise=['var-noise',0],guider=['var-guider',0],
                              sampler=['var-sampler',0],sigmas=['var-schedule',0],latent_image=sampler['latent_image'])
    elif operation=='refine':
        if data['workflow']=='flux-schnell': raise ValueError('Use FLUX Dev or SDXL for refinement; Schnell is a four-step draft model')
        graph['input']=node('LoadImage',image='pixanvil/'+data['input_name'])
        graph['input-size']=node('ImageScale',image=['input',0],upscale_method='lanczos',width=data['width'],height=data['height'],crop='disabled')
        graph['input-encode']=node('VAEEncode',pixels=['input-size',0],vae=graph[decode_id]['inputs']['vae'])
        sampler.update(latent_image=['input-encode',0],denoise=data['denoise'])
    elif operation not in ('txt2img','edit'):
        raise ValueError('Unsupported generation operation')

    refinement=data.get('refinement')
    if refinement:
        if refinement['workflow']=='flux-schnell': raise ValueError('Schnell cannot be a refinement pass')
        values={**data,**refinement,'operation':'txt2img','refinement':None,'emphasis':[],
                'negative':data.get('negative','') if refinement['workflow']=='sdxl' else ''}
        refined,second=compile_recipe(values,info,advanced=False)
        graph.pop(output_id)
        second=copy.deepcopy(second)
        # Prefix every link to keep both model stages distinct in the saved graph.
        for n in second.values():
            for key,value in n['inputs'].items():
                if isinstance(value,list): n['inputs'][key]=['ref-'+value[0],value[1]]
        second_sampler=next(n for n in second.values() if n['class_type']=='KSampler')
        second_decode=next(n for n in second.values() if n['class_type']=='VAEDecode')
        graph['ref-encode']=node('VAEEncode',pixels=[decode_id,0],vae=second_decode['inputs']['vae'])
        second_sampler['inputs'].update(latent_image=['ref-encode',0],denoise=refinement['denoise'])
        graph.update({'ref-'+k:v for k,v in second.items()})
        data['refinement']={k:refined[k] for k in ('workflow','model','steps','cfg','seed','sampler','scheduler','resolved_prompt','workflow_version')}
        data['refinement']['denoise']=refinement['denoise']
        data['refinement']['method']='decode-encode-img2img'
    errors=validate_graph(graph,info)
    if errors: raise ValueError('; '.join(errors))
    return data,graph


def compile_upscale(values, source, info):
    from .workflows import validate_graph
    models=choices(info,'UpscaleModelLoader','model_name')
    model=values['upscale_model'] or (models[0] if models else '')
    if model not in models: raise ValueError('Select an installed upscale model')
    width,height=source['width']*values['scale'],source['height']*values['scale']
    # Current supported preset is the installed 4x model, including its intermediate image.
    if model!='RealESRGAN_x4plus.pth': raise ValueError('This release supports RealESRGAN_x4plus.pth')
    if source['width']*source['height']*16 > 32_000_000:
        raise ValueError('Source is too large for the 4x upscaler; maximum input is 2 megapixels')
    data={**copy.deepcopy(source['recipe']),**values,'workflow':'upscale','workflow_version':1,
          'model':model,'width':width,'height':height,'steps':0,'cfg':0,'sampler':'none','scheduler':'none',
          'refinement':None,'seed':source['recipe']['seed'],'resolved_prompt':source['recipe']['resolved_prompt']}
    graph={
        'input':node('LoadImage',image='pixanvil/'+data['input_name']),
        'model':node('UpscaleModelLoader',model_name=model),
        'upscale':node('ImageUpscaleWithModel',upscale_model=['model',0],image=['input',0]),
        'size':node('ImageScale',image=['upscale',0],upscale_method='lanczos',width=width,height=height,crop='disabled'),
        'save':node('SaveImage',images=['size',0],filename_prefix='PixAnvil'),
    }
    errors=validate_graph(graph,info)
    if errors: raise ValueError('; '.join(errors))
    return data,graph
