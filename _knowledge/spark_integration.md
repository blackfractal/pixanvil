# SPARK integration — implementation findings

## Video (added 2026-11-16)

Sulphur text-to-video works on SPARK and through PixAnvil (`workflows/sulphur-video.json`). A 97-frame 768 × 512 clip with audio rendered in 208 to 241 seconds across three runs, including model loading.

What the ComfyUI container needed, none of which ComfyUI's own `requirements.txt` provides:

- **torchaudio:** no build loads against NVIDIA's `torch 2.10.0a0` snapshot. torchaudio 2.10.0 is installed with `--no-deps` and its compiled libraries are deleted so it runs in pure-Python mode. Full account: `SPARK_PLAN/_knowledge/TORCHAUDIO_ON_SPARK.md`.
- **Pins for `ComfyUI_LTX2_SM`:** `transformers` 4.57.6 (the node breaks on 5.x), `tokenizers` 0.22.2, `huggingface_hub` 0.36.2, `diffusers` 0.36.0.
- **Extra packages:** `opencv-python-headless`, `accelerate`, `gguf`, `omegaconf`, `GitPython`, `toml`, `ftfy`.

`spark_services.py` (master in `SPARK_PLAN/scripts`, deployed to `~/ai/scripts`) applies all of this when the `comfyui` container is created, and removes the torchaudio libraries on every start.

**Current state (later on 2026-11-16):** SPARK runs a single ComfyUI **0.45.4** on port 8188 (`~/ai/comfyui`, pinned to tag `v0.45.4`), in a container built from scratch by the script. The old 0.17.0 checkout is kept unused at `~/ai/comfyui-0.17.0-retired`; saved workflows, outputs and inputs were copied across. Through PixAnvil on this install: FLUX Dev, SDXL, Sulphur video and MiniMax H3 video all render.

Node behaviour worth knowing: the sampler seed is capped at 2147483647; an empty negative prompt crashes the sampler; `SaveVideo` reports its MP4 under the `images` key of the history output; ComfyUI lists Hugging Face `.cache` files in the `gguf` model list.

MiniMax H3 runs on SPARK in the same ComfyUI as everything else (it needs 0.30.0 or newer; a separate second install was tried first and then merged):

- **Files** (from `Comfy-Org/MiniMax-H3`, fetched by `~/ai/scripts/h3_download.sh`, about 42 GB): `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors`, `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`, `vae/minimax_h3_video_vae_int8_convrot.safetensors`, `vae/minimax_h3_audio_vae_fp32.safetensors`, `loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`.
- **Result:** a 73-frame 832 × 480 text-to-video clip with 32 kHz stereo audio rendered in 80 seconds including model loading, using the graph from ComfyUI's bundled `video_minimax_h3_t2v` template with the 8-step turbo LoRA. No torchaudio or `transformers` workarounds were needed in that container.
- **License:** the MiniMax H3 Community License excludes the US, EU, UK and South Korea. Jonathan confirmed he is covered and may still apply for a license.
- **PixAnvil:** `workflows/minimax-h3-video.json`. H3 takes frame counts of 17n+5 and has no negative prompt; Sulphur takes 8n+1. Each video package declares its own frame rule, lengths and canvases.
- **Switching cost:** the first H3 render after a Sulphur render took 111 s against 80 s on a fresh start, so expect model reloading when alternating between the two.

## Models added 2026-11-24

Fetched by `~/ai/scripts/plans_download.sh` on SPARK (resumable; log beside it), all from `Comfy-Org` repositories on Hugging Face, about 59 GB:

| File (under `~/ai/models/`) | Size | For | State |
|---|---|---|---|
| `diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors` | 20.5 GB | Instruction image edits (`workflows/qwen-image-edit.json`) | In use; one real edit verified |
| `text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` | 9.4 GB | Qwen Image Edit text/vision encoder | In use |
| `vae/qwen_image_vae.safetensors` | 0.3 GB | Qwen Image Edit VAE | In use |
| `diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors` | 21.0 GB | H3 reference-driven video (route 2 in `_plans/video_editing.md`) | Downloaded, never run |
| `loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` | 2.0 GB | 4-step turbo for the model above | Downloaded, never run |
| `model_patches/minimax_h3_fun_controlnet_union_2.0_pruned_int8_convrot.safetensors` | 4.5 GB | H3 video-to-video guidance (route 3) | Downloaded, never run |
| `checkpoints/v1-5-pruned-emaonly-fp16.safetensors` | 2.1 GB | SD 1.5 base, for LoRA and hypernetwork parity with A1111 | Downloaded, no PixAnvil workflow yet |

