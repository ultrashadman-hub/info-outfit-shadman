# ===========================================================
#  Free Fire Info API + JWT Generator (Vercel-ready)
#  ✅ Output 100% same as before (sorted keys)
#  ✅ No external JWT API
#  Credit: @Jahid_x_Empire
# ===========================================================

import asyncio
import json
import uuid
import time
import sys
import os
from datetime import datetime, timedelta

import httpx
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf import json_format
from google.protobuf.internal.decoder import _DecodeVarint, _DecodeVarint32

# ---------- Proto imports ----------
try:
    import FreeFire_pb2, main_pb2, AccountPersonalShow_pb2
    import GetOutfit_pb2
    print("✅ Proto files imported successfully")
except ImportError as e:
    print(f"❌ Proto import error: {e}")
    sys.exit(1)

try:
    import jwt as pyjwt
    HAS_JWT = True
except ImportError:
    HAS_JWT = False

# ---------- JSON output (SORTED KEYS → same as Flask jsonify) ----------
try:
    import orjson
    def _jsonify(data, status=200):
        return Response(
            orjson.dumps(data, option=orjson.OPT_SORT_KEYS),
            status=status,
            mimetype='application/json'
        )
except ImportError:
    def _jsonify(data, status=200):
        return Response(
            json.dumps(data, separators=(',', ':'), ensure_ascii=False, sort_keys=True),
            status=status,
            mimetype='application/json'
        )


app = Flask(__name__)
CORS(app)

# ===========================================================
# CONFIG
# ===========================================================
MAIN_KEY = b'Yg&tc%DEuh6%Zc^8'
MAIN_IV  = b'6oyZDr22E3ychjM%'

USERAGENT = "Dalvik/2.1.0 (Linux; U; Android 14; CPH2095 Build/RKQ1.211119.001)"
LOGIN_URL = "https://loginbp.ppmainecoonghj.com"
OB_VERSION     = "OB55"
CLIENT_VERSION = "1.132.1"

# ===========================================================
# CREDENTIALS
# ===========================================================
ACCOUNT_CREDENTIALS = {
    region: {"uid": os.getenv(f"FF_{region}_UID", "").strip(),
             "password": os.getenv(f"FF_{region}_PASSWORD", "").strip()}
    for region in ("BD", "IND", "BR")
    if os.getenv(f"FF_{region}_UID", "").strip()
    and os.getenv(f"FF_{region}_PASSWORD", "").strip()
}

# BD default guest account (environment variables FF_BD_UID / FF_BD_PASSWORD override this)
ACCOUNT_CREDENTIALS.setdefault("BD", {
    "uid": "8003090050",
    "password": "90AC1FB5340E3E5AB8F8F071B0588B9702CC5228973A676446A23586479EDC47",
})
ACCOUNT_CREDENTIALS.setdefault("IND", {
    "uid": "4542695698",
    "password": "5350DEC06133B573880FA00A692A3186D772E38B0",
})

REGION_CONFIG = {
    "BD":  {"server_url": "https://clientbp.ppmainecoonghj.com",   "release_version": "OB55"},
    "IND": {"server_url": "https://client.ind.freefiremobile.com", "release_version": "OB55"},
    "BR":  {"server_url": "https://client.us.freefiremobile.com",  "release_version": "OB55"},
}
REGION_PRIORITY = ["BD", "IND", "BR"]

# in-memory token cache (per serverless warm container)
_token_cache = {}

# ===========================================================
# AES HELPERS
# ===========================================================
def pad_text(text: bytes) -> bytes:
    padding_length = AES.block_size - (len(text) % AES.block_size)
    return text + bytes([padding_length] * padding_length)


def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pad_text(plaintext))


def encrypt_message(plaintext: bytes) -> bytes:
    return aes_cbc_encrypt(MAIN_KEY, MAIN_IV, plaintext)


