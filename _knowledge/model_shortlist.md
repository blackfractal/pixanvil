# Model candidates for SPARK

Research checked 2026-09-17. These are candidates, not a claim that every pipeline has been benchmarked on this machine. SPARK has 128 GB shared CPU/GPU memory; the whole pipeline, activations, operating system, and other services share that budget. A model fitting in memory does not establish interactive performance.

| Task | First candidates | Reason and caveat |
|---|---|---|
| Still images, realism and illustration | Z-Image base and Turbo | Publisher supplies a 6B foundation model and an eight-step Turbo alternative. Compare both with model-appropriate guidance and steps. |
| Still image detail and text | Qwen-Image-2512 | Publisher reports improvements in realism, natural detail, and text layout. Measure full pipeline latency and memory. |
| Fast creative images | Krea 2 Turbo | Eight-step distilled model, with ComfyUI integration listed. Canvas conditioning and end-to-end SPARK latency still need verification. |
| Larger quality comparison | FLUX.2 Dev | 32B generation/editing model. Choose a locally supported precision, include the local text encoder, and test before selecting a default. Gated download. |
| Video, including audio workflows | LTX-2.3 | Text/image-to-video candidate; evaluate duration, memory, audio pipeline, and available nodes. |
| Video motion/quality comparison | Wan 2.2 | Existing high/low I2V weights make this a practical comparison without immediately downloading another large backbone. |

Primary references:

- https://huggingface.co/Tongyi-MAI/Z-Image
- https://huggingface.co/Qwen/Qwen-Image-2512
- https://github.com/krea-ai/krea-2
- https://huggingface.co/black-forest-labs/FLUX.2-dev
- https://huggingface.co/Lightricks/LTX-2.3
- https://huggingface.co/Wan-AI/Wan2.2-I2V-A14B

The user prefers uncensored models. Open weights alone do not establish that property; FLUX.2 Dev explicitly documents safety training. The exact checkpoint/fine-tune needs evaluation. SPARK already contains `gguf/sulphur_distil-Q6_K.gguf`; its filename matches a published LTX 2.3 derivative, but provenance/hash and working node configuration have not been verified. The publisher describes Sulphur 2 as an uncensored LTX 2.3-based video model: https://huggingface.co/SulphurAI/Sulphur-2-base . Do not infer that it is the best general-purpose video model solely from that label.

No Hugging Face login is needed for the initial app test using installed weights. Gated repositories such as FLUX.2 Dev need the user to accept the repository's access conditions and authenticate locally when selected. Dependent encoders can have separate access conditions. Never put an HF token in a prompt, committed configuration, app logs, or chat.

Initial v1 integrations use the installed FLUX.1 Dev/Schnell and SDXL weights. That proves the app's generation path; it is not the final quality comparison. Downloading new weights remains outside the app installer.