The Qwen Image Edit graph follows ComfyUI's bundled `image_qwen_image_edit_2511` template (shift 3.1, CFGNorm, `FluxKontextImageScale`, `TextEncodeQwenImageEditPlus`, euler/simple, CFG 4) without its optional LoRA.

**Disk on SPARK:** 3.7 TB total. 445 GB used before these downloads, 501 GB after (15%), 3.0 TB free. The largest users are `/usr/share/ollama` (130 GB of language models) and `~/ai/models` (about 267 GB).

Seen on Hugging Face but not downloaded, because nothing here has been checked against ComfyUI 0.45.4: `Comfy-Org/MiniMax-H3-Image` (an H3 image model, published 2026-11-23) and `Comfy-Org/Qwen-Image-2.1`.

The "Installed video files ... not proof of a complete working video pipeline" note below predates this.

Updated: 2026-09-18 UTC. **Core image generation is working on SPARK and confirmed by Jonathan in the browser.** The initial runtime/setup notes below are historical. Current image-tool live acceptance results are recorded in `.local/image-tools-live.json`.

## Network and inventory verified

- `spark-86e6.local` resolves to **192.168.1.160** on the current network (the older `.155` address is stale).
- SSH `exordium@spark-86e6.local` works through the owner's existing credentials.
- GPU: NVIDIA GB10. `/home/exordium/ai/comfyui/models` resolves to `/home/exordium/ai/models`.
- Installed image weights: `flux1-dev.safetensors`, `flux1-schnell.safetensors`, `sd_xl_base_1.0.safetensors`, DreamShaper XL alpha and Lightning, associated CLIP/T5/VAE files.
- Installed video files include Wan 2.1/2.2, `sulphur_distil-Q6_K.gguf`, Gemma GGUF, and LTX 2.3 video/audio VAEs. These filenames are an inventory, not proof of a complete working video pipeline.
- Approximately 3.2 TB free on SPARK at inspection.

## Startup finding

The existing service manager reported **ComfyUI NOT CREATED**, despite the source and model directories being present. Its configured `nvcr.io/nvidia/pytorch:25.11-py3` image was absent. Starting via `python3 ~/ai/scripts/spark_services.py comfyui` began downloading it. Existing landing and WebUI services were left running.

The service manager documents 25.10+ as the line with native GB10 support. A cached 25.01 image detects CUDA/GB10 but warns that the GPU may not be supported and can use slower compatibility execution. A separate `pixanvil-smoke` container on port 8189 was started as a temporary test path using that cached image. It leaves the normal `comfyui` service and port 8188 configuration intact. Its dependency installation also encountered slow downloads and intermittent DNS errors. Do not use fallback timings as optimized SPARK performance measurements.

The active app configuration now uses `http://192.168.1.160:8188`: the hostname returned both IPv6 and IPv4 and intermittently timed out over HTTP on this network. Direct IPv4 requests worked. The normal 25.11 container finished installation and serves ComfyUI 0.17.0 with NVIDIA PyTorch 2.10.0a0 on GB10. The fallback failed an old-PyTorch compatibility check and remains stopped. Some unrelated video/audio custom nodes still have missing dependencies. No model weights were downloaded during initial implementation.

## Tests and launch

Image tools: five real SPARK checks passed on 2026-09-18 UTC, with evidence in `.local/image-tools-live.json`: SDXL zero-strength variation (pixel-identical to its source, 40.6 s including loading), 0.15-strength variation (17.0 s), DreamShaper XL image refinement (9.5 s), RealESRGAN 2× upscale to 2432×1664 (24.9 s), and a two-checkpoint generation/refinement graph at 512×512 (60.4 s including model switching). These are individual observed timings, not promised performance. All five outputs are in the actual gallery. The noise node was also verified on SPARK's PyTorch for both 4-channel SDXL and 16-channel FLUX latent shapes, including exact strength endpoints and deterministic intermediate noise.

