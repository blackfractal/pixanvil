import json
import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def state_directory():
    return Path(os.environ.get('PIXANVIL_STATE', ROOT / '.local')).resolve()


class Config:
    def __init__(self, state=None):
        self.state = Path(state or state_directory()).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.path = self.state / 'config.json'
        self.data = {
            'comfy_url': 'http://spark-86e6.local:8188',
            'library': str(self.state / 'library'),
            'ssh_host': 'exordium@spark-86e6.local',
        }
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text('utf-8')))

    def save(self, values):
        self.data.update(values)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def public(self):
        return {**self.data, 'state_directory': str(self.state)}

    def discovery(self):
        try:
            addresses = sorted({x[4][0] for x in socket.getaddrinfo('spark-86e6.local', 8188, socket.AF_INET)})
            return {'hostname': 'spark-86e6.local', 'addresses': addresses}
        except OSError as exc:
            return {'hostname': 'spark-86e6.local', 'addresses': [], 'error': str(exc)}
