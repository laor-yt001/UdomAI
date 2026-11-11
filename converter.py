import os
import json
import time
import math
import numpy as np
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
import uuid
import subprocess
import shutil
import requests
import ffmpeg
import imageio_ffmpeg
from PIL import Image
from utils import get_temp_dir, cleanup_file
import re
import logging
import threading

# Suppress Hugging Face Hub unauthenticated warning logs
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("transformers").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)
_VOXCPM2_PIPELINES = {}
_VOXCPM2_PIPELINE_LOCK = threading.Lock()
_FAILED_VOXCPM2_MODELS = set()
_VOXCPM2_UNAVAILABLE = False

# Configure ffmpeg binary environment and create ffmpeg.exe alias
ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
ffmpeg_dir = os.path.dirname(ffmpeg_exe)
if ffmpeg_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

ffmpeg_alias = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == 'nt' else "ffmpeg")
if not os.path.exists(ffmpeg_alias) and os.path.exists(ffmpeg_exe):
    try:
        import shutil
        shutil.copyfile(ffmpeg_exe, ffmpeg_alias)
    except Exception as e:
        print(f"Could not create ffmpeg alias: {e}")

from pydub import AudioSegment
AudioSegment.converter = ffmpeg_alias
AudioSegment.ffmpeg = ffmpeg_alias

def _run_ffmpeg_with_progress(stream, output_path, progress_callback):
    try:
        process = stream.run_async(
            cmd=imageio_ffmpeg.get_ffmpeg_exe(),
            pipe_stderr=True,
            pipe_stdout=False,
            quiet=True
        )
        
        size_pattern = re.compile(r"size=\s*(\d+[a-zA-Z]+)")
        speed_pattern = re.compile(r"speed=\s*([\d.]+x)")
        stderr_lines = []
        
        while True:
            line = process.stderr.readline()
            if not line:
                break
            
            line_str = line.decode('utf-8', errors='ignore')
            stderr_lines.append(line_str)
            
            if progress_callback:
                size_match = size_pattern.search(line_str)
                speed_match = speed_pattern.search(line_str)
                
                if size_match and speed_match:
                    size = size_match.group(1)
                    speed = speed_match.group(1)
                    progress_callback(f"Converting... Size: {size}, Speed: {speed}")
                    
        process.wait()
        
        # If output file exists and is non-empty, consider it successful
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
            
        if process.returncode != 0:
            err_msg = "".join(stderr_lines[-5:]).strip()
            print(f"FFmpeg error with code {process.returncode}: {err_msg}")
            return None
            
        return output_path if (os.path.exists(output_path) and os.path.getsize(output_path) > 0) else None
    except Exception as e:
        print(f"Error during ffmpeg execution: {e}")
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
        return None

def convert_video_to_audio(input_path, output_format='mp3', progress_callback=None):
    """
    Converts a video or audio file to a specified audio format.
    """
    temp_dir = get_temp_dir()
    output_filename = f"{uuid.uuid4()}.{output_format}"
    output_path = os.path.join(temp_dir, output_filename)
    
    stream = (
        ffmpeg
        .input(input_path)
        .output(output_path, acodec='libmp3lame' if output_format == 'mp3' else 'copy', qscale=2)
        .overwrite_output()
    )
    
    res = _run_ffmpeg_with_progress(stream, output_path, progress_callback)
    if res and os.path.exists(res) and os.path.getsize(res) > 0:
        return res

    # Direct subprocess fallback for audio extraction if ffmpeg-python stream failed
    try:
        import subprocess
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-y", "-i", input_path]
        if output_format == 'mp3':
            cmd.extend(["-vn", "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-q:a", "2", "-map_metadata", "-1", output_path])
        else:
            cmd.extend(["-vn", "-acodec", "copy", "-map_metadata", "-1", output_path])
        
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=7200)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception as e:
        print(f"Direct FFmpeg audio fallback failed: {e}")

    return None

def has_copyright_metadata(input_path):
    import subprocess, re
    import imageio_ffmpeg
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-i", input_path]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=14400)
        stderr_text = res.stderr.decode("utf-8", errors="ignore")
        # Look for metadata block
        metadata_match = re.search(r"Metadata:(.*?)(?:Duration:|Stream #)", stderr_text, re.DOTALL | re.IGNORECASE)
        if metadata_match:
            meta_text = metadata_match.group(1).lower()
            for field in ['title', 'artist', 'album', 'copyright', 'description', 'comment']:
                if re.search(fr"^\s*{field}\s*:\s*(.+)", meta_text, re.MULTILINE):
                    return True
    except Exception:
        pass
    return False

def convert_video_format(input_path, output_format='mp4', progress_callback=None):
    """
    Converts a video file to a different video format.
    """
    if has_copyright_metadata(input_path) and progress_callback:
        progress_callback("⚠️ Copyright metadata detected and removed from original file.")

    temp_dir = get_temp_dir()
    output_filename = f"{uuid.uuid4()}.{output_format}"
    output_path = os.path.join(temp_dir, output_filename)
    
    stream = (
        ffmpeg
        .input(input_path)
        .output(output_path, map_metadata="-1")
        .overwrite_output()
    )
    
    res = _run_ffmpeg_with_progress(stream, output_path, progress_callback)
    if res and os.path.exists(res) and os.path.getsize(res) > 0:
        return res

    # Direct subprocess fallback for video format conversion
    try:
        import subprocess
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-y", "-i", input_path, "-vcodec", "libx264", "-acodec", "aac", "-strict", "-2", "-map_metadata", "-1", output_path]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=14400)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception as e:
        print(f"Direct FFmpeg video fallback failed: {e}")

    return None

def convert_image_format(input_path, output_format='png'):
    """
    Converts an image file to a different format using Pillow.
    """
    temp_dir = get_temp_dir()
    output_filename = f"{uuid.uuid4()}.{output_format}"
    output_path = os.path.join(temp_dir, output_filename)
    
    try:
        with Image.open(input_path) as img:
            rgb_im = img.convert('RGB')
            rgb_im.save(output_path, format=output_format.upper())
        return output_path
    except Exception as e:
        print(f"Error converting image format: {e}")
        return None

def convert_document_format(input_path, output_format='pdf'):
    """
    Converts a document (PDF, DOCX, TXT) to another document format (PDF, DOCX, TXT).
    """
    temp_dir = get_temp_dir()
    output_filename = f"{uuid.uuid4()}.{output_format}"
    output_path = os.path.join(temp_dir, output_filename)
    
    ext = os.path.splitext(input_path)[1].lower()
    extracted_text = ""
    
    try:
        if ext == '.pdf':
            import fitz
            doc = fitz.open(input_path)
            for page in doc:
                extracted_text += str(page.get_text()) + "\n"
        elif ext in ['.docx', '.doc']:
            import docx
            doc = docx.Document(input_path)
            extracted_text = "\n".join([p.text for p in doc.paragraphs])
        else:
            with open(input_path, 'r', encoding='utf-8', errors='ignore') as f:
                extracted_text = f.read()
                
        if not extracted_text.strip():
            extracted_text = "No text content found in original file."

        if output_format == 'txt':
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(extracted_text)
            return output_path
            
        elif output_format == 'docx':
            import docx
            doc = docx.Document()
            for line in extracted_text.split('\n'):
                doc.add_paragraph(line)
            doc.save(output_path)
            return output_path
            
        elif output_format == 'pdf':
            import fitz
            doc = fitz.open()
            page = doc.new_page()
            margin = 50
            rect = fitz.Rect(margin, margin, page.rect.width - margin, page.rect.height - margin)
            page.insert_textbox(rect, extracted_text, fontsize=11)
            doc.save(output_path)
            return output_path
            
    except Exception as e:
        print(f"Error converting document: {e}")
        return None

def detect_speech_segments(input_path, min_silence_dur=0.4, noise_threshold=-28):
    """
    Detects start and end timestamps (seconds) of spoken parts in media using FFmpeg silencedetect.
    Returns list of tuples: [(start_sec, end_sec), ...]
    """
    duration = get_video_duration(input_path)
    if duration <= 0:
        return []
        
    try:
        import subprocess
        import re
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, "-i", input_path,
            "-af", f"silencedetect=noise={noise_threshold}dB:d={min_silence_dur}",
            "-f", "null", "-"
        ]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        
        silence_starts = []
        silence_ends = []
        
        for line in res.stderr.split('\n'):
            if "silence_start:" in line:
                m = re.search(r"silence_start:\s*([\d.]+)", line)
                if m:
                    silence_starts.append(float(m.group(1)))
            elif "silence_end:" in line:
                m = re.search(r"silence_end:\s*([\d.]+)", line)
                if m:
                    silence_ends.append(float(m.group(1)))
                    
        silences = []
        for i in range(min(len(silence_starts), len(silence_ends))):
            s_start = silence_starts[i]
            s_end = silence_ends[i]
            if s_end > s_start:
                silences.append((s_start, s_end))
                
        if not silences:
            return [(0.0, duration)]
            
        segments = []
        curr = 0.0
        for s_start, s_end in silences:
            if s_start > curr + 0.3:
                segments.append((curr, s_start))
            curr = s_end
            
        if curr < duration - 0.3:
            segments.append((curr, duration))
            
        return segments if segments else [(0.0, duration)]
    except Exception as e:
        print(f"Speech segment detection error: {e}")
        return []

def check_audio_rms(file_path, min_max_vol=-25.0, min_mean_vol=-45.0):
    """
    Checks if an audio file has audible music/SFX content using FFmpeg volumedetect filter.
    Returns True if max_volume > min_max_vol AND mean_volume > min_mean_vol, else False.
    """
    try:
        import subprocess
        import re
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, "-i", file_path,
            "-af", "volumedetect",
            "-f", "null", "-"
        ]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        match_max = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", res.stderr)
        match_mean = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", res.stderr)
        if match_max:
            max_vol = float(match_max.group(1))
            mean_vol = float(match_mean.group(1)) if match_mean else -99.0
            return max_vol > min_max_vol and mean_vol > min_mean_vol
    except Exception as e:
        print(f"Error checking RMS volume: {e}")
    return os.path.exists(file_path) and os.path.getsize(file_path) > 500

def extract_bgm_demucs(input_path, output_path=None, return_vocals=False, progress_callback=None):
    """
    Uses Demucs v4 (htdemucs) AI stem separation to cleanly extract background music & sound effects
    (no_vocals stem) regardless of audio channel layout (mono or stereo).
    Fallback to DSP notch filtering if Demucs fails or BGM RMS volume is near silence (< -45dB).
    """
    dur_in = get_video_duration(input_path)
    # Skip Demucs for very long videos (> 30 minutes) to avoid hanging, OOM crashes, or extreme delays.
    if dur_in > 1800:
        print(f"Video duration ({dur_in}s) > 1800s. Skipping Demucs, using fast DSP fallback.")
        res = extract_bgm_no_vocals_dsp(input_path, output_path)
        return (res, None) if return_vocals else res

    if progress_callback:
        progress_callback("🎵 [Separating BGM] Demucs AI isolating...")

    temp_dir = get_temp_dir()
    if not output_path:
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_bgm_demucs.mp3")
        
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
        
    import subprocess, shutil, sys
    
    # 1. Extract audio track to temporary WAV file (ensures Demucs & sphn can load cleanly)
    temp_input_wav = os.path.join(temp_dir, f"{uuid.uuid4()}_input_track.wav")
    demucs_out_dir = os.path.join(temp_dir, f"demucs_{uuid.uuid4().hex[:8]}")
    try:
        cmd_wav = [
            ffmpeg_exe, "-y", "-i", input_path,
            "-ar", "44100", "-ac", "2", temp_input_wav
        ]
        subprocess.run(cmd_wav, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=14400)
        
        if os.path.exists(temp_input_wav) and os.path.getsize(temp_input_wav) > 1000:
            cmd = [
                sys.executable, "-m", "demucs.separate", "--two-stems=vocals", "-n", "htdemucs",
                temp_input_wav, "-o", demucs_out_dir
            ]
            dur_in = get_video_duration(input_path)
            # Demucs on CPU can take up to 10-15x real-time. Give it ample timeout.
            demucs_timeout = max(900, int(dur_in * 15))
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=demucs_timeout)
            
            no_vocals_file = None
            vocals_file = None
            for root, _, files in os.walk(demucs_out_dir):
                for f in files:
                    if "no_vocals" in f.lower():
                        no_vocals_file = os.path.join(root, f)
                    elif "vocals" in f.lower() and "no_vocals" not in f.lower():
                        vocals_file = os.path.join(root, f)
                    
            if no_vocals_file and os.path.exists(no_vocals_file):
                # Convert no_vocals stem to 44.1kHz MP3
                cmd_cvt = [
                    ffmpeg_exe, "-y", "-i", no_vocals_file,
                    "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
                    output_path
                ]
                subprocess.run(cmd_cvt, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=14400)
                
                out_vocals_path = None
                if return_vocals and vocals_file and os.path.exists(vocals_file):
                    out_vocals_path = os.path.join(temp_dir, f"{uuid.uuid4()}_vocals_demucs.mp3")
                    cmd_cvt_voc = [
                        ffmpeg_exe, "-y", "-i", vocals_file,
                        "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
                        out_vocals_path
                    ]
                    subprocess.run(cmd_cvt_voc, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=14400)

                shutil.rmtree(demucs_out_dir, ignore_errors=True)
                cleanup_file(temp_input_wav)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                    return (output_path, out_vocals_path) if return_vocals else output_path
    except Exception as e:
        print(f"Demucs extraction error: {e}")
        shutil.rmtree(demucs_out_dir, ignore_errors=True)
        cleanup_file(temp_input_wav)

    # Fallback to DSP filter if Demucs fails
    res_dsp = extract_bgm_no_vocals_dsp(input_path, output_path)
    return (res_dsp, None) if return_vocals else res_dsp