Two additional FLUX Dev checks passed at 1024×1024, recorded in `.local/flux-image-tools-live.json`: zero-strength variation reproduced its source pixels exactly (100.1 s including model loading), and a 12-step image-to-image refinement at denoise 0.25 completed in 20.6 s. Seven real advanced-tool renders have therefore been verified and saved in the gallery. The variation, refinement, and upscale outputs were also visually inspected as local image files; this is separate from browser layout verification.

`comfy_nodes/pixanvil_noise.py` was installed as `/home/exordium/ai/comfyui/custom_nodes/pixanvil_noise.py`. Only ComfyUI was restarted, after checking its queue was empty. The dedicated SDXL Refiner checkpoint is absent; the implemented second-model feature uses image decoding/re-encoding with the target model, not a dedicated SDXL latent handoff. UI syntax, element bindings, served assets, and API capabilities were checked. Browser visual automation remains unavailable (no connected surfaces).

48 automated tests currently pass, including a real CLI subprocess through a live local API and simulated ComfyUI inference, HTTP adapter tests, exclusive coordinator locking, live progress transport/reconnection/job isolation, and image-operation input/recipe/recovery validation. These tests use obvious synthetic fixture pixels and temporary libraries. They never seed the user's gallery with fake model outputs.

App: `http://127.0.0.1:8790`.

```powershell
python pixanvil.py serve
python pixanvil.py generate "your prompt"
python scripts/live_acceptance.py
```

The live acceptance script uses the user's water-knight prompt, verifies the actual ComfyUI prompt id, checks the image file, and confirms the new asset in Gallery. It writes `.local/live-acceptance.json` only on success. `--wait 3600` can wait for runtime readiness; this is not itself a passing result.

No connected browser automation surface was available in the session. The user has been given the running local URL for visual review. JavaScript syntax and local HTTP serving were checked; visual browser acceptance remains distinct from automated backend tests.

## Slow-network checkpoint

**Latest check: 2026-09-17 21:50 UTC.** The normal `nvcr.io/nvidia/pytorch:25.11-py3` image has finished downloading (Docker reports 19.5 GB) and `comfyui` has been running its dependency installation for about five minutes. Recent wheel downloads ran at approximately 0.48–1.2 MB/s. It is currently downloading workflow-template media packages; `/system_stats` on port 8188 is not responding yet. The compatibility `pixanvil-smoke` container exited with a `comfy_kitchen`/old-PyTorch `infer_schema` type incompatibility; do not use it as a ready fallback. The optimized runtime is now the primary path. No live acceptance report exists yet.

The user confirmed this network is throttled and requested keeping downloads on it. The normal service's Docker image download remains active; the separate `pixanvil-smoke` container is downloading Python packages using its cached base image. At the last check, its 21.7 MB frontend and 10.6 MB embedded documentation wheels had finished and the 12.3 MB Transformers wheel was downloading. The model weights themselves are already installed. Neither ComfyUI endpoint has yet passed readiness or real generation acceptance.

The user offered to pause and return in about an hour. On resume, inspect both `docker ps -a` and `docker logs`, plus `/tmp/pixanvil-install.log` inside `pixanvil-smoke`; finish any dependency repair before testing. The app defaults to port 8188; choosing the compatibility runtime on 8189 must be explicit. Do not start another duplicate download or infer successful installation from elapsed time. A live acceptance waiter was started with `--wait 7200`; inspect `.local/live-acceptance.json` and Gallery before submitting another copy of the water-knight prompt.

Live progress was subsequently implemented in Generate, History, and CLI output using a prompt-scoped ComfyUI WebSocket. Counts/percentages describe the current node; decoding/saving clear the sampler percentage, and only HTTP history plus successful collection marks a job complete. Progress disconnects reconnect independently of generation. Binary preview frames are ignored for now. The local server and acceptance waiter were restarted to load this change; actual SPARK progress verification remains pending.
