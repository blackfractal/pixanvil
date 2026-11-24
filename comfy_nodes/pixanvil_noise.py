"""PixAnvil's deterministic seed variation node. No additional dependencies."""
import math


def blend_weights(cosine, amount):
    if amount <= 0: return 1.0, 0.0
    if amount >= 1: return 0.0, 1.0
    cosine = max(-1.0, min(1.0, cosine))
    if abs(cosine) > .9995: return 1-amount, amount
    angle = math.acos(cosine)
    return math.sin((1-amount)*angle)/math.sin(angle), math.sin(amount*angle)/math.sin(angle)


class VariationNoise:
    def __init__(self, seed, variation_seed, strength):
        self.seed, self.variation_seed, self.strength = seed, variation_seed, strength

    def generate_noise(self, latent):
        import torch
        import comfy.sample
        samples = latent['samples']
        batch = latent.get('batch_index')
        with torch.random.fork_rng(devices=[]):
            original = comfy.sample.prepare_noise(samples, self.seed, batch)
            if self.strength == 0: return original
            alternate = comfy.sample.prepare_noise(samples, self.variation_seed, batch)
        if self.strength == 1: return alternate
        # Blend independently per image; preserve ComfyUI's normal noise at either endpoint.
        output = original.clone()
        for index in range(original.shape[0]):
            a, b = original[index].flatten().float(), alternate[index].flatten().float()
            denominator = a.norm()*b.norm()
            cosine = float(torch.dot(a,b)/denominator) if denominator > 0 else 1.0
            wa, wb = blend_weights(cosine, self.strength)
            output[index] = original[index]*wa + alternate[index]*wb
        return output


class PixAnvilVariationNoise:
    @classmethod
    def INPUT_TYPES(cls):
        seed = ('INT', {'default':0, 'min':0, 'max':2**64-1})
        return {'required': {'seed':seed, 'variation_seed':seed,
                'strength':('FLOAT', {'default':.15, 'min':0, 'max':1, 'step':.01})}}
    RETURN_TYPES = ('NOISE',)
    FUNCTION = 'create'
    CATEGORY = 'PixAnvil'

    def create(self, seed, variation_seed, strength):
        return (VariationNoise(seed,variation_seed,strength),)


NODE_CLASS_MAPPINGS = {'PixAnvilVariationNoise': PixAnvilVariationNoise}
NODE_DISPLAY_NAME_MAPPINGS = {'PixAnvilVariationNoise':'PixAnvil Seed Variation'}
