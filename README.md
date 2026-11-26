# PixAnvil

A local image studio. Windows owns your gallery and generation history; ComfyUI on SPARK renders the images. The app is served only on `127.0.0.1`.

## Quick start

From this directory in PowerShell (Python 3.11+):

```powershell
python -m pip install -r requirements.txt
python pixanvil.py generate "Create a high quality image of a young, handsome teenage water knight casting a powerful water spell. Sleeveless armor, cinematic lighting"
```

This starts PixAnvil if needed, opens the gallery, submits the prompt to ComfyUI, waits for the result, and prints its saved Windows path. The gallery refreshes automatically. Default: installed **FLUX.1 Dev**, 1024×1024, 28 steps. First model load takes longer than subsequent renders.

If ComfyUI is stopped, add `--start-comfy` or use **Start ComfyUI** in the app. First-ever startup can download the configured SPARK container and install its dependencies, which can take substantially longer than a normal start.

```powershell
# Open the gallery without generating
python pixanvil.py open

# Enter your prompt interactively
python pixanvil.py generate

# Faster iteration with a different installed model
python pixanvil.py generate "A watercolor mountain village" --workflow flux-schnell

# SDXL supports negative prompts and phrase emphasis in the UI
python pixanvil.py generate "A cinematic water knight" --workflow sdxl --seed 42 --quality quality

# Two portrait candidates, with exact consecutive seeds
python pixanvil.py generate "A watercolor knight" --width 832 --height 1216 --count 2 --seed 42

# A 4-second 768×512 video with sound (about 3.5 minutes on SPARK)
python pixanvil.py generate "A wizard casts a spell in a stone courtyard. Crackling energy, a rising whoosh." --workflow sulphur-video

# A 5-second 832×448 video with stereo sound on MiniMax H3
python pixanvil.py generate "A wizard casts a spell in a stone courtyard. Crackling energy, a rising whoosh." --workflow minimax-h3-video

# Animate a still image: the clip opens on it (a file is imported into the gallery first)
python pixanvil.py generate "He thrusts his arm forward and casts the spell, a crackling roar" --workflow minimax-h3-video --source C:\path	o\image.png

# Change an image by instruction, using a gallery asset id as the source
python pixanvil.py generate "Change the cloak to crimson red. Keep everything else the same." --workflow qwen-image-edit --source <asset-id>

# Add an outside image to the gallery
python pixanvil.py import C:\path	o\image.png

# Run the app in the foreground; Ctrl+C stops the server
python pixanvil.py serve --open

# Stop the server, including one started in the background by generate or open.
# Unfinished jobs are saved and resume on the next start.
python pixanvil.py stop

# Connection diagnostics
python pixanvil.py status
```

Windows also supports `.\pixanvil.cmd generate "your prompt"`. The Python commands above are the canonical entry points. A command can use `--no-browser` and `--port 8790`; `generate --timeout` controls how long the terminal waits, not the lifetime of a submitted job. `generate --json` prints one JSON result (asset ids, file paths, seeds) on stdout and sends progress to stderr, for scripts and agents.

Open **http://127.0.0.1:8790**. Gallery, Generate, History, and Settings are functional, including seed variations, image-to-image refinement, second-model passes, and RealESRGAN upscaling. Video with sound is available through the Sulphur and MiniMax H3 workflows, and MiniMax H3 can also animate a still image (see Video below). Images can be changed by instruction with Qwen Image Edit (see Image tools). A/B comparison, full folder/collection organization, adapters (LoRAs, hypernetworks), and realtime painting remain later modules.

## Connection and storage

- **SPARK:** the active configuration uses `http://192.168.1.160:8188`. The hostname `spark-86e6.local` resolved to both IPv4 and IPv6, and HTTP requests through it intermittently timed out on this network. Settings → Find SPARK shows discovered IPv4 addresses; use the verified address when the hostname is unreliable. Recheck it when networks change.
- **SSH:** `exordium@spark-86e6.local`. The Start button runs `python3 ~/ai/scripts/spark_services.py comfyui` through your existing Windows SSH configuration. Establish trusted host keys and key access in your own terminal first if needed. PixAnvil does not save SSH passwords or private keys.
- **Default library:** `.local/library/Generations/YYYY-MM-DD/` in this repo. Choose another absolute Windows folder in Settings before generating. Changing the setting affects new outputs; existing assets retain their original locations.
- **App state:** `.local/config.json`, `.local/pixanvil.sqlite3`, and `.local/server.log`. Set `PIXANVIL_STATE` before launching to place app state elsewhere. Use the same value in every terminal instance. App state and generated media are ignored by Git.
- **Backup:** copy the app-state database/config and every configured library, with the server stopped. Originals, thumbnails, and recipe sidecars are in each library. Model weights stay on SPARK and are not part of a library backup.
- **Metadata:** every output is a PNG with PixAnvil recipe metadata and the exact submitted ComfyUI API graph, plus a JSON sidecar. Generated API graphs are not mislabeled as ComfyUI editor-layout files.

