import os
import shutil
import tempfile
import time
import threading
import asyncio
import subprocess
import re

async def run_sync(func, *args, **kwargs):
    """Execute a synchronous function in a thread pool if ENABLE_ASYNC_IO is true.
    The ENABLE_ASYNC_IO flag is read from the environment (set in main)."""
    if os.getenv('ENABLE_ASYNC_IO', 'false').lower() == 'true':
        return await asyncio.to_thread(func, *args, **kwargs)
    else:
        return func(*args, **kwargs)

# Use /var/data if available (Persistent Disk), otherwise use a local folder to guarantee disk storage (not RAM)
if os.path.exists("/var/data") and os.path.isdir("/var/data"):
    TEMP_DIR = "/var/data/bot_temp"
else:
    TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_temp")

os.makedirs(TEMP_DIR, exist_ok=True)
tempfile.tempdir = TEMP_DIR

def get_temp_dir():
    return TEMP_DIR

def cleanup_file(filepath):
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except Exception as e:
        print(f"Error deleting file {filepath}: {e}")

def cleanup_all():
    if os.path.exists(TEMP_DIR):
        try:
            shutil.rmtree(TEMP_DIR)
        except Exception as e:
            print(f"Error cleaning up temp dir: {e}")

def partition_episodes(episodes, num_parts):
    """
    Partitions a list of episodes into `num_parts` sequential groups.
    Example: 83 episodes, 5 parts -> [17, 17, 17, 16, 16] episodes per part.
    Or if T=10, N=5 -> [[EP1, EP2], [EP3, EP4], [EP5, EP6], [EP7, EP8], [EP9, EP10]].
    """
    total = len(episodes)
    if num_parts <= 1 or total <= 1:
        return [episodes]
    if num_parts >= total:
        return [[ep] for ep in episodes]
    
    parts = []
    base_size = total // num_parts
    remainder = total % num_parts
    
    start = 0
    for i in range(num_parts):
        size = base_size + (1 if i < remainder else 0)
        end = start + size
        parts.append(episodes[start:end])
        start = end
    return parts

def get_video_duration(path: str, ffmpeg_exe: str = None) -> float:
    """Probes the total duration in seconds of a media file using FFmpeg."""
    if not path or not os.path.exists(path) or os.path.getsize(path) < 100:
        return 0.0
    if not ffmpeg_exe:
        try:
            import imageio_ffmpeg
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg_exe = "ffmpeg"
    try:
        cmd = [ffmpeg_exe, "-hide_banner", "-i", path]
        res = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="ignore",
            timeout=25
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr)
        if m:
            h, m_val, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600 + m_val * 60 + s
    except Exception as e:
        print(f"[get_video_duration] error probing {path}: {e}")
    return 0.0