# ===========================================================
# MANUAL PROTOBUF (MajorLogin payload)
# ===========================================================
def encode_varint(value):
    out = []
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def build_payload_from_dict(fields_dict):
    payload = b''
    for key, value in sorted(fields_dict.items()):
        field_num = int(key)
        if isinstance(value, bool):
            payload += encode_varint((field_num << 3) | 0) + encode_varint(1 if value else 0)
        elif isinstance(value, int):
            payload += encode_varint((field_num << 3) | 0) + encode_varint(value)
        elif isinstance(value, str):
            data = value.encode('utf-8')
            payload += encode_varint((field_num << 3) | 2) + encode_varint(len(data)) + data
        elif isinstance(value, bytes):
            payload += encode_varint((field_num << 3) | 2) + encode_varint(len(value)) + value
        elif isinstance(value, dict):
            sub = build_payload_from_dict(value)
            payload += encode_varint((field_num << 3) | 2) + encode_varint(len(sub)) + sub
        else:
            raise TypeError(f"Unsupported type for field {field_num}")
    return payload


def decode_protobuf(data):
    pos = 0
    length = len(data)
    fields = {}
    while pos < length:
        key, pos = _DecodeVarint(data, pos)
        field_number = key >> 3
        wire_type = key & 7
        if wire_type == 0:
            value, pos = _DecodeVarint(data, pos)
        elif wire_type == 2:
            size, pos = _DecodeVarint32(data, pos)
            raw = data[pos:pos + size]
            pos += size
            try:
                value = decode_protobuf(raw)
            except Exception:
                value = raw
        elif wire_type == 5:
            value = int.from_bytes(data[pos:pos + 4], 'little')
            pos += 4
        elif wire_type == 1:
            value = int.from_bytes(data[pos:pos + 8], 'little')
            pos += 8
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")

        if field_number in fields:
            if not isinstance(fields[field_number], list):
                fields[field_number] = [fields[field_number]]
            fields[field_number].append(value)
        else:
            fields[field_number] = value
    return fields


def build_major_login_payload(open_id, access_token):
    fields = {
        3: str(datetime.now())[:-7],
        4: "free fire",
        5: 2,
        7: f"{CLIENT_VERSION}",
        8: "Android OS 11 / API-30 (RQ3A.210805.001)",
        9: "Handheld",
        10: "Verizon",
        11: "WIFI",
        12: 1080,
        13: 2400,
        14: "440",
        15: "ARMv8",
        16: 6144,
        17: "Adreno (TM) 650",
        18: "OpenGL ES 3.2 V@1.50",
        19: f"Google|{uuid.uuid4()}",
        20: "",
        21: "en",
        22: open_id,
        23: "4",
        24: "Handheld",
        25: {6: 55, 8: 81},
        29: access_token,
        30: 2,
        41: "Verizon",
        42: "WIFI",
        57: "7428b253defc164018c604a1ebbfebdf",
        60: 128512, 61: 38000, 62: 110731, 63: 18000, 64: 18000, 65: 26628,
        66: 25000, 67: 119234, 73: 3,
        74: "/data/app/~~random/base.apk",
        76: 1, 77: "hash|base.apk", 78: 3, 79: 2, 81: "64", 83: "2024010012",
        86: "OpenGLES3", 87: 16383, 88: 4,
        89: b"FwQVTgUPX1UaUllDDwcWCRBpWAUOUgsvA1snWlBaO1kFYg==",
        92: 9000, 93: "android", 94: "", 95: 110009, 97: 1, 98: 0, 99: "4", 100: "4",
    }
    return build_payload_from_dict(fields)


# ===========================================================
# OAUTH
# ===========================================================
OAUTH_HEADERS = {
    "User-Agent": "GarenaMSDK/5.5.0P1(SM-G998B;Android 13;en-US;USA;)",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "close",
}
OAUTH_PAYLOAD_TEMPLATE = {
    "response_type": "token",
    "client_type": "2",
    "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
    "client_id": "100067",
}


