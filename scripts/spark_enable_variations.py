"""Run on the SPARK host after copying comfy_nodes/pixanvil_noise.py."""
import json
import subprocess
import urllib.request

with urllib.request.urlopen('http://127.0.0.1:8188/queue',timeout=5) as response:
    queue=json.load(response)
if queue.get('queue_running') or queue.get('queue_pending'):
    raise SystemExit('ComfyUI has active work; no restart performed')
check='''
import sys
sys.path.insert(0, '/workspace/ComfyUI')
sys.path.insert(0, '/workspace/ComfyUI/custom_nodes')
import torch
import comfy.sample
from pixanvil_noise import VariationNoise
for channels in (4,16):
    latent={'samples':torch.zeros((1,channels,8,8))}
    a=comfy.sample.prepare_noise(latent['samples'],123)
    b=comfy.sample.prepare_noise(latent['samples'],456)
    assert torch.equal(VariationNoise(123,456,0).generate_noise(latent),a)
    assert torch.equal(VariationNoise(123,456,1).generate_noise(latent),b)
    middle=VariationNoise(123,456,.15).generate_noise(latent)
    assert torch.equal(middle,VariationNoise(123,456,.15).generate_noise(latent))
    assert not torch.equal(middle,a)
    assert torch.isfinite(middle).all()
print('Seed variation noise verified for SDXL and FLUX shapes')
'''
subprocess.run(['docker','exec','comfyui','python','-c',check],check=True)
subprocess.run(['docker','restart','comfyui'],check=True)
