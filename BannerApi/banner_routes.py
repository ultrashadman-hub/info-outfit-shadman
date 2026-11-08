
from __future__ import annotations

import asyncio
import os
import io
import re
import json
import base64
import unicodedata
from functools import lru_cache
from urllib.parse import urlencode

import requests
from flask import Blueprint, request, jsonify, Response, current_app
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_IMPORT_ERROR = None
except Exception as e:
    Image = ImageDraw = ImageFont = None
    PIL_IMPORT_ERROR = str(e)

def _require_pillow():
    if PIL_IMPORT_ERROR or Image is None:
        raise RuntimeError("Pillow is required for banner endpoints. Install it with: pip install Pillow. Error: " + str(PIL_IMPORT_ERROR))

from Api.Account import get_garena_token, get_major_login, get_open_id_from_access_token
from Api.InGame import get_player_personal_show
try:
    from Utilities.enhancer import enrich_player_response
except Exception:
    enrich_player_response = lambda x: x

banner_bp = Blueprint("banner_api", __name__)

PNG_API = os.environ.get("PNG_API", "https://cucivk.vercel.app/getpng?pngid={id}")
ASSET_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "BannerAssets")

CANVAS_W = 1866
CANVAS_H = 400
AVATAR_W = 400
TEXT_X = 485