async def oauth_guest(uid, password):
    payload = OAUTH_PAYLOAD_TEMPLATE.copy()
    payload["uid"] = uid
    payload["password"] = password
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://100067.connect.garena.com/oauth/guest/token/grant",
                data=payload, headers=OAUTH_HEADERS
            )
    except Exception as e:
        return None, f"OAuth request error: {e}"

    if r.status_code != 200:
        return None, f"OAuth HTTP {r.status_code}: {r.text[:200]}"
    try:
        j = r.json()
    except ValueError:
        return None, "OAuth invalid JSON"

    open_id = j.get("open_id")
    access_token = j.get("access_token")
    if not open_id or not access_token:
        return None, f"OAuth missing fields: keys={list(j.keys())}"
    return (open_id, access_token), None


# ===========================================================
# MAJOR LOGIN
# ===========================================================
MAJORLOGIN_HEADERS = {
    "User-Agent": "UnityPlayer/2021.3.14f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)",
    "Accept": "*/*",
    "Accept-Encoding": "deflate, gzip",
    "X-Ga-Sv": "1789534056",
    "Authorization": "Bearer ",
    "X-Ga": "v1 1",
    "ReleaseVersion": f"{OB_VERSION}",
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Unity-Version": "2021.3.14f1",
}


async def major_login_full(open_id, access_token):
    if not access_token or not access_token.strip():
        return None, "Missing access_token for MajorLogin"

    plain = build_major_login_payload(open_id, access_token)
    encrypted = encrypt_message(plain)

    headers = MAJORLOGIN_HEADERS.copy()
    headers["Authorization"] = f"Bearer {access_token.strip()}"

    url = LOGIN_URL.rstrip('/') + "/MajorLogin"
    last_error = ""

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                r = await client.post(url, content=encrypted, headers=headers)

            if r.status_code == 200:
                body = r.content
                if len(body) > 64:
                    try:
                        d0 = decode_protobuf(body)
                        if 8 not in d0:
                            body = body[64:]
                    except Exception:
                        body = body[64:]

                decoded = decode_protobuf(body)

                jwt_token = decoded.get(8)
                if isinstance(jwt_token, bytes):
                    jwt_token = jwt_token.decode('utf-8', errors='replace')

                client_url = decoded.get(10)
                if isinstance(client_url, bytes):
                    client_url = client_url.decode('utf-8', errors='replace')
                if client_url and not client_url.startswith(('http://', 'https://')):
                    client_url = 'https://' + client_url

                if jwt_token and client_url:
                    lock_region = None
                    if HAS_JWT:
                        try:
                            payload_dec = pyjwt.decode(jwt_token,
                                                       options={"verify_signature": False})
                            lock_region = payload_dec.get("lock_region")
                        except Exception:
                            pass

                    return {
                        "token":      jwt_token,
                        "lockRegion": lock_region,
                        "serverUrl":  client_url,
                    }, None

                last_error = "Missing JWT or client URL (Field 8/10)"
            else:
                last_error = f"HTTP {r.status_code}: {r.content[:150]!r}"
        except Exception as e:
            last_error = f"Request error: {e}"

        print(f"[!] MajorLogin Attempt {attempt+1} failed: {last_error}")
        await asyncio.sleep(1)

    return None, f"MajorLogin failed after 3 attempts: {last_error}"


def decode_jwt(token):
    if not HAS_JWT or not token:
        return {}
    try:
        return pyjwt.decode(token, options={"verify_signature": False})
    except Exception:
        return {}


# ===========================================================
# TOKEN MANAGER (cache)
# ===========================================================
async def generate_token_direct(region):
    cred = ACCOUNT_CREDENTIALS.get(region)
    if not cred:
        return None

    creds, err = await oauth_guest(cred['uid'], cred['password'])
    if err:
        print(f"❌ OAuth error ({region}): {err}")
        return None
    open_id, access_token = creds

    ml, err = await major_login_full(open_id, access_token)
    if err:
        print(f"❌ MajorLogin error ({region}): {err}")
        return None

    return {
        'token':      f"Bearer {ml['token']}",
        'region':     ml.get('lockRegion') or region,
        'server_url': ml.get('serverUrl') or REGION_CONFIG[region]['server_url'],
        'expires_at': time.time() + 25200,   # 7 hours
    }