Ordinary jobs save automatically. If transfer fails after rendering, collection is retried without generating another image. After a restart the coordinator reconciles saved prompt ids with ComfyUI. If a submission response was lost, it searches queue/history using the local job id rather than blindly resubmitting. An unresolved job blocks subsequent dispatch until it can be reconciled or you choose **Abandon local job** in History. Abandoning never cancels remote execution.

Only jobs still in PixAnvil's local queue can be cancelled in v1. Running ComfyUI work is not interrupted because another client may share the backend.

Generate and History show a live progress bar with counts such as **Sampling · 12 / 28 · 43%**. The terminal prints the same step counts as an ASCII bar. Percentages describe the current node's reported work, not total render time; loading, prompt encoding, decoding, and saving use stage labels. PixAnvil subscribes to ComfyUI's WebSocket before submission, filters events by the exact remote prompt id, and reconnects after interruptions. If live progress is unavailable, generation and result collection continue through HTTP. A full sampling bar does not mean the image is saved yet. Browser and terminal displays refresh every two seconds; intermediate previews are not implemented.

## Workflows and controls

`workflows/*.json` packages contain versioned execution graphs, model discovery rules, profiles, and explicit bindings from UI settings to node inputs. Installed nodes and model filenames are checked before enabling a workflow. SDXL, FLUX.1 Dev, and FLUX.1 Schnell are initial integrations because their weights are already on SPARK. They are not a claim that these are the best models available today.

Prompt presets can be saved/loaded/deleted. The current preset picker replaces prompt text and emphasis while retaining the current model/settings. SDXL offers one phrase-emphasis slider; FLUX hides unsupported weighting and negative-prompt controls. Seeds travel as decimal strings through the UI/API to preserve all 64 bits. A random seed is resolved before submission; batch members receive consecutive seeds.

Read PNG settings from Generate to recover PixAnvil recipes or A1111 fields. Unknown metadata is preserved in the inspection result. Arbitrary ComfyUI graphs can be inspected but are not automatically converted into forms or executed. Imported settings must be reviewed for available models and workflow compatibility.

The frontend uses native JavaScript modules and CSS, served directly by FastAPI. No Node build or frontend package installation is required. The planned React stack was provisional; v1 keeps deployment smaller while preserving backend/workflow/module boundaries.

## Video

Two video workflows are available in Generate and on the command line. Both produce an H.264 MP4 at 24 fps with a generated audio track; describe sounds and speech in the prompt. Every speed/quality profile uses eight steps for both.

| Workflow | Model | Canvases | Lengths | Notes |
|---|---|---|---|---|
| **Sulphur · Video** (`sulphur-video`) | Sulphur, an LTX 2.3 fine-tune (`sulphur_distil-Q6_K.gguf`), through the `ComfyUI_LTX2_SM` custom node | 768 × 512, 512 × 768, 640 × 640 | 25 to 241 frames (1.0 to 10.0 s), 8n+1 | Negative prompt supported; an empty one is replaced by the package default because the encoder node fails without it. Seeds limited to 0–2147483647. |
| **MiniMax H3 · Video** (`minimax-h3-video`) | MiniMax H3 with its 8-step turbo LoRA, on ComfyUI's built-in nodes | 832 × 448, 448 × 832, 768 × 768, 1344 × 768 | 56 to 362 frames (2.3 to 15.1 s), 17n+5 | No negative prompt. Stereo 32 kHz audio. Check the MiniMax H3 Community License before use: it excludes some territories. |

Each video package in `workflows/` declares its own canvases and frame-count rule. The Generate page lists every length the rule allows, with its exact time at 24 fps; `--frames` accepts the same values.

