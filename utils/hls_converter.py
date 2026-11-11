import os
import uuid
import subprocess
from pathlib import Path
import ffmpeg
import imageio_ffmpeg
from utils import get_temp_dir, cleanup_file

def convert_to_hls(input_path: str, output_dir: str = None, start: float | None = None,
                duration: float | None = None, progress_callback: callable | None = None) -> str:
    """Convert any video file to an HLS playlist.
    Returns the absolute path to the generated ``.m3u8`` manifest.
    The output is stored in a unique sub‑folder under ``output_dir`` (or a temp dir).
    Optional ``start`` and ``duration`` trim the source.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input video not found: {input_path}")
    base_dir = Path(output_dir) if output_dir else Path(get_temp_dir())
    base_dir.mkdir(parents=True, exist_ok=True)
    clip_dir = base_dir / f"hls_{uuid.uuid4().hex}"
    clip_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = clip_dir / "index.m3u8"
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    input_opts = []
    if start is not None:
        input_opts += ["-ss", str(start)]
    if duration is not None:
        input_opts += ["-t", str(duration)]
    cmd = [ffmpeg_exe] + input_opts + ["-i", input_path,
        "-c:v", "libx264", "-c:a", "aac",
        "-hls_time", "4",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", "segment_%03d.ts",
        "-hls_list_size", "0",
        "-f", "hls",
        str(manifest_path)]
    if progress_callback:
        progress_callback(f"Running ffmpeg conversion to HLS: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg HLS conversion failed: {result.stderr.decode(errors='ignore')}")
    if not manifest_path.exists() or not any(clip_dir.glob("*.ts")):
        raise RuntimeError("HLS output not generated correctly")
    return str(manifest_path)
