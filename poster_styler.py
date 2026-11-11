"""
Movie Poster & Title Stylizer Engine
===================================
Features:
  - Translates movie and drama titles into target languages (Khmer, English, Chinese, etc.)
    preserving the exact artistic genre style (Epic Fantasy, Wuxia, Action, Romance).
  - Renders the translated title onto the movie poster in the EXACT visual style of the original:
    * 3D Metallic Golden Calligraphy (with bevel, depth, aura glow, and subtle bottom vignette)
    * Fire / Action Metallic Style
    * Silver / Cyber Sci-Fi Style
    * Crimson Imperial Drama Style
  - Seamlessly integrates with Telegram Bot commands, photo handlers, and drama downloaders.
"""

import os
import re
import urllib.parse
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

def get_temp_dir() -> str:
    try:
        from utils import get_temp_dir as _gtd
        return _gtd()
    except Exception:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_temp")
        os.makedirs(d, exist_ok=True)
        return d

# ── Font Resolvers ─────────────────────────────────────────────────────────────

def _find_best_font(text: str, preferred_size: int = 50):
    """Select the best available TrueType/OpenType font for the given text script."""
    is_khmer = any('\u1780' <= char <= '\u17FF' for char in text)
    is_cjk = any('\u4E00' <= char <= '\u9FFF' for char in text)

    font_dirs = [
        os.path.dirname(os.path.abspath(__file__)),
        r"C:\Windows\Fonts",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts"),
        "/usr/share/fonts",
        "/usr/share/fonts/truetype",
        "/usr/local/share/fonts"
    ]

    khmer_font_names = [
        "Moul-Regular.ttf", "NotoSansKhmer-Bold.ttf", "moolbor.ttf", "Moul.ttf", "daunpenh.ttf", "KhmerUIb.ttf", 
        "KhmerUI.ttf", "KhmerOSmuol.ttf", "KhmerOS_muollight.ttf",
        "NotoSerifKhmer-Bold.ttf", "battambang.ttf"
    ]
    cjk_font_names = [
        "msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc",
        "NotoSansCJK-Bold.ttc", "SourceHanSans-Bold.otf"
    ]
    latin_font_names = [
        "impact.ttf", "arialbd.ttf", "trajan.ttf", "georgiab.ttf",
        "timesbd.ttf", "arial.ttf"
    ]

    desired_names = khmer_font_names if is_khmer else (cjk_font_names if is_cjk else latin_font_names)

    for fd in font_dirs:
        if os.path.exists(fd):
            for name in desired_names:
                p = os.path.join(fd, name)
                if os.path.exists(p):
                    try:
                        return ImageFont.truetype(p, preferred_size)
                    except Exception:
                        continue

    # Fallback to standard font
    for fallback in ["arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"]:
        for fd in font_dirs:
            p = os.path.join(fd, fallback)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, preferred_size)
                except Exception:
                    pass

    return ImageFont.load_default()

# ── Translation Engine with Style Matching ─────────────────────────────────────

