import os
import subprocess
import imageio_ffmpeg

def get_video_duration(filepath):
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        res = subprocess.run([ffmpeg_exe, "-i", filepath], stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=10)
        stderr_text = res.stderr.decode('utf-8', errors='ignore')
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", stderr_text)
        if m:
            h, m, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return h * 3600 + m * 60 + s
    except Exception as e:
        print(f"[video_splitter] Error getting duration: {e}")
    return 0

def split_video(filepath, num_clips, output_dir=None):
    """Splits a video file into `num_clips` equal parts. Returns a list of file paths."""
    if num_clips <= 1:
        return [filepath]
        
    duration = get_video_duration(filepath)
    if duration <= 0:
        return [filepath]
    
    if output_dir is None:
        output_dir = os.path.dirname(filepath)
        
    clip_duration = duration / num_clips
    output_files = []
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    
    base_name = os.path.basename(filepath)
    name, ext = os.path.splitext(base_name)
    
    for i in range(num_clips):
        start_time = i * clip_duration
        out_path = os.path.join(output_dir, f"{name}_part{i+1}{ext}")
        cmd = [
            ffmpeg_exe, "-y",
            "-ss", str(start_time),
            "-i", filepath,
            "-t", str(clip_duration),
            "-c", "copy",  # Fast stream copy without re-encoding
            out_path
        ]
        try:
            subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, check=True)
            output_files.append(out_path)
        except Exception as e:
            print(f"[video_splitter] Error splitting part {i+1}: {e}")
            
    return output_files if output_files else [filepath]
