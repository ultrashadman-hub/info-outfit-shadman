"""Compatibility adapter for the supplied JXE OB55 Info API."""
import asyncio
import app as _info

def get_garena_token(uid, password):
    try:
        creds, err = asyncio.run(_info.oauth_guest(str(uid), str(password)))
        if err or not creds:
            return None
        open_id, access_token = creds
        return {"open_id": open_id, "access_token": access_token}
    except Exception:
        return None

def get_major_login(access_token, open_id):
    try:
        result, err = asyncio.run(_info.major_login_full(str(open_id), str(access_token)))
        if err or not result:
            return None
        return result
    except Exception:
        return None

def get_open_id_from_access_token(access_token):
    # The supplied source does not include a standalone access-token lookup.
    return None