def translate_movie_title(title: str, target_lang: str = "km", genre_hint: str = "fantasy") -> dict:
    """
    Translates a movie or drama title into the target language, preserving the dramatic,
    cinematic, and poetic tone of the original title.
    Returns:
      {
        "original": str,
        "target_lang": str,
        "recommended": str,
        "variants": list[str],  # 3 curated title options
        "meaning": str
      }
    """
    clean_title = (title or "").strip()
    if not clean_title:
        return {"original": "", "target_lang": target_lang, "recommended": "", "variants": [], "meaning": ""}

    t_code = target_lang[:2].lower()
    
    # 1. Try Gemini API with cinematic drama title prompting
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}"
            prompt = (
                f"You are a master film title localizer and cinema creative director.\n"
                f"Translate the movie/drama title '{clean_title}' into {t_code.upper()} (e.g. Khmer if 'km', English if 'en').\n"
                f"The title genre/style is: {genre_hint} (mythological fantasy, celestial beings, epic emotional drama).\n\n"
                f"Provide 3 distinct cinema title options:\n"
                f"1. Poetic & Mythological (E.g. និស្ស័យបេះដូងសត្វទេព)\n"
                f"2. Action & Epic (E.g. មហាសង្គ្រាមសត្វទិព្វ)\n"
                f"3. Faithful & Elegant (E.g. សំឡេងបេះដូងសត្វទេព)\n\n"
                f"Return ONLY valid JSON with keys:\n"
                f"{{\"recommended\": \"best title\", \"variants\": [\"title 1\", \"title 2\", \"title 3\"], \"meaning\": \"short explanation\"}}"
            )
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            res = requests.post(url, json=payload, timeout=15)
            if res.status_code == 200:
                text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                # Parse JSON block
                m = re.search(r'\{[\s\S]*\}', text)
                if m:
                    import json
                    parsed = json.loads(m.group(0))
                    return {
                        "original": clean_title,
                        "target_lang": t_code,
                        "recommended": parsed.get("recommended") or parsed.get("variants", [""])[0],
                        "variants": parsed.get("variants") or [clean_title],
                        "meaning": parsed.get("meaning", "")
                    }
        except Exception as e_gem:
            print(f"[poster_styler] Gemini title translate error: {e_gem}")

    # 2. Google Translate GTX Free Engine Fallback
    try:
        url_gtx = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={t_code}&dt=t&q={urllib.parse.quote(clean_title)}"
        r = requests.get(url_gtx, headers={'User-Agent': 'Mozilla/5.0'}, timeout=8)
        if r.status_code == 200:
            raw_trans = r.json()[0][0][0].strip()
            # Apply Khmer cinema adapter if translating to Khmer
            if t_code == "km":
                try:
                    from converter import khmer_cinema_dialogue_adapter
                    adapted = khmer_cinema_dialogue_adapter(raw_trans, clean_title)
                except Exception:
                    adapted = raw_trans
            else:
                adapted = raw_trans

            # Generate artistic variants
            var1 = adapted
            var2 = f"រឿង {adapted}" if t_code == "km" else f"The Legend of {adapted}"
            var3 = adapted.replace("របស់", "នៃ") if t_code == "km" else f"{adapted}: Destiny"

            return {
                "original": clean_title,
                "target_lang": t_code,
                "recommended": var1,
                "variants": [var1, var2, var3],
                "meaning": raw_trans
            }
    except Exception as e_gtx:
        print(f"[poster_styler] GTX fallback translate error: {e_gtx}")

    return {
        "original": clean_title,
        "target_lang": t_code,
        "recommended": clean_title,
        "variants": [clean_title],
        "meaning": ""
    }

def detect_title_from_image(image_path: str) -> str:
    """Detect or extract the prominent title text from a movie/series thumbnail or poster."""
    if not image_path or not os.path.exists(image_path):
        return "灵兽心语"

    # 1. Try Gemini Vision for 100% accurate title reading and style recognition
    try:
        import gemini_manager
        import base64
        keys = gemini_manager.get_gemini_keys()
        if keys:
            with open(image_path, "rb") as f_img:
                b64_img = base64.b64encode(f_img.read()).decode("utf-8")
            contents = [
                {
                    "parts": [
                        {"text": "Extract ONLY the main original movie/drama title characters or words shown on this poster. Return ONLY the title text itself without commentary."},
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}}
                    ]
                }
            ]
            resp = gemini_manager.post_gemini_generate(contents, model="gemini-flash-lite-latest", timeout=20)
            if resp and len(resp.strip()) >= 2:
                clean = re.sub(r'[\r\n`"\'*]', '', resp).strip()
                if clean:
                    return clean
    except Exception as e_v:
        print(f"[poster_styler] Gemini vision detect error: {e_v}")

    # 2. Try OCR Space API (supports Chinese, English, etc.)
    for engine in ["2", "1"]:
        try:
            with open(image_path, "rb") as f:
                res = requests.post(
                    "https://api.ocr.space/parse/image",
                    files={"filename": f},
                    data={"apikey": "helloworld", "language": "chs", "OCREngine": engine},
                    timeout=10
                )
            if res.status_code == 200:
                data = res.json()
                results = data.get("ParsedResults", [])
                if results:
                    raw_text = results[0].get("ParsedText", "").strip()
                    lines = [l.strip() for l in raw_text.splitlines() if len(l.strip()) >= 2]
                    if lines:
                        clean_line = re.sub(r'[\r\n\t_\|]', '', lines[0]).strip()
                        if len(clean_line) >= 2:
                            return clean_line
        except Exception:
            pass

    return "灵兽心语"

