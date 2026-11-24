"""Best-effort live telemetry; HTTP history remains authoritative for completion."""
import json
import threading
from urllib.parse import urlencode

from websockets.sync.client import connect


STAGES = {
    'KSampler': 'Sampling', 'KSamplerAdvanced': 'Sampling',
    'SamplerCustomAdvanced': 'Sampling',
    'CheckpointLoaderSimple': 'Loading model', 'UNETLoader': 'Loading model',
    'DualCLIPLoader': 'Loading text encoders', 'CLIPLoader': 'Loading text encoder',
    'CLIPTextEncode': 'Encoding prompt', 'VAELoader': 'Loading image decoder',
    'VAEDecode': 'Decoding image', 'SaveImage': 'Saving image',
    'VAEEncode': 'Encoding source image', 'ImageUpscaleWithModel': 'Upscaling',
    'UpscaleModelLoader': 'Loading upscaler', 'LoadImage': 'Reading source image',
    'LTX2_SM_Model': 'Loading video model', 'LTX2_SM_Clip': 'Loading text encoder',
    'LTX2_SM_VAE': 'Loading video decoder', 'LTX2_SM_AUDIO_VAE': 'Loading audio decoder',
    'LTX2_SM_ENCODER': 'Encoding prompt', 'LTX2_LATENTS': 'Preparing frames',
    'LTX2_SM_KSampler': 'Sampling video', 'LTX2_DECO_VIDEO': 'Decoding video',
    'LTX2_DECO_AUDIO': 'Decoding audio', 'CreateVideo': 'Assembling video',
    'SaveVideo': 'Saving video', 'ImageFromBatch': 'Saving poster frame',
    'MiniMaxH3ImageToVideo': 'Encoding prompt', 'LoraLoaderModelOnly': 'Loading model',
    'VAEDecodeAudio': 'Decoding audio',
}


class Progress:
    def __init__(self, url, job, connector=connect):
        self.url = ('wss' if url.startswith('https:') else 'ws') + url[url.index(':'):].rstrip('/')
        self.url += '/ws?' + urlencode({'clientId': 'pixanvil-'+job['id']})
        self.graph = job['graph']
        self.prompt_id = job.get('prompt_id')
        self.connector = connector
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.attempted = threading.Event()
        self.pending = []
        self.data = {'connected': False, 'stage': 'Waiting for execution', 'node': None}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='pixanvil-progress')
        self.thread.start()
        # Subscribe before submission when possible, without making telemetry mandatory.
        self.attempted.wait(3.5)
        return self

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=4)

    def snapshot(self):
        with self.lock: return dict(self.data)

    def bind(self, prompt_id):
        with self.lock:
            self.prompt_id = prompt_id
            pending, self.pending = self.pending, []
            for message in pending: self.handle(message)

    def handle(self, message):
        if not isinstance(message, dict): return
        data = message.get('data')
        if not isinstance(data, dict): return
        with self.lock:
            if not self.prompt_id:
                self.pending = (self.pending + [message])[-100:]
                return
            if data.get('prompt_id') != self.prompt_id: return
            kind = message.get('type')
            node = data.get('node')
            if node is not None: node = str(node)
            if kind == 'executing':
                if node is None:
                    self.data = {'connected': True, 'stage': 'Finishing render', 'node': None}
                elif node in self.graph:
                    cls = self.graph[node]['class_type']
                    stage = STAGES.get(cls, 'Processing')
                    if node.startswith('ref-'): stage = 'Refinement: '+stage.lower()
                    self.data = {'connected': True, 'stage': stage, 'node': node}
            elif kind == 'progress' and node in self.graph:
                value, maximum = data.get('value'), data.get('max')
                if type(value) is not int or type(maximum) is not int or not 0 <= value <= maximum or maximum <= 0:
                    return
                cls = self.graph[node]['class_type']
                stage = STAGES.get(cls, 'Processing')
                if node.startswith('ref-'): stage = 'Refinement: '+stage.lower()
                self.data = {'connected': True, 'stage': stage, 'node': node,
                             'value': value, 'max': maximum, 'percent': round(100*value/maximum)}

    def run(self):
        while not self.stop.is_set():
            try:
                with self.connector(self.url, proxy=None, open_timeout=3, close_timeout=1,
                                    max_size=8*1024*1024) as ws:
                    with self.lock:
                        self.data = {'connected': True, 'stage': 'Waiting for progress', 'node': None}
                    self.attempted.set()
                    while not self.stop.is_set():
                        try: raw = ws.recv(timeout=.5)
                        except TimeoutError: continue
                        if not isinstance(raw, str): continue  # Binary previews are a separate feature.
                        try: message = json.loads(raw)
                        except ValueError: continue
                        self.handle(message)
            except Exception:
                pass  # Progress transport must never fail, cancel, or resubmit a render.
            finally:
                self.attempted.set()
                with self.lock:
                    self.pending = []
                    self.data = {'connected': False, 'stage': 'Live progress reconnecting', 'node': None}
            self.stop.wait(2)


def terminal_progress(progress):
    if not progress: return ''
    if not progress.get('connected'): return 'Live progress unavailable; checking job status'
    stage = progress['stage']
    if 'max' not in progress: return stage
    filled = min(20, progress['value']*20//progress['max'])
    return f"{stage} [{'#'*filled}{'-'*(20-filled)}] {progress['value']}/{progress['max']} ({progress['percent']}%)"