async def get_token(region):
    cached = _token_cache.get(region)
    if cached and cached.get('expires_at', 0) > time.time():
        return cached
    token_info = await generate_token_direct(region)
    if token_info:
        _token_cache[region] = token_info
        return token_info
    return None


# ===========================================================
# ACCOUNT INFO FETCH
# ===========================================================
async def GetAccountInformation(uid, region):
    try:
        token_info = await get_token(region)
        if not token_info:
            return None

        actual_region = token_info.get('region', region)
        token = token_info['token']
        server_url = token_info['server_url']
        config = REGION_CONFIG.get(actual_region, REGION_CONFIG["BD"])

        req_msg = main_pb2.GetPlayerPersonalShow()
        json_format.ParseDict({'a': uid, 'b': '7'}, req_msg)
        data_enc = aes_cbc_encrypt(MAIN_KEY, MAIN_IV, req_msg.SerializeToString())

        headers = {
            'User-Agent': USERAGENT,
            'Connection': "Keep-Alive",
            'Accept-Encoding': "gzip",
            'Content-Type': "application/octet-stream",
            'Authorization': token,
            'X-Unity-Version': "2018.4.11f1",
            'X-GA': "v1 1",
            'ReleaseVersion': config['release_version'],
        }

        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            resp = await client.post(server_url + '/GetPlayerPersonalShow',
                                     content=data_enc, headers=headers)

        if resp.status_code != 200:
            return None

        account_info = AccountPersonalShow_pb2.AccountPersonalShowInfo()
        account_info.ParseFromString(resp.content)
        result = json.loads(json_format.MessageToJson(account_info))

        is_banned = result.get("isBanned", False)
        if isinstance(is_banned, bool):
            result["ban_status"] = "🔴 BANNED" if is_banned else "🟢 UNBANNED"
        else:
            result["ban_status"] = "❓ UNKNOWN"

        result["region"] = actual_region
        return result
    except Exception as e:
        print(f"❌ GetAccountInformation error ({region}): {e}")
        return None


# ===========================================================
# HELPERS
# ===========================================================
def get_item_name(item_id):
    if not item_id or item_id == "0" or item_id == 0:
        return "N/A"
    try:
        with httpx.Client(timeout=3.0) as client:
            r = client.get(f"https://api.danger.workers.dev/item/{item_id}")
            if r.status_code == 200:
                data = r.json()
                return data.get("name", str(item_id))
            return str(item_id)
    except Exception:
        return str(item_id)


def get_rank_name(rp):
    try:
        rp = int(rp)
    except Exception:
        return "N/A"
    if rp == 0: return "Bronze I"
    if rp < 100: return "Bronze II"
    if rp < 200: return "Bronze III"
    if rp < 300: return "Silver I"
    if rp < 400: return "Silver II"
    if rp < 500: return "Silver III"
    if rp < 600: return "Gold I"
    if rp < 700: return "Gold II"
    if rp < 800: return "Gold III"
    if rp < 900: return "Platinum I"
    if rp < 1000: return "Platinum II"
    if rp < 1100: return "Platinum III"
    if rp < 1200: return "Diamond I"
    if rp < 1300: return "Diamond II"
    if rp < 1400: return "Diamond III"
    if rp < 1500: return "Heroic"
    if rp < 2000: return "Master"
    return "Grandmaster"


def ts_to_bst(ts):
    try:
        dt = datetime.fromtimestamp(int(ts)) + timedelta(hours=6)
        return dt.strftime("%d %b %Y at %I:%M:%S %p") + " (BST)"
    except Exception:
        return "N/A"


