import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import os
import mimetypes

FB_API_VERSION = "v19.0"

def check_fb_token(page_id, token):
    """
    Check if the provided Facebook Page ID and Token are valid.
    Returns (True, page_name) if valid, (False, error_message) if invalid.
    """
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}?access_token={token}"
    try:
        res = requests.get(url, timeout=10, verify=False)
        data = res.json()
        if "error" in data:
            return False, data["error"].get("message", "Unknown error")
        return True, data.get("name", "Unknown Page")
    except Exception as e:
        return False, str(e)

def post_to_facebook(page_id, token, message="", media_path=None, title=None, thumb_path=None, tags=None, collaborators=None):
    """
    Post text, photo, or video to a Facebook Page.
    Returns (True, post_id) on success, (False, error_message) on failure.
    """
    if token:
        token = str(token).strip()
    if page_id:
        page_id = str(page_id).strip()
    if message:
        message = message.replace('/n', '\n').replace('\\n', '\n')
    if not media_path:
        # Text only post
        url = f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/feed"
        payload = {
            "message": message,
            "access_token": token
        }
        if tags:
            payload["tags"] = tags
        if collaborators:
            payload["collaborators"] = collaborators
        try:
            res = requests.post(url, data=payload, timeout=15, verify=False)
            try:
                data = res.json()
                if "error" in data:
                    return False, data["error"].get("message", "Unknown error")
                return True, data.get("id")
            except ValueError:
                return False, f"API Error (HTTP {res.status_code}): {res.text[:200]}"
        except Exception as e:
            return False, str(e)
    else:
        # Post with media
        mime_type, _ = mimetypes.guess_type(media_path)
        if not mime_type:
            if media_path.lower().endswith(('.mp4', '.mov', '.avi', '.mkv')):
                mime_type = 'video/mp4'
            elif media_path.lower().endswith(('.jpg', '.jpeg', '.png')):
                mime_type = 'image/jpeg'
        
        if mime_type and mime_type.startswith("video"):
            url = f"https://graph-video.facebook.com/{FB_API_VERSION}/{page_id}/videos"
            try:
                file_size = os.path.getsize(media_path)
                # Phase 1: Start
                payload_start = {
                    "access_token": token,
                    "upload_phase": "start",
                    "file_size": file_size
                }
                res_start = requests.post(url, data=payload_start, timeout=30, verify=False)
                start_data = res_start.json()
                if "error" in start_data:
                    return False, start_data["error"].get("message", "Unknown error in upload start")
                
                upload_session_id = start_data.get("upload_session_id")
                video_id = start_data.get("video_id")
                start_offset = int(start_data.get("start_offset", 0))
                end_offset = int(start_data.get("end_offset", file_size))
                
                # Phase 2: Transfer
                with open(media_path, "rb") as f:
                    while start_offset < file_size:
                        f.seek(start_offset)
                        chunk = f.read(end_offset - start_offset)
                        if not chunk:
                            break
                        
                        payload_trans = {
                            "access_token": token,
                            "upload_phase": "transfer",
                            "upload_session_id": upload_session_id,
                            "start_offset": start_offset
                        }
                        files_trans = {
                            "video_file_chunk": (os.path.basename(media_path), chunk, mime_type)
                        }
                        res_trans = requests.post(url, data=payload_trans, files=files_trans, timeout=120, verify=False)
                        trans_data = res_trans.json()
                        if "error" in trans_data:
                            return False, trans_data["error"].get("message", "Unknown error in transfer phase")
                        
                        start_offset = int(trans_data.get("start_offset", start_offset))
                        end_offset = int(trans_data.get("end_offset", start_offset))

                # Phase 3: Finish
                payload_finish = {
                    "access_token": token,
                    "upload_phase": "finish",
                    "upload_session_id": upload_session_id,
                    "description": message
                }
                if title:
                    payload_finish["title"] = title
                if tags:
                    payload_finish["tags"] = tags
                if collaborators:
                    payload_finish["collaborators"] = collaborators

                thumb_f = None
                files_finish = None
                if thumb_path and os.path.exists(thumb_path):
                    thumb_f = open(thumb_path, "rb")
                    files_finish = {"thumb": (os.path.basename(thumb_path), thumb_f, 'image/jpeg')}
                
                res_finish = requests.post(url, data=payload_finish, files=files_finish, timeout=60, verify=False)
                if thumb_f:
                    thumb_f.close()
                    
                finish_data = res_finish.json()
                if "error" in finish_data:
                    return False, finish_data["error"].get("message", "Unknown error in finish phase")
                    
                return True, video_id
            except ValueError:
                return False, f"API Error (HTTP {res_start.status_code}): {res_start.text[:200]}"
            except Exception as e:
                return False, str(e)
        else:
            # Assume image
            url = f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/photos"
            payload = {
                "message": message,
                "access_token": token
            }
            if tags:
                payload["tags"] = tags
            if collaborators:
                payload["collaborators"] = collaborators
            try:
                with open(media_path, "rb") as f:
                    files = {"source": (os.path.basename(media_path), f, mime_type)}
                    res = requests.post(url, data=payload, files=files, timeout=60, verify=False)
                try:
                    data = res.json()
                    if "error" in data:
                        return False, data["error"].get("message", "Unknown error")
                    return True, data.get("id")
                except ValueError:
                    return False, f"API Error (HTTP {res.status_code}): {res.text[:200]}"
            except Exception as e:
                return False, str(e)