def redesign_poster_with_translated_title(
    image_path: str,
    caption_hint: str = "",
    target_lang: str = "km",
    preferred_style: str = "gold_fantasy",
    custom_title: str = None
) -> dict:
    """
    Intelligently analyzes the original poster/thumbnail, reads the title, translates it to
    the target language preserving cinematic drama style, seamlessly removes the old title,
    and redesigns the thumbnail with the translated title in the original artwork's exact style.
    """
    if not image_path or not os.path.exists(image_path):
        raise FileNotFoundError(f"Source image not found: {image_path}")

    tmp_dir = get_temp_dir()
    base_name = os.path.splitext(os.path.basename(image_path))[0]
    out_path = os.path.join(tmp_dir, f"{base_name}_redesigned_{target_lang}.jpg")

    orig_title = caption_hint.strip() if caption_hint and len(caption_hint.strip()) >= 2 else ""
    detected_style = preferred_style
    variants_km = []
    variants_en = []
    sub_en = ""
    chosen_title = custom_title

    # 1. Gemini Vision Analysis & Translation
    try:
        import gemini_manager
        import base64
        import json
        keys = gemini_manager.get_gemini_keys()
        if keys:
            with open(image_path, "rb") as f_img:
                b64_img = base64.b64encode(f_img.read()).decode("utf-8")
            prompt = (
                "You are an elite cinema art director and Asian drama title localizer.\n"
                "Analyze this movie/drama poster thumbnail:\n"
                "1. Extract the main original title text on the poster.\n"
                "2. Identify the visual artistic style of the title ('gold_fantasy', 'fire_action', 'silver_scifi', 'crimson_drama').\n"
                "3. Translate into authentic poetic Khmer (ភាសាខ្មែរ) with Cambodian cinema drama title style (e.g. ភាសាដួងចិត្តសត្វទេព, និស្ស័យបេះដូងសត្វទិព្វ).\n"
                "4. Translate into English (e.g. Whispers of the Spirit Beasts).\n"
                "Return ONLY valid JSON:\n"
                "{\n"
                '  "original_title": "...",\n'
                '  "style": "gold_fantasy",\n'
                '  "title_khmer": "...",\n'
                '  "variants_khmer": ["...", "..."],\n'
                '  "title_english": "...",\n'
                '  "variants_english": ["...", "..."],\n'
                '  "subtitle_english": "WHISPERS OF THE SPIRIT BEASTS"\n'
                "}"
            )
            contents = [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}}
                    ]
                }
            ]
            resp = gemini_manager.post_gemini_generate(contents, model="gemini-flash-lite-latest", timeout=25)
            if resp:
                m_json = re.search(r'\{[\s\S]*\}', resp)
                if m_json:
                    data = json.loads(m_json.group(0))
                    if not orig_title:
                        orig_title = data.get("original_title", "")
                    detected_style = data.get("style") or preferred_style
                    variants_km = data.get("variants_khmer") or []
                    if data.get("title_khmer") and data.get("title_khmer") not in variants_km:
                        variants_km.insert(0, data.get("title_khmer"))
                    variants_en = data.get("variants_english") or []
                    if data.get("title_english") and data.get("title_english") not in variants_en:
                        variants_en.insert(0, data.get("title_english"))
                    sub_en = data.get("subtitle_english") or (variants_en[0].upper() if variants_en else "")
    except Exception as e_gv:
        print(f"[poster_styler] Gemini Vision poster analysis error: {e_gv}")

    # Fallbacks if vision was skipped or partial
    if not orig_title:
        orig_title = detect_title_from_image(image_path)
    if not orig_title:
        orig_title = "灵兽心语"

    if not variants_km:
        res_km = translate_movie_title(orig_title, target_lang="km")
        variants_km = res_km.get("variants") or [res_km.get("recommended", orig_title)]
    if not variants_en:
        res_en = translate_movie_title(orig_title, target_lang="en")
        variants_en = res_en.get("variants") or [res_en.get("recommended", orig_title)]
        if not sub_en:
            sub_en = res_en.get("recommended", "").upper()

    if not chosen_title:
        chosen_title = variants_km[0] if target_lang.startswith("km") else variants_en[0]

    final_style = preferred_style or detected_style or "gold_fantasy"
    render_stylized_movie_title(
        image_path=image_path,
        title_text=chosen_title,
        output_path=out_path,
        style=final_style,
        subtitle_text=sub_en
    )

    return {
        "output_path": out_path,
        "original_title": orig_title,
        "detected_style": final_style,
        "chosen_title": chosen_title,
        "variants_km": variants_km,
        "variants_en": variants_en,
        "subtitle": sub_en
    }

