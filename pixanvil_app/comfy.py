import httpx


class Comfy:
    def __init__(self, url):
        self.url = url.rstrip('/')

    def request(self, method, path, **kwargs):
        with httpx.Client(timeout=kwargs.pop('timeout', 12), trust_env=False) as client:
            response = client.request(method, self.url+path, **kwargs)
            response.raise_for_status()
            return response.json()

    def info(self):
        return self.request('GET','/object_info', timeout=30)

    def health(self):
        return self.request('GET','/system_stats', timeout=4)

    def watch(self, job):
        from .progress import Progress
        return Progress(self.url, job).start()

    def submit(self, graph, job_id, workflow):
        return self.request('POST','/prompt', json={'prompt':graph, 'client_id': 'pixanvil-'+job_id,
            'extra_data':{'pixanvil_job_id':job_id,'pixanvil_workflow':workflow}},timeout=30)

    def history(self, prompt_id):
        return self.request('GET','/history/'+prompt_id).get(prompt_id)

    def queue(self):
        return self.request('GET','/queue')

    def reconcile(self, job_id):
        queue = self.queue()
        for entry in queue.get('queue_running',[]) + queue.get('queue_pending',[]):
            if len(entry)>3 and entry[3].get('pixanvil_job_id') == job_id:
                return entry[1]
        history = self.request('GET','/history',params={'max_items':200})
        for prompt_id, result in history.items():
            prompt = result.get('prompt',[])
            if len(prompt)>3 and prompt[3].get('pixanvil_job_id') == job_id:
                return prompt_id
        return None

    def download(self, output):
        params = {k:output.get(k,'') for k in ('filename','subfolder','type')}
        with httpx.Client(timeout=90, trust_env=False) as client:
            response = client.get(self.url+'/view',params=params)
            response.raise_for_status()
            if len(response.content)>80*1024*1024:
                raise ValueError('Output exceeds 80 MB')
            return response.content

    def upload(self, name, data):
        result = self.request('POST','/upload/image', files={'image':(name,data,'image/png')},
                              data={'subfolder':'pixanvil','type':'input','overwrite':'false'},timeout=90)
        if result.get('name')!=name or result.get('subfolder')!='pixanvil' or result.get('type')!='input':
            raise ValueError('ComfyUI returned an unexpected input path; render was not submitted')
        return 'pixanvil/'+name


def choices(info, node, field):
    spec = info.get(node,{}).get('input',{}).get('required',{}).get(field)
    if spec and isinstance(spec[0],list): return spec[0]
    if spec and spec[0]=='COMBO' and len(spec)>1: return spec[1].get('options',[])
    return []