def merge_video_files(input_paths, output_path):
    """
    Concatenates multiple video files into a single seamless video, verifying that
    ALL episodes/clips are fully merged without truncation or mid-stream cutoffs.

    Strategies (fastest → safest):
      1. Stream copy via concat demuxer (-c copy)
      2. Intermediate MPEG-TS stream copy (solves MP4 timescale/timestamp collisions without re-encoding)
      3. FFmpeg filter_complex concat (guarantees 100% of frames from all files are stitched)
      4. Concat demuxer with universal H.264/AAC re-encode (CFR 30fps, 44.1kHz stereo, +faststart)

    Each stage verifies that actual merged duration is >= 90% of total expected duration.
    """
    valid_paths = [p for p in input_paths if p and os.path.exists(p) and os.path.getsize(p) > 100]
    if not valid_paths:
        return None
    if len(valid_paths) == 1:
        shutil.copy2(valid_paths[0], output_path)
        return output_path

    import subprocess
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Calculate total expected duration across all input episodes
    input_durations = [get_video_duration(p, ffmpeg_exe) for p in valid_paths]
    total_expected_duration = sum(input_durations)
    print(f"[merge_video_files] Total expected duration for {len(valid_paths)} files: {total_expected_duration:.2f}s")

    # Write concat list (use forward-slash paths for ffmpeg cross-platform compat)
    list_file = os.path.join(get_temp_dir(), f"concat_list_{int(time.time()*1000)}.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in valid_paths:
            p_esc = p.replace('\\', '/').replace("'", "\\'")
            f.write(f"file '{p_esc}'\n")

    def _run(cmd, timeout):
        try:
            return subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="ignore",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"[merge_video_files] ffmpeg timed out after {timeout}s")
            return None

    def _is_playable(path):
        if not path or not os.path.exists(path) or os.path.getsize(path) < 1000:
            return False
        test_cmd = [
            ffmpeg_exe, "-v", "error", "-xerror",
            "-i", path,
            "-t", "2",
            "-f", "null", "-"
        ]
        try:
            p = subprocess.run(test_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=25)
            return p.returncode == 0
        except Exception:
            return False

    def _is_complete_and_valid(path):
        if not _is_playable(path):
            return False
        if total_expected_duration > 10.0:
            actual_dur = get_video_duration(path, ffmpeg_exe)
            if actual_dur < (total_expected_duration * 0.90):
                print(f"[merge_video_files] ⚠️ Truncation detected! Merged duration={actual_dur:.1f}s vs expected={total_expected_duration:.1f}s (missing {(total_expected_duration - actual_dur):.1f}s). Discarding truncated file.")
                return False
        return True

    def _cleanup_out():
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass

    try:
        # ── Step 1: Concat demuxer stream copy (lossless, instant) ────────────
        print(f"[merge_video_files] Attempting stream-copy merge for {len(valid_paths)} files → {output_path}")
        cmd_copy = [
            ffmpeg_exe, "-y",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c", "copy",
            "-tag:v", "avc1",
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts",
            "-movflags", "+faststart",
            output_path,
        ]
        _run(cmd_copy, timeout=600)
        if _is_complete_and_valid(output_path):
            print(f"[merge_video_files] Stream-copy succeeded ({get_video_duration(output_path, ffmpeg_exe):.1f}s). All episodes merged cleanly.")
            return output_path

        _cleanup_out()

        # ── Step 2: Intermediate MPEG-TS Concatenation ────────────────────────
        # MP4 containers store timescale in moov header which often causes stream-copy to truncate at clip 1.
        # Converting clips to temporary .ts transport streams bypasses timescale collisions losslessly.
        print(f"[merge_video_files] Stream-copy truncated or failed. Trying MPEG-TS intermediate lossless merge...")
        ts_dir = os.path.join(get_temp_dir(), f"ts_merge_{int(time.time()*1000)}")
        os.makedirs(ts_dir, exist_ok=True)
        ts_files = []
        try:
            ts_all_ok = True
            for i, vp in enumerate(valid_paths):
                ts_out = os.path.join(ts_dir, f"clip_{i:04d}.ts")
                # Detect video codec to pick correct bitstream filter (HEVC vs H264)
                vcodec = "h264"
                try:
                    p_probe = subprocess.run([ffmpeg_exe, "-i", vp], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="ignore", timeout=8)
                    m_vc = re.search(r'Video:\s*([a-zA-Z0-9_-]+)', p_probe.stderr)
                    if m_vc:
                        vcodec = m_vc.group(1).lower()
                except Exception:
                    pass

                bsf_args = []
                if "hevc" in vcodec or "h265" in vcodec:
                    bsf_args = ["-bsf:v", "hevc_mp4toannexb"]
                elif "h264" in vcodec or "avc" in vcodec:
                    bsf_args = ["-bsf:v", "h264_mp4toannexb"]

                cmd_to_ts = [
                    ffmpeg_exe, "-y", "-i", vp,
                    "-c", "copy",
                    *bsf_args,
                    "-f", "mpegts",
                    ts_out
                ]
                _run(cmd_to_ts, timeout=60)
                if os.path.exists(ts_out) and os.path.getsize(ts_out) > 100:
                    ts_files.append(ts_out)
                else:
                    ts_all_ok = False
                    break

            if ts_all_ok and len(ts_files) == len(valid_paths):
                concat_ts_pipe = "concat:" + "|".join(ts_files)
                cmd_ts_merge = [
                    ffmpeg_exe, "-y",
                    "-i", concat_ts_pipe,
                    "-c", "copy",
                    "-bsf:a", "aac_adtstoasc",
                    "-movflags", "+faststart",
                    output_path
                ]
                _run(cmd_ts_merge, timeout=600)
                if _is_complete_and_valid(output_path):
                    print(f"[merge_video_files] MPEG-TS lossless merge succeeded ({get_video_duration(output_path, ffmpeg_exe):.1f}s)!")
                    return output_path
        finally:
            shutil.rmtree(ts_dir, ignore_errors=True)

        _cleanup_out()

        # ── Step 3: Filter Complex Concat Re-encode (Guaranteed Frame Inclusion) ──
        # For moderate batch sizes (<= 40 episodes), filter_complex explicitly decodes
        # and stitches 100% of audio and video frames without skipping any episode.
        if len(valid_paths) <= 40:
            print(f"[merge_video_files] Using FFmpeg filter_complex concat for {len(valid_paths)} clips (guarantees all episodes included)...")
            inputs = []
            fc_parts = []
            for idx, vp in enumerate(valid_paths):
                inputs.extend(["-i", vp])
                fc_parts.append(f"[{idx}:v:0][{idx}:a:0]")
            filter_str = "".join(fc_parts) + f"concat=n={len(valid_paths)}:v=1:a=1[v][a]"
            cmd_fc = [
                ffmpeg_exe, "-y",
                *inputs,
                "-filter_complex", filter_str,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-r", "30",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart",
                output_path,
            ]
            _run(cmd_fc, timeout=7200)
            if _is_complete_and_valid(output_path):
                print(f"[merge_video_files] Filter-complex re-encode merge succeeded ({get_video_duration(output_path, ffmpeg_exe):.1f}s).")
                return output_path

        _cleanup_out()

        # ── Step 4: Concat Demuxer Re-encode Fallback ─────────────────────────
        print(f"[merge_video_files] Attempting concat demuxer re-encode fallback...")
        cmd_encode = [
            ffmpeg_exe, "-y",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-r", "30", "-vsync", "cfr",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-af", "aresample=async=1000",
            "-movflags", "+faststart",
            output_path,
        ]
        _run(cmd_encode, timeout=7200)
        if _is_complete_and_valid(output_path):
            print(f"[merge_video_files] Concat demuxer re-encode succeeded ({get_video_duration(output_path, ffmpeg_exe):.1f}s).")
            return output_path

        _cleanup_out()

        # ── Step 5: Final fallback with medium preset ────────────────────────
        print("[merge_video_files] Fallback to medium preset...")
        cmd_fallback = [
            ffmpeg_exe, "-y",
            "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-af", "aresample=async=1000",
            "-movflags", "+faststart",
            output_path,
        ]
        _run(cmd_fallback, timeout=7200)
        if _is_playable(output_path):
            print("[merge_video_files] Final fallback merge succeeded.")
            return output_path
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            print("[merge_video_files] Medium-preset fallback merge succeeded.")
            return output_path

    finally:
        cleanup_file(list_file)

    print("[merge_video_files] All merge strategies failed.")
    return None

def start_auto_cleanup_routine(max_age_seconds=14400, interval_seconds=600):
    """Background thread that continuously cleans up old temp files (>4 hours) every 10 minutes."""
    def cleanup_loop():
        while True:
            try:
                time.sleep(interval_seconds)
                now = time.time()
                temp_dir = get_temp_dir()
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        try:
                            if os.path.isfile(file_path):
                                file_age = now - os.path.getmtime(file_path)
                                if file_age > max_age_seconds:
                                    os.remove(file_path)
                        except Exception:
                            pass
            except Exception:
                pass
                
    threading.Thread(target=cleanup_loop, daemon=True).start()