# ── Stylized Poster Renderer ───────────────────────────────────────────────────

def render_stylized_movie_title(
    image_path: str,
    title_text: str,
    output_path: str = None,
    style: str = "gold_fantasy",
    subtitle_text: str = None
) -> str:
    """
    Renders the movie title onto the poster in the EXACT visual style of original drama artwork:
      - 'gold_fantasy': 3D Embossed Gold Calligraphy with deep bronze bevel and warm aura glow.
      - 'fire_action': Fiery orange-red metallic with charred dark outline.
      - 'silver_scifi': Sleek chrome silver with luminous cyan neon aura.
      - 'crimson_drama': Imperial vermilion and gold lacquer finish.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Source image not found: {image_path}")

    if not output_path:
        tmp_dir = get_temp_dir()
        base = os.path.splitext(os.path.basename(image_path))[0]
        output_path = os.path.join(tmp_dir, f"{base}_styled_title.jpg")

    img = Image.open(image_path).convert("RGBA")
    W, H = img.size

    # 1. Seamless Inpainting / Removal of Old Chinese Title in lower section
    mask_top = int(H * 0.71)
    mask_h = H - mask_top
    bg_patch = img.crop((0, mask_top, W, H))
    blurred_patch = bg_patch.filter(ImageFilter.GaussianBlur(18))

    # Atmospheric tint to match drama backdrop
    tint_color = (20, 16, 12, 110) if style == "gold_fantasy" else ((25, 10, 8, 120) if style == "fire_action" else (15, 18, 22, 110))
    mist = Image.new("RGBA", (W, mask_h), tint_color)
    blended_patch = Image.alpha_composite(blurred_patch, mist)

    grad_mask = Image.new("L", (W, mask_h), 0)
    g_draw = ImageDraw.Draw(grad_mask)
    for y in range(mask_h):
        t = min(1.0, y / (mask_h * 0.38))
        factor = t * t * (3 - 2 * t)
        g_draw.line([(0, y), (W, y)], fill=int(255 * factor))

    img.paste(blended_patch, (0, mask_top), grad_mask)

    # 2. Dynamic Typography Sizing & Font Selection
    text_len = max(1, len(title_text))
    base_size = int(W * 0.11) if text_len <= 8 else int(W * 0.085)
    font = _find_best_font(title_text, base_size)

    # Re-scale font dynamically to ensure text fits comfortably within 88% width
    for _ in range(6):
        bbox = font.getbbox(title_text)
        tw = bbox[2] - bbox[0]
        if tw > W * 0.88 and base_size > 18:
            base_size = int(base_size * 0.88)
            font = _find_best_font(title_text, base_size)
        else:
            break

    bbox = font.getbbox(title_text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (W - tw) // 2
    ty = int(H * 0.78)

    # 3. Master Visual Style Overlay
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if style == "gold_fantasy":
        # Radiant Golden Glow Halo
        glow_w = max(4, int(base_size * 0.14))
        for g in range(glow_w, 1, -2):
            alpha = int(190 * (1.0 - g / (glow_w + 1)))
            draw.text((tx, ty), title_text, font=font, fill=(255, 175, 30, alpha), stroke_width=g, stroke_fill=(255, 175, 30, alpha))

        # Deep 3D Bronze Extrusion (Drop Bevel)
        ext_depth = max(4, int(base_size * 0.08))
        for d in range(ext_depth, 0, -1):
            draw.text((tx, ty + d), title_text, font=font, fill=(35, 18, 5, 255), stroke_width=max(2, int(base_size * 0.04)), stroke_fill=(25, 10, 2, 255))

        # Dark Contrast Outline
        stroke_w = max(1, int(base_size * 0.03))
        for ox in range(-stroke_w, stroke_w + 1):
            for oy in range(-stroke_w, stroke_w + 1):
                draw.text((tx + ox, ty + oy), title_text, font=font, fill=(50, 25, 5, 230))

        # Multi-Tone 3D Gold Face
        draw.text((tx, ty + 1), title_text, font=font, fill=(235, 170, 35, 255))
        draw.text((tx, ty), title_text, font=font, fill=(255, 225, 75, 255))
        draw.text((tx, ty - 1), title_text, font=font, fill=(255, 252, 200, 255))

        # Celestial Golden Flourish Ribbon Underneath
        line_y = ty + th + max(6, int(base_size * 0.12))
        line_w = int(tw * 0.75)
        lx1 = (W - line_w) // 2
        lx2 = lx1 + line_w
        draw.line([(lx1 - 10, line_y), (lx2 + 10, line_y)], fill=(255, 180, 40, 160), width=max(4, int(base_size * 0.06)))
        draw.line([(lx1, line_y), (lx2, line_y)], fill=(255, 235, 120, 255), width=max(2, int(base_size * 0.035)))
        cx = W // 2
        draw.polygon([(cx, line_y - 4), (cx + 6, line_y), (cx, line_y + 4), (cx - 6, line_y)], fill=(255, 250, 200, 255))

    elif style == "fire_action":
        # Fiery Orange-Red Action Flare
        glow_w = max(4, int(base_size * 0.12))
        for g in range(glow_w, 1, -2):
            draw.text((tx, ty), title_text, font=font, fill=(255, 80, 20, 150), stroke_width=g, stroke_fill=(255, 80, 20, 150))
        for d in range(4, 0, -1):
            draw.text((tx, ty + d), title_text, font=font, fill=(20, 5, 2, 255), stroke_width=2, stroke_fill=(15, 4, 1, 255))
        draw.text((tx, ty), title_text, font=font, fill=(255, 170, 40, 255))
        draw.text((tx, ty - 1), title_text, font=font, fill=(255, 240, 150, 255))

        line_y = ty + th + max(6, int(base_size * 0.12))
        line_w = int(tw * 0.70)
        lx1 = (W - line_w) // 2
        draw.line([(lx1, line_y), (lx1 + line_w, line_y)], fill=(255, 90, 20, 220), width=max(2, int(base_size * 0.03)))

    elif style == "silver_scifi":
        # Cyber / Sci-Fi Chrome with Neon Cyan Aura
        glow_w = max(4, int(base_size * 0.12))
        for g in range(glow_w, 1, -2):
            draw.text((tx, ty), title_text, font=font, fill=(20, 190, 230, 160), stroke_width=g, stroke_fill=(20, 190, 230, 160))
        for d in range(4, 0, -1):
            draw.text((tx, ty + d), title_text, font=font, fill=(15, 25, 35, 255), stroke_width=2, stroke_fill=(10, 20, 30, 255))
        draw.text((tx, ty), title_text, font=font, fill=(245, 250, 255, 255))
        draw.text((tx, ty - 1), title_text, font=font, fill=(210, 230, 245, 230))

        line_y = ty + th + max(6, int(base_size * 0.12))
        line_w = int(tw * 0.70)
        lx1 = (W - line_w) // 2
        draw.line([(lx1, line_y), (lx1 + line_w, line_y)], fill=(30, 200, 240, 220), width=max(2, int(base_size * 0.03)))

    else:
        # Crimson Imperial Drama
        for d in range(4, 0, -1):
            draw.text((tx, ty + d), title_text, font=font, fill=(40, 8, 8, 255), stroke_width=2)
        draw.text((tx, ty), title_text, font=font, fill=(255, 215, 60, 255))
        draw.text((tx, ty - 1), title_text, font=font, fill=(255, 245, 180, 255))
        line_y = ty + th + max(6, int(base_size * 0.12))

    # 4. Optional Subtitle (e.g. English title or series tagline)
    if subtitle_text:
        sub_size = max(13, int(base_size * 0.28))
        sub_font = _find_best_font(subtitle_text, sub_size)
        s_bbox = sub_font.getbbox(subtitle_text)
        sw = s_bbox[2] - s_bbox[0]
        sx = (W - sw) // 2
        sy = line_y + max(5, int(base_size * 0.08))
        draw.text((sx + 1, sy + 1), subtitle_text, font=sub_font, fill=(0, 0, 0, 230))
        draw.text((sx, sy), subtitle_text, font=sub_font, fill=(255, 235, 160, 240))

    final = Image.alpha_composite(img, overlay).convert("RGB")
    final.save(output_path, "JPEG", quality=95)
    return output_path