- **Start and end images (MiniMax H3 only):** with the H3 workflow selected, Generate shows two optional slots, **Start image** and **End image**. Fill either or both with **Choose an image file** (the file is imported into the gallery first), or open a gallery image and choose **Animate** to set it as the start image. The clip opens on the start image and finishes on the end image; the prompt describes the motion and sound. With both slots empty it is plain text-to-video. On the command line use `--source` and `--end`. By default the canvas matches the image's shape at about half a megapixel (a 5:4 image becomes 768 × 640); picking a listed canvas center-crops the image to it. An end image is cropped to the same canvas; with only an end image the canvas follows that image. The result links back to its source with **View source**. Sulphur has not been tested for this and does not offer it.
- **First frame:** open a clip and choose **First frame** to add its opening frame to the gallery as an image, linked to the clip. From there it can be edited and animated again. Clips saved before 2026-11-24 did not keep a full-size first frame.
- **Last frame:** clips made since 2026-11-25 also keep their last frame (`<clip>.last.png`), which Branches continues from.
- **Branches:** a node graph of long shots. Images are nodes and image-to-video clips connect them, left to right.
  - **What's shown:** the graph comes from what each clip already records (start image, end image), so clips made anywhere appear automatically.
  - **Clip ends:** a clip's end is its end image, or its last frame drawn faded until something is built from it.
  - **Building:** **Extend from here** and **Extend backwards** open Generate with the node as the start or end image.
  - **The view:** drag nodes to arrange them (positions are saved), scroll to zoom, press F to fit. Click a clip to play it.
  - **Adding to Branches:** open an image or a text-to-video clip in Gallery and choose **Add to Branches**.
  - **Labels:** give a node a short label such as `A1` in the side panel. It shows on the node and names exported files.
  - **Chains:** shift-click connections (or two images with one route between them) to get **Play chain**, which plays them back to back, and **Export chain**, which joins them into one MP4 saved to the gallery. Joining drops the repeated first frame at each join, fits every clip to the first clip's size and frame rate, and keeps sound (silence under clips without it). Joining uses the ffmpeg bundled by `imageio-ffmpeg`.
  - **Takes:** every clip between the same two images is one connection, drawn once however many there are, labelled like `5.2 s · take 2/5` (the preferred take's length and number), or with a custom label. Dotted means no finished take yet; orange means takes are queued; blinking means a take is generating now, in green when nothing is queued after it. Selection is blue. Click a connection to play its preferred take (★), browse and delete takes in a list, **Play all takes** (loops, and says so), **Open take gallery** to view them in a grid and delete several at once, or **New take** to open Generate with the take's settings, both images and a new seed, so the result becomes another take of the same connection. Take numbers follow the order the remaining takes were made, so deleting one renumbers the later ones.
  - **Export selected:** select images and connections and download a zip of `images/<label>.png`, `clips/<from>-<to>.mp4` (the preferred take; other takes are `-take<n>`) and a `graph.json` describing the connections. Useful for producing separate pieces assembled elsewhere.
  - **Status:** phases 1 and 1b, takes, and first slices of the timeline and export in `_plans/branches.md`. The grouped Gallery (batches and take sets) and drag handles are next; the timeline, splits and groups come later.
- **Storage:** clips save as `.mp4` beside images in the dated library folder, with a poster-frame thumbnail and the JSON recipe sidecar. The recipe is not embedded in the MP4, so keep the sidecar with the file. Read PNG settings does not apply to videos; use **Reuse settings** from the gallery.
- **Gallery:** video cards carry a play badge with the length. The viewer plays the clip with sound and offers Download MP4. Vary, Refine, Upscale, and second-model refinement are image-only.
- **Verified:** Sulphur 4-second 768 × 512 clips render in roughly 3.5 to 4 minutes on SPARK including model loading. A 5-second 768 × 640 MiniMax H3 clip from a start image took 163 seconds. The other canvases and lengths are offered but have not each been rendered.
- **SPARK setup:** both run in one ComfyUI 0.45.4. The Sulphur node needs specific package versions and a torchaudio workaround in the container. These are applied by `spark_services.py`; see `_knowledge/spark_integration.md`.

## Image tools

Open a gallery image and choose **Vary**, **Refine**, **Upscale**, **Edit**, or **Animate** (Animate is described under Video). The original stays intact; a new result appears in Gallery with a **View source** link and the operation settings in its recipe.

- **Vary:** keeps the source model, canvas, and base seed, with a separate variation seed and strength slider. Zero uses the original noise; one uses the variation noise entirely. A batch keeps the base seed fixed and increments only the variation seed. This is seed-noise interpolation, distinct from image-to-image editing. Start from an original generation or a seed variation; use Refine for edited/upscaled images. Requires the small bundled `comfy_nodes/pixanvil_noise.py` node on SPARK. It uses existing PyTorch/ComfyUI dependencies. Exact A1111 matching is not promised.
- **Refine:** runs the selected image through FLUX Dev or SDXL with an editable prompt and denoise strength. Low strength preserves more of the source; larger values permit larger changes. The input is resized to the selected canvas, up to 1536 pixels per side. Schnell is not offered as a refinement model.
- **Second-model refinement:** expand this section in Generate and enable a final image-to-image pass with another FLUX Dev or SDXL model. Each stage uses its own text encoder and VAE, allowing supported cross-family image passes. Only the final image is saved; both stage settings and the full graph remain in its recipe. This is decode → encode → image-to-image refinement, not the dedicated SDXL base/refiner latent handoff. The dedicated SDXL Refiner checkpoint is not installed and that handoff remains a later compatibility task.
- **Edit:** changes the image from a plain instruction ("change the cloak to crimson red; keep everything else the same") with Qwen Image Edit 2511. The result keeps the source's shape at about one megapixel, so its size can differ from the source's. Say what must stay the same; the model otherwise feels free to restyle. Profiles run 10, 20, 30 or 40 steps; a 20-step edit took about 2.5 minutes on SPARK including model loading. One real edit has been checked by eye; edits to people's appearance have not been evaluated.
- **What to make:** the menu at the top of Generate holds one of six modes: New image, New video, Edit an image by instruction, Refine, Upscale, and Seed variation. The mode changes only when you change it, and the Workflow list shows only the workflows that can run it. Edit, Refine and Upscale show a **Source image** slot with **Choose an image file**; New video shows the optional start and end slots. Seed variation is greyed out until a generated image is the source, because it reuses that image's seed and model: start it with **Vary** in the gallery.
- **Import:** `python pixanvil.py import <file>` or **Choose an image file** adds an outside image to `Imports/` in the library. PNG files are kept byte for byte; other formats are converted to PNG. Importing the same file twice gives the same asset. Imported images can be refined, upscaled, edited and animated, but not seed-varied.
- **Upscale:** the installed `RealESRGAN_x4plus.pth` runs at 4×; choose either that output size or a Lanczos resize to 2×. Inputs are capped at 2 megapixels to bound the intermediate 4× image. No diffusion redraw is added to this operation.

Refine, upscale, edit and animate jobs snapshot their source PNG in `.local/inputs/` before entering the queue. The worker verifies its hash and uploads it to ComfyUI's `input/pixanvil/` folder before submission. Network upload retries do not submit duplicate renders, and later library moves do not change queued inputs. Include the inputs directory in backups while unfinished jobs exist. The saved source hash and parent asset id remain in output metadata.

`GET /api/image-tools` reports installed capabilities. `POST /api/jobs` accepts `operation`, `source_asset_id`, `end_asset_id` (video end image), `variation_seed`, `variation_strength`, `denoise`, `scale`, `upscale_model`, and an optional `refinement` object alongside generation settings. `GET /api/assets/{id}` returns source/child metadata. `POST /api/assets/import` (multipart `file`) adds an image and returns its asset; `POST /api/assets/{id}/first-frame` does the same for a clip's opening frame. The `operation` values are `txt2img`, `variation`, `refine`, `upscale`, `edit` and `img2vid`.

## Tests

```powershell
python -m pytest -q
node --check frontend/app.js
```

Tests cover real CLI → local API → durable queue → generated PNG → gallery behavior using a deterministic simulated ComfyUI backend, plus the HTTP adapter, restart recovery, ambiguous submissions, collection failures, seed precision, metadata round trips, workflow compatibility, presets, local-origin restrictions, and cancellation ownership. Test images are synthetic fixtures, never presented as real SPARK generations. The live SPARK acceptance test is separate and recorded in `_knowledge/spark_integration.md`.

Dependencies are local CPU app dependencies; no model weights or inference libraries are installed on Windows. ComfyUI's GPU environment remains on SPARK. New model downloads and workflow benchmarks are separate from the app installer.
