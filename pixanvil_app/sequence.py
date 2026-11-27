"""Join a chain of clips into one video with ffmpeg (bundled by imageio-ffmpeg).

Each clip after the first starts on the image the previous clip ended on, so its first frame repeats
the previous clip's last frame. That first frame is dropped at every join so the picture does not stall."""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg

AUDIO = 'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo'


@dataclass
class Piece:
    """Part of a clip file: frames `start` up to, not including, `end` (None for the end of the file)."""
    path: Path
    start: int = 0
    end: int | None = None


@dataclass
class Clip:
    path: Path
    width: int
    height: int
    fps: float
    seconds: float
    audio: bool


def ffmpeg(*args, timeout=900):
    """Run the bundled ffmpeg. Raises RuntimeError with the end of its log on failure."""
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-nostdin', *map(str, args)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError('ffmpeg failed: ' + result.stderr.strip()[-1500:])
    return result


def probe(path):
    """Size, frame rate, length and whether there is sound, read from ffmpeg's description of the file."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    # With no output named, ffmpeg describes the input and exits with an error; the description is what is needed.
    log = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-nostdin', '-i', str(path)],
                         capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60).stderr
    video = re.search(r'Stream #.*?Video: .*?(\d{2,5})x(\d{2,5})[\s,\[].*?([\d.]+) fps', log)
    duration = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', log)
    if not video or not duration:
        raise ValueError(f'{path.name} is not a readable video')
    hours, minutes, seconds = duration.groups()
    return Clip(path, int(video[1]), int(video[2]), float(video[3]),
                int(hours)*3600 + int(minutes)*60 + float(seconds), bool(re.search(r'Stream #.*?Audio:', log)))


def render(pieces, output):
    """Join `pieces` (Piece, or a path for a whole clip), in order, into an H.264 MP4 at `output`. Returns
    (width, height, fps, seconds). One piece alone is a cut.

    Every clip is fitted to the first clip's size and frame rate. Sound is kept where clips have it,
    with silence under clips that have none."""
    pieces = [p if isinstance(p, Piece) else Piece(Path(p)) for p in pieces]
    clips = [probe(p.path) for p in pieces]
    if not clips:
        raise ValueError('A chain needs at least one clip')
    first = clips[0]
    width, height, fps = first.width, first.height, first.fps
    sound = any(c.audio for c in clips)
    filters, joined = [], ''
    for i, (piece, clip) in enumerate(zip(pieces, clips)):
        first = piece.start + (0 if i == 0 else 1)
        until = f':end_frame={piece.end}' if piece.end is not None else ''
        filters.append(f'[{i}:v]trim=start_frame={first}{until},setpts=PTS-STARTPTS,fps={fps},'
                       f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                       f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v{i}]')
        joined += f'[v{i}]'
        if sound:
            skip = first/clip.fps
            stop = piece.end/clip.fps if piece.end is not None else clip.seconds
            if clip.audio:
                filters.append(f'[{i}:a]atrim=start={skip:.6f}:end={stop:.6f},asetpts=PTS-STARTPTS,{AUDIO}[a{i}]')
            else:
                filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={max(stop-skip, 0.01):.6f},{AUDIO}[a{i}]')
            joined += f'[a{i}]'
    filters.append(f'{joined}concat=n={len(clips)}:v=1:a={int(sound)}[v]' + ('[a]' if sound else ''))
    output = Path(output)
    part = output.with_name(output.stem + '.part.mp4')
    inputs = [arg for p in pieces for arg in ('-i', p.path)]
    audio = ['-map', '[a]', '-c:a', 'aac', '-b:a', '192k'] if sound else []
    ffmpeg('-y', *inputs, '-filter_complex', ';'.join(filters), '-map', '[v]', *audio,
           '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', part)
    part.replace(output)
    return width, height, fps, probe(output).seconds


def first_frame(video, image):
    """Save a video's first frame as a PNG."""
    image = Path(image)
    part = image.with_name(image.stem + '.part.png')
    ffmpeg('-y', '-i', video, '-frames:v', '1', part)
    part.replace(image)


def frame_at(video, number, image):
    """Save frame `number` (counting from 0) of a video as a PNG, chosen by its number, not by time."""
    image = Path(image)
    part = image.with_name(image.stem + '.part.png')
    ffmpeg('-y', '-i', video, '-vf', f'select=eq(n\\,{int(number)})', '-fps_mode', 'passthrough', '-frames:v', '1', part)
    if not part.is_file():
        raise ValueError(f'The video has no frame {number}')
    part.replace(image)
