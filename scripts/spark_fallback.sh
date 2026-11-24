#!/usr/bin/env bash
# Temporary compatibility test only. Does not replace the normal comfyui service.
set -eu
docker run -d --name pixanvil-smoke --gpus all --ipc=host \
  --ulimit memlock=-1 --ulimit stack=67108864 -p 8189:8189 \
  -v /home/exordium/ai/comfyui:/workspace/ComfyUI \
  -v /home/exordium/ai/models:/workspace/models \
  nvcr.io/nvidia/pytorch:25.01-py3 bash -c '
    cd /workspace/ComfyUI
    grep -Ev "^(torch|torchvision|torchaudio)$" requirements.txt > /tmp/pixanvil-requirements.txt
    python -m pip install --no-deps -r /tmp/pixanvil-requirements.txt > /tmp/pixanvil-install.log 2>&1 &&
    python main.py --listen 0.0.0.0 --port 8189 --disable-mmap --disable-pinned-memory --disable-all-custom-nodes
  '