# ===========================================================
# /info  —  SAME OUTPUT AS BEFORE (SORTED KEYS)
# ===========================================================
@app.route('/info')
def get_full_info():
    uid = request.args.get('uid')
    if not uid:
        return _jsonify({"error": "UID required"}, 400)
    try:
        uid_int = int(uid)
    except Exception:
        return _jsonify({"error": "Invalid UID"}, 400)

    async def try_all_regions_parallel():
        tasks = [asyncio.create_task(GetAccountInformation(uid_int, r))
                 for r in REGION_PRIORITY]
        try:
            for coro in asyncio.as_completed(tasks, timeout=20):
                try:
                    data = await coro
                    if data:
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                        return data
                except Exception:
                    continue
        except asyncio.TimeoutError:
            pass

        for t in tasks:
            if not t.done():
                t.cancel()
        return None

    try:
        account_data = asyncio.run(try_all_regions_parallel())
    except Exception as e:
        print(f"❌ Global error: {e}")
        account_data = None

    if not account_data:
        return _jsonify({"error": "Player not found"}, 404)

    used_region = account_data.get("region", "Unknown")

    basic   = account_data.get("basicInfo", {})
    clan    = account_data.get("clanBasicInfo", {})
    social  = account_data.get("socialInfo", {})
    pet     = account_data.get("petInfo", {})
    captain = account_data.get("captainBasicInfo", {})
    credit  = account_data.get("creditScoreInfo", {})

    prime_level = "N/A"
    try:
        prime_data = basic.get("primeLevel")
        if isinstance(prime_data, dict):
            prime_level = prime_data.get("level", "N/A")
        elif prime_data is not None:
            prime_level = str(prime_data)
    except Exception:
        prime_level = "N/A"

    response = {
        "status": "success",
        "server_used": used_region,
        "BanStatus": account_data.get("ban_status", "❓ UNKNOWN"),
        "BasicInformation": {
            "PrimeLevel": prime_level,
            "Name": basic.get("nickname", "N/A"),
            "UID": uid,
            "Level": basic.get("level", "N/A"),
            "Exp": basic.get("exp", "N/A"),
            "Region": basic.get("region", "N/A"),
            "Likes": basic.get("liked", "N/A"),
            "HonorScore": credit.get("creditScore", "N/A"),
            "CelebrityStatus": "Yes" if basic.get("showBrRank") else "No",
            "Title": get_item_name(basic.get("title", "0")),
            "Signature": social.get("signature", "N/A"),
        },
        "ActivityInformation": {
            "MostRecentOB": basic.get("releaseVersion", "N/A"),
            "BooyahPass": "Yes" if basic.get("hasElitePass") else "No",
            "CurrentBpBadges": basic.get("badgeCnt", "N/A"),
            "BRRank": get_rank_name(basic.get("rankingPoints", 0)),
            "BRPoints": basic.get("rankingPoints", 0),
            "ShowBRRank": "True" if basic.get("showBrRank") else "False",
            "ShowCSRank": "True" if basic.get("showCsRank") else "False",
            "CreatedAt": ts_to_bst(basic.get("createAt", 0)),
            "LastLogin": ts_to_bst(basic.get("lastLoginAt", 0)),
        },
        "GuildInformation": {
            "GuildName": clan.get("clanName", "No Guild"),
            "GuildID": clan.get("clanId", "N/A"),
            "GuildLevel": clan.get("clanLevel", "N/A"),
            "LiveMembers": clan.get("memberNum", "N/A"),
            "MaxMembers": clan.get("capacity", "N/A"),
        },
        "PetDetails": {
            "Equipped": "Yes" if pet.get("isSelected") else "No",
            "PetNick": pet.get("name", "N/A"),
            "PetType": get_item_name(pet.get("id", "0")),
            "PetSkill": get_item_name(pet.get("selectedSkillId", "0")),
            "PetSkin": get_item_name(pet.get("skinId", "0")),
            "PetExp": pet.get("exp", "N/A"),
            "PetLevel": pet.get("level", "N/A"),
        },
        "LeaderInformation": {
            "Name": captain.get("nickname", "N/A"),
            "UID": captain.get("accountId", "N/A"),
            "Level": captain.get("level", "N/A"),
            "Region": captain.get("region", "N/A"),
            "BooyahPass": "Yes" if captain.get("hasElitePass") else "No",
            "CreatedAt": ts_to_bst(captain.get("createAt", 0)),
            "LastLogin": ts_to_bst(captain.get("lastLoginAt", 0)),
            "MostRecentOB": captain.get("releaseVersion", "N/A"),
            "Title": get_item_name(captain.get("title", "0")),
            "BpBadges": captain.get("badgeCnt", "N/A"),
            "BRRank": get_rank_name(captain.get("rankingPoints", 0)),
            "BRPoints": captain.get("rankingPoints", 0),
        },
    }

    return _jsonify(response)