@lru_cache(maxsize=1)
def _discover_font_paths():
    """Find as many Unicode fonts as possible.

    This avoids depending on one predefined font. The player name and guild name
    are drawn exactly from the API response, and every character is rendered
    using the first installed/bundled font that really contains that glyph.
    """
    preferred = [
        os.path.join(ASSET_DIR, "arial_unicode_bold.otf"),
        os.path.join(ASSET_DIR, "NotoSansCherokee.ttf"),
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansSymbols-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/system/fonts/NotoSans-Regular.ttf",
        "/system/fonts/NotoSans-Bold.ttf",
        "/system/fonts/NotoSansSymbols-Regular-Subsetted.ttf",
        "/system/fonts/NotoSansSymbols2-Regular.ttf",
        "/system/fonts/NotoSansCJK-Regular.ttc",
        "/system/fonts/NotoSansDevanagari-Regular.ttf",
        "/system/fonts/NotoSansBengali-Regular.ttf",
        "/system/fonts/NotoColorEmoji.ttf",
        "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
    search_dirs = [
        ASSET_DIR,
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        "/opt/homebrew/share/fonts",
        "/system/fonts",
        "/data/data/com.termux/files/usr/share/fonts",
    ]
    paths = []
    seen = set()
    for path in preferred:
        if path and path not in seen and os.path.exists(path):
            seen.add(path); paths.append(path)
    for root in search_dirs:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if name.lower().endswith((".ttf", ".otf", ".ttc")):
                    path = os.path.join(dirpath, name)
                    if path not in seen:
                        seen.add(path); paths.append(path)
    return tuple(paths)

@lru_cache(maxsize=512)
def _load_font_from_path(path, size):
    return ImageFont.truetype(path, size=size)

@lru_cache(maxsize=4096)
def _font_file_has_char(path, ch):
    """True only when the font cmap contains the exact Unicode codepoint.

    PIL's getmask can return the tofu/missing-glyph box, so this check prevents
    those square boxes whenever another installed font has the real glyph.
    """
    if not path or len(ch) != 1:
        return True
    cp = ord(ch)
    # Combining marks do not need their own visible glyph in every font.
    if unicodedata.combining(ch):
        return True
    try:
        from fontTools.ttLib import TTFont
        font = TTFont(path, fontNumber=0, lazy=True)
        for table in font["cmap"].tables:
            if cp in table.cmap:
                try:
                    font.close()
                except Exception:
                    pass
                return True
        try:
            font.close()
        except Exception:
            pass
    except Exception:
        # If fontTools cannot inspect it, let PIL try it.
        return True
    return False

def _font_candidates(size):
    if ImageFont is None:
        return []
    fonts = []
    for path in _discover_font_paths():
        try:
            fonts.append(_load_font_from_path(path, size))
        except Exception:
            pass
    if not fonts:
        fonts.append(ImageFont.load_default())
    return fonts

def _load_font(size):
    fonts = _font_candidates(size)
    return fonts[0] if fonts else None

def _clean(value, default=""):
    """Keep FF name/guild as exact as possible; remove only invisible control bytes."""
    if value is None:
        return default
    value = str(value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b\ufeff]", "", value)
    value = value.strip()
    return value if value else default

def _to_int(value, default=None):
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default

def _first(*vals):
    for v in vals:
        if v not in (None, "", {}, [], 0, "0"):
            return v
    return None

def _dig(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur

def _normalize_png_id(icon_id):
    if icon_id in (None, "", 0, "0", "none", "null"):
        return None
    icon_id = str(icon_id).strip()
    icon_id = icon_id.split("?")[0].split("#")[0].strip("/")
    if icon_id.lower().endswith(".png"):
        icon_id = icon_id[:-4]
    return icon_id or None

def _icon_url(icon_id):
    """Build URL for your own Vercel PNG API.

    Default: https://cucivk.vercel.app/getpng?pngid={id}
    You can change it without editing code by setting env variable:
    PNG_API=https://your-api.vercel.app/getpng?pngid={id}
    """
    icon_id = _normalize_png_id(icon_id)
    if not icon_id:
        return None
    api = PNG_API
    if "{id}" in api:
        return api.format(id=icon_id)
    if "(id)" in api:
        return api.replace("(id)", icon_id)
    sep = "&" if "?" in api else "?"
    return f"{api}{sep}pngid={icon_id}"

def _fetch_image(icon_id, kind="icon"):
    """Fetch an image from the new PNG API and return a PIL RGBA image.

    It tries both without .png and with .png because your /getpng endpoint supports both.
    If fetching fails, None is returned so banner rendering can still continue.
    """
    icon_id = _normalize_png_id(icon_id)
    if not icon_id:
        return None

    urls = []
    base_url = _icon_url(icon_id)
    if base_url:
        urls.append(base_url)
        if not base_url.lower().endswith(".png") and "pngid=" in base_url:
            urls.append(base_url.replace(f"pngid={icon_id}", f"pngid={icon_id}.png"))

    headers = {
        "User-Agent": "Mozilla/5.0 BannerRenderer/1.0",
        "Accept": "image/png,image/*;q=0.9,*/*;q=0.1",
        "Cache-Control": "no-cache",
    }

    for url in urls:
        try:
            r = requests.get(url, timeout=15, headers=headers)
            ctype = (r.headers.get("content-type") or "").lower()
            if r.status_code != 200 or not r.content:
                continue
            # Your API may return image/png. If it returns octet-stream but PIL can open it, accept it.
            if ("image" not in ctype) and not r.content[:8].startswith(b"\x89PNG"):
                continue
            img = Image.open(io.BytesIO(r.content))
            img.load()
            return img.convert("RGBA")
        except Exception as e:
            try:
                current_app.logger.warning("PNG fetch failed kind=%s id=%s url=%s error=%s", kind, icon_id, url, e)
            except Exception:
                pass
    return None


def _visible_bbox(img):
    try:
        return img.split()[-1].getbbox()
    except Exception:
        return None

def _detect_inner_white_border_crop(img):
    """Crop to internal white square border, removing outer purple/extra canvas."""
    if img is None:
        return None
    img = img.convert("RGBA")
    bbox = _visible_bbox(img)
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    pix = img.load()
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            r, g, b, a = pix[x, y]
            if a > 80 and r > 205 and g > 205 and b > 205 and max(r, g, b) - min(r, g, b) < 50:
                xs.append(x); ys.append(y)
    if len(xs) > max(80, (w*h)//300):
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        pad = max(2, int(min(w,h)*0.012))
        left, top = max(0, left-pad), max(0, top-pad)
        right, bottom = min(w, right+pad), min(h, bottom+pad)
        cw, ch = right-left, bottom-top
        if cw > w*0.42 and ch > h*0.42:
            return img.crop((left, top, right, bottom))
    side = min(w,h)
    left, top = max(0,(w-side)//2), max(0,(h-side)//2)
    return img.crop((left, top, left+side, top+side))

def _fit_headpic_icon(img, size=(400,400)):
    """Show headPic only until white border, perfectly zoomed."""
    bg = Image.new("RGBA", size, (20,20,20,255))
    if img is None:
        return bg
    img = _detect_inner_white_border_crop(img)
    if img is None:
        return bg
    w, h = img.size
    scale = min(size[0]/max(1,w), size[1]/max(1,h))
    nw, nh = max(1,int(w*scale)), max(1,int(h*scale))
    img = img.resize((nw,nh), Image.LANCZOS)
    bg.alpha_composite(img, ((size[0]-nw)//2, (size[1]-nh)//2))
    return bg

def _load_prime(level):
    try:
        level = max(0, min(8, int(level)))
    except Exception:
        level = 0
    path = os.path.join(ASSET_DIR, f"prime{level}.png")
    if os.path.exists(path):
        try:
            return Image.open(path).convert("RGBA")
        except Exception:
            pass
    return None

def _load_prime8_frame():
    path = os.path.join(ASSET_DIR, "prime8frame.png")
    if os.path.exists(path):
        try:
            return Image.open(path).convert("RGBA")
        except Exception:
            pass
    return None

def _font_has_char(font, ch):
    try:
        path = getattr(font, "path", None)
        if path and not _font_file_has_char(path, ch):
            return False
        mask = font.getmask(ch)
        return bool(mask and mask.getbbox()) or unicodedata.combining(ch)
    except Exception:
        return False

def _choose_font_for_char(ch, fonts, default_font):
    # Keep spaces on the default font for consistent spacing.
    if ch.isspace() or unicodedata.combining(ch):
        return default_font
    for f in fonts:
        if _font_has_char(f, ch):
            return f
    return default_font

def _iter_text_units(text):
    """Group base characters with combining marks so the original response text is preserved."""
    buf = ""
    for ch in str(text):
        if not buf:
            buf = ch
        elif unicodedata.combining(ch) or ch in "\ufe0f\ufe0e":
            buf += ch
        else:
            yield buf
            buf = ch
    if buf:
        yield buf

def _draw_text(draw, xy, text, font, stroke_width=4):
    """Draw the exact nickname/guild string from the response with font fallback.

    No nickname/guild character is replaced or mapped to a predefined alphabet.
    Missing boxes are avoided by checking real glyph coverage across all bundled
    and system fonts before drawing each character.
    """
    if not text or font is None:
        return
    text = str(text)
    x, y = xy
    try:
        size = getattr(font, "size", 60)
    except Exception:
        size = 60
    fonts = _font_candidates(size) or [font]
    for unit in _iter_text_units(text):
        chosen = _choose_font_for_char(unit[0], fonts, font)
        draw.text((x, y), unit, font=chosen, fill="white", stroke_width=stroke_width, stroke_fill="black")
        try:
            x += chosen.getlength(unit)
        except Exception:
            try:
                bbox = draw.textbbox((0, 0), unit, font=chosen, stroke_width=stroke_width)
                x += max(8, bbox[2] - bbox[0])
            except Exception:
                x += max(16, int(size * 0.45))

def _text_width(draw, text, font, stroke_width=4):
    if not text or font is None:
        return 0
    try:
        size = getattr(font, "size", 60)
    except Exception:
        size = 60
    fonts = _font_candidates(size) or [font]
    total = 0
    for unit in _iter_text_units(str(text)):
        chosen = _choose_font_for_char(unit[0], fonts, font)
        try:
            total += chosen.getlength(unit)
        except Exception:
            try:
                bbox = draw.textbbox((0, 0), unit, font=chosen, stroke_width=stroke_width)
                total += max(8, bbox[2] - bbox[0])
            except Exception:
                total += max(16, int(size * 0.45))
    return total

def _fit_font(draw, text, max_width, start, minimum):
    text = _clean(text)
    for size in range(start, minimum - 1, -3):
        font = _load_font(size)
        try:
            if _text_width(draw, text, font, stroke_width=4) <= max_width:
                return font
        except Exception:
            return font
    return _load_font(minimum)

def _fit_full_icon(img, size=(400, 400), pad=0):
    """Fit full icon including white border/frame; no center crop."""
    bg = Image.new("RGBA", size, (20, 20, 20, 255))
    if img is None:
        return bg
    img = img.convert("RGBA")
    try:
        alpha = img.split()[-1]
        bbox = alpha.getbbox()
        if bbox:
            img = img.crop(bbox)
    except Exception:
        pass
    max_w, max_h = size[0] - pad * 2, size[1] - pad * 2
    w, h = img.size
    scale = min(max_w / max(1, w), max_h / max(1, h))
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    bg.alpha_composite(img, ((size[0] - nw) // 2, (size[1] - nh) // 2))
    return bg

def _crop_square(img, size=(400, 400)):
    return _fit_full_icon(img, size=size, pad=0)

def _prepare_pin_icon(img, size=(82, 82)):
    """Crop pink/magenta bottom strip from pin PNG, then fit cleanly."""
    if img is None:
        return None
    img = img.convert("RGBA")
    try:
        w, h = img.size
        pix = img.load()
        cut = h
        for y in range(h - 1, max(0, h - int(h * 0.55)), -1):
            samples = pink = darkbase = 0
            for x in range(0, w, max(1, w // 90)):
                r, g, b, a = pix[x, y]
                if a > 30:
                    samples += 1
                    if r > 105 and b > 105 and g < 110:
                        pink += 1
                    if b > r + 20 and b > g + 20 and r > 35:
                        darkbase += 1
            if samples and ((pink / samples > 0.28) or (darkbase / samples > 0.36)):
                cut = y
            elif cut != h:
                break
        if cut < h:
            img = img.crop((0, 0, w, max(1, cut)))
    except Exception:
        pass
    return _fit_full_icon(img, size=size, pad=1)

def _crop_banner(img, size=(1466, 400)):
    if img is None:
        return Image.new("RGBA", size, (40, 40, 40, 255))
    w, h = img.size
    tw, th = size
    scale = max(tw / max(1, w), th / max(1, h))
    nw, nh = int(w * scale), int(h * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left = max(0, (nw - tw) // 2)
    top = max(0, (nh - th) // 2)
    return img.crop((left, top, left + tw, top + th))

def _decode_jwt_uid(jwt_token):
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        return str(data.get("account_id") or data.get("external_uid") or data.get("uid") or "")
    except Exception:
        return None

def resolve_uid_from_request():
    uid = request.args.get("uid") or request.args.get("account_id") or request.args.get("accountId")
    if uid:
        return str(uid).strip()

    jwt_token = request.args.get("jwt") or request.args.get("token")
    if jwt_token:
        uid = _decode_jwt_uid(jwt_token)
        if uid:
            return uid

    access_token = request.args.get("access_token")
    if access_token:
        try:
            user_info = get_open_id_from_access_token(access_token)
            if user_info and user_info.get("open_id"):
                major = get_major_login(access_token, user_info["open_id"])
                if major and isinstance(major, dict) and major.get("token"):
                    uid = _decode_jwt_uid(major["token"])
                    if uid:
                        return uid
                    # Some MajorLogin wrappers return account id directly
                    if major.get("accountId") or major.get("account_id"):
                        return str(major.get("accountId") or major.get("account_id"))
            # Last fallback: inspect response may include uid/account_id
            if user_info:
                for key in ("uid", "account_id", "accountId", "user_id"):
                    if user_info.get(key):
                        return str(user_info.get(key))
        except Exception as e:
            print("access_token uid resolve failed:", e)

    guest_uid = request.args.get("guest_uid") or request.args.get("bot_uid") or request.args.get("login_uid")
    password = request.args.get("password") or request.args.get("pw") or request.args.get("pass")
    if guest_uid and password:
        try:
            garena = get_garena_token(guest_uid, password)
            if garena and garena.get("access_token") and garena.get("open_id"):
                major = get_major_login(garena["access_token"], garena["open_id"])
                if major and isinstance(major, dict) and major.get("token"):
                    uid = _decode_jwt_uid(major["token"])
                    if uid:
                        return uid
        except Exception:
            pass

    return None


def _extract_prime(basic, root):
    candidates = [
        basic.get("primeLevel"),
        basic.get("primeInfo"),
        basic.get("prime_info"),
        basic.get("primeprivilegedetail"),
        root.get("primeInfo"),
        root.get("prime_info"),
        root.get("primeLevel"),
    ]
    for obj in candidates:
        if isinstance(obj, dict):
            for key in ("level", "primeLevel", "prime_level", "primelevel"):
                if obj.get(key) not in (None, ""):
                    return _to_int(obj.get(key), 0)
        elif obj not in (None, ""):
            return _to_int(obj, 0)
    return 0

def _normalize_info_response(raw):
    data = enrich_player_response(raw) if isinstance(raw, dict) else {}
    basic = data.get("basicInfo") or data.get("basic_info") or data.get("basicinfo") or {}
    clan = data.get("clanBasicInfo") or data.get("clan_basic_info") or data.get("clanbasicinfo") or {}
    captain = data.get("captainBasicInfo") or data.get("captain_basic_info") or {}

    bannerid = _first(
        basic.get("bannerId"), basic.get("banner_id"), basic.get("bannerid"),
        captain.get("bannerId") if not basic else None
    )
    headpicid = _first(
        basic.get("headPic"), basic.get("head_pic"), basic.get("headpic"),
        basic.get("avatarId"), basic.get("avatar_id")
    )
    pinid = _first(basic.get("pinId"), basic.get("pin_id"), basic.get("pinid"))
    level = _first(basic.get("level"), data.get("level"))
    name = _first(basic.get("nickname"), basic.get("playerName"), data.get("nickname"))
    guild = _first(clan.get("clanName"), clan.get("clan_name"), clan.get("guildName"), "")

    return {
        "data": data,
        "basic": basic,
        "clan": clan,
        "profile": {
            "uid": str(_first(basic.get("accountId"), basic.get("account_id"), request.args.get("uid"), "")),
            "server": (request.args.get("server") or request.args.get("region") or "IND").upper(),
            "bannerid": bannerid,
            "headpicid": headpicid,
            "pinid": pinid,
            "level": level,
            "playernickname": _clean(name, "Unknown"),
            "guildname": _clean(guild, ""),
            "prime": _extract_prime(basic, data),
        }
    }

def fetch_profile_by_uid(uid):
    """Fetch player data using the OB55 token generator shipped in app.py."""
    import app as info_api

    server = (request.args.get("server") or request.args.get("region") or "BD").upper()
    if server not in ("BD", "IND", "BR"):
        raise RuntimeError("Unsupported server. Use BD, IND or BR.")

    try:
        raw = asyncio.run(info_api.GetAccountInformation(int(uid), server))
    except Exception as exc:
        raise RuntimeError(f"Player information request failed: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError("Player information API returned no data")

    # The Info API's PersonalShow response may not include outfit inventory.
    # If profileInfo is absent, the renderer reports a clear message instead
    # of silently drawing a misleading/empty outfit.
    extracted = _normalize_info_response(raw)
    extracted["profile"]["uid"] = str(uid)
    extracted["profile"]["server"] = server
    return extracted

def render_profile_banner(profile):
    headpicid = profile.get("headpicid")
    bannerid = profile.get("bannerid")
    pinid = profile.get("pinid")
    prime = _to_int(profile.get("prime"), 0)
    level = _clean(profile.get("level"), "0")
    playername = _clean(profile.get("playernickname") or profile.get("playername"), "Unknown")
    guildname = _clean(profile.get("guildname"), "")

    _require_pillow()
    avatar = _fit_headpic_icon(_fetch_image(headpicid, "headpic"), (400, 400))
    banner = _crop_banner(_fetch_image(bannerid, "banner"), (1466, 400))

    if prime == 8:
        frame = _load_prime8_frame()
        if frame:
            avatar = Image.alpha_composite(avatar, frame.resize((400,400), Image.LANCZOS))

    canvas = Image.new("RGBA", (1866, 400), (0,0,0,255))
    canvas.alpha_composite(avatar, (0, 0))
    canvas.alpha_composite(banner, (400, 0))

    # dark overlay on text area
    shade = Image.new("RGBA", (1466, 400), (0, 0, 0, 55))
    canvas.alpha_composite(shade, (400, 0))

    draw = ImageDraw.Draw(canvas)

    pin = _prepare_pin_icon(_fetch_image(pinid, "pin"), (82, 82))
    if pin:
        canvas.alpha_composite(pin, (-10, 322))

    prime_img = _load_prime(prime)
    if prime_img and prime > 0:
        canvas.alpha_composite(prime_img.resize((76, 76), Image.LANCZOS), (352, -14))

    name_font = _fit_font(draw, playername, 930, 112, 46)
    guild_font = _fit_font(draw, guildname, 930, 80, 34)
    level_font = _load_font(42)

    _draw_text(draw, (485, 56), playername, name_font, stroke_width=5)
    if guildname:
        _draw_text(draw, (485, 235), guildname, guild_font, stroke_width=4)

    lvl_text = f"Lvl.{level}"
    try:
        bbox = draw.textbbox((0,0), lvl_text, font=level_font, stroke_width=3)
        tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
        _draw_text(draw, (1866 - tw - 24, 400 - th - 28), lvl_text, level_font, stroke_width=3)
    except Exception:
        pass

    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()

def _host_url(path, **params):
    base = request.host_url.rstrip("/")
    query = urlencode({k:v for k,v in params.items() if v not in (None, "")})
    return f"{base}{path}" + (f"?{query}" if query else "")

def _profile_with_urls(profile):
    p = {k:v for k,v in profile.items() if v not in (None, "", {}, [])}
    p["icon_urls"] = {}
    for label, key in [("banner", "bannerid"), ("headpic", "headpicid"), ("pin", "pinid")]:
        url = _icon_url(p.get(key))
        if url:
            p["icon_urls"][label] = url
    if p.get("prime") not in (None, "", 0, "0"):
        p["icon_urls"]["prime"] = _host_url(f"/banner_asset/prime{p.get('prime')}.png")
    return p

@banner_bp.route("/banner_asset/<name>", methods=["GET"])
def banner_asset(name):
    if not re.fullmatch(r"prime[0-8]\.png|prime8frame\.png", name):
        return jsonify({"error": "invalid asset"}), 400
    path = os.path.join(ASSET_DIR, name)
    if not os.path.exists(path):
        return jsonify({"error": "asset not found"}), 404
    with open(path, "rb") as f:
        return Response(f.read(), mimetype="image/png")

@banner_bp.route("/banner/get_personal_show", methods=["GET"])
def banner_get_personal_show():
    uid = resolve_uid_from_request()
    if not uid:
        return jsonify({"status":"error", "message":"Provide uid, token/jwt, access_token, or guest_uid+password"}), 400
    try:
        extracted = fetch_profile_by_uid(uid)
        return jsonify({
            "status": "success",
            "profile": _profile_with_urls(extracted["profile"]),
            "rawInfo": extracted["data"]
        })
    except Exception as e:
        return jsonify({"status":"error", "message":str(e)}), 500

@banner_bp.route("/banner/all", methods=["GET"])
def banner_all():
    uid = resolve_uid_from_request()
    if not uid:
        return jsonify({"status":"error", "message":"Provide uid, token/jwt, access_token, or guest_uid+password"}), 400
    try:
        extracted = fetch_profile_by_uid(uid)
        profile = _profile_with_urls(extracted["profile"])
        profile["banner_image"] = _host_url("/onlybanner/all", uid=uid, server=profile.get("server"))
        return jsonify({
            "status": "success",
            "profile": profile,
            "endpoints": {
                "get_personal_show": _host_url("/banner/get_personal_show", uid=uid, server=profile.get("server")),
                "banner_png": profile["banner_image"],
                "custom_from_uid": _host_url("/cbanner/all", uid=uid, server=profile.get("server")),
                "custom_full": _host_url(
                    "/cfullbanner",
                    bannerid=profile.get("bannerid"),
                    headpicid=profile.get("headpicid"),
                    pinid=profile.get("pinid"),
                    prime=profile.get("prime"),
                    playername=profile.get("playernickname"),
                    guildname=profile.get("guildname"),
                    level=profile.get("level")
                )
            }
        })
    except Exception as e:
        return jsonify({"status":"error", "message":str(e)}), 500

@banner_bp.route("/onlybanner/all", methods=["GET"])
def only_banner_all():
    uid = resolve_uid_from_request()
    if not uid:
        return jsonify({"status":"error", "message":"Provide uid, token/jwt, access_token, or guest_uid+password"}), 400
    try:
        extracted = fetch_profile_by_uid(uid)
        img = render_profile_banner(extracted["profile"])
        return Response(img, mimetype="image/png")
    except Exception as e:
        return jsonify({"status":"error", "message":str(e)}), 500

@banner_bp.route("/cbanner/all", methods=["GET"])
def cbanner_all():
    uid = resolve_uid_from_request()
    if not uid:
        return jsonify({"status":"error", "message":"Provide uid, token/jwt, access_token, or guest_uid+password"}), 400
    try:
        extracted = fetch_profile_by_uid(uid)
        profile = extracted["profile"]
        for key, aliases in {
            "bannerid": ["bannerid", "bannerId"],
            "headpicid": ["headpicid", "headPicId", "avatarid"],
            "pinid": ["pinid", "pinId"],
            "prime": ["prime", "prime_level", "primeLevel"],
        }.items():
            for a in aliases:
                if request.args.get(a) not in (None, ""):
                    profile[key] = request.args.get(a)
                    break
        img = render_profile_banner(profile)
        return Response(img, mimetype="image/png")
    except Exception as e:
        return jsonify({"status":"error", "message":str(e)}), 500


@banner_bp.route("/banner/test_png", methods=["GET"])
def banner_test_png():
    pngid = request.args.get("pngid") or request.args.get("id")
    kind = request.args.get("kind") or "icon"
    if not pngid:
        return jsonify({"status": "error", "message": "Provide pngid"}), 400
    url = _icon_url(pngid)
    img = _fetch_image(pngid, kind)
    if img is None:
        return jsonify({"status": "error", "pngid": pngid, "url": url, "message": "PNG not found or not image"}), 404
    out = io.BytesIO()
    img.save(out, "PNG")
    return Response(out.getvalue(), mimetype="image/png", headers={"X-PNG-Source": url or ""})

@banner_bp.route("/cfullbanner", methods=["GET"])
def cfullbanner():
    profile = {
        "bannerid": request.args.get("bannerid") or request.args.get("bannerId"),
        "headpicid": request.args.get("headpicid") or request.args.get("headPicId") or request.args.get("avatarid"),
        "pinid": request.args.get("pinid") or request.args.get("pinId"),
        "prime": request.args.get("prime") or request.args.get("prime_level") or request.args.get("primeLevel") or 0,
        "playernickname": request.args.get("playername") or request.args.get("playerName") or request.args.get("nickname") or "Unknown",
        "guildname": request.args.get("guildname") or request.args.get("guildName") or request.args.get("clanName") or "",
        "level": request.args.get("level") or "0",
    }
    if not profile.get("bannerid") and not profile.get("headpicid"):
        return jsonify({"status":"error", "message":"Provide at least bannerid or headpicid"}), 400
    return Response(render_profile_banner(profile), mimetype="image/png")


# ---------------------------------------------------------------------------
# Outfit preview renderer
# GET /outfit/all?uid=<player_uid>&server=IND
# avatarId alone uses the cucivk PNG API. Every other outfit PNG ID uses the
# ultimates_png CDN, including clothes and weaponSkinShows.
# ---------------------------------------------------------------------------
OUTFIT_BG_PATH = os.path.join(ASSET_DIR, "outfitbanner_template.png")
WEAPON_PNG_BASE = "https://cdn.jsdelivr.net/gh/bot1912545-netizen/ultimates_png@main/PNG/{id}.png"

def _fetch_outfit_png(png_id, avatar=False, weapon=False):
    """Fetch avatarId from cucivk; fetch every other outfit ID from the CDN.

    `weapon` is retained as a call-site hint for the showcase cleanup logic;
    it does not choose the image host.
    """
    png_id = _normalize_png_id(png_id)
    if not png_id:
        return None
    urls = [_icon_url(png_id)] if avatar else [WEAPON_PNG_BASE.format(id=png_id)]
    for url in urls:
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=12, headers={"User-Agent":"Mozilla/5.0", "Accept":"image/png,image/*"})
            if resp.status_code == 200 and resp.content:
                im = Image.open(io.BytesIO(resp.content)).convert("RGBA")
                im.load()
                return im
        except Exception:
            pass
    return None

def _strip_showcase_color_bar(im):
    """Remove CDN preview bars and connected purple/dark square-tile backgrounds.

    The CDN may return a complete inventory tile rather than a transparent item.
    Detect the horizontal rarity strip first, then remove only background-colored
    pixels connected to the image edges so similarly colored details inside the
    actual item are preserved.
    """
    if im is None:
        return None
    im = im.convert("RGBA")
    w, h = im.size
    if w < 8 or h < 8:
        return im

    from collections import deque
    px = im.load()

    # Find a saturated, mostly horizontal rarity bar near the bottom. Crop at
    # the start of the run rather than trimming a fixed percentage of artwork.
    scan_start = max(0, int(h * 0.78))
    bar_rows = []
    for y in range(scan_start, h):
        colored = 0
        vivid_pink = 0
        opaque = 0
        for x in range(0, w, max(1, w // 120)):
            r, g, b, a = px[x, y]
            if a > 80:
                opaque += 1
                if max(r, g, b) - min(r, g, b) > 45 and max(r, g, b) > 95:
                    colored += 1
                if r > 100 and b > 100 and g < 125 and r > g * 1.25 and b > g * 1.15:
                    vivid_pink += 1
        if opaque and (vivid_pink / opaque >= 0.20 or colored / opaque >= 0.72):
            bar_rows.append(y)

    # Only crop when the detected strip touches the bottom and spans several
    # adjacent rows, preventing accidental cropping of colorful item artwork.
    if bar_rows and bar_rows[-1] >= h - 2:
        run_start = bar_rows[-1]
        for y in range(bar_rows[-1] - 1, scan_start - 1, -1):
            if y in set(bar_rows):
                run_start = y
            else:
                break
        if h - run_start >= max(2, int(h * 0.012)):
            im = im.crop((0, 0, w, max(1, run_start)))
            w, h = im.size
            px = im.load()

    # Flood-fill only edge-connected dark/purple/magenta tile pixels. This
    # removes square corners while preserving disconnected dark details in the
    # item itself. A small color tolerance handles gradients and compression.
    seen = bytearray(w * h)
    queue = deque()

    def is_tile_background(x, y):
        r, g, b, a = px[x, y]
        if a <= 8:
            return True
        dark = max(r, g, b) < 58
        purple = r > 35 and b > 38 and r > g * 1.18 and b > g * 1.12 and g < 105
        magenta = r > 90 and b > 90 and g < 115 and r > g * 1.25 and b > g * 1.15
        return dark or purple or magenta

    for x in range(w):
        queue.append((x, 0))
        queue.append((x, h - 1))
    for y in range(h):
        queue.append((0, y))
        queue.append((w - 1, y))

    while queue:
        x, y = queue.popleft()
        idx = y * w + x
        if seen[idx]:
            continue
        seen[idx] = 1
        if not is_tile_background(x, y):
            continue
        r, g, b, _a = px[x, y]
        px[x, y] = (r, g, b, 0)
        if x > 0 and not seen[idx - 1]: queue.append((x - 1, y))
        if x + 1 < w and not seen[idx + 1]: queue.append((x + 1, y))
        if y > 0 and not seen[idx - w]: queue.append((x, y - 1))
        if y + 1 < h and not seen[idx + w]: queue.append((x, y + 1))

    bbox = im.getchannel("A").getbbox()
    return im.crop(bbox) if bbox else im

def _contain_rgba(im, box_w, box_h, padding=0.08):
    if im is None:
        return None
    im = im.convert("RGBA")
    bbox = im.getchannel("A").getbbox()
    if bbox:
        im = im.crop(bbox)
    if im.width < 1 or im.height < 1:
        return None
    scale = min((box_w*(1-padding))/im.width, (box_h*(1-padding))/im.height)
    size = (max(1, int(im.width*scale)), max(1, int(im.height*scale)))
    return im.resize(size, Image.Resampling.LANCZOS)

def _paste_center(canvas, im, center, box, padding=0.08):
    fitted = _contain_rgba(im, box[0], box[1], padding)
    if fitted is not None:
        canvas.alpha_composite(fitted, (int(center[0]-fitted.width/2), int(center[1]-fitted.height/2)))

def _paste_hex_center(canvas, im, center, box, padding=0.04):
    """Fit transparent artwork into a true hexagonal viewport."""
    fitted = _contain_rgba(im, box[0], box[1], padding)
    if fitted is None:
        return
    layer = Image.new("RGBA", box, (0, 0, 0, 0))
    layer.alpha_composite(fitted, ((box[0] - fitted.width) // 2,
                                   (box[1] - fitted.height) // 2))
    mask = Image.new("L", box, 0)
    md = ImageDraw.Draw(mask)
    bw, bh = box
    # Flat-top hexagon, matching the six-sided slots in the reference layout.
    md.polygon([(bw * 0.25, 0), (bw * 0.75, 0), (bw, bh * 0.5),
                (bw * 0.75, bh), (bw * 0.25, bh), (0, bh * 0.5)], fill=255)
    layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", box, 0), mask))
    canvas.alpha_composite(layer, (int(center[0] - bw / 2), int(center[1] - bh / 2)))


def _draw_outfit_hex(canvas, center, png_id, weapon=False, label=""):
    """Render any non-avatar CDN item without its inventory tile or color bar."""
    im = _fetch_outfit_png(png_id, weapon=weapon)
    if im is not None:
        # Clothes and weaponSkinShows both arrive as square inventory tiles on
        # some IDs, so apply cleanup to both categories, not just weapons.
        im = _strip_showcase_color_bar(im)
        _paste_hex_center(canvas, im, center, (98, 108), padding=0.035)

@banner_bp.route("/outfit/all", methods=["GET"])
def outfit_all():
    _require_pillow()
    uid = (request.args.get("uid") or "").strip()
    if not uid or not uid.isdigit() or int(uid) <= 0:
        return jsonify({"status": "error", "message": "Provide a valid numeric uid: /outfit/all?uid=123"}), 400

    try:
        extracted = fetch_profile_by_uid(uid)
        data = extracted.get("data") or {}
        basic = data.get("basicInfo") or data.get("basic_info") or data.get("basicinfo") or {}
        profile = data.get("profileInfo") or data.get("profile_info") or data.get("profileinfo") or {}
        if not os.path.isfile(OUTFIT_BG_PATH):
            return jsonify({"status": "error", "message": "Outfit background asset is missing"}), 500

        # Keep the background's native 1536x865 ratio. Cropping it into a 3:2
        # canvas caused the boxes and their contents to drift out of alignment.
        bg = Image.open(OUTFIT_BG_PATH).convert("RGBA")
        canvas = bg.copy()
        w, h = canvas.size

        # Centers are measured against the pre-implemented hexagons in the
        # supplied background (left: hair/facepaint/mask/gun/entry; right:
        # top/bottom/shoes/look-changer). No extra boxes or connector lines.
        left_nodes = [(302, 125), (180, 275), (180, 475), (302, 625), (430, 720)]
        right_nodes = [(1230, 125), (1350, 275), (1350, 475), (1230, 625)]

        # Character stays centered and is scaled to fit without clipping.
        avatar_id = profile.get("avatarId", profile.get("avatar_id"))
        character = _fetch_outfit_png(avatar_id, avatar=True)
        if character is not None:
            _paste_center(canvas, character, (w // 2, int(h * 0.51)),
                          (470, int(h * 0.88)), padding=0.015)

        clothes = profile.get("clothes") or []
        clothes = [v for v in clothes if _normalize_png_id(v)][:6]
        # All clothes IDs are fetched from the ultimates_png CDN. Clothes IDs
        # conventionally arrive as hair, face/mask/tattoo, top,
        # bottom, shoes. Place the first three on the left and remaining three
        # on the right; empty slots stay empty rather than showing fake labels.
        clothes_slots = [left_nodes[0], left_nodes[1], left_nodes[2],
                         right_nodes[0], right_nodes[1], right_nodes[2]]
        for i, png_id in enumerate(clothes):
            _draw_outfit_hex(canvas, clothes_slots[i], png_id, weapon=False)

        # weaponSkinShows also use the dedicated CDN. Their conventional
        # order is gun/weapon skin, entry animation, look changer. The first two
        # belong on the left; look changer belongs on the right.
        showcase = basic.get("weaponSkinShows") or basic.get("weapon_skin_shows") or []
        showcase = [v for v in showcase if _normalize_png_id(v)]
        showcase_slots = [left_nodes[3], left_nodes[4], right_nodes[3]]
        for i, png_id in enumerate(showcase[:3]):
            _draw_outfit_hex(canvas, showcase_slots[i], png_id, weapon=True)

        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        out.seek(0)
        return Response(out.getvalue(), mimetype="image/png")
    except Exception as e:
        current_app.logger.exception("Outfit renderer failed for uid=%s", uid)
        return jsonify({"status": "error", "message": str(e)}), 500