def extract_bgm_no_vocals_dsp(input_path, output_path=None):
    """
    Ultra-fast DSP audio extraction for background music & sound effects.
    Cancels center-channel panned vocals and eliminates 300Hz-3.4kHz human speech frequencies.
    Runs in < 0.3s without CPU/memory bottlenecks.
    """
    temp_dir = get_temp_dir()
    if not output_path:
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_bgm_dsp.mp3")
        
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    import subprocess
    
    af_filter = "pan=stereo|c0=0.5*c0-0.5*c1|c1=0.5*c1-0.5*c0, equalizer=f=300:width_type=h:width=400:g=-24, equalizer=f=1200:width_type=h:width=1800:g=-30, equalizer=f=2800:width_type=h:width=1200:g=-24, volume=1.8"
    try:
        cmd = [
            ffmpeg_exe, "-y", "-i", input_path,
            "-af", af_filter, "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        if check_audio_rms(output_path):
            return output_path
    except Exception:
        pass

    try:
        af_notch = "equalizer=f=300:width_type=h:width=400:g=-24, equalizer=f=1200:width_type=h:width=1800:g=-30, equalizer=f=2800:width_type=h:width=1200:g=-24, volume=1.8"
        cmd = [
            ffmpeg_exe, "-y", "-i", input_path,
            "-af", af_notch, "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
    except Exception:
        pass

    return None

def extract_bgm_no_vocals(input_path, output_path=None):
    """Wrapper function to extract BGM without vocals."""
    return extract_bgm_demucs(input_path, output_path)

def mix_tts_with_bgm(tts_audio_path, bgm_audio_path, target_dur=None, bgm_volume=0.70, tts_volume=1.40):
    """
    Mixes translated/recap AI speech track (tts_volume=1.40, boosted voice) with original background music & sound effects track (bgm_volume=0.70) in 44.1kHz Stereo.
    """
    temp_dir = get_temp_dir()
    mixed_path = os.path.join(temp_dir, f"{uuid.uuid4()}_mixed_speech_bgm.mp3")
    
    if not bgm_audio_path or not os.path.exists(bgm_audio_path) or os.path.getsize(bgm_audio_path) < 500:
        return tts_audio_path
        
    try:
        tts_dur = get_video_duration(tts_audio_path)
        bgm_dur = get_video_duration(bgm_audio_path)
        dur = target_dur or tts_dur or bgm_dur
        
        if bgm_dur > 0 and tts_dur > bgm_dur:
            bgm_in = ffmpeg.input(bgm_audio_path, stream_loop=-1).audio
        else:
            bgm_in = ffmpeg.input(bgm_audio_path).audio
            
        tts_in = ffmpeg.input(tts_audio_path).audio
        
        # Translate voice is prominently louder than BGM & Sound Effects (Voice 1.40 vs BGM 0.70)
        tts_vol = tts_in.filter('volume', tts_volume)
        bgm_vol = bgm_in.filter('volume', bgm_volume)
        mixed_stream = ffmpeg.filter([tts_vol, bgm_vol], 'amix', inputs=2, duration='longest', dropout_transition=2, normalize=False)
        mixed_stream = mixed_stream.filter('aresample', 44100).filter('aformat', channel_layouts='stereo')
        
        out_opts = {'acodec': 'libmp3lame', 'b:a': '192k', 'ar': '44100', 'ac': 2}
        if dur and dur > 0:
            out_opts['t'] = dur
            
        stream = ffmpeg.output(mixed_stream, mixed_path, **out_opts).overwrite_output()
        _run_ffmpeg_with_progress(stream, mixed_path, None)
        
        if os.path.exists(mixed_path) and os.path.getsize(mixed_path) > 100:
            return mixed_path
    except Exception as e:
        print(f"Error mixing TTS with BGM: {e}")
        
    return tts_audio_path

def build_atempo_filter_chain(speed_ratio):
    """
    FFmpeg atempo filter requires values between 0.5 and 2.0.
    Chains multiple atempo filters for values outside this range.
    """
    speed_ratio = max(0.25, min(4.0, speed_ratio))
    if 0.5 <= speed_ratio <= 2.0:
        return [("atempo", speed_ratio)]
    elif speed_ratio > 2.0:
        f1 = 2.0
        f2 = speed_ratio / 2.0
        return [("atempo", f1), ("atempo", f2)]
    else:
        f1 = 0.5
        f2 = speed_ratio / 0.5
        return [("atempo", f1), ("atempo", f2)]

import numpy as np

# ─── Whisper model cache (loaded once, reused across all dub calls) ───────────
_WHISPER_MODEL_CACHE = {}
_WHISPER_MODEL_LOCK = None

WHISPER_MODEL_CHOICES = ["gemini", "tiny", "base", "small", "medium", "large-v3"]


def _normalize_whisper_model(model_name=None):
    if model_name is None:
        model_name = os.getenv("WHISPER_MODEL", "small")
    normalized = str(model_name or "small").strip().lower().replace(" ", "-")
    if normalized in ("gemini", "gemini-api", "default", "auto", ""):
        return "small"
    aliases = {
        "tiny": "tiny",
        "tiny-en": "tiny",
        "base": "base",
        "base-en": "base",
        "small": "small",
        "small-en": "small",
        "medium": "medium",
        "med": "medium",
        "md": "medium",
        "sm": "small",
        "large": "large-v3",
        "large-v3": "large-v3",
    }
    return aliases.get(normalized, normalized if normalized in WHISPER_MODEL_CHOICES else "small")


def _get_whisper_model(model_name=None):
    """Returns the cached WhisperModel, loading it on first call."""
    global _WHISPER_MODEL_CACHE, _WHISPER_MODEL_LOCK
    import threading
    if _WHISPER_MODEL_LOCK is None:
        _WHISPER_MODEL_LOCK = threading.Lock()
    resolved_model = _normalize_whisper_model(model_name)
    with _WHISPER_MODEL_LOCK:
        if resolved_model not in _WHISPER_MODEL_CACHE:
            # Proxy protection for Hugging Face download:
            # If SOCKS proxy is configured in environment but socksio isn't installed,
            # temporarily strip SOCKS proxy so HF hub downloads directly without crashing httpx
            proxy_keys = ['ALL_PROXY', 'all_proxy', 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy']
            stripped_proxies = {}
            has_socks_proxy = any('socks' in str(os.environ.get(k, '')).lower() for k in proxy_keys)
            if has_socks_proxy:
                try:
                    import socksio
                except ImportError:
                    for k in proxy_keys:
                        if 'socks' in str(os.environ.get(k, '')).lower():
                            stripped_proxies[k] = os.environ.pop(k)

            try:
                from faster_whisper import WhisperModel
                device = "cuda" if os.environ.get("CUDA_VISIBLE_DEVICES") not in (None, "") else "cpu"
                compute_type = "int8_float16" if device == "cuda" else "int8"
                try:
                    _WHISPER_MODEL_CACHE[resolved_model] = WhisperModel(resolved_model, device=device, compute_type=compute_type)
                    logger.info(f"[Whisper] Loaded model '{resolved_model}' into cache.")
                except Exception as e_model:
                    logger.warning(f"Could not load WhisperModel({resolved_model}) with device={device}: {e_model}")
                    try:
                        _WHISPER_MODEL_CACHE[resolved_model] = WhisperModel(resolved_model, device="cpu", compute_type="int8")
                        logger.info(f"[Whisper] Fallback-loaded model '{resolved_model}' to CPU cache.")
                    except Exception as e_cpu:
                        logger.error(f"Failed to load WhisperModel({resolved_model}) on CPU: {e_cpu}")
                        return None
            except Exception as e:
                logger.error(f"faster_whisper not available: {e}")
                return None
            finally:
                for k, v in stripped_proxies.items():
                    os.environ[k] = v

        return _WHISPER_MODEL_CACHE.get(resolved_model)

# ─── Persistent requests session for translation HTTP calls ──────────────────
_TRANSLATE_SESSION = None

def _get_translate_session():
    global _TRANSLATE_SESSION
    if _TRANSLATE_SESSION is None:
        import requests as _requests
        _TRANSLATE_SESSION = _requests.Session()
        _TRANSLATE_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
    return _TRANSLATE_SESSION

def detect_voice_presentation_from_audio(audio_path):
    """
    Estimates speaker voice presentation (male vs female) from fundamental pitch (F0).
    Typical adult male F0 is 85-155 Hz; adult female F0 is 165-260+ Hz.
    """
    if not audio_path or not os.path.exists(audio_path):
        return "female"
    try:
        import librosa
        y, sample_rate = librosa.load(audio_path, sr=16000, mono=True, duration=5.0)
        y = np.asarray(y, dtype=np.float32)
        if y.size < sample_rate // 4 or float(np.max(np.abs(y))) < 0.01:
            return "female"
        pitches = np.asarray(librosa.yin(
            y,
            fmin=65,
            fmax=350,
            sr=sample_rate,
            frame_length=1024,
            hop_length=256,
        ), dtype=float)
        voiced_pitch = pitches[np.isfinite(pitches) & (pitches >= 65) & (pitches <= 350)]
        if voiced_pitch.size > 0:
            median_pitch = float(np.median(voiced_pitch))
            return "male" if median_pitch < 160 else "female"
    except Exception as e:
        logger.debug(f"Librosa pitch detection exception: {e}")

    # Fallback: autocorrelation pitch detector
    try:
        import wave
        with wave.open(audio_path, 'rb') as wf:
            framerate = wf.getframerate()
            nframes = min(wf.getnframes(), framerate * 3)
            raw_data = wf.readframes(nframes)
            data = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)
            if data.size > framerate // 4 and np.max(np.abs(data)) > 100:
                corr = np.correlate(data[:framerate], data[:framerate], mode='full')
                corr = corr[len(corr)//2:]
                min_lag = int(framerate / 350)
                max_lag = int(framerate / 70)
                if max_lag < len(corr):
                    peak_lag = min_lag + np.argmax(corr[min_lag:max_lag])
                    if peak_lag > 0:
                        pitch = framerate / peak_lag
                        return "male" if pitch < 160 else "female"
    except Exception:
        pass

    return "female"


def _detect_gender_from_pcm_slice(slice_pcm, sr=16000):
    """
    Fast, robust pitch detector on in-memory PCM float32 samples.
    Returns 'male' if median F0 < 160Hz, else 'female'.
    """
    if slice_pcm is None or len(slice_pcm) < sr // 4:
        return "female"
    try:
        import librosa
        pitches = librosa.yin(slice_pcm, fmin=65, fmax=350, sr=sr, frame_length=1024, hop_length=256)
        valid = pitches[np.isfinite(pitches) & (pitches >= 65) & (pitches <= 350)]
        if len(valid) > 0:
            median_f0 = float(np.median(valid))
            return "male" if median_f0 < 160 else "female"
    except Exception:
        pass
    # Fast autocorrelation fallback
    try:
        if len(slice_pcm) > sr // 4 and float(np.max(np.abs(slice_pcm))) > 0.01:
            corr = np.correlate(slice_pcm[:sr], slice_pcm[:sr], mode='full')
            corr = corr[len(corr)//2:]
            min_lag = int(sr / 350)
            max_lag = int(sr / 65)
            if max_lag < len(corr):
                peak_lag = min_lag + int(np.argmax(corr[min_lag:max_lag]))
                pitch_hz = sr / peak_lag
                return "male" if pitch_hz < 160 else "female"
    except Exception:
        pass
    return "female"

VOXCPM_MODELS = {
    "openbmb/VoxCPM2": "VoxCPM2 (Flagship 2B Multilingual)",
    "openbmb/VoxCPM1.5": "VoxCPM1.5 (0.6B Fast & Expressive)",
    "openbmb/VoxCPM-0.5B": "VoxCPM0.5 (0.5B Lightweight)",
}

DEFAULT_VOXCPM_MODEL = "openbmb/VoxCPM1.5"

VOXCPM_ALIASES = {
    "voxcpm2": "openbmb/VoxCPM2",
    "voxcpm-2": "openbmb/VoxCPM2",
    "vox-cpm2": "openbmb/VoxCPM2",
    "voxcpm1.5": "openbmb/VoxCPM1.5",
    "voxcpm-1.5": "openbmb/VoxCPM1.5",
    "vox-cpm1.5": "openbmb/VoxCPM1.5",
    "voxcpm15": "openbmb/VoxCPM1.5",
    "voxcpm0.5": "openbmb/VoxCPM-0.5B",
    "voxcpm-0.5": "openbmb/VoxCPM-0.5B",
    "vox-cpm0.5": "openbmb/VoxCPM-0.5B",
    "voxcpm0.5b": "openbmb/VoxCPM-0.5B",
    "voxcpm-0.5b": "openbmb/VoxCPM-0.5B",
    "voxcpm05": "openbmb/VoxCPM-0.5B",
    "voxcpm": "openbmb/VoxCPM2",
    "vox": "openbmb/VoxCPM2",
    "openbmb/voxcpm2": "openbmb/VoxCPM2",
    "openbmb/voxcpm1.5": "openbmb/VoxCPM1.5",
    "openbmb/voxcpm-0.5b": "openbmb/VoxCPM-0.5B",
}

def normalize_voxcpm_model(model_name=None):
    """
    Normalizes any input (short name, alias, HF URL, repo ID) to a canonical Hugging Face repo ID.
    Supports switching between openbmb/VoxCPM2, openbmb/VoxCPM1.5, openbmb/VoxCPM-0.5B, or any custom HF repo.
    """
    if not model_name or str(model_name).strip().lower() in ("default", "auto", "voxcpm", "voxcpm2"):
        model_name = os.getenv("VOXCPM_MODEL") or DEFAULT_VOXCPM_MODEL
    model_str = str(model_name).strip()

    if "huggingface.co/" in model_str:
        clean_url = model_str.split("?")[0].split("#")[0].rstrip("/")
        parts = clean_url.split("huggingface.co/")[-1].split("/")
        if len(parts) >= 2:
            model_str = f"{parts[0]}/{parts[1]}"

    lookup_key = model_str.lower().strip()
    if lookup_key in VOXCPM_ALIASES:
        return VOXCPM_ALIASES[lookup_key]

    if "/" not in model_str:
        lower = model_str.lower()
        if "1.5" in lower or "15" in lower:
            return "openbmb/VoxCPM1.5"
        elif "0.5" in lower or "05" in lower:
            return "openbmb/VoxCPM-0.5B"
        elif "2" in lower:
            return "openbmb/VoxCPM2"
        return f"openbmb/{model_str}"

    return model_str


_VOXCPM_PIPELINES = {}
_VOXCPM_PIPELINE_LOCK = threading.Lock()
_FAILED_VOXCPM_MODELS = set()
_VOXCPM2_PIPELINES = _VOXCPM_PIPELINES
_VOXCPM2_PIPELINE_LOCK = _VOXCPM_PIPELINE_LOCK
_FAILED_VOXCPM2_MODELS = _FAILED_VOXCPM_MODELS
_VOXCPM2_UNAVAILABLE = False


def _normalize_tts_backend(backend_name=None):
    value = (backend_name or os.getenv("TTS_BACKEND") or "voxcpm2").strip().lower().replace("_", "-")
    aliases = {
        "edge": "edge-tts",
        "edge-tts": "edge-tts",
        "edgetts": "edge-tts",
        "vox": "voxcpm2",
        "voxcpm": "voxcpm2",
        "voxcpm2": "voxcpm2",
        "vox-cpm2": "voxcpm2",
        "voxcpm1.5": "voxcpm2",
        "voxcpm-1.5": "voxcpm2",
        "voxcpm0.5": "voxcpm2",
        "voxcpm-0.5": "voxcpm2",
        "voxcpm-0.5b": "voxcpm2",
        "huggingface": "voxcpm2",
        "gtts": "gtts",
        "google-tts": "gtts",
        "googletts": "gtts",
    }
    return aliases.get(value, value if value in {"edge-tts", "voxcpm2", "voxcpm", "gtts"} else "edge-tts")


def _get_voxcpm2_gguf_path():
    """Locates the direct VoxCPM2-BaseLM-Q8_0.gguf model file."""
    candidates = [
        os.getenv("VOXCPM2_GGUF_PATH"),
        os.path.join(os.path.dirname(__file__), "model", "VoxCPM2-BaseLM-Q8_0.gguf"),
        os.path.join(os.path.dirname(__file__), "models", "VoxCPM2-BaseLM-Q8_0.gguf"),
        os.path.abspath("model/VoxCPM2-BaseLM-Q8_0.gguf"),
        os.path.abspath("models/VoxCPM2-BaseLM-Q8_0.gguf"),
        os.path.join(os.path.dirname(__file__), "model", "VoxCPM2-BaseLM-F16.gguf"),
        os.path.join(os.path.dirname(__file__), "models", "VoxCPM2-BaseLM-F16.gguf"),
    ]
    for p in candidates:
        if p and os.path.exists(p) and os.path.getsize(p) > 1000000:
            return p
    return None


def _get_voxcpm_cli():
    """Finds any available VoxCPM CLI runner executable."""
    for name in ["voxcpm2-cli", "voxcpm_tts", "voxcpm-tts", "voxcpm", "voxcpm_cli"]:
        found = shutil.which(name)
        if found:
            return found

    search_dirs = [
        os.path.join(os.path.dirname(__file__), "model"),
        os.path.join(os.path.dirname(__file__), "models"),
        os.path.join(os.path.dirname(__file__), "venv", "Scripts"),
        os.path.abspath("model"),
        os.path.abspath("models"),
    ]
    for d in search_dirs:
        if not os.path.exists(d):
            continue
        for name in ["voxcpm2-cli.exe", "voxcpm_tts.exe", "voxcpm-tts.exe", "voxcpm.exe", "voxcpm2-cli", "voxcpm_tts", "voxcpm"]:
            cand = os.path.join(d, name)
            if os.path.exists(cand):
                return cand
    return None


def get_voxcpm_pipeline(model_name=None):
    """
    Retrieves or initializes a cached VoxCPM pipeline from Hugging Face or local path.
    Supports switching between openbmb/VoxCPM2, openbmb/VoxCPM1.5, openbmb/VoxCPM-0.5B, etc.
    """
    model_id = normalize_voxcpm_model(model_name)
    if model_id in _FAILED_VOXCPM_MODELS:
        return None, model_id

    with _VOXCPM_PIPELINE_LOCK:
        if model_id in _VOXCPM_PIPELINES:
            return _VOXCPM_PIPELINES[model_id], model_id

        try:
            import torch
            # Robust audio loader monkeypatch for torchaudio to bypass broken torchcodec
            try:
                import torchaudio
                import soundfile as _sf
                def _safe_torchaudio_load(uri, **kwargs):
                    data, sr = _sf.read(uri, dtype='float32')
                    if data.ndim == 1:
                        tensor = torch.from_numpy(data).unsqueeze(0)
                    else:
                        tensor = torch.from_numpy(data.T)
                    return tensor, sr
                torchaudio.load = _safe_torchaudio_load
            except Exception as _patch_err:
                logger.debug(f"[VoxCPM] torchaudio monkeypatch note: {_patch_err}")

            from voxcpm import VoxCPM
            device = os.getenv("VOXCPM_DEVICE")
            if not device:
                device = "cuda" if torch.cuda.is_available() else "cpu"

            logger.info(f"[VoxCPM] Initializing foundation model '{model_id}' (device={device})...")

            # Check if local directory exists with weights
            local_dir = None
            if os.path.isdir(model_id):
                local_dir = model_id
            else:
                base_name = model_id.split("/")[-1]
                for cand_dir in [
                    os.path.join(os.path.dirname(__file__), "model"),
                    os.path.join(os.path.dirname(__file__), "models"),
                    os.path.join(os.path.dirname(__file__), "model", base_name),
                    os.path.join(os.path.dirname(__file__), "models", base_name),
                ]:
                    if os.path.isdir(cand_dir) and os.path.exists(os.path.join(cand_dir, "config.json")) and (
                        os.path.exists(os.path.join(cand_dir, "model.safetensors")) or
                        os.path.exists(os.path.join(cand_dir, "pytorch_model.bin"))
                    ):
                        local_dir = cand_dir
                        break

            target_source = local_dir or model_id
            pipe = VoxCPM.from_pretrained(
                target_source,
                load_denoiser=False,
                optimize=False,
                device=device,
            )
            _VOXCPM_PIPELINES[model_id] = pipe
            logger.info(f"[VoxCPM] Successfully loaded '{model_id}'")
            return pipe, model_id
        except Exception as e:
            logger.warning(f"[VoxCPM] Could not initialize model '{model_id}': {e}")
            _FAILED_VOXCPM_MODELS.add(model_id)
            return None, model_id


def khmer_to_phonetic_latin(text):
    """
    Converts Khmer script (Unicode \\u1780-\\u17ff) into human-readable phonetic Latin
    transcriptions (Romanized Khmer / G2P) so VoxCPM models (VoxCPM-0.5B, VoxCPM1.5,
    VoxCPM2) can vocalize Khmer speech naturally with character voice cloning.
    """
    if not text or not str(text).strip():
        return text or ""

    # Safety net: If text contains Chinese / East Asian characters, translate to Khmer via GTX first!
    if re.search(r'[\u4e00-\u9fff]', text):
        try:
            import urllib.parse
            session = _get_translate_session()
            url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=km&dt=t&q={urllib.parse.quote(text)}"
            res = session.get(url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
                if translated and len(translated.strip()) > 0:
                    text = translated.strip()
        except Exception:
            pass

    if not re.search(r'[\u1780-\u17ff]', text):
        return text or ""

    # Comprehensive dictionary of conversational phrases and words
    KHMER_WORDS_DICT = {
        'ជំរាបសួរ': 'chumreap suor',
        'ជម្រាបសួរ': 'chumreap suor',
        'សួស្តី': 'suosdey',
        'សួស្ដី': 'suosdey',
        'ជំរាបលា': 'chumreap lea',
        'ជម្រាបលា': 'chumreap lea',
        'លាហើយ': 'lea haeuy',
        'បាទ': 'baat',
        'ចាស': 'chaas',
        'ចាស៎': 'chaas',
        'អរគុណ': 'orkun',
        'សូមអរគុណ': 'som orkun',
        'សូមទោស': 'somtos',
        'អត់ទោស': 'ottos',
        'មិនអីទេ': 'min ey te',
        'ខ្ញុំ': 'khnhom',
        'ខ្ញុំបាទ': 'khnhom baat',
        'ខ្ញុំម្ចាស់': 'khnhom mchas',
        'អ្នក': 'neak',
        'លោក': 'lok',
        'លោកស្រី': 'loksrey',
        'កញ្ញា': 'kanya',
        'លោកពូ': 'lokpou',
        'អ្នកមីង': 'neakming',
        'បង': 'bong',
        'បងប្រុស': 'bong proh',
        'បងស្រី': 'bong srey',
        'ប្អូន': 'p\'oun',
        'ប្អូនប្រុស': 'p\'oun proh',
        'ប្អូនស្រី': 'p\'oun srey',
        'លោកវេជ្ជបណ្ឌិត': 'lok vejjabandet',
        'វេជ្ជបណ្ឌិត': 'vejjabandet',
        'លោកគ្រូពេទ្យ': 'lok kroupet',
        'អ្នកគ្រូពេទ្យ': 'neak kroupet',
        'គ្រូពេទ្យ': 'kroupet',
        'ពេទ្យ': 'pet',
        'គិលានុបដ្ឋាយិកា': 'kilanoopatheyika',
        'មន្ទីរពេទ្យ': 'muntirpet',
        'អ្នកជំងឺ': 'neak chomngu',
        'ជំងឺ': 'chomngu',
        'ព្យាបាល': 'phyabal',
        'វះកាត់': 'veah kat',
        'បន្ទប់វះកាត់': 'bantub veah kat',
        'សង្គ្រោះបន្ទាន់': 'sang krouh bantoan',
        'សង្គ្រោះ': 'sang krouh',
        'អ្នកខ្លាំងអាវស': 'neak khlang aov sor',
        'មែនទេ': 'men te',
        'មែនហើយ': 'men haeuy',
        'ពិតមែន': 'pit men',
        'ពិតជា': 'pit chea',
        'ហេតុអ្វី': 'het avei',
        'យ៉ាងម៉េច': 'yang mech',
        'យ៉ាងម៉េចហើយ': 'yang mech haeuy',
        'ទៅណា': 'tov na',
        'មកពីណា': 'mok pi na',
        'នៅឯណា': 'nov ae na',
        'ស្រលាញ់': 'sralanh',
        'ស្រឡាញ់': 'sralanh',
        'ចូលចិត្ត': 'choul chet',
        'ឈឺ': 'chheu',
        'ឈឺចាប់': 'chheu chap',
        'របួស': 'robuos',
        'ជួយ': 'chuoy',
        'ជួយផង': 'chuoy phong',
        'ប្រយ័ត្ន': 'proyat',
        'លឿនឡើង': 'loeun laeng',
        'ឆាប់ឡើង': 'chhab laeng',
        'ឈប់': 'chhub',
        'កុំ': 'kom',
        'កុំបារម្ភ': 'kom barom',
        'កុំភ័យ': 'kom phey',
        'ដឹង': 'doeng',
        'មិនដឹង': 'min doeng',
        'យល់': 'yul',
        'មិនយល់': 'min yul',
        'បាន': 'ban',
        'មិនបាន': 'min ban',
        'ត្រូវ': 'trow',
        'ត្រូវតែ': 'trow tae',
        'មិនត្រូវ': 'min trow',
        'ល្អ': 'l\'or',
        'ល្អណាស់': 'l\'or nas',
        'ស្អាត': 'sa\'at',
        'ច្រើន': 'chraen',
        'តិចតួច': 'tek tuoch',
        'មួយ': 'muoy',
        'ពីរ': 'pii',
        'បី': 'bei',
        'បួន': 'buon',
        'ប្រាំ': 'pram',
        'ថ្ងៃនេះ': 'thngai nih',
        'ម្សិលមិញ': 'msel minh',
        'ថ្ងៃស្អែក': 'thngai s\'aek',
        'ឥឡូវនេះ': 'elaov nih',
        'ពេលនេះ': 'pel nih',
        'ម៉ោង': 'maong',
        'នាទី': 'neati',
        'រឿង': 'reuang',
        'ជីវិត': 'chivit',
        'បេះដូង': 'beh doung',
        'ខួរក្បាល': 'khuor kbal',
        'ឈាម': 'chheam',
        'សម្ពាធឈាម': 'sampeat chheam',
        'ចង្វាក់បេះដូង': 'changvak beh doung',
        'ថ្នាំ': 'thnam',
        'ចាក់ថ្នាំ': 'chak thnam',
        'លេបថ្នាំ': 'leb thnam',
        'គាត់': 'koat',
        'នាង': 'neang',
        'យើង': 'yeung',
        'ពួកគេ': 'pouke',
        'មាន': 'mean',
        'គ្មាន': 'kmean',
        'ទៅ': 'tov',
        'មក': 'mok',
        'នៅ': 'nov',
        'និង': 'ning',
        'ឬ': 'reue',
        'ប៉ុន្តែ': 'pontae',
        'ពីព្រោះ': 'pi pruoh',
        'ដោយសារ': 'doy sar',
        'អី': 'ey',
        'ស្អី': 's\'ey',
        'អ្វី': 'avei',
        'ដូចម្តេច': 'doychmtech',
        'ធ្ងន់ធ្ងរ': 'thngon thngor',
        'សុខសប្បាយ': 'sok sabay',
        'ធូរស្បើយ': 'thour sbaey',
        'ហើយ': 'haeuy',
        'ហើយឬនៅ': 'haeuy reue nov',
        'តើ': 'tae',
        'អូខេ': 'okay',
        'បាទអរគុណ': 'baat orkun',
        'ចាសអរគុណ': 'chaas orkun',
    }

    result = text
    for kw in sorted(KHMER_WORDS_DICT.keys(), key=len, reverse=True):
        if kw in result:
            result = result.replace(kw, f" {KHMER_WORDS_DICT[kw]} ")

    # If all Khmer characters replaced, return cleaned spacing
    if not re.search(r'[\u1780-\u17ff]', result):
        return re.sub(r'\s+', ' ', result).strip()

    # Fallback Syllable / Character-level Khmer phonetic transcoder
    KHMER_SERIES2 = set('គឃងជឈញឌឍទធនពភមយរលវ')
    KHMER_CONSONANTS = {
        'ក': 'k', 'ខ': 'kh', 'គ': 'k', 'ឃ': 'kh', 'ង': 'ng',
        'ច': 'ch', 'ឆ': 'chh', 'ជ': 'ch', 'ឈ': 'chh', 'ញ': 'nh',
        'ដ': 'd', 'ឋ': 'th', 'ឌ': 'd', 'ឍ': 'th', 'ណ': 'n',
        'ត': 't', 'ថ': 'th', 'ទ': 't', 'ធ': 'th', 'ន': 'n',
        'ប': 'b', 'ផ': 'ph', 'ព': 'p', 'ភ': 'ph', 'ម': 'm',
        'យ': 'y', 'រ': 'r', 'ល': 'l', 'វ': 'v',
        'ស': 's', 'ហ': 'h', 'ឡ': 'l', 'អ': 'a'
    }
    KHMER_INDEPENDENT = {
        'ឦ': 'ey', 'ឧ': 'u', 'ឩ': 'ou', 'ឪ': 'ov',
        'ឫ': 'rue', 'ឬ': 'roeu', 'ឭ': 'lue', 'ឮ': 'loeu',
        'ឯ': 'ae', 'ឰ': 'ai', 'ឲ': 'aoy', 'ឳ': 'ov', 'ឥ': 'e'
    }
    KHMER_VOWELS = {
        'ា': ('a', 'ea'), 'ិ': ('e', 'i'), 'ី': ('ey', 'ee'),
        'ឹ': ('oe', 'ue'), 'ឺ': ('oeu', 'eu'), 'ុ': ('o', 'u'),
        'ូ': ('ou', 'oo'), 'ួ': ('uo', 'uor'), 'ើ': ('aeu', 'eu'),
        'ឿ': ('oea', 'uea'), 'ៀ': ('ie', 'ie'), 'េ': ('e', 'ei'),
        'ែ': ('ae', 'ae'), 'ៃ': ('ai', 'ey'), 'ោ': ('ao', 'ou'),
        'ៅ': ('ov', 'aw')
    }
    KHMER_SIGNS = {
        'ំ': ('om', 'um'), 'ះ': ('ah', 'eh'), 'ៈ': 'a',
        '័': 'oat', '៏': '', '៍': '', '៌': '', '៎': '',
        '៑': '', '៓': '', '។': '.', '៕': '.', '៖': ':', 'ៗ': ' '
    }

    # Number mapping
    num_map = {'០': '0', '១': '1', '២': '2', '៣': '3', '៤': '4', '៥': '5', '៦': '6', '៧': '7', '៨': '8', '៩': '9'}
    for kn, an in num_map.items():
        result = result.replace(kn, an)
    for iv, rom in KHMER_INDEPENDENT.items():
        result = result.replace(iv, rom)

    out = []
    i = 0
    n = len(result)
    while i < n:
        c = result[i]
        if c in KHMER_CONSONANTS:
            base_cons = c
            is_s2 = base_cons in KHMER_SERIES2
            cons_rom = KHMER_CONSONANTS[base_cons]
            sub_cons = ''
            j = i + 1
            while j + 1 < n and result[j] == '\u17d2':
                sub_c = result[j+1]
                if sub_c in KHMER_CONSONANTS:
                    sub_cons += KHMER_CONSONANTS[sub_c]
                    if sub_c in KHMER_SERIES2:
                        is_s2 = True
                j += 2

            vowel_rom = ''
            sign_rom = ''
            has_vowel = False
            while j < n:
                next_c = result[j]
                if next_c in KHMER_VOWELS:
                    v_tuple = KHMER_VOWELS[next_c]
                    vowel_rom += v_tuple[1] if is_s2 else v_tuple[0]
                    has_vowel = True
                    j += 1
                elif next_c in KHMER_SIGNS:
                    s_val = KHMER_SIGNS[next_c]
                    if isinstance(s_val, tuple):
                        sign_rom += s_val[1] if is_s2 else s_val[0]
                    else:
                        sign_rom += s_val
                    j += 1
                elif next_c == '\u17c6':
                    sign_rom += 'um' if is_s2 else 'om'
                    j += 1
                elif next_c == '\u17c7':
                    sign_rom += 'eh' if is_s2 else 'ah'
                    j += 1
                else:
                    break

            if not has_vowel and not sign_rom:
                vowel_rom = 'o' if is_s2 else 'a'

            out.append(cons_rom + sub_cons + vowel_rom + sign_rom)
            i = j
        elif c in KHMER_SIGNS:
            s_val = KHMER_SIGNS[c]
            out.append(s_val[0] if isinstance(s_val, tuple) else s_val)
            i += 1
        elif '\u1780' <= c <= '\u17ff':
            i += 1
        else:
            out.append(c)
            i += 1

    return re.sub(r'\s+', ' ', ''.join(out)).strip()


def _generate_voxcpm_tts(text, lang='km', gender='female', output_path=None, model_name=None, ref_audio=None, prompt_text=None):
    if not text or not text.strip():
        return None

    clean_lang = (lang or 'km').lower().strip()[:2]
    # For Khmer language or text containing Khmer characters, transcode to natural phonetic Latin
    # so VoxCPM models (VoxCPM-0.5B, VoxCPM1.5, VoxCPM2) can accurately vocalize Khmer speech with voice cloning.
    spoken_text = text.strip()
    if clean_lang in ('km', 'kh') or bool(re.search(r'[\u1780-\u17ff]', spoken_text)):
        spoken_text = khmer_to_phonetic_latin(spoken_text)
        logger.info(f"[VoxCPM] Converted Khmer text to phonetic transcription: '{spoken_text}'")

    if not spoken_text or not spoken_text.strip():
        return None

    if not output_path:
        output_path = os.path.join(get_temp_dir(), f"{uuid.uuid4()}_voxcpm_tts.wav")

    model_id = normalize_voxcpm_model(model_name)

    # 1. Direct Hugging Face / native VoxCPM pipeline with character voice cloning
    pipe, resolved_model = get_voxcpm_pipeline(model_id)
    if pipe is not None:
        try:
            gen_kwargs = {"text": spoken_text}
            is_v2 = "voxcpm2" in resolved_model.lower() or "voxcpm-2" in resolved_model.lower()
            if ref_audio and os.path.exists(ref_audio) and os.path.getsize(ref_audio) > 100:
                if is_v2:
                    gen_kwargs["reference_wav_path"] = ref_audio
                else:
                    gen_kwargs["prompt_wav_path"] = ref_audio
                    prompt_speech = prompt_text or text
                    if bool(re.search(r'[\u1780-\u17ff]', prompt_speech)):
                        prompt_speech = khmer_to_phonetic_latin(prompt_speech)
                    gen_kwargs["prompt_text"] = prompt_speech

            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
            audio_out = None
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(pipe.generate, **gen_kwargs)
                    audio_out = future.result(timeout=35.0)
            except FutureTimeoutError:
                logger.warning(f"[VoxCPM] Generation timed out (>35s) on CPU for '{resolved_model}' – falling back gracefully.")
                audio_out = None
            except Exception as e_run:
                logger.warning(f"[VoxCPM] Generation with ref_audio error for '{resolved_model}': {e_run} – retrying pure text...")
                try:
                    with ThreadPoolExecutor(max_workers=1) as executor2:
                        future2 = executor2.submit(pipe.generate, text=spoken_text)
                        audio_out = future2.result(timeout=25.0)
                except Exception as e_retry:
                    logger.warning(f"[VoxCPM] Pure text generation also failed: {e_retry}")
                    audio_out = None

            if audio_out is not None:
                import soundfile as sf
                if hasattr(audio_out, "numpy"):
                    audio_out = audio_out.numpy()
                elif hasattr(audio_out, "cpu"):
                    audio_out = audio_out.cpu().numpy()

                sample_rate = getattr(getattr(pipe, "tts_model", None), "sample_rate", 48000)
                sf.write(output_path, audio_out, sample_rate)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                    logger.info(f"[VoxCPM] Successfully synthesized Khmer speech with '{resolved_model}' ({sample_rate}Hz, cloned={bool(ref_audio)})")
                    return output_path
        except Exception as e_gen:
            logger.warning(f"[VoxCPM] Generation error with '{resolved_model}': {e_gen}")

    # 2. Direct GGUF CLI fallback if available (for VoxCPM2 GGUF models)
    if "voxcpm2" in resolved_model.lower():
        gguf_path = _get_voxcpm2_gguf_path()
        voxcpm_cli = _get_voxcpm_cli()
        if voxcpm_cli and gguf_path:
            model_dir = os.path.dirname(gguf_path)
            acoustic = os.path.join(model_dir, "VoxCPM2-Acoustic-F16.gguf")

            cli_attempts = []
            if os.path.exists(acoustic):
                cli_attempts.append([voxcpm_cli, "-t", spoken_text, "-o", output_path, gguf_path, acoustic])
            cli_attempts.append([voxcpm_cli, "-m", gguf_path, "-t", spoken_text, "-o", output_path])
            cli_attempts.append([voxcpm_cli, "--model-path", gguf_path, "--text", spoken_text, "--output", output_path])
            cli_attempts.append([voxcpm_cli, "design", "--model-path", model_dir, "--text", spoken_text, "--output", output_path])

            for cmd in cli_attempts:
                try:
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                        logger.info("[VoxCPM2] Successfully synthesized speech with direct GGUF CLI.")
                        return output_path
                except Exception as e_cli:
                    logger.debug(f"voxcpm CLI attempt failed: {e_cli}")

    return None


_generate_voxcpm2_tts = _generate_voxcpm_tts


def generate_neural_tts(text, lang='km', gender='female', output_path=None, rate=None, pitch=None, backend=None, voxcpm_model=None, ref_audio=None, prompt_text=None):
    """
    Generates high-quality neural voice synthesis using edge-tts with robust gTTS fallback.
    Supports selecting a backend via the `backend` argument or `TTS_BACKEND` environment variable.
    Supports selecting a VoxCPM model via `voxcpm_model` or `VOXCPM_MODEL` environment variable.
    Supports voice cloning via `ref_audio` for VoxCPM.
    """
    if not text or not text.strip():
        return None

    temp_dir = get_temp_dir()
    if not output_path:
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_neural_tts.mp3")

    clean_lang = lang.lower().strip()[:2]
    if clean_lang == 'kh':
        clean_lang = 'km'
    clean_text = text.strip()
    backend_name = _normalize_tts_backend(backend)

    # Strip sound tags and brackets like [Music], (Laugh), etc.
    speech_text = re.sub(r'\[.*?\]|\(.*?\)|[\(\)\[\]{}♪♫♬]', '', clean_text).strip()
    # Check if there are any pronounceable characters (letters, numbers, Khmer/Asian script)
    if not re.search(r'[\w\u1780-\u17ff\u4e00-\u9fff]', speech_text):
        # Segment contains only punctuation/cues; return None so dubber skips cleanly
        return None
    clean_text = speech_text

    # Enhance Khmer spoken prosody and breathing pauses for human-like natural delivery
    if clean_lang == 'km' and clean_text:
        for conj in ['ប៉ុន្តែ', 'ពីព្រោះ', 'ដូច្នេះ']:
            clean_text = clean_text.replace(f' {conj}', f', {conj}')
        clean_text = re.sub(r',\s*,+', ',', clean_text)

    # VoxCPM (0.5B, 1.5, 2.0) neural synthesis with speaker voice cloning
    if backend_name in {'voxcpm', 'voxcpm2'}:
        result = _generate_voxcpm_tts(
            clean_text, lang=clean_lang, gender=gender,
            output_path=output_path, model_name=voxcpm_model,
            ref_audio=ref_audio, prompt_text=prompt_text
        )
        if result and os.path.exists(result) and os.path.getsize(result) > 100:
            return result

    # 1. Try edge-tts for Khmer, English, Chinese, etc.
    if backend_name in {'edge-tts', 'gtts', 'voxcpm', 'voxcpm2'}:
        EDGE_VOICES = {
            'km': {'female': 'km-KH-SreymomNeural', 'male': 'km-KH-PisethNeural'},
            'en': {'female': 'en-US-AvaNeural', 'male': 'en-US-GuyNeural'},
            'zh': {'female': 'zh-CN-XiaoxiaoNeural', 'male': 'zh-CN-YunxiNeural'},
            'vi': {'female': 'vi-VN-HoaiMyNeural', 'male': 'vi-VN-NamMinhNeural'},
            'th': {'female': 'th-TH-PremwadeeNeural', 'male': 'th-TH-NiwatNeural'},
            'ko': {'female': 'ko-KR-SunHiNeural', 'male': 'ko-KR-InJoonNeural'},
            'ja': {'female': 'ja-JP-NanamiNeural', 'male': 'ja-JP-KeitaNeural'},
            'fr': {'female': 'fr-FR-DeniseNeural', 'male': 'fr-FR-HenriNeural'},
            'es': {'female': 'es-ES-ElviraNeural', 'male': 'es-ES-AlvaroNeural'},
            'de': {'female': 'de-DE-KatjaNeural', 'male': 'de-DE-ConradNeural'},
            'ru': {'female': 'ru-RU-SvetlanaNeural', 'male': 'ru-RU-DmitryNeural'}
        }

        voice_map = EDGE_VOICES.get(clean_lang, EDGE_VOICES['km'] if clean_lang == 'km' else EDGE_VOICES['en'])
        voice = voice_map.get(gender, voice_map['female'])

        if voice and clean_text:
            try:
                import edge_tts
                import asyncio
                async def _run_edge():
                    kwargs = {"text": clean_text, "voice": voice}
                    effective_rate = rate
                    effective_pitch = pitch
                    if not effective_rate and clean_lang == 'km':
                        # TVB cinema drama cadence: +3% to +5% for natural conversational pacing instead of slow monotone
                        effective_rate = "+5%" if any(clean_text.endswith(x) for x in ['!', '?', '?!']) else "+3%"
                    if not effective_pitch and clean_lang == 'km':
                        effective_pitch = "-2Hz" if gender == 'male' else "+1Hz"

                    if effective_rate:
                        kwargs["rate"] = effective_rate
                    if effective_pitch:
                        kwargs["pitch"] = effective_pitch
                    communicate = edge_tts.Communicate(**kwargs)
                    await communicate.save(output_path)

                for attempt in range(2):
                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.run_coroutine_threadsafe(_run_edge(), loop).result(20)
                    except RuntimeError:
                        asyncio.run(_run_edge())
                    except Exception as e_inner:
                        err_str = str(e_inner)
                        if "No audio was received" in err_str:
                            break
                        logger.debug(f"edge-tts attempt {attempt+1} retryable error: {e_inner}")
                        continue

                    if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
                        return output_path
            except Exception as e:
                if "No audio was received" not in str(e):
                    logger.warning(f"edge-tts fallback to gTTS: {e}")

    # 2. Fallback to gTTS
    try:
        from gtts import gTTS
        gtts_lang = 'zh-CN' if clean_lang == 'zh' else ('km' if clean_lang in ('km', 'kh') else clean_lang)
        tts = gTTS(text=clean_text, lang=gtts_lang, slow=False)
        tts.save(output_path)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            return output_path
    except Exception as e_gtts:
        logger.debug(f"gTTS error: {e_gtts}")

    return None


def khmer_cinema_dialogue_adapter(text: str, src_text: str = None) -> str:
    """
    Transforms translated text into natural, authentic spoken Khmer cinema dubbing dialogue.
    Adapts stiff, literal machine translations (from Google Translate/MyMemory/fallback models)
    into emotionally expressive dialogue with natural Cambodian drama pronouns,
    spoken colloquial particles (ណា៎, តើ, ហ្នឹងហើយ, ស្អីគេ?!), and realistic intonation.
    """
    if not text or not text.strip():
        return text

    t = text.strip()

    # 1. Clean robotic numbers, quotes, and markdown formatting
    t = re.sub(r'^[0-9]+[\.\:\-\)]\s*', '', t)
    t = re.sub(r'[*#_~`>\[\]\(\)]', '', t).strip()

    # 2. Comprehensive Cambodian Cinema Dubbing Idiom & Phrasing Replacements
    REPLACEMENTS = [
        # Questions & Confrontations
        ('តើអ្នកកំពុងធ្វើអ្វី', 'ឯងកំពុងធ្វើស្អីហ្នឹង?'),
        ('តើអ្នកចង់បានអ្វី', 'ឯងចង់បានស្អី?'),
        ('តើអ្នកចង់មានន័យថាម៉េច', 'ឯងចង់មានន័យថាម៉េច?!'),
        ('តើអ្នកចង់និយាយអ្វី', 'ឯងចង់និយាយស្អី?'),
        ('តើអ្នកឈ្មោះអ្វី', 'ឯងឈ្មោះអី?'),
        ('តើអ្នកទៅណា', 'ឯងទៅណា?'),
        ('តើអ្នកនៅឯណា', 'ឯងនៅឯណា?'),
        ('តើអ្នកសុខសប្បាយជាទេ', 'ឯងមិនអីទេហី?'),
        ('តើអ្នកគិតយ៉ាងម៉េច', 'ឯងគិតថាម៉េចដែរ?'),
        ('តើឯងគិតយ៉ាងម៉េច', 'ឯងគិតថាម៉េចដែរ?'),
        ('តើមានរឿងអ្វីកើតឡើង', 'មានរឿងអីកើតឡើងហ្នឹង?!'),
        ('តើមានបញ្ហាអ្វី', 'មានរឿងអីកើតឡើង?'),
        ('តើពិតមែនទេ', 'ពិតមែនអ្ហេស?!'),
        ('តើត្រូវទេ', 'ត្រូវអត់?'),
        ('តើអ្នកដឹងទេ', 'ឯងដឹងអត់?'),
        ('តើអ្នកឮទេ', 'ឯងឮអត់?'),
        ('តើអ្នកឃើញទេ', 'ឯងឃើញអត់?'),
        ('តើអ្នកចាំទេ', 'ឯងចាំអត់?'),
        ('តើអ្នកអាចជួយខ្ញុំបានទេ', 'ជួយខ្ញុំបន្តិចបានទេ?'),

        # Shock, Denial, Emotion
        ('មិនអាចទៅរួចទេ', 'មិនអាចទេ...!'),
        ('មិនអាចជាការពិតទេ', 'មិនពិតទេ...!'),
        ('វាជាការពិត', 'វាជាការពិតណា៎!'),
        ('ខ្ញុំមិនដឹងទេ', 'ខ្ញុំអត់ដឹងទេ!'),
        ('ខ្ញុំមិនយល់ទេ', 'ខ្ញុំអត់យល់ទេ!'),
        ('ខ្ញុំមិនចង់បានទេ', 'ខ្ញុំអត់ចង់បានទេ'),
        ('ខ្ញុំមិនជឿទេ', 'ខ្ញុំមិនជឿទេ...!'),
        ('ពិតជាអស្ចារ្យណាស់', 'ឡូយខ្លាំងណាស់!'),
        ('ពិតជាល្អណាស់', 'ល្អខ្លាំងណាស់!'),
        ('ខ្ញុំស្រឡាញ់អ្នក', 'បងស្រឡាញ់អូន'),

        # Urgency & Commands
        ('ឆាប់ឡើង', 'ឆាប់ឡើង'),
        ('ប្រញាប់ឡើង', 'ប្រញាប់ឡើង'),
        ('ឈប់សិន', 'ឈប់សិន'),
        ('ឈប់ភ្លាម', 'ឈប់ភ្លាម'),
        ('កុំបារម្ភ', 'កុំបារម្ភអី'),
        ('កុំខ្លាច', 'កុំខ្លាចអី'),
        ('កុំយំ', 'កុំយំអីណា៎'),
        ('កុំរត់', 'កុំរត់'),
        ('ចេញទៅឲ្យឆ្ងាយ', 'ចេញទៅឲ្យឆ្ងាយ!'),
        ('ទៅឆ្ងាយទៅ', 'ចេញទៅឲ្យឆ្ងាយ!'),
        ('ចេញទៅ', 'ចេញទៅ'),
        ('ជួយផង', 'ជួយផង'),
        ('ជួយខ្ញុំផង', 'ជួយខ្ញុំផង'),
        ('សម្លាប់វា', 'សម្លាប់វាទៅ'),
        ('កុំមកជិត', 'កុំមកជិតយើង'),

        # Stiff Bureaucratic Verbs -> Authentic Spoken Verbs
        ('ធ្វើការចាកចេញទៅកាន់', 'ចេញទៅ'),
        ('ចាកចេញទៅកាន់', 'ចេញទៅ'),
        ('ធ្វើការចាកចេញ', 'ចេញទៅ'),
        ('ចេញទៅទៅ', 'ចេញទៅ'),
        ('ធ្វើការវាយប្រហារ', 'វាយប្រហារ'),
        ('ធ្វើការសម្លាប់', 'សម្លាប់'),
        ('ធ្វើការនិយាយ', 'និយាយ'),
        ('ធ្វើការជួយ', 'ជួយ'),
        ('ធ្វើការសង្គ្រោះ', 'ជួយសង្គ្រោះ'),
        ('ធ្វើការការពារ', 'ការពារ'),
        ('ធ្វើការហូប', 'ញ៉ាំ'),
        ('ធ្វើការញ៉ាំ', 'ញ៉ាំ'),
        ('ធ្វើការសម្រេចចិត្ត', 'សម្រេចចិត្ត'),
        ('ធ្វើការជ្រើសរើស', 'ជ្រើសរើស'),
        ('ធ្វើការប្រយុទ្ធ', 'ប្រយុទ្ធ'),

        # Grammar & Conjunction Flattening
        ('ហេតុអ្វីបានជា', 'ម៉េចបានជា'),
        ('ហេតុអ្វី', 'ហេតុអី'),
        ('យ៉ាងម៉េចដែរ', 'យ៉ាងម៉េចហើយ?'),
        ('យ៉ាងម៉េច', 'ម៉េចហើយ'),
        ('សូមអរគុណ', 'អរគុណច្រើន'),
        ('សូមទោស', 'សុំទោសណា៎'),
        ('អត់ទោស', 'សុំទោសផង'),
        ('មិនអីទេ', 'មិនអីទេណា៎'),
        ('បាយចៀន', 'បាយឆា'),
        ('អង្ករចៀន', 'បាយឆា'),

        # Spoken Prepositions
        ('ទៅកាន់', 'ទៅ'),
        ('មកកាន់', 'មក'),
        ('ជាមួយនឹង', 'ជាមួយ'),
        ('ដោយសារតែ', 'ពីព្រោះ'),

        # Modern Drama, Medical & TVB Hong Kong Cinema Dubbing (Fixing Robotic Translations)
        ('វេជ្ជបណ្ឌិត', 'លោកវេជ្ជបណ្ឌិត'),
        ('អ្នកជំងឺ', 'អ្នកជំងឺ'),
        ('បន្ទប់សង្គ្រោះបន្ទាន់', 'បន្ទប់សង្គ្រោះបន្ទាន់'),
        ('ការវះកាត់', 'ការវះកាត់'),
        ('បន្ទប់វះកាត់', 'បន្ទប់វះកាត់'),
        ('សម្ពាធឈាមធ្លាក់ចុះ', 'សម្ពាធឈាមធ្លាក់ចុះហើយ!'),
        ('ចង្វាក់បេះដូងឈប់លោត', 'ចង្វាក់បេះដូងឈប់លោតហើយ!'),
        ('ឆាប់ជួយសង្គ្រោះ', 'ឆាប់ជួយសង្គ្រោះភ្លាម!'),
        ('ឆាប់ហៅឡានពេទ្យ', 'ឆាប់ហៅឡានពេទ្យមក!'),
        ('ផុតពីគ្រោះថ្នាក់ហើយ', 'ផុតពីគ្រោះថ្នាក់ហើយ!'),
        ('លោកប្រធាន', 'លោកប្រធាន'),
        ('ប្រធានផ្នែក', 'ប្រធានផ្នែក'),
        ('កុំទាន់អស់សង្ឃឹម', 'កុំទាន់អស់សង្ឃឹម!'),
        ('កុំបោះបង់', 'កុំបោះបង់ឲ្យសោះ!'),
        ('តស៊ូឡើង', 'ត្រូវតែតស៊ូឡើង!'),
        ('ស្តាប់ខ្ញុំសិន', 'ស្តាប់ខ្ញុំសិន!'),
        ('ចាំបន្តិចសិន', 'ចាំបន្តិចសិន!'),
        ('មិនអីទេ', 'មិនអីទេ'),
        ('ច្បាស់ហើយ', 'ច្បាស់ហើយ!'),
        ('ហ្នឹងហើយ', 'ហ្នឹងហើយ'),
        ('ពិតមែនឬ', 'ពិតមែនហ្នឹង?!'),
        ('បាទលោក', 'បាទលោក'),
        ('ចាសលោក', 'ចាសលោក'),
        ('សូមមេត្តារង់ចាំ', 'សូមមេត្តារង់ចាំបន្តិច!'),
        ('ប្រយ័ត្នខ្លួន', 'ប្រយ័ត្នខ្លួន!'),

        # Short Drama / Hongguo / Cinema Action & Confrontation (Fixing Robotic Translations)
        ('បញ្ឈប់ដៃ', 'ឈប់ភ្លាម!'),
        ('ឈប់ដៃ', 'ឈប់ភ្លាម!'),
        ('តើឯងកំពុងស្វែងរកសេចក្តីស្លាប់មែនទេ', 'ឯងចង់ងាប់មែនទេ?!'),
        ('តើអ្នកកំពុងស្វែងរកសេចក្តីស្លាប់មែនទេ', 'ឯងចង់ងាប់មែនទេ?!'),
        ('ស្វែងរកសេចក្តីស្លាប់', 'ឯងចង់ងាប់មែនទេ?!'),
        ('ស្វែងរកការស្លាប់', 'ចង់ងាប់?!'),
        ('ឯងចង់ស្លាប់', 'ឯងចង់ងាប់មែនទេ?!'),
        ('ចង់ស្លាប់', 'ចង់ងាប់?!'),
        ('ទៅស្លាប់ទៅ', 'ទៅងាប់ទៅ!'),
        ('ស្លាប់ទៅ', 'ងាប់ទៅ!'),
        ('កុំបន្ទោសខ្ញុំដែលគ្មានមេត្តា', 'កុំបន្ទោសថាយើងដៃឆៅឲ្យសោះ!'),
        ('កុំបន្ទោសខ្ញុំដែលគ្មានមេត្តាធម៌', 'កុំបន្ទោសថាយើងដៃឆៅឲ្យសោះ!'),
        ('កុំបន្ទោសខ្ញុំ', 'កុំបន្ទោសយើងឲ្យសោះ!'),
        ('តើអ្នកហ៊ាន', 'ឯងហ៊ានផង?!'),
        ('អ្នកហ៊ាន', 'ឯងហ៊ានផង?!'),
        ('ឯងហ៊ាន', 'ឯងហ៊ានផង?!'),
        ('ក្រអឺតក្រទម', 'ឈ្លើយណាស់!'),
        ('ថ្លើមធំសម្បើមណាស់', 'ថ្លើមធំសម្បើមណាស់!'),
        ('លោកម្ចាស់វ៉ាង', 'ព្រះអង្គម្ចាស់'),
        ('ស្តេចនេះ', 'យើងជាស្ដេច'),
        ('ទាសករ', 'ខ្ញុំម្ចាស់'),
        ('កាកសំណល់', 'មនុស្សឥតបានការ!'),
        ('មនុស្សឥតបានការ', 'អាមនុស្សឥតបានការ!'),
        ('សត្វតិរច្ឆាន', 'អាតិរច្ឆាន!'),
        ('តើអ្នកចង់ទៅណា', 'ឯងចង់ទៅណា?!'),
        ('តើអ្នកជានរណា', 'ឯងជាអ្នកណា?!'),
        ('តើអ្នកជាអ្នកណា', 'ឯងជាអ្នកណា?!'),
        ('តើវាអាចទៅរួចយ៉ាងដូចម្តេច', 'ម៉េចក៏អាចអញ្ចឹងទៅកើត?!'),
        ('ខ្ញុំនឹងសម្លាប់អ្នក', 'យើងនឹងសម្លាប់ឯង!'),
        ('គ្មានប្រយោជន៍ទេ', 'គ្មានប្រយោជន៍ទេ!'),
        ('កុំគិតថានឹងគេចផុត', 'កុំសង្ឃឹមថានឹងរត់រួចឲ្យសោះ!'),
        ('តើអ្នកគិតថាអ្នកជានរណា', 'ឯងគិតថាឯងជាស្អី?!'),
        ('កុំមកធ្វើជាមនុស្សល្ងង់', 'កុំមកពាលនៅទីនេះ!'),
        ('សូមមេត្តាអភ័យទោស', 'សូមមេត្តាលើកលែងទោសផង!'),
        ('មិនហ៊ានទេ', 'ខ្ញុំម្ចាស់មិនហ៊ានទេ...'),
        ('មនុស្សឆ្កួត', 'អាមនុស្សឆ្កួត!'),
        ('រឿងឆ្កួតៗ', 'រឿងឆ្កួតឡប់ស្អីហ្នឹង?!'),
        ('តើវាពិតជាអស្ចារ្យណាស់មែនទេ', 'ឡូយខ្លាំងណាស់មែនទេ?!'),
        ('កុំប៉ះពាល់ខ្ញុំ', 'កុំមកប៉ះយើង!'),
        ('លែងខ្ញុំ', 'លែងយើងភ្លាម!'),
        ('ទៅឲ្យផុត', 'ទៅឲ្យផុតទៅ!'),
        ('ដកថយ', 'ឆាប់ដកថយ!'),
        ('ប្រយ័ត្ន', 'ប្រយ័ត្នខ្លួន!'),
        ('ខ្ញុំមិនអោយអ្នកទៅទេ', 'យើងមិនឲ្យឯងទៅណាដាច់ខាត!'),
        ('កុំទៅ', 'កុំទាន់អាលទៅ!'),
        ('ខ្ញុំនឹងមិនអត់ទោសអោយអ្នកទេ', 'យើងគ្មានថ្ងៃលើកលែងឲ្យឯងជាដាច់ខាត!'),
    ]

    # Sort replacements by length descending to prevent sub-string collision
    for old_w, new_w in sorted(REPLACEMENTS, key=lambda x: len(x[0]), reverse=True):
        t = t.replace(old_w, new_w)

    # 3. Dynamic Pronoun & Tone Adaptation for Drama Dubbing
    t = re.sub(r'(^|\s)តើអ្នក(?=\s)', r'\1ឯង', t)
    t = re.sub(r'(^|\s)អ្នក(?=ជា|បាន|នឹង|កំពុង|ត្រូវ|ចង់)', r'\1ឯង', t)
    t = re.sub(r'(^|\s)ខ្ញុំគឺជា(?=\s)', r'\1ខ្ញុំជា', t)
    t = re.sub(r'(^|\s)អ្នកគឺជា(?=\s)', r'\1ឯងជា', t)

    # 4. Spoken Emotional Cadence & Intonation
    if not t.endswith(('?', '!', '...', '។')):
        if any(q in t for q in ['ស្អី', 'ម៉េច', 'ណា', 'ឯណា', 'អត់', 'ហី', 'អ្ហេស', 'មែនទេ']):
            t = t.rstrip('។. ') + '?'
        elif any(exc in t for exc in ['ឆាប់ឡើង', 'ឈប់', 'ជួយផង', 'ចេញទៅ', 'មិនអាចទេ', 'ឡូយ', 'កុំ', 'សម្លាប់']):
            t = t.rstrip('។. ') + '!'

    # 5. Clean up duplicate or trailing mismatched punctuation
    t = re.sub(r'\?+', '?', t)
    t = re.sub(r'!+', '!', t)
    t = re.sub(r'!\?', '?!', t)
    t = re.sub(r'\?!+', '?!', t)
    t = t.replace('ចេញទៅទៅ', 'ចេញទៅ')

    return t.strip()


def _g4f_cinematic_translate_batch(segments_text: list, src_lang: str = "zh") -> dict:
    """
    Cinematic Local/Free AI batch translation pipeline using g4f LLM7 (free, no API key).
    Strategy: src_lang → English (cinematic movie dialogue via LLM7) → Khmer (GTX).
    Returns dict: {index: translated_khmer_text}

    This provides Gemini-equivalent cinematic quality by:
    1. Using LLM7 (free LLM API) to translate with cinematic/movie-dubbing instructions
    2. Converting the natural English result to Khmer via GTX (highly accurate for en→km)
    3. Applying khmer_cinema_dialogue_adapter for final Khmer spoken polish
    """
    if not segments_text:
        return {}

    result = {}
    LANG_NAMES = {'zh': 'Chinese (中文)', 'en': 'English', 'vi': 'Vietnamese', 'th': 'Thai',
                  'ko': 'Korean', 'ja': 'Japanese', 'fr': 'French', 'es': 'Spanish'}
    src_name = LANG_NAMES.get(src_lang[:2], 'Chinese')

    # --- Step 1: LLM7 (g4f free) → cinematic English translation ---
    english_lines = {}
    try:
        import g4f
        from g4f.client import Client as G4FClient
        prov = g4f.Provider.LLM7
        client = G4FClient(provider=prov)

        # Build numbered batch for LLM7
        numbered = "\n".join(f"{i+1}. {txt.strip()}" for i, txt in enumerate(segments_text))
        cinema_prompt = (
            f"You are a professional movie dubbing translator specializing in TVB Hong Kong dramas and Chinese cinema.\n"
            f"Translate each numbered {src_name} dialogue line into natural, emotionally expressive ENGLISH movie dialogue.\n\n"
            f"CINEMATIC DUBBING RULES:\n"
            f"1. Use authentic spoken movie English — conversational, emotional, NOT stiff textbook grammar.\n"
            f"2. Convey genuine feelings: anger, urgency, sadness, surprise, affection — match the drama's tone.\n"
            f"3. Keep lines concise and punchy for voice dubbing timing.\n"
            f"4. Use natural spoken exclamations: 'Stop it!', 'Get out!', 'You really love me?', 'No way...!'\n"
            f"5. Output ONLY the numbered lines. No explanations. No markdown.\n\n"
            f"{numbered}"
        )

        resp = client.chat.completions.create(
            model='',
            messages=[{'role': 'user', 'content': cinema_prompt}],
            timeout=45
        )
        raw_en = resp.choices[0].message.content if resp and resp.choices else ""

        for line in raw_en.strip().splitlines():
            m = re.match(r'^(\d+)[\.\:\-\)]\s*(.+)', line.strip())
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(segments_text):
                    english_lines[idx] = m.group(2).strip()

        logger.info(f"[LocalAI Cinematic] LLM7 translated {len(english_lines)}/{len(segments_text)} lines to English.")
    except Exception as e_llm7:
        logger.warning(f"[LocalAI Cinematic] LLM7 batch failed: {e_llm7}. Falling back to GTX direct zh→km.")

    # --- Step 2: GTX English → Khmer (or direct src→km if LLM7 failed) ---
    import urllib.parse
    session = _get_translate_session()

    for i, src_txt in enumerate(segments_text):
        try:
            en_txt = english_lines.get(i, "")
            if en_txt:
                # GTX: English → Khmer (en→km is very accurate in Google Translate)
                url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=km&dt=t&q={urllib.parse.quote(en_txt)}"
            else:
                # Fallback: Direct src→km via GTX
                s_code = src_lang[:2] if src_lang != "auto" else "zh"
                url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl={s_code}&tl=km&dt=t&q={urllib.parse.quote(src_txt)}"

            res = session.get(url, timeout=6)
            if res.status_code == 200:
                data = res.json()
                km_raw = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
                if km_raw and km_raw != src_txt:
                    # Step 3: Cinematic Khmer polish
                    result[i] = khmer_cinema_dialogue_adapter(km_raw, src_txt)
                    continue
        except Exception as e_gtx:
            logger.debug(f"[LocalAI Cinematic] GTX step failed for segment {i}: {e_gtx}")

        # Last resort: adapter on raw source
        result[i] = khmer_cinema_dialogue_adapter(src_txt, src_txt)

    return result


def translate_nllb_ct2(text, src_lang="zh", tgt_lang="km", force_local=False):
    """
    Translates text using fast AI API / Google Translate GTX endpoint (0.1s response time).
    When force_local=True, skips Gemini API and uses Local/Free AI cinematic pipeline:
      LLM7 (g4f free) → cinematic English → GTX English→Khmer → khmer_cinema_dialogue_adapter.
    All Khmer outputs are passed through khmer_cinema_dialogue_adapter for human movie intonation.
    """
    if not text or not text.strip():
        return text

    LANG_NAMES = {'km': 'Khmer (ភាសាខ្មែរ)', 'en': 'English', 'zh': 'Chinese (中文)', 'vi': 'Vietnamese', 'th': 'Thai'}
    tgt_name = LANG_NAMES.get(tgt_lang[:2], 'Khmer')
    src_name = LANG_NAMES.get(src_lang[:2], 'Chinese')
    
    # 1. Primary: Gemini API with multi-key automatic failover rotation (if not forced local)
    if not force_local:
        try:
            from gemini_manager import post_gemini_generate
            prompt = (
                f"You are a master cinematic movie dubbing translator and voice actor director.\n"
                f"Translate the following spoken dialogue from {src_name} into natural, cinematic, emotionally expressive "
                f"conversational {tgt_name} (ភាសានិយាយកុនរស់រវើកបែបមនុស្សពិតបញ្ចូលសំឡេងផ្ទាល់).\n\n"
                f"DUBBING RULES FOR REAL HUMAN VOICE:\n"
                f"1. Conversational Movie Phrasing: Use authentic spoken movie dialogue that actors naturally speak. Do not use stiff, literal, or robotic grammar.\n"
                f"2. Emotional Nuance & Inflection: Express genuine human feelings (anger, joy, sorrow/crying, threat, suspense, teasing) matching the context.\n"
                f"3. Expressive Punctuation: Include natural punctuation marks (?, !, ..., ,, ។) so the neural voice model automatically produces pitch rises on questions, emphasis on exclamations, and dramatic breathing pauses.\n"
                f"4. Natural Pacing: Keep line length and syllable count concise to match the original timing — neither too fast nor too slow.\n"
                f"Output ONLY the translated spoken dialogue with no markdown, quotes, or explanations:\n\n{text}"
            )
            tr_text = post_gemini_generate(
                prompt,
                model="gemini-flash-lite-latest",
                timeout=20,
                generation_config={"maxOutputTokens": 1024, "temperature": 0.35}
            )
            if tr_text and len(tr_text) > 0 and tr_text != text:
                if tgt_lang[:2] == 'km':
                    tr_text = khmer_cinema_dialogue_adapter(tr_text, text)
                return tr_text
        except Exception as e_gem:
            print(f"Gemini translation error: {e_gem}")

    # 2. Local/Free AI Cinematic Pipeline: LLM7 → English → GTX → Khmer (when force_local or Gemini failed)
    #    For Khmer target only: use the 3-step cinematic chain (LLM7→EN→GTX→KM→adapter)
    if tgt_lang[:2] == 'km' and src_lang[:2] != 'km':
        try:
            import g4f
            from g4f.client import Client as G4FClient
            prov = g4f.Provider.LLM7
            client = G4FClient(provider=prov)
            cinema_prompt = (
                f"Translate this {src_name} movie dialogue into natural, emotional, spoken English dubbing. "
                f"Output ONLY the English translation — no explanations:\n\n{text}"
            )
            resp = client.chat.completions.create(
                model='', messages=[{'role': 'user', 'content': cinema_prompt}], timeout=20
            )
            en_txt = (resp.choices[0].message.content or "").strip() if resp and resp.choices else ""
            if en_txt and en_txt != text and len(en_txt) > 2:
                # GTX: English → Khmer
                import urllib.parse
                session = _get_translate_session()
                url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=km&dt=t&q={urllib.parse.quote(en_txt)}"
                res = session.get(url, timeout=5)
                if res.status_code == 200:
                    data = res.json()
                    km_txt = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
                    if km_txt and km_txt != en_txt:
                        return khmer_cinema_dialogue_adapter(km_txt, text)
        except Exception as e_llm7:
            logger.debug(f"LLM7 cinematic translate error: {e_llm7}")

    # 3. Google Translate GTX Free Endpoint (High-speed 0.05s response, unlimited quota)
    try:
        import urllib.parse
        session = _get_translate_session()
        s_code = src_lang[:2] if src_lang != "auto" else "auto"
        t_code = tgt_lang[:2]
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl={s_code}&tl={t_code}&dt=t&q={urllib.parse.quote(text)}"
        res = session.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
            if translated and len(translated.strip()) > 0 and translated.strip() != text.strip():
                res_txt = translated.strip()
                if tgt_lang[:2] == 'km':
                    res_txt = khmer_cinema_dialogue_adapter(res_txt, text)
                return res_txt
    except Exception as e_gtx:
        logger.debug(f"GTX translation error: {e_gtx}")

    # 4. MyMemory API fallback (secondary backup)
    try:
        import urllib.parse
        session = _get_translate_session()
        s_code = src_lang[:2] if src_lang != "auto" else "zh"
        t_code = tgt_lang[:2]
        mm_url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(text)}&langpair={s_code}|{t_code}"
        res_mm = session.get(mm_url, timeout=4)
        if res_mm.status_code == 200:
            mm_data = res_mm.json()
            mm_text = mm_data.get("responseData", {}).get("translatedText", "").strip()
            if mm_text and mm_text.upper() != "NO QUERY SPECIFIED" and not mm_text.startswith("MYMEMORY WARNING") and mm_text != text:
                if tgt_lang[:2] == 'km':
                    mm_text = khmer_cinema_dialogue_adapter(mm_text, text)
                return mm_text
    except Exception as e_mm:
        logger.debug(f"MyMemory translation error: {e_mm}")

    if tgt_lang[:2] == 'km':
        text = khmer_cinema_dialogue_adapter(text, text)

    return text

def _group_whisper_words(words, max_pause=0.45, max_segment_duration=4.2, word_separator=" ", audio_pcm=None, sr=16000):
    """
    Groups Faster-Whisper words into natural cinematic dialogue clauses (មួយឃ្លា = 1 segment).
    Breaks on commas, pauses (>=0.45s), semicolons, exclamation/question marks, and clause boundaries.
    Attaches pitch-based speaker gender ('male' vs 'female') for every clause.
    """
    grouped = []
    current_words = []
    current_start = None
    previous_end = None

    def flush():
        nonlocal current_words, current_start, previous_end
        if current_words and current_start is not None and previous_end is not None:
            seg_start = round(current_start, 2)
            seg_end = round(previous_end, 2)
            seg_text = word_separator.join(current_words).strip()
            if seg_text and seg_end > seg_start:
                # Detect gender from PCM slice if provided
                gender = "female"
                if audio_pcm is not None and len(audio_pcm) > 0:
                    s_idx = max(0, int(seg_start * sr))
                    e_idx = min(len(audio_pcm), int(seg_end * sr))
                    slice_pcm = audio_pcm[s_idx:e_idx]
                    gender = _detect_gender_from_pcm_slice(slice_pcm, sr=sr)

                grouped.append({
                    "start": seg_start,
                    "end": seg_end,
                    "gender": gender,
                    "text": seg_text,
                })
        current_words = []
        current_start = None
        previous_end = None

    for word in words:
        text = str(word.get("word", "")).strip()
        start = float(word.get("start", 0))
        end = float(word.get("end", start))
        if not text or end <= start:
            continue
        if current_words and (
            (previous_end is not None and (start - previous_end) >= max_pause)
            or (current_start is not None and (end - current_start) >= max_segment_duration)
        ):
            flush()
        if current_start is None:
            current_start = start
        current_words.append(text)
        previous_end = end
        # Break clause on punctuation: comma, period, question, exclamation, semicolon, colon (Western & Asian)
        if re.search(r"[,.;:!?，。！？；、]$", text):
            flush()
    flush()
    return grouped


def transcribe_with_gemini_timestamps(input_path, src_lang="auto", progress_callback=None):
    """
    Transcribes audio/video media with exact timestamps using Google Gemini API.
    Converts media to a clean 24kHz mono audio stream, sends to Gemini with timestamp prompt,
    and returns parsed list of segment dicts: [{'start': float, 'end': float, 'text': str}].
    Includes regex-based recovery so long transcripts with unclosed JSON are never lost.
    Returns None if Gemini keys are unavailable or transcription fails.
    """
    if not input_path or not os.path.exists(input_path):
        return None

    try:
        from gemini_manager import get_gemini_keys, post_gemini_audio
        if not get_gemini_keys():
            logger.info("[Gemini Transcribe] No Gemini API keys configured.")
            return None

        temp_dir = get_temp_dir()
        audio_sample = os.path.join(temp_dir, f"{uuid.uuid4()}_gemini_src.mp3")
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        # 2-pass: first decode HE-AACv2/any exotic codec → raw PCM (avoids codec decode failures)
        # then apply filters on clean PCM. This ensures HE-AACv2 audio is fully decoded.
        pcm_path = os.path.join(temp_dir, f"{uuid.uuid4()}_raw_pcm.wav")
        cmd_pcm = [
            ffmpeg_exe, "-y",
            "-i", input_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            pcm_path
        ]
        subprocess.run(cmd_pcm, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)

        # Pick source: use decoded PCM if available (better for HE-AACv2), else original
        audio_src = pcm_path if (os.path.exists(pcm_path) and os.path.getsize(pcm_path) > 1000) else input_path

        cmd = [
            ffmpeg_exe, "-y",
            "-i", audio_src,
            "-vn",
            "-af", "highpass=f=70,lowpass=f=11000,volume=1.8",
            "-acodec", "libmp3lame",
            "-ar", "24000",
            "-ac", "1",
            "-b:a", "192k",
            audio_sample
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        cleanup_file(pcm_path)
        if not os.path.exists(audio_sample) or os.path.getsize(audio_sample) < 100:
            return None

        clean_src = (src_lang or "auto").lower().strip()
        lang_hint = ""
        if clean_src != "auto":
            lang_hint = f" The original speech language is {clean_src}."

        prompt = (
            "You are an expert film dubbing sound director and dialogue timing AI.\n"
            f"Transcribe EVERY SINGLE WORD and DIALOGUE SPOKEN in this audio (Cantonese, Chinese, English, etc.), "
            f"even rapid speech, quiet dialogue, whispering, interruptions, doctor/patient conversations, or speech mixed under loud background music.\n"
            f"Do NOT combine multiple sentences into one big chunk. Transcribe sentence by sentence with accurate start and end timestamps in seconds.{lang_hint}\n\n"
            "MANDATORY TRANSCRIPTION RULES:\n"
            "1. 100% COMPLETE COVERAGE: Capture EVERY SINGLE spoken line from beginning to end without omitting or skipping anything. Never summarize. Include rapid dialogue, doctor/patient exchanges, shouting, whispers, and emotional reactions.\n"
            "2. VERBATIM ORIGINAL SPEECH: Transcribe verbatim in the exact original language spoken (do not translate).\n"
            "3. ACCURATE NATURAL TIMESTAMPS: Break down speech into natural conversational sentences or short phrases (typically 0.8s to 4.5s each) with accurate 'start' and 'end' timestamps in seconds.\n"
            "4. SPEAKER GENDER RECOGNITION: For EACH line, listen to the speaker's voice pitch and character and label 'gender': 'male' or 'female'.\n"
            "5. OUTPUT FORMAT: Return ONLY a valid JSON array of objects. Absolutely no markdown explanations, chatter, or notes.\n"
            "Format example:\n"
            "[\n"
            '  {"start": 0.0, "end": 2.4, "gender": "female", "text": "transcribed speech line 1"},\n'
            '  {"start": 2.6, "end": 5.1, "gender": "male", "text": "transcribed speech line 2"}\n'
            "]"
        )

        def _parse_time_val(val):
            if val is None:
                return None
            if isinstance(val, (int, float)):
                return float(val)
            val_s = str(val).strip()
            val_s = re.sub(r'^[\[\(]|[\)\]]$', '', val_s).strip()
            val_s = re.sub(r'[sS]e?c?$', '', val_s).strip()
            if not val_s:
                return None
            if ':' in val_s:
                parts = val_s.split(':')
                try:
                    if len(parts) == 3:
                        return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
                    elif len(parts) == 2:
                        return float(parts[0]) * 60.0 + float(parts[1])
                except Exception:
                    pass
            try:
                return float(val_s)
            except Exception:
                return None

        # --- Helper: parse one Gemini response with time offset ---
        def _parse_gemini_resp(raw, time_offset=0.0):
            if not raw or not raw.strip():
                return []
            ct = raw.strip()
            if ct.startswith("```"):
                ct = re.sub(r"^```[a-zA-Z]*\n?", "", ct)
                ct = re.sub(r"\n?```$", "", ct).strip()
            parsed = None
            mm = re.search(r'\[\s*\{.*\}\s*\]', ct, re.DOTALL)
            if mm:
                try: parsed = json.loads(mm.group(0))
                except Exception: pass
            if not parsed:
                try: parsed = json.loads(ct)
                except Exception: pass
            if not parsed:
                pat = r'\{\s*"start"\s*:\s*([^,\}\s]+)\s*,\s*"end"\s*:\s*([^,\}\s]+)(?:,\s*"gender"\s*:\s*"([a-zA-Z]+)")?\s*,\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"\s*\}'
                hits = list(re.finditer(pat, ct))
                if hits:
                    parsed = []
                    for h in hits:
                        try:
                            sv = _parse_time_val(h.group(1).strip('"\''))
                            ev = _parse_time_val(h.group(2).strip('"\''))
                            gv = (h.group(3) or "female").lower()
                            raw_t = h.group(4)
                            try:
                                tv = json.loads(f'"{raw_t}"').strip()
                            except Exception:
                                tv = raw_t.strip()
                            if tv and sv is not None and ev is not None and ev > sv:
                                parsed.append({"start": sv, "end": ev, "gender": gv, "text": tv})
                        except Exception:
                            pass
            if not isinstance(parsed, list):
                return []
            out = []
            for item in parsed:
                if not isinstance(item, dict): continue
                text = str(item.get("text", "")).strip()
                if not text: continue
                s_raw = _parse_time_val(item.get("start"))
                e_raw = _parse_time_val(item.get("end"))
                if s_raw is None:
                    continue

                # Auto-detect whether Gemini returned absolute video time or relative chunk time
                if time_offset > 0.0 and s_raw >= (time_offset - 1.0):
                    s = s_raw
                    if e_raw is not None and e_raw > s:
                        e = e_raw
                    elif e_raw is not None and e_raw > 0:
                        e = s + e_raw
                    else:
                        e = s + max(1.0, len(text.split()) * 0.4)
                else:
                    s = time_offset + max(0.0, s_raw)
                    if e_raw is not None and e_raw > s_raw:
                        e = time_offset + e_raw
                    elif e_raw is not None and e_raw > 0:
                        e = s + e_raw
                    else:
                        e = s + max(1.0, len(text.split()) * 0.4)

                s = round(s, 2)
                e = round(max(s + 0.2, e), 2)

                g_raw = str(item.get("gender", "")).lower().strip()
                gender = "male" if ("man" in g_raw or "boy" in g_raw or "masc" in g_raw or "male" in g_raw) and "female" not in g_raw else "female"
                out.append({"start": s, "end": e, "gender": gender, "text": text})
            return out

        # --- Chunked transcription: split into ≤35s windows without overlap to prevent duplicate/hallucinated text ---
        audio_dur_secs = get_video_duration(audio_sample)
        CHUNK_MAX = 35.0     # max seconds per Gemini call

        all_segments = []
        if audio_dur_secs <= CHUNK_MAX:
            if progress_callback:
                progress_callback(f"🎙️ [Transcribing] Gemini analyzing: (0s - {audio_dur_secs:.0f}s)...")
            # Short: send whole file
            raw_resp = post_gemini_audio(
                audio_sample, prompt, model="gemini-3.5-flash",
                timeout=120, generation_config={"maxOutputTokens": 8192, "temperature": 0.1}
            )
            cleanup_file(audio_sample)
            all_segments = _parse_gemini_resp(raw_resp, time_offset=0.0)
        else:
            # Long: chunk it into non-overlapping 35s windows for strict timeline alignment
            total_chunks = max(1, int(math.ceil(audio_dur_secs / CHUNK_MAX)))
            logger.info(f"[Gemini Transcribe] Audio {audio_dur_secs:.1f}s > {CHUNK_MAX}s — using 35s chunked transcription ({total_chunks} chunks).")
            chunk_start = 0.0
            chunk_idx = 0
            while chunk_start < audio_dur_secs:
                c_end = min(chunk_start + CHUNK_MAX, audio_dur_secs)
                if progress_callback:
                    progress_callback(f"🎙️ [Transcribing] Gemini analyzing: Chunk {chunk_idx + 1}/{total_chunks} ({chunk_start:.0f}s - {c_end:.0f}s)...")
                chunk_path = os.path.join(temp_dir, f"{uuid.uuid4()}_chunk{chunk_idx}.mp3")
                chunk_len = c_end - chunk_start
                # Frame-accurate output seeking (-i audio_sample -ss chunk_start -t chunk_len)
                subprocess.run(
                    [ffmpeg_exe, "-y", "-i", audio_sample, "-ss", str(chunk_start), "-t", str(chunk_len),
                     "-acodec", "libmp3lame", "-ar", "24000", "-ac", "1", "-b:a", "192k", chunk_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60
                )
                if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 100:
                    chunk_prompt = (
                        "You are an expert film dubbing sound director and dialogue timing AI.\n"
                        f"Transcribe EVERY SINGLE WORD and DIALOGUE SPOKEN in this audio clip (Cantonese, Chinese, English, etc.), "
                        f"even rapid speech, quiet dialogue, whispering, interruptions, doctor/patient conversations, or speech mixed under loud background music.\n"
                        f"Do NOT combine multiple sentences into one big chunk. Transcribe sentence by sentence with accurate start and end timestamps in seconds (between 0.0s and {chunk_len:.1f}s).{lang_hint}\n\n"
                        "MANDATORY TRANSCRIPTION RULES:\n"
                        "1. 100% COMPLETE COVERAGE: Capture EVERY SINGLE spoken line from beginning to end without omitting or skipping anything. Never summarize. Include rapid dialogue, doctor/patient exchanges, shouting, whispers, and emotional reactions.\n"
                        "2. VERBATIM ORIGINAL SPEECH: Transcribe verbatim in the exact original language spoken (do not translate).\n"
                        "3. ACCURATE NATURAL TIMESTAMPS: Break down speech into natural conversational sentences or short phrases (typically 0.8s to 4.5s each) with accurate 'start' and 'end' timestamps in seconds.\n"
                        "4. SPEAKER GENDER RECOGNITION: For EACH line, listen carefully to the speaker's voice pitch and character: if a man or boy is speaking, set 'gender': 'male'. If a woman or girl is speaking, set 'gender': 'female'.\n"
                        "5. OUTPUT FORMAT: Return ONLY a valid JSON array of objects. Absolutely no markdown explanations, chatter, or notes.\n"
                        "Format example:\n"
                        "[\n"
                        '  {"start": 0.5, "end": 2.4, "gender": "female", "text": "transcribed speech line 1"},\n'
                        '  {"start": 2.6, "end": 5.1, "gender": "male", "text": "transcribed speech line 2"}\n'
                        "]"
                    )
                    raw_chunk = None
                    for attempt in range(2):
                        raw_chunk = post_gemini_audio(
                            chunk_path, chunk_prompt, model="gemini-3.5-flash",
                            timeout=120, generation_config={"maxOutputTokens": 8192, "temperature": 0.1}
                        )
                        if raw_chunk and raw_chunk.strip():
                            break

                    chunk_segs = _parse_gemini_resp(raw_chunk, time_offset=chunk_start)
                    # Faster-Whisper chunk fallback if Gemini produced no segments for this clip
                    if not chunk_segs:
                        try:
                            clean_src = (src_lang or "auto").lower().strip()
                            clean_code = None if clean_src == "auto" else clean_src[:2]
                            w_chunk = transcribe_with_whisper_timestamps(chunk_path, src_lang=clean_code, whisper_model="small")
                            if w_chunk:
                                for ws in w_chunk:
                                    ws["start"] = round(chunk_start + ws["start"], 2)
                                    ws["end"] = round(chunk_start + ws["end"], 2)
                                    ws["gender"] = "male" if detect_voice_presentation_from_audio(chunk_path) == "male" else "female"
                                    chunk_segs.append(ws)
                        except Exception as e_w_chk:
                            logger.debug(f"Whisper chunk fallback error: {e_w_chk}")

                    cleanup_file(chunk_path)
                    for seg in chunk_segs:
                        # Ensure segment fits inside this chunk window (+/- 2.0s tolerance for natural sentence tail)
                        if seg["start"] >= (chunk_start - 2.0) and seg["start"] < (c_end + 2.5):
                            all_segments.append(seg)
                else:
                    cleanup_file(chunk_path)
                chunk_start += CHUNK_MAX
                chunk_idx += 1
            cleanup_file(audio_sample)

        if progress_callback and all_segments:
            progress_callback(f"✅ Gemini transcribed {len(all_segments)} dialogue segments successfully")

        data = all_segments

        if not isinstance(data, list) or not data:
            return None

        segments = []
        for item in data:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            try:
                start = round(float(item.get("start", 0.0)), 2)
                end = round(float(item.get("end", start + 1.0)), 2)
                g_raw = str(item.get("gender", "")).lower().strip()
                gender = "male" if "man" in g_raw or "boy" in g_raw or "masc" in g_raw or g_raw == "male" else "female"
                if end > start:
                    segments.append({"start": start, "end": end, "gender": gender, "text": text})
            except (ValueError, TypeError):
                continue

        segments.sort(key=lambda x: (x["start"], x["end"]))
        if segments:
            logger.info(f"[Gemini Transcribe] Successfully transcribed {len(segments)} segments with gender detection and full coverage.")
            return segments
    except Exception as e:
        logger.warning(f"[Gemini Transcribe] Error: {e}")

    return None


def transcribe_with_whisper_timestamps(input_path, src_lang="auto", max_pause=0.45, whisper_model=None):
    """
    Transcribes audio with Faster-Whisper with word_timestamps=True and high-performance in-memory PCM decoding.
    Bypasses PyAV file container decoding issues by pre-converting audio to 16kHz float32 PCM array via FFmpeg.
    Groups words into cinematic dialogue clauses (មួយឃ្លា = 1 segment) with accurate start/end timestamps
    and pitch-based speaker gender ('male' vs 'female') for every clause.
    """
    if not input_path or not os.path.exists(input_path):
        return None

    clean_src = None
    if src_lang and src_lang != "auto":
        l = src_lang.lower().strip()
        clean_src = {'khmer': 'km', 'english': 'en', 'chinese': 'zh', 'vietnamese': 'vi', 'thai': 'th', 'korean': 'ko', 'japanese': 'ja', 'french': 'fr', 'spanish': 'es', 'german': 'de', 'russian': 'ru', 'arabic': 'ar', 'hindi': 'hi'}.get(l, l[:2])

    try:
        model = _get_whisper_model(whisper_model)
        if not model:
            logger.warning("[Whisper Transcribe] Failed to obtain WhisperModel.")
            return None

        # 1. Pre-decode media to 16kHz float32 mono array using FFmpeg (avoids PyAV metadata_errors bug)
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, '-y', '-i', input_path, '-vn', '-f', 'f32le', '-ac', '1', '-ar', '16000', 'pipe:1']
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        raw_pcm, _ = p.communicate()
        if not raw_pcm or len(raw_pcm) < 16000 * 4 * 0.2:
            logger.warning("[Whisper Transcribe] Could not decode audio PCM from input media.")
            return None
        audio_np = np.frombuffer(raw_pcm, dtype=np.float32)
        total_media_dur = len(audio_np) / 16000.0

        # 2. Transcribe attempts: VAD first for clean voice boundaries, fallback to non-VAD if coverage is low
        attempts = [
            {
                "vad_filter": True,
                "vad_parameters": {
                    "threshold": 0.20,
                    "min_speech_duration_ms": 60,
                    "min_silence_duration_ms": 400,
                    "speech_pad_ms": 300,
                },
            },
            {"vad_filter": False},
        ]
        best_segments = []
        for attempt_index, vad_options in enumerate(attempts):
            segments_list = []
            try:
                segments, info = model.transcribe(
                    audio_np,
                    language=clean_src,
                    word_timestamps=True,
                    beam_size=5,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.50,
                    hallucination_silence_threshold=2.0,
                    **vad_options,
                )
                for segment in segments:
                    words = getattr(segment, "words", None)
                    if words:
                        segment_words = [
                            {"start": word.start, "end": word.end, "word": word.word}
                            for word in words
                        ]
                        detected_language = clean_src or getattr(info, "language", None)
                        separator = "" if detected_language in {"zh", "ja"} else " "
                        clause_segs = _group_whisper_words(
                            segment_words,
                            max_pause=max_pause,
                            max_segment_duration=4.2,
                            word_separator=separator,
                            audio_pcm=audio_np,
                            sr=16000
                        )
                        segments_list.extend(clause_segs)
                    else:
                        text = (getattr(segment, "text", "") or "").strip()
                        if text and segment.end > segment.start:
                            seg_s = round(segment.start, 2)
                            seg_e = round(segment.end, 2)
                            s_idx = max(0, int(seg_s * 16000))
                            e_idx = min(len(audio_np), int(seg_e * 16000))
                            gender = _detect_gender_from_pcm_slice(audio_np[s_idx:e_idx], sr=16000)
                            segments_list.append({
                                "start": seg_s,
                                "end": seg_e,
                                "gender": gender,
                                "text": text,
                            })

                segments_list.sort(key=lambda item: (item["start"], item["end"]))
                if segments_list:
                    tot_speech = sum(s["end"] - s["start"] for s in segments_list)
                    coverage = (tot_speech / total_media_dur) if total_media_dur > 0 else 1.0
                    cur_best_tot = sum(s["end"] - s["start"] for s in best_segments)
                    if len(segments_list) > len(best_segments) or tot_speech > cur_best_tot:
                        best_segments = segments_list
                    # If VAD attempt achieved comprehensive coverage, return immediately
                    if attempt_index == 0 and coverage >= 0.50 and len(segments_list) >= 4:
                        logger.info(f"[Whisper Transcribe] High performance VAD captured {len(segments_list)} clauses ({tot_speech:.1f}s speech).")
                        return segments_list
            except Exception as exc:
                logger.warning(
                    "Faster-Whisper transcription attempt %s failed: %s",
                    "with VAD" if attempt_index == 0 else "without VAD",
                    exc,
                )

        if best_segments:
            logger.info(f"[Whisper Transcribe] Captured best {len(best_segments)} clauses with gender.")
            return best_segments
    except Exception as e:
        logger.error(f"Faster-Whisper error: {e}")

    return None

def prepare_telegram_thumbnail(thumb_path: str) -> str | None:
    """
    Ensures the thumbnail image is a properly formatted JPEG (max 320x320) for Telegram video previews.
    Returns the path to the formatted JPEG thumbnail file.
    """
    if not thumb_path or not os.path.exists(thumb_path):
        return None
    try:
        from PIL import Image
        temp_dir = get_temp_dir()
        out_jpg = os.path.join(temp_dir, f"{uuid.uuid4()}_tg_thumb.jpg")
        with Image.open(thumb_path) as img:
            img = img.convert("RGB")
            img.thumbnail((320, 320), Image.Resampling.LANCZOS)
            img.save(out_jpg, "JPEG", quality=92)
        if os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 0:
            return out_jpg
    except Exception as e:
        print(f"prepare_telegram_thumbnail error: {e}")
    return thumb_path

def apply_thumbnail_to_video(video_path: str, thumb_path: str) -> str:
    """
    Returns video_path cleanly without adding corrupted secondary video streams into MP4 container,
    ensuring Telegram natively displays custom cover art passed via Pyrogram's thumb parameter.
    """
    return video_path

def translate_and_dub_media(input_path, target_lang='km', src_lang='auto', is_video=True, progress_callback=None, thumbnail_path=None, keep_bgm=True, tts_backend=None, whisper_model=None, transcribe_engine=None, translate_engine=None, voxcpm_model=None):
    """
    Full AI Video Localization & Dubbing Pipeline (v3 - Mouth-Tracking Edition):
    1. Transcribes with Gemini AI API or Faster-Whisper to extract exact per-segment timestamps.
    2. Translates each segment using Gemini API or Local/Free AI (MyMemory/GTX).
    3. Synthesizes Neural TTS (edge-tts / VoxCPM2) per segment.
    4. Speed-compresses each TTS clip to FIT inside its original time slot (tts_dur / seg_dur as atempo).
    5. Delays each clip to its original start time so speech lines up with mouth movements.
    6. Mixes all delayed clips into a single full-length audio timeline.
    7. Skips BGM mixing if DSP vocal removal produces near-silence (avoids polluting audio).
    8. Applies loudnorm to match original audio level.
    9. Merges dubbed audio track with original video stream, locked to original duration.
    """
    temp_dir = get_temp_dir()
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    orig_dur = get_video_duration(input_path)

    # Determine transcription engine: "gemini" vs "local" (faster-whisper)
    # Determine transcription engine: default to Gemini AI API whenever keys exist for 100% dialogue & gender capture
    from gemini_manager import get_gemini_keys
    has_gemini = bool(get_gemini_keys())
    use_gemini_transcribe = False
    if transcribe_engine:
        te_lower = str(transcribe_engine).strip().lower()
        if te_lower in ("whisper", "local", "faster-whisper"):
            use_gemini_transcribe = False
        elif te_lower in ("gemini", "auto", "default") and has_gemini:
            use_gemini_transcribe = True
    elif whisper_model and str(whisper_model).strip().lower() not in ("gemini", "auto", "default"):
        # User explicitly configured a specific Whisper model (e.g. tiny, base, small, medium, large-v3)
        use_gemini_transcribe = False
    elif has_gemini:
        use_gemini_transcribe = True
    elif whisper_model and str(whisper_model).strip().lower() == "gemini":
        use_gemini_transcribe = True

    whisper_segs = None
    if use_gemini_transcribe:
        if progress_callback: progress_callback("⚡ Transcribing speech with Gemini AI API...")
        whisper_segs = transcribe_with_gemini_timestamps(input_path, src_lang=src_lang, progress_callback=progress_callback)
        if not whisper_segs:
            # Retry once in case of temporary network timeout or rate-limit
            time.sleep(1.5)
            whisper_segs = transcribe_with_gemini_timestamps(input_path, src_lang=src_lang, progress_callback=progress_callback)
        if not whisper_segs:
            logger.info("[Dubbing] Gemini transcription produced no segments or failed. Falling back to Faster-Whisper...")

    if not whisper_segs:
        local_model = "small" if (whisper_model == "gemini" or not whisper_model) else whisper_model
        if progress_callback: progress_callback(f"⚡ Transcribing speech with Faster-Whisper ({local_model})...")
        whisper_segs = transcribe_with_whisper_timestamps(input_path, src_lang=src_lang, whisper_model=local_model)

    # Fallback: use silencedetect-based segments if Whisper fails
    if not whisper_segs:
        if progress_callback: progress_callback("⚡ Falling back to silence-detect for timestamps...")
        raw_segments = detect_speech_segments(input_path)
        if raw_segments:
            from plugins.document_parser import transcribe_audio_video
            whisper_segs = []
            for (ss, se) in raw_segments:
                dur = se - ss
                if dur < 0.2: continue
                seg_slice = os.path.join(temp_dir, f"{uuid.uuid4()}_slice.mp3")
                try:
                    subprocess.run([ffmpeg_exe, "-y", "-ss", str(ss), "-t", str(dur), "-i", input_path, "-acodec", "libmp3lame", seg_slice], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                    txt = transcribe_audio_video(seg_slice, src_lang=src_lang)
                    cleanup_file(seg_slice)
                    if txt and len(txt.strip()) > 1 and "Error" not in txt:
                        whisper_segs.append({"start": ss, "end": se, "text": txt.strip()})
                except Exception:
                    cleanup_file(seg_slice)

    if not whisper_segs:
        # Last resort: full transcript, single segment spanning entire video
        from plugins.document_parser import transcribe_audio_video
        full_transcript = transcribe_audio_video(input_path, src_lang=src_lang)
        if not full_transcript or "Error" in full_transcript or len(full_transcript.strip()) < 2:
            return "ERROR: Could not extract speech from media to translate."
        whisper_segs = [{"start": 0.0, "end": orig_dur if orig_dur > 0 else 60.0, "text": full_transcript.strip()}]

    LANG_NAMES_MAP = {
        'km': 'Khmer (ភាសាខ្មែរ)', 'en': 'English', 'zh': 'Chinese (中文)',
        'vi': 'Vietnamese (Tiếng Việt)', 'th': 'Thai (ภาษาไทย)', 'ko': 'Korean (한국어)',
        'ja': 'Japanese (日本語)', 'fr': 'French (Français)', 'es': 'Spanish (Español)',
        'de': 'German (Deutsch)', 'ru': 'Russian (Русский)', 'ar': 'Arabic (العربية)',
        'hi': 'Hindi (हिन्दी)'
    }
    target_lang_name = LANG_NAMES_MAP.get(target_lang[:2], 'Khmer')
    if progress_callback: progress_callback(f"🧠 Translating & dubbing {len(whisper_segs)} segments to {target_lang_name}...")

    # Step 2: Batch translate ALL segments via Gemini API or Local AI (MyMemory/GTX)
    clean_src = src_lang.lower().strip() if src_lang and src_lang.lower().strip() != "auto" else "auto"
    valid_segs = [(idx, seg) for idx, seg in enumerate(whisper_segs)
                  if (seg["end"] - seg["start"]) >= 0.15 and seg.get("text", "").strip()]

    # Check translate engine: "gemini" vs "local"
    use_local_translate = (str(translate_engine or "").strip().lower() in ("local", "mymemory", "gtx"))

    # ── Batch translation via Gemini with multi-key failover rotation ─────────
    translations = {}   # idx -> translated text
    if valid_segs and not use_local_translate:
        try:
            from gemini_manager import post_gemini_generate
            LANG_NAMES_LOCAL = {'km': 'Khmer (ភាសាខ្មែរ)', 'en': 'English', 'zh': 'Chinese (中文)',
                                 'vi': 'Vietnamese', 'th': 'Thai', 'ko': 'Korean', 'ja': 'Japanese',
                                 'fr': 'French', 'es': 'Spanish', 'de': 'German', 'ru': 'Russian',
                                 'ar': 'Arabic', 'hi': 'Hindi'}
            tgt_name_b = LANG_NAMES_LOCAL.get(target_lang[:2], 'Khmer')
            src_name_b = LANG_NAMES_LOCAL.get(clean_src[:2], 'the original spoken language')

            kh_to_en = {'០':'0', '១':'1', '២':'2', '៣':'3', '៤':'4', '៥':'5', '៦':'6', '៧':'7', '៨':'8', '៩':'9'}

            # Process in optimal batches of 45 segments to minimize API calls while preserving context
            CHUNK_SIZE = 45
            for chunk_start in range(0, len(valid_segs), CHUNK_SIZE):
                chunk = valid_segs[chunk_start:chunk_start + CHUNK_SIZE]
                numbered = "\n".join(f"{i+1}. [Duration: {round(seg['end'] - seg['start'], 1)}s] {seg['text'].strip()}" for i, (_, seg) in enumerate(chunk))
                scene_flow = " -> ".join(seg['text'].strip() for _, seg in chunk[:8])

                batch_prompt = (
                    f"You are a master Cambodian cinema & TVB Hong Kong drama dubbing translator and script director (អ្នកដឹកនាំនិងបញ្ចូលសំឡេងរឿងភាគហុងកុង TVB និយាយខ្មែរ).\n"
                    f"Translate each numbered dialogue line from {src_name_b} into authentic, natural, emotional spoken {tgt_name_b} as heard in TVB Hong Kong dramas (រឿងភាគហុងកុង និយាយខ្មែរ) and professional Cambodian cinema.\n\n"
                    f"SCENE DIALOGUE FLOW: {scene_flow}\n\n"
                    f"MANDATORY CINEMA DUBBING & STORYLINE RULES:\n"
                    f"1. AUDIENCE COMPREHENSION FIRST (អ្នកស្ដាប់ត្រូវតែយល់សាច់រឿងច្បាស់): Translate the TRUE meaning and dramatic intent so the audience understands the plot, character motives, and relationships effortlessly. NEVER translate literally word-by-word into stiff textbook grammar.\n"
                    f"2. NATURAL SPOKEN DRAMA KHMER (ភាសានិយាយភាពយន្តពិតៗបែប TVB): Use real spoken words that Cambodian cinema voice actors speak in movies (e.g. ឯង, ខ្ញុំ, លោកគ្រូពេទ្យ, លោកវេជ្ជបណ្ឌិត, មិនបាច់, ហ្នឹងហើយ, មែនទេ, ច្បាស់ជា, ឆាប់ឡើង, ប្រយ័ត្នខ្លួន).\n"
                    f"3. CONTEXT-AWARE DRAMA PRONOUNS (សព្វនាមតួអង្គក្នុងរឿង):\n"
                    f"   - Doctor / Patient / Hospital: លោកវេជ្ជបណ្ឌិត / លោកគ្រូពេទ្យ / អ្នកជំងឺ / ប្អូន / ខ្ញុំ / ឯង / លោកប្រធាន\n"
                    f"   - Police / Crime / Investigation: លោកប៉ូលីស / លោកមេបញ្ជាការ / មេ / ពួកយើង\n"
                    f"   - Modern / Friends / Colleagues: ឯង / យើង / បង / អូន / ពួកឯង\n"
                    f"   - Master / Disciple / Martial Arts: លោកគ្រូ / សិស្សគណ / លោកម្ចាស់ / អ្នកប្រុស / ព្រះអង្គ / ខ្ញុំម្ចាស់\n"
                    f"   - Family / Elders: លោកប៉ា / អ្នកម៉ាក់ / ពូ / មីង / ក្មួយ\n"
                    f"4. ACCURATE MEDICAL & CINEMA TERMINOLOGY:\n"
                    f"   - 複診 / 复诊 -> 'មកពិនិត្យឡើងវិញ' / 'មកពិនិត្យម្តងទៀត'\n"
                    f"   - 自殺 / 自杀 -> 'ធ្វើអត្តឃាត' (e.g. 'ខ្ញុំអត់បានធ្វើអត្តឃាតទេ!')\n"
                    f"   - 血管 / 神經 -> 'សរសៃឈាម' / 'សរសៃប្រសាទ'\n"
                    f"   - 傷口 / 割傷 -> 'មុខរបួស' / 'មុតដៃ'\n"
                    f"   - 找死 / 找死吧 -> 'ឯងចង់ងាប់មែនទេ?!' (Never translate as 'ស្វែងរកការស្លាប់')\n"
                    f"   - 住手 -> 'ឈប់ភ្លាម!' (Never translate as 'បញ្ឈប់ដៃ')\n"
                    f"   - 放肆 -> 'ឈ្លើយណាស់!' / 'ថ្លើមធំសម្បើមណាស់!'\n"
                    f"   - 别过来 -> 'កុំមកជិតយើង!'\n"
                    f"   - 救命 -> 'ជួយផង!'\n"
                    f"5. EMOTION & INTONATION INFLECTION:\n"
                    f"   - Urgent / Medical Emergency: Sharp, intense exclamation (!, ?!)\n"
                    f"   - Denial / Explanation: Emotional conversational rhythm (មិនមែនចឹងទេ!, មិញហ្នឹង...)\n"
                    f"   - Sympathy / Advice: Natural calm delivery with friendly particles (ណា, ហ្នឹងហើយ)\n"
                    f"6. LIP-SYNC & STRICT TIME DURATION: Next to each line is its target duration [Duration: X.Xs]. Your translated line MUST fit naturally in that exact time window! Keep sentences punchy, rhythmic, and time-synced so the speech tempo matches the actor on screen without dragging or cutting off into the next line!\n"
                    f"7. OUTPUT FORMAT: Return ONLY the exact same numbered lines (1. [dialogue], 2. [dialogue]). Absolutely no notes, chat, or explanations.\n\n"
                    f"{numbered}"
                )

                raw_out = post_gemini_generate(
                    batch_prompt,
                    model="gemini-3.5-flash",
                    timeout=45,
                    generation_config={"maxOutputTokens": 4096, "temperature": 0.25}
                )

                if raw_out:
                    for line in raw_out.strip().splitlines():
                        clean_l = line.strip()
                        for kh, en in kh_to_en.items():
                            clean_l = clean_l.replace(kh, en)
                        m = re.match(r'^(\d+)[\.\:\-\)]\s*(.+)', clean_l)
                        if m:
                            line_no = int(m.group(1)) - 1
                            if 0 <= line_no < len(chunk):
                                orig_idx = chunk[line_no][0]
                                translated_line = m.group(2).strip()
                                translated_line = re.sub(r'^\[(?:Duration:)?\s*[\d\.]+s?\]\s*', '', translated_line).strip()
                                if target_lang[:2] == 'km':
                                    translated_line = khmer_cinema_dialogue_adapter(translated_line, str(chunk[line_no][1].get("text") or ""))
                                translations[orig_idx] = translated_line

                if progress_callback:
                    progress_callback(f"✅ Cinema Dubbing: translated {len(translations)}/{len(valid_segs)} lines...")
        except Exception as e_batch:
            logger.warning(f"Batch Gemini translation failed ({e_batch}), falling back to local engine.")
    elif valid_segs and use_local_translate:
        # ── Local/Free AI Cinematic Batch Pipeline (LLM7 → English → GTX → Khmer → adapter) ──
        if progress_callback: progress_callback("🎬 Translating with Local/Free AI Cinematic Engine (LLM7 + GTX)...")
        try:
            seg_texts = [seg.get("text", "").strip() for _, seg in valid_segs]
            batch_results = _g4f_cinematic_translate_batch(seg_texts, src_lang=clean_src)
            for chunk_start_i, (orig_idx, _) in enumerate(valid_segs):
                if chunk_start_i in batch_results:
                    translations[orig_idx] = batch_results[chunk_start_i]
            if progress_callback:
                progress_callback(f"✅ Local/Free AI Cinematic: translated {len(translations)}/{len(valid_segs)} lines.")
        except Exception as e_local_batch:
            logger.warning(f"[LocalAI Cinematic] Batch pipeline failed ({e_local_batch}), falling back to per-segment GTX.")

    # ── Single retry batch for missing segments to prevent individual per-segment Gemini quota exhaustion
    missing = [(i, s) for (i, s) in valid_segs if i not in translations]
    if missing and not use_local_translate and len(missing) > 1:
        try:
            from gemini_manager import post_gemini_generate
            retry_numbered = "\n".join(f"{i+1}. {s['text'].strip()}" for i, (_, s) in enumerate(missing))
            retry_prompt = (
                f"Translate each numbered line into natural Cambodian cinema movie dubbing Khmer:\n\n{retry_numbered}"
            )
            retry_out = post_gemini_generate(
                retry_prompt,
                model="gemini-flash-lite-latest",
                timeout=25,
                generation_config={"maxOutputTokens": 2048, "temperature": 0.3}
            )
            if retry_out:
                for line in retry_out.strip().splitlines():
                    m = re.match(r'^(\d+)[\.\:\-\)]\s*(.+)', line.strip())
                    if m:
                        idx_num = int(m.group(1)) - 1
                        if 0 <= idx_num < len(missing):
                            orig_i = missing[idx_num][0]
                            tr_text = m.group(2).strip()
                            if target_lang[:2] == 'km':
                                tr_text = khmer_cinema_dialogue_adapter(tr_text, str(missing[idx_num][1].get("text") or ""))
                            translations[orig_i] = tr_text
        except Exception as e_retry:
            logger.warning(f"Retry batch failed: {e_retry}")

    # ── Fast local fallback (GTX / MyMemory) for any remaining lines (never exhausts Gemini API quota)
    remaining_missing = [(i, s) for (i, s) in valid_segs if i not in translations]
    if remaining_missing:
        def _translate_local_fast(idx_seg):
            idx, seg = idx_seg
            if idx in translations:
                return
            txt = translate_nllb_ct2(seg["text"].strip(), src_lang=clean_src, tgt_lang=target_lang, force_local=True)
            translations[idx] = txt

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(remaining_missing))) as ex:
            list(ex.map(_translate_local_fast, remaining_missing))

    # ── Parallel per-segment worker: voice profile + TTS + ffmpeg pad ───────
    delayed_segment_files = []
    lock = __import__('threading').Lock()
    segment_failures = []

    # ── Step 3: Neural TTS, Gender Matching, and Sample-Accurate Audio Timeline ───
    delayed_segment_files = []
    timed_segments = []   # list of (seg_start: float, samples: np.ndarray)
    segment_failures = []
    lock = threading.Lock()
    tts_completed_counter = [0]

    def _process_segment(item):
        idx, seg, next_start = item
        seg_start = seg["start"]
        seg_end   = seg["end"]
        seg_dur   = max(0.2, seg_end - seg_start)

        def mark_failed(reason):
            with lock:
                segment_failures.append(idx)
                tts_completed_counter[0] += 1
                cur_done = tts_completed_counter[0]
            if progress_callback and (cur_done % 2 == 0 or cur_done == len(valid_items)):
                tts_engine_name = "VoxCPM" if _normalize_tts_backend(tts_backend) in ("voxcpm", "voxcpm2") else "Edge-TTS"
                progress_callback(f"🗣️ [Voice Dubbing] {tts_engine_name} voices: {cur_done}/{len(valid_items)} lines ({target_lang.upper()})...")
            logger.warning("Skipping dub segment %s: %s", idx + 1, reason)
            return False

        txt_tr = translations.get(idx, seg.get("text", "").strip())
        txt_tr = re.sub(r'[*#_~`>\[\]\(\)]', ' ', txt_tr or "").strip()
        txt_tr = re.sub(r'^\[(?:Duration:)?\s*[\d\.]+s?\]\s*', '', txt_tr).strip()
        if target_lang[:2] == 'km':
            txt_tr = khmer_cinema_dialogue_adapter(txt_tr, seg.get("text"))
        if not txt_tr:
            return mark_failed("translation was empty")

        # 1. Exact Speaker Gender Recognition
        detected_gender = str(seg.get("gender") or "").strip().lower()
        if detected_gender not in ("male", "female"):
            detected_gender = None

        seg_slice_path = os.path.join(temp_dir, f"{uuid.uuid4()}_voice_slice.wav")
        has_voice_slice = False
        try:
            sample_start = max(0.0, seg_start)
            sample_duration = min(4.0, max(1.0, seg_dur))
            # Output seeking (-i input_path -ss start -t dur) for accurate voice slice
            subprocess.run([
                ffmpeg_exe, "-y", "-i", input_path, "-ss", str(sample_start), "-t", str(sample_duration),
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", seg_slice_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            if os.path.exists(seg_slice_path) and os.path.getsize(seg_slice_path) > 300:
                has_voice_slice = True
                # If gender wasn't determined by Gemini, calculate pitch from voice slice
                if not detected_gender:
                    detected_gender = detect_voice_presentation_from_audio(seg_slice_path)
        except Exception as e_gen:
            logger.warning(f"Voice slice error seg {idx}: {e_gen}")

        if not detected_gender or detected_gender not in ("male", "female"):
            detected_gender = "female"

        # 2. Neural TTS with voice matching
        try:
            ref_slice = seg_slice_path if has_voice_slice else None
            orig_seg_text = seg.get("text", "").strip()
            raw_tts = generate_neural_tts(
                txt_tr, lang=target_lang[:2], gender=detected_gender,
                backend=tts_backend, voxcpm_model=voxcpm_model,
                ref_audio=ref_slice, prompt_text=orig_seg_text
            )
        except Exception as exc:
            return mark_failed(f"TTS error: {exc}")
        finally:
            cleanup_file(seg_slice_path)
        if not raw_tts or not os.path.exists(raw_tts):
            return mark_failed("TTS returned no audio")

        tts_dur = get_video_duration(raw_tts)
        if tts_dur <= 0:
            cleanup_file(raw_tts)
            return mark_failed("generated audio had no duration")

        # 3. Exact Time-Matching & Natural Lip-Sync Fitting
        # Keep speed changes subtle (0.90x to 1.35x) so voice sounds authentic and human, not chipmunk or robot.
        max_allowed = max(0.4, next_start - seg_start - 0.05) if next_start > seg_start else (seg_dur + 0.6)
        target_dur = min(seg_dur, max_allowed)
        ideal_speed = tts_dur / max(0.2, target_dur)
        # Bounded between 0.90x (very slightly slowed) and 1.35x (natural fast pace) to preserve human voice timbre
        speed_factor = max(0.90, min(1.35, ideal_speed))

        tempo_wav = os.path.join(temp_dir, f"{uuid.uuid4()}_tempo_{idx}.wav")
        try:
            # Use ffmpeg-python library (imported as 'ffmpeg' at module level) with explicit cmd path
            import ffmpeg as _ffmpeg_lib
            stream_in = _ffmpeg_lib.input(raw_tts).audio
            if abs(speed_factor - 1.0) >= 0.04:
                for f_name, f_val in build_atempo_filter_chain(speed_factor):
                    stream_in = stream_in.filter(f_name, f_val)
            stream_in = stream_in.filter('aresample', 44100).filter('aformat', channel_layouts='stereo')
            stretched_dur = tts_dur / speed_factor
            if stretched_dur > max_allowed:
                fade_st = max(0.1, max_allowed - 0.08)
                stream_in = stream_in.filter('afade', t='out', st=str(fade_st), d='0.08')
                stream_in = stream_in.filter('atrim', duration=str(max_allowed))
            _ffmpeg_lib.output(stream_in, tempo_wav, acodec='pcm_s16le', ar='44100', ac=2).overwrite_output().run(
                cmd=ffmpeg_exe, capture_stdout=True, capture_stderr=True
            )
        except Exception as e_ff:
            logger.error(f"FFmpeg tempo pad error seg {idx}: {e_ff}")
            # Fallback: use subprocess directly for tempo adjustment
            try:
                if abs(speed_factor - 1.0) >= 0.04:
                    atempo_filters = ",".join(f"atempo={v}" for _, v in build_atempo_filter_chain(speed_factor))
                    af_chain = f"{atempo_filters},aresample=44100,aformat=channel_layouts=stereo"
                else:
                    af_chain = "aresample=44100,aformat=channel_layouts=stereo"
                trim_args = []
                if (tts_dur / speed_factor) > max_allowed:
                    trim_args = ["-t", str(max_allowed)]
                subprocess.run(
                    [ffmpeg_exe, "-y", "-i", raw_tts, "-af", af_chain] + trim_args +
                    ["-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", tempo_wav],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
                )
            except Exception as e_sub:
                logger.error(f"Subprocess tempo fallback error seg {idx}: {e_sub}")
        finally:
            cleanup_file(raw_tts)

        # 4. Extract raw PCM samples for sample-accurate master timeline
        if os.path.exists(tempo_wav) and os.path.getsize(tempo_wav) > 100:
            try:
                cmd = [ffmpeg_exe, "-y", "-i", tempo_wav, "-f", "s16le", "-ar", "44100", "-ac", "2", "pipe:1"]
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
                if proc.stdout:
                    samples = np.frombuffer(proc.stdout, dtype=np.int16).reshape(-1, 2)
                    with lock:
                        timed_segments.append((seg_start, samples))
                        tts_completed_counter[0] += 1
                        cur_done = tts_completed_counter[0]
                    if progress_callback and (cur_done % 2 == 0 or cur_done == len(valid_items)):
                        tts_engine_name = "VoxCPM" if _normalize_tts_backend(tts_backend) in ("voxcpm", "voxcpm2") else "Edge-TTS"
                        progress_callback(f"🗣️ [Voice Dubbing] {tts_engine_name} voices: {cur_done}/{len(valid_items)} lines ({target_lang.upper()})...")
                    return True
            except Exception as e_pcm:
                logger.error(f"PCM timeline error seg {idx}: {e_pcm}")
            finally:
                cleanup_file(tempo_wav)

        return mark_failed("audio timing or encoding failed")

    # Build sequence with lookahead to next segment start
    valid_items = []
    for i, (idx, seg) in enumerate(valid_segs):
        next_s = valid_segs[i+1][1]["start"] if (i + 1 < len(valid_segs)) else (orig_dur if orig_dur > 0 else seg["end"] + 3.0)
        valid_items.append((idx, seg, next_s))

    if progress_callback:
        progress_callback(f"🎙️ Generating TTS & exact mouth timing for {len(valid_items)} segments in parallel...")

    from concurrent.futures import ThreadPoolExecutor
    use_vox_workers = _normalize_tts_backend(tts_backend) in ("voxcpm2", "voxcpm")
    max_workers = 1 if use_vox_workers else min(6, max(1, len(valid_items)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        generated_segments = sum(bool(result) for result in executor.map(_process_segment, valid_items))

    if segment_failures and progress_callback:
        progress_callback(
            f"⚠️ Generated dubbed audio for {generated_segments}/{len(valid_items)} speech segments. "
            "Some lines could not be synthesized; check the bot log."
        )

    # Step 3: Master Sample-Accurate Audio Timeline Assembly (Zero Drift!)
    final_speech_track = None
    if timed_segments:
        if progress_callback: progress_callback("🎛️ Assembling sample-accurate audio timeline (zero drift)...")
        combined_speech = os.path.join(temp_dir, f"{uuid.uuid4()}_dubbed_speech.mp3")
        sr = 44100
        max_t = max(orig_dur + 1.0, max(s[0] + len(s[1])/sr for s in timed_segments) + 1.0)
        total_samples = int(max_t * sr)
        master_audio = np.zeros((total_samples, 2), dtype=np.int32)

        for seg_start, samples in timed_segments:
            start_idx = int(seg_start * sr)
            end_idx = start_idx + len(samples)
            if end_idx <= total_samples:
                master_audio[start_idx:end_idx] += samples
            else:
                avail = total_samples - start_idx
                if avail > 0:
                    master_audio[start_idx:total_samples] += samples[:avail]

        # Prevent integer clipping and normalize
        np.clip(master_audio, -32768, 32767, out=master_audio)
        master_int16 = master_audio.astype(np.int16)

        try:
            cmd = [
                ffmpeg_exe, "-y",
                "-f", "s16le", "-ar", str(sr), "-ac", "2", "-i", "pipe:0",
                "-acodec", "libmp3lame", "-b:a", "192k",
                combined_speech
            ]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.communicate(input=master_int16.tobytes())
            if os.path.exists(combined_speech) and os.path.getsize(combined_speech) > 100:
                final_speech_track = combined_speech
        except Exception as e_mix:
            logger.error(f"Error encoding master timeline: {e_mix}")
            if delayed_segment_files:
                final_speech_track = delayed_segment_files[0]

    if not final_speech_track:
        return "ERROR: Failed to generate dubbed speech track."

    # Step 4: Extract background music & sound effects using Demucs v4 AI stem separation
    final_audio = final_speech_track
    if keep_bgm:
        try:
            if progress_callback: progress_callback("🎵 Isolating Background Music & Sound Effects with Demucs AI...")
            bgm_result = extract_bgm_demucs(input_path, return_vocals=True, progress_callback=progress_callback)
            if isinstance(bgm_result, tuple):
                bgm_path, vocals_path = bgm_result
            else:
                bgm_path, vocals_path = bgm_result, None

            if bgm_path and os.path.exists(bgm_path) and os.path.getsize(bgm_path) > 1000:
                if progress_callback: progress_callback("🎧 Mixing dubbed voice with BGM and ducked original vocals (to retain cries/laughs)...")
                
                mixed_path = os.path.join(temp_dir, f"{uuid.uuid4()}_mixed_all.mp3")
                
                # Dynamic cinematic sidechain ducking (TVB / Broadcast standard):
                # Preserves Foley effects & background music at 0.85 when silent;
                # smoothly ducks music under speech so dialogue is punchy, crisp, and 100% intelligible.
                filter_graph = (
                    "[0:a]volume=0.85[bgm]; "
                    "[1:a]asplit[tts_mix][sc]; "
                    "[bgm][sc]sidechaincompress=threshold=0.06:ratio=4:attack=50:release=350[ducked_bgm]; "
                    "[ducked_bgm][tts_mix]amix=inputs=2:duration=first:normalize=0,aresample=44100,aformat=channel_layouts=stereo[outa]"
                )
                cmd = [
                    ffmpeg_exe, "-y",
                    "-i", bgm_path,
                    "-i", final_speech_track,
                    "-filter_complex", filter_graph,
                    "-map", "[outa]",
                    "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
                    mixed_path
                ]
                
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
                
                if os.path.exists(mixed_path) and os.path.getsize(mixed_path) > 1000:
                    final_audio = mixed_path
                cleanup_file(bgm_path)
                if vocals_path: cleanup_file(vocals_path)
            else:
                if bgm_path: cleanup_file(bgm_path)
                if vocals_path: cleanup_file(vocals_path)
                if progress_callback: progress_callback("ℹ️ BGM stem not available — using pure voice dub.")
        except Exception as e_bgm:
            print(f"BGM isolation & mixing error: {e_bgm}")
    else:
        if progress_callback: progress_callback("ℹ️ Background audio skipped per user request — using pure voice dub.")

    # Step 5: Apply loudnorm to match original audio loudness (-16 LUFS broadcast standard)
    if progress_callback: progress_callback("🔊 Normalizing audio loudness to match original video...")
    loudnorm_path = os.path.join(temp_dir, f"{uuid.uuid4()}_loudnorm.mp3")
    try:
        norm_timeout = max(300, int((orig_dur or 60) * 0.75))
        subprocess.run(
            [ffmpeg_exe, "-y", "-i", final_audio,
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-acodec", "libmp3lame", "-ar", "44100", "-ac", "2", "-b:a", "192k",
             loudnorm_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=norm_timeout
        )
        if os.path.exists(loudnorm_path) and os.path.getsize(loudnorm_path) > 100:
            final_audio = loudnorm_path
    except Exception as e_ln:
        print(f"Loudnorm failed: {e_ln}")

    # Step 6: Merge dubbed audio with original video (locked to original duration)
    output_ext = "mp4" if is_video else "mp3"
    output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_dubbed.{output_ext}")

    if is_video:
        if progress_callback: progress_callback("🎬 Merging dubbed audio with original video...")
        try:
            out_opts = {'vcodec': 'copy', 'acodec': 'aac', 'b:a': '192k', 'ar': '44100', 'ac': 2, 'movflags': '+faststart'}
            if orig_dur and orig_dur > 0:
                out_opts['t'] = orig_dur

            video_in = ffmpeg.input(input_path).video
            audio_in = ffmpeg.input(final_audio).audio
            stream = ffmpeg.output(video_in, audio_in, output_path, **out_opts).overwrite_output()
            res = _run_ffmpeg_with_progress(stream, output_path, progress_callback)
            if not res or not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
                raise Exception("Stream copy failed; falling back to smooth CFR encode")
        except Exception as e_merge:
            logger.info(f"FFmpeg copy merge fallback: {e_merge}. Re-encoding with smooth CFR 30fps...")
            try:
                out_opts_re = {
                    'vcodec': 'libx264', 'crf': '18', 'preset': 'fast', 'pix_fmt': 'yuv420p',
                    'r': '30', 'vsync': 'cfr',
                    'acodec': 'aac', 'b:a': '192k', 'ar': '44100', 'ac': 2, 'movflags': '+faststart'
                }
                if orig_dur and orig_dur > 0:
                    out_opts_re['t'] = orig_dur
                video_in = ffmpeg.input(input_path).video
                audio_in = ffmpeg.input(final_audio).audio
                stream = ffmpeg.output(video_in, audio_in, output_path, **out_opts_re).overwrite_output()
                res = _run_ffmpeg_with_progress(stream, output_path, progress_callback)
            except Exception as e_re:
                logger.error(f"FFmpeg re-encode merge error: {e_re}")
                res = None
    else:
        shutil.copy(final_audio, output_path)
        res = output_path

    # Cleanup temp files
    for sf in delayed_segment_files:
        cleanup_file(sf)
    if final_speech_track != final_audio:
        cleanup_file(final_speech_track)
    cleanup_file(final_audio)

    if res and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        if thumbnail_path and os.path.exists(thumbnail_path) and is_video:
            if progress_callback: progress_callback("🖼️ Applying custom thumbnail to video...")
            thumbed = apply_thumbnail_to_video(output_path, thumbnail_path)
            if thumbed and thumbed != output_path:
                cleanup_file(output_path)
                return thumbed
        return output_path

    return "ERROR: Video dubbing process failed."

def mix_recap_sidechain_ducking(bgm_wav_path, narration_wav_path, output_audio_path=None):
    """
    Applies dynamic FFmpeg sidechaincompress audio ducking to automatically lower background music & sound effects (no_vocals stem)
    whenever the narrator speaks, then mixes narrator audio with ducked BGM at 44.1kHz Stereo.
    """
    temp_dir = get_temp_dir()
    if not output_audio_path:
        output_audio_path = os.path.join(temp_dir, f"{uuid.uuid4()}_ducked_recap.mp3")
        
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    import subprocess
    
    filter_graph = (
        "[0:a]volume=0.85[bgm]; "
        "[1:a]asplit[narrator][sc]; "
        "[bgm][sc]sidechaincompress=threshold=0.08:ratio=4:attack=100:release=400[ducked_bgm]; "
        "[ducked_bgm][narrator]amix=inputs=2:duration=first:normalize=0,aresample=44100,aformat=channel_layouts=stereo[outa]"
    )
    
    cmd = [
        ffmpeg_exe, "-y",
        "-i", bgm_wav_path,
        "-i", narration_wav_path,
        "-filter_complex", filter_graph,
        "-map", "[outa]",
        "-acodec", "libmp3lame",
        "-ar", "44100",
        "-ac", "2",
        "-b:a", "192k",
        output_audio_path
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
        if os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 100:
            return output_audio_path
    except Exception as e:
        print(f"Sidechain ducking error: {e}")
        
    return mix_tts_with_bgm(narration_wav_path, bgm_wav_path, bgm_volume=0.25)

def clean_recap_speech_text(text):
    """Strips markdown, emojis, bullet points, numbers, and section headers to leave clean spoken recap text only."""
    t = re.sub(r'[\*\_~`>#]', ' ', text)
    t = re.sub(r'[📌💡🎯🎬▶✔✅⭐🏆•\-]\s*', ' ', t)
    t = re.sub(r'^\s*\d+[\.\)]\s*', ' ', t, flags=re.MULTILINE)
    t = re.sub(r'(Core Concept|Main Story Meaning|Detailed Scene-by-Scene|Key Point Breakdown|Key Highlights|Main Points|Core Topic|Story Overview|Discussion|Important Insights|Lessons|Final Conclusion|Main Takeaway|Summary|Executive Overview|Chronological Scene|Deep Analytical Insights|សង្ខេបលម្អិត|ខ្លឹមសារ|សេចក្តីសន្និដ្ឋាន)\s*[:\&]*', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'^(Here is|Summary|Recap|Khmer|English|Note):.*$', '', t, flags=re.MULTILINE | re.IGNORECASE)
    lines = [line.strip() for line in t.split('\n') if line.strip()]
    cleaned = ". ".join(lines)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

def recap_video_audio(input_path, target_lang='km', src_lang='auto', is_video=True, voiceover=False, progress_callback=None, thumbnail_path=None):
    """
    Automated Movie Recap Generation Pipeline:
    1. Transcribes media with Faster-Whisper ASR to extract scene timestamps and transcript.
    2. Uses Gemini 2.0 Flash to generate a dramatic 3rd-person narrator recap script & executive breakdown in target language.
    3. Synthesizes dramatic cinematic voiceover using edge-tts (km-KH-SreymomNeural, rate="-8%").
    4. Separates vocals and ambient BGM with Demucs v4 stem separation, discarding original vocals.
    5. Applies dynamic FFmpeg sidechaincompress audio ducking so BGM lowers whenever the narrator speaks.
    6. Cuts key scenes & matches video speed to narration.
    """
    temp_dir = get_temp_dir()
    
    if progress_callback: progress_callback("⚡ Transcribing scenes with Faster-Whisper ASR...")
    whisper_segs = transcribe_with_whisper_timestamps(input_path, src_lang=src_lang)
    
    from plugins.document_parser import transcribe_audio_video
    transcript = transcribe_audio_video(input_path, src_lang=src_lang)
    
    if not transcript or "Error" in transcript or "Unsupported" in transcript or len(transcript.strip()) < 3:
        return "ERROR: Could not extract speech from media to generate recap.", None
        
    LANG_NAMES_MAP = {
        'km': 'Khmer (ភាសាខ្មែរ)',
        'en': 'English',
        'zh': 'Chinese (中文)',
        'vi': 'Vietnamese (Tiếng Việt)',
        'th': 'Thai (ภาษาไทย)',
        'ko': 'Korean (한국어)',
        'ja': 'Japanese (日本語)',
        'fr': 'French (Français)',
        'es': 'Spanish (Español)',
        'de': 'German (Deutsch)',
        'ru': 'Russian (Русский)',
        'ar': 'Arabic (العربية)',
        'hi': 'Hindi (हिन्दी)'
    }
    target_lang_name = LANG_NAMES_MAP.get(target_lang[:2], 'Khmer')
    
    if progress_callback: progress_callback(f"🧠 Generating Cinematic Movie Recap Script in {target_lang_name} (Gemini 2.0 Flash)...")
    
    import asyncio
    from plugins.core import get_ai_response
    prompt = (
        f"You are Udom AI, an elite film director and movie recap narrator.\n"
        f"Generate a HIGHLY DETAILED, EXECUTIVE-GRADE PROFESSIONAL MOVIE RECAP & SYNTHESIS of the following video transcript in {target_lang_name}.\n\n"
        f"CRITICAL REQUIREMENTS:\n"
        f"1. Explain the FULL STORY context, background setting, character motivations, plot twists, and main message with extreme narrative flair.\n"
        f"2. Detail EVERY MAJOR SCENE in chronological sequence so the audience grasps every plot point without watching the original.\n"
        f"3. Provide deep analytical insights, core takeaways, and professional summary conclusions.\n\n"
        f"Structure your analysis in {target_lang_name} using these exact sections:\n\n"
        f"🏆 **1. EXECUTIVE OVERVIEW & CORE MEANING (ខ្លឹមសារ និងអត្ថន័យដើមនៃវីដេអូ)**\n"
        f"• Complete breakdown of what the movie is about, the main theme, background context, and core story purpose.\n\n"
        f"🎬 **2. CHRONOLOGICAL SCENE & HIGHLIGHT BREAKDOWN (សង្ខេបលម្អិតតាមចំនុច និងសាច់រឿង)**\n"
        f"• Detailed bullet points explaining key scenes, actions, plot progression, and climax.\n\n"
        f"💡 **3. DEEP ANALYTICAL INSIGHTS & LESSONS (ការវិភាគយ៉ាងស៊ីជម្រៅ និងមេរៀន)**\n"
        f"• Expert cinematic analysis of character arcs, philosophical messages, and hidden details.\n\n"
        f"🎯 **4. FINAL CONCLUSION & KEY TAKEAWAYS (សេចក្តីសន្និដ្ឋាន និងសារៈសំខាន់)**\n"
        f"• Final executive conclusion and summary takeaway.\n\n"
        f"Transcript:\n{transcript}"
    )
    
    try:
        try:
            loop = asyncio.get_running_loop()
            recap_text = asyncio.run_coroutine_threadsafe(get_ai_response(0, prompt), loop).result(45)
        except RuntimeError:
            recap_text = asyncio.run(get_ai_response(0, prompt))
    except Exception as e:
        print(f"AI Recap Error: {e}")
        recap_text = f"Failed to generate recap: {e}"
        
    if not voiceover:
        return recap_text, None
        
    # Generate Dramatic 3rd-Person Voiceover Script
    if progress_callback: progress_callback("🎙 Scripting Dramatic 3rd-Person Narrator Voiceover...")
    vo_script_prompt = (
        f"You are Udom AI, a world-class documentary and movie recap narrator.\n"
        f"Write a smooth, highly engaging, dramatic 3rd-person spoken RECAP VOICE-OVER NARRATION in {target_lang_name} based on this transcript.\n\n"
        f"GUIDELINES:\n"
        f"1. Narrative Arc: Start with a captivating hook, describe scene developments in dramatic chronological order, and finish with a memorable conclusion.\n"
        f"2. Style: Write in natural, fluent, cinematic spoken narration like a professional YouTube movie recap narrator.\n"
        f"3. Pure Spoken Text: Output ONLY pure spoken sentences in {target_lang_name}. Do NOT include section titles, numbers, bullet symbols, emojis, markdown asterisks, or quotes.\n\n"
        f"Transcript:\n{transcript}"
    )
    
    try:
        try:
            loop = asyncio.get_running_loop()
            raw_script = asyncio.run_coroutine_threadsafe(get_ai_response(0, vo_script_prompt), loop).result(30)
        except RuntimeError:
            raw_script = asyncio.run(get_ai_response(0, vo_script_prompt))
    except Exception:
        raw_script = recap_text
        
    tts_speech = clean_recap_speech_text(raw_script)
    if not tts_speech:
        tts_speech = clean_recap_speech_text(recap_text)
        
    if progress_callback: progress_callback("🎙 Synthesizing Cinematic Neural Narrator Voice (edge-tts -8% speed)...")
    raw_tts = generate_neural_tts(tts_speech, lang=target_lang[:2], rate="-8%")
    if not raw_tts or not os.path.exists(raw_tts):
        return recap_text, None
        
    # Isolate original BGM & Sound effects using Demucs v4 (discarding original vocals)
    if progress_callback: progress_callback("🎵 Isolating Ambient BGM & SFX with Demucs v4 (discarding original vocals)...")
    bgm_path = extract_bgm_demucs(input_path)
    
    orig_dur = get_video_duration(input_path)
    tts_dur = get_video_duration(raw_tts)
    target_dur = tts_dur if tts_dur > 0 else (orig_dur if orig_dur > 0 else 60.0)
    
    # Dynamic Sidechain Audio Ducking
    final_recap_audio = raw_tts
    if bgm_path and os.path.exists(bgm_path) and check_audio_rms(bgm_path):
        if progress_callback: progress_callback("🎧 Applying Dynamic Sidechain Audio Ducking (narrator ducking BGM volume)...")
        ducked = mix_recap_sidechain_ducking(bgm_path, raw_tts)
        if ducked and os.path.exists(ducked):
            final_recap_audio = ducked
        
    if is_video and orig_dur > 0 and target_dur > 0:
        if progress_callback: progress_callback("🎬 Edit & Speed Engine: Syncing video speed & scenes to narration...")
        
        output_path = os.path.join(temp_dir, f"{uuid.uuid4()}_movie_recap.mp4")
        
        speech_segments = whisper_segs if whisper_segs else detect_speech_segments(input_path)
        cut_video_path = None
        
        try:
            if speech_segments and len(speech_segments) > 1:
                segment_clips = []
                segs = speech_segments if isinstance(speech_segments[0], tuple) else [(s["start"], s["end"]) for s in speech_segments]
                for s_idx, (s_start, s_end) in enumerate(segs[:12]):
                    c_dur = s_end - s_start
                    if c_dur < 0.3: continue
                    c_out = os.path.join(temp_dir, f"{uuid.uuid4()}_recap_clip_{s_idx}.mp4")
                    try:
                        (
                            ffmpeg
                            .input(input_path, ss=s_start, t=c_dur)
                            .output(c_out, vcodec='copy', an=None)
                            .overwrite_output()
                            .run(cmd=imageio_ffmpeg.get_ffmpeg_exe(), capture_stdout=True, capture_stderr=True)
                        )
                        if os.path.exists(c_out) and os.path.getsize(c_out) > 100:
                            segment_clips.append(c_out)
                    except Exception:
                        pass
                
                if segment_clips:
                    concat_txt = os.path.join(temp_dir, f"{uuid.uuid4()}_recap_list.txt")
                    with open(concat_txt, "w", encoding="utf-8") as f_lst:
                        for clp in segment_clips:
                            f_lst.write(f"file '{clp}'\n")
                            
                    concat_raw = os.path.join(temp_dir, f"{uuid.uuid4()}_recap_concat.mp4")
                    try:
                        (
                            ffmpeg
                            .input(concat_txt, format='concat', safe=0)
                            .output(concat_raw, vcodec='copy', an=None)
                            .overwrite_output()
                            .run(cmd=imageio_ffmpeg.get_ffmpeg_exe(), capture_stdout=True, capture_stderr=True)
                        )
                        if os.path.exists(concat_raw) and os.path.getsize(concat_raw) > 100:
                            cut_video_path = concat_raw
                    except Exception:
                        pass
                    finally:
                        cleanup_file(concat_txt)
                        for clp in segment_clips:
                            cleanup_file(clp)

            source_v = cut_video_path if cut_video_path else input_path
            source_v_dur = get_video_duration(source_v) or orig_dur
            pts_factor = max(0.5, min(2.0, target_dur / max(1.0, source_v_dur)))
            
            video_input = ffmpeg.input(source_v).video.filter('setpts', f'{pts_factor}*PTS')
            audio_input = ffmpeg.input(final_recap_audio).audio
            
            out_opts = {'vcodec': 'libx264', 'acodec': 'aac', 'pix_fmt': 'yuv420p', 'ar': '44100', 'b:a': '192k', 't': target_dur}
            stream = (
                ffmpeg
                .output(video_input, audio_input, output_path, **out_opts)
                .overwrite_output()
            )
            res_media = _run_ffmpeg_with_progress(stream, output_path, progress_callback)
            if cut_video_path:
                cleanup_file(cut_video_path)
        except Exception as e_edit:
            print(f"Edit & Speed Engine Error: {e_edit}")
            res_media = final_recap_audio
    else:
        res_media = final_recap_audio
        
    cleanup_file(raw_tts)
    if bgm_path:
        cleanup_file(bgm_path)

    # Apply custom thumbnail to final video if provided
    if thumbnail_path and os.path.exists(thumbnail_path) and is_video and isinstance(res_media, str) and res_media.endswith(".mp4") and os.path.exists(res_media):
        if progress_callback: progress_callback("🖼️ Applying custom thumbnail to recap video...")
        thumbed = apply_thumbnail_to_video(res_media, thumbnail_path)
        if thumbed and thumbed != res_media:
            cleanup_file(res_media)
            res_media = thumbed

    return recap_text, res_media

def get_video_duration(file_path):
    """Accurately extracts video or audio duration in seconds using imageio_ffmpeg, ffprobe, null-scan, OpenCV, or ffmpeg-python."""
    if not file_path or not os.path.exists(file_path):
        return 0.0

    import subprocess
    import re

    # Method 1: Standard FFmpeg header regex
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-analyzeduration", "10M", "-probesize", "10M", "-i", file_path]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=14400)
        stderr_text = res.stderr.decode("utf-8", errors="ignore")
        match = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", stderr_text)
        if match:
            hours, minutes, seconds = match.groups()
            dur = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
            if dur > 0:
                return dur
    except Exception as e:
        print(f"Error getting duration with imageio_ffmpeg: {e}")

    # Method 2: ffprobe CLI if available
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        ffprobe_exe = ffmpeg_exe.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
        cmd_ffprobe = [ffprobe_exe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
        res_p = subprocess.run(cmd_ffprobe, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        stdout_text = res_p.stdout.decode("utf-8", errors="ignore").strip()
        dur = float(stdout_text)
        if dur > 0:
            return dur
    except Exception:
        pass

    # Method 3: Fast null scan for files with Duration: N/A
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-y", "-i", file_path, "-map", "0:v:0?", "-c", "copy", "-f", "null", "-"]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=7200)
        stderr_text = res.stderr.decode("utf-8", errors="ignore")
        matches = re.findall(r"time=\s*(\d+):(\d+):([\d.]+)", stderr_text)
        if matches:
            h, m, s = matches[-1]
            dur = float(h) * 3600 + float(m) * 60 + float(s)
            if dur > 0:
                return dur
    except Exception:
        pass

    # Method 4: OpenCV fallback
    try:
        import cv2
        cap = cv2.VideoCapture(file_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        if fps > 0 and frame_count > 0:
            return frame_count / fps
    except Exception:
        pass

    return 0.0

def extract_dm_chapters(input_path):
    """
    Parses video chapters via ffmpeg to find 'dm_clip_seek_before' and 'dm_clip_seek_after'
    Returns a dict with 'before': (start, end) and 'after': (start, end) if found.
    """
    import re
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-i", input_path]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        stderr_text = res.stderr.decode("utf-8", errors="ignore")
        chapters = {'before': None, 'after': None}
        for block in stderr_text.split("Chapter #")[1:]:
            start_match = re.search(r"start\s+([\d.]+),\s+end\s+([\d.]+)", block)
            title_match = re.search(r"title\s*:\s*([^\r\n]+)", block)
            if start_match and title_match:
                title = title_match.group(1).strip()
                if "dm_clip_seek_before" in title.lower() or title.lower() == "intro":
                    chapters['before'] = (float(start_match.group(1)), float(start_match.group(2)))
                elif "dm_clip_seek_after" in title.lower() or title.lower() == "outro":
                    chapters['after'] = (float(start_match.group(1)), float(start_match.group(2)))
        return chapters
    except Exception:
        return {'before': None, 'after': None}

def clip_video_into_parts(input_path, num_clips=3, known_duration=0, progress_callback=None):
    """
    Splits a video file into num_clips equal segment files using FFmpeg.
    Uses fast stream copy first, falling back to ultrafast re-encoding if needed.
    """
    temp_dir = get_temp_dir()
    duration = get_video_duration(input_path)
    if duration <= 0 and known_duration > 0:
        duration = float(known_duration)

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    if duration <= 0:
        raise ValueError("Could not determine video duration. This video format or stream cannot be automatically clipped.")

    dm_chaps = extract_dm_chapters(input_path)
    if has_copyright_metadata(input_path) and progress_callback:
        progress_callback("⚠️ Copyright metadata detected and removed from clips.")
        
    main_start = 0.0
    main_end = duration

    if dm_chaps['before']:
        main_start = dm_chaps['before'][1]
    if dm_chaps['after']:
        main_end = dm_chaps['after'][0]
        
    main_duration = main_end - main_start
    if main_duration <= 0:
        main_duration = duration # Fallback if chapters are messed up
        main_start = 0.0

    clip_duration = main_duration / max(1, num_clips)
    output_files = []

    def _extract_clip(start_sec, dur_sec, prefix="clip", source_file=None):
        if source_file is None:
            source_file = input_path
        out_f = os.path.join(temp_dir, f"{uuid.uuid4()}_{prefix}.mp4")
        try:
            cmd_copy = [
                ffmpeg_exe, "-y",
                "-ss", f"{start_sec:.3f}",
                "-i", source_file,
                "-t", f"{dur_sec:.3f}",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-map_metadata", "-1",
                out_f
            ]
            subprocess.run(cmd_copy, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=7200)
            if not os.path.exists(out_f) or os.path.getsize(out_f) < 1000:
                cmd_reencode = [
                    ffmpeg_exe, "-y",
                    "-ss", f"{start_sec:.3f}",
                    "-i", source_file,
                    "-t", f"{dur_sec:.3f}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k",
                    "-map_metadata", "-1",
                    out_f
                ]
                subprocess.run(cmd_reencode, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=14400)
            if os.path.exists(out_f) and os.path.getsize(out_f) > 500:
                return out_f
        except Exception as e:
            print(f"Error creating clip {prefix}: {e}")
        return None

    # 1. Extract dm_clip_seek_before (intro) if exists
    before_clip = None
    if dm_chaps['before']:
        b_start, b_end = dm_chaps['before']
        if progress_callback: progress_callback("✂️ Extracting Intro Ad...")
        before_clip = _extract_clip(b_start, b_end - b_start, "intro")

    # 2. Extract dm_clip_seek_after (outro) if exists
    after_clip = None
    if dm_chaps['after']:
        a_start, a_end = dm_chaps['after']
        if progress_callback: progress_callback("✂️ Extracting Outro Ad...")
        after_clip = _extract_clip(a_start, a_end - a_start, "outro")

    # 3. Extract main video first
    if progress_callback: progress_callback("✂️ Extracting Main Video...")
    main_video = _extract_clip(main_start, main_duration, "main_video")
    if not main_video:
        main_video = input_path

    # 4. Extract main parts from main video
    for i in range(num_clips):
        start_time = (i * clip_duration) if main_video != input_path else (main_start + (i * clip_duration))
        if progress_callback:
            progress_callback(f"✂️ Cutting video clip {i+1} of {num_clips}...")
        main_part = _extract_clip(start_time, clip_duration, f"part_{i+1}", source_file=main_video)
        if main_part:
            output_files.append(main_part)

    if main_video and main_video != input_path:
        try: os.remove(main_video)
        except: pass

    # The user requested: "final result 3 clip + dm_clip_seek_after and dm_clip_seek_before"
    # Wait, the prompt says "out of 3 clip if it have and final result 3 clip + dm_clip_seek_after and dm_clip_seek_before".
    # So we should probably prepend the before clip and append the after clip.
    result = {
        "intro": before_clip,
        "main_clips": output_files,
        "outro": after_clip,
    }
    return result

def clip_video_by_duration(input_path, duration_sec=60, known_duration=0, progress_callback=None):
    """
    Splits a video/audio file into chunks of duration_sec seconds using FFmpeg.
    Uses fast stream copy first, falling back to ultrafast re-encoding if needed.
    """
    import math
    temp_dir = get_temp_dir()
    total_dur = get_video_duration(input_path)
    if total_dur <= 0 and known_duration > 0:
        total_dur = float(known_duration)

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    if total_dur <= 0 or duration_sec <= 0:
        raise ValueError("Could not determine total video duration. This video format or stream cannot be clipped by duration.")

    dm_chaps = extract_dm_chapters(input_path)
    if has_copyright_metadata(input_path) and progress_callback:
        progress_callback("⚠️ Copyright metadata detected and removed from clips.")
        
    main_start = 0.0
    main_end = total_dur

    if dm_chaps['before']:
        main_start = dm_chaps['before'][1]
    if dm_chaps['after']:
        main_end = dm_chaps['after'][0]
        
    main_duration = main_end - main_start
    if main_duration <= 0:
        main_duration = total_dur
        main_start = 0.0

    num_clips = int(math.ceil(main_duration / float(duration_sec)))
    output_files = []

    def _extract_clip(start_sec, dur_sec, prefix="clip", source_file=None):
        if source_file is None:
            source_file = input_path
        out_f = os.path.join(temp_dir, f"{uuid.uuid4()}_{prefix}.mp4")
        try:
            cmd_copy = [
                ffmpeg_exe, "-y",
                "-ss", f"{start_sec:.3f}",
                "-i", source_file,
                "-t", f"{dur_sec:.3f}",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-map_metadata", "-1",
                out_f
            ]
            subprocess.run(cmd_copy, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=7200)
            if not os.path.exists(out_f) or os.path.getsize(out_f) < 1000:
                cmd_reencode = [
                    ffmpeg_exe, "-y",
                    "-ss", f"{start_sec:.3f}",
                    "-i", source_file,
                    "-t", f"{dur_sec:.3f}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k",
                    "-map_metadata", "-1",
                    out_f
                ]
                subprocess.run(cmd_reencode, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=14400)
            if os.path.exists(out_f) and os.path.getsize(out_f) > 500:
                return out_f
        except Exception as e:
            print(f"Error creating clip {prefix}: {e}")
        return None

    # 1. Extract intro
    before_clip = None
    if dm_chaps['before']:
        b_start, b_end = dm_chaps['before']
        if progress_callback: progress_callback("✂️ Extracting Intro Ad...")
        before_clip = _extract_clip(b_start, b_end - b_start, "intro")

    # 2. Extract outro
    after_clip = None
    if dm_chaps['after']:
        a_start, a_end = dm_chaps['after']
        if progress_callback: progress_callback("✂️ Extracting Outro Ad...")
        after_clip = _extract_clip(a_start, a_end - a_start, "outro")

    # 3. Extract main video first
    if progress_callback: progress_callback("✂️ Extracting Main Video...")
    main_video = _extract_clip(main_start, main_duration, "main_video")
    if not main_video:
        main_video = input_path

    # 4. Extract main parts from main video
    for i in range(num_clips):
        start_time = (i * float(duration_sec)) if main_video != input_path else (main_start + (i * float(duration_sec)))
        segment_dur = min(float(duration_sec), main_end - (start_time if main_video == input_path else (main_start + start_time)))
        if segment_dur < 0.5:
            continue

        if progress_callback:
            progress_callback(f"✂️ Cutting clip {i+1} of {num_clips} ({segment_dur:.0f}s)...")

        main_part = _extract_clip(start_time, segment_dur, f"part_dur_{i+1}", source_file=main_video)
        if main_part:
            output_files.append(main_part)

    if main_video and main_video != input_path:
        try: os.remove(main_video)
        except: pass

    result = {
        "intro": before_clip,
        "main_clips": output_files,
        "outro": after_clip,
    }
    return result