# ===========================================================
# /token  —  SAME OUTPUT AS OLD EXTERNAL API
# ===========================================================
def _build_token_response(open_id, access_token, jwt_token):
    decoded = decode_jwt(jwt_token)
    platform_raw = decoded.get("external_type")
    try:
        platform = int(platform_raw) if platform_raw is not None else None
    except (TypeError, ValueError):
        platform = platform_raw

    return {
        "account_id":   decoded.get("account_id"),
        "account_name": decoded.get("nickname"),
        "open_id":      open_id,
        "access_token": access_token,
        "platform":     platform,
        "region":       decoded.get("lock_region"),
        "status":       "success",
        "token":        jwt_token,
    }


async def full_login(uid, password):
    creds, err = await oauth_guest(uid, password)
    if err:
        return None, err
    open_id, access_token = creds

    ml, err = await major_login_full(open_id, access_token)
    if err:
        return None, err

    return _build_token_response(open_id, access_token, ml["token"]), None


@app.route('/token', methods=['GET'])
@app.route('/login', methods=['GET'])
def route_login():
    uid = request.args.get('uid')
    password = request.args.get('password')
    if not uid or not password:
        return _jsonify({"status": "failed", "message": "Missing uid or password"}, 400)
    result, err = asyncio.run(full_login(uid, password))
    if err:
        return _jsonify({"status": "failed", "message": err}, 400)
    return _jsonify(result)


@app.route('/access-jwt', methods=['GET'])
def route_access_jwt():
    access_token = request.args.get('access_token')
    open_id = request.args.get('open_id')
    if not access_token:
        return _jsonify({"status": "failed", "message": "Missing access_token"}, 400)
    if not open_id:
        return _jsonify({"status": "failed", "message": "Missing open_id"}, 400)
    ml, err = asyncio.run(major_login_full(open_id, access_token))
    if err:
        return _jsonify({"status": "failed", "message": err}, 400)
    return _jsonify(_build_token_response(open_id, access_token, ml["token"]))


# ===========================================================
# MISC ROUTES
# ===========================================================
@app.route('/')
def home():
    return _jsonify({
        "status": "running",
        "version": "OB55",
        "endpoint": "/info?uid=UID",
        "example": "/info?uid=2084018498",
        "priority": "BD → IND → BR",
        "credit": "@Itz_Jahid_X"
    })


@app.route('/status')
def token_status():
    status = {}
    for region, info in _token_cache.items():
        expires_in = info['expires_at'] - time.time()
        status[region] = {"has_token": True, "expires_in": f"{expires_in/3600:.1f} hours"}
    return _jsonify({"total_tokens": len(_token_cache), "tokens": status})



# ===========================================================
# Outfit API integration
# ===========================================================
try:
    from BannerApi.banner_routes import banner_bp
    app.register_blueprint(banner_bp)
except Exception as _banner_import_error:
    print(f"Banner API unavailable: {_banner_import_error}")

# ===========================================================
# Vercel entry point
# ===========================================================
handler = app

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5004, debug=False)