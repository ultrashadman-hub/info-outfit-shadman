"""Player-info compatibility adapter.

The bundled Info API currently exposes GetAccountInformation. Its protobuf
response is not guaranteed to include the separate GetOutfit inventory fields.
"""
import asyncio
import app as _info

def get_player_personal_show(server_url, token, uid, *args, **kwargs):
    try:
        region = (kwargs.get("region") or "BD").upper()
        return asyncio.run(_info.GetAccountInformation(int(uid), region))
    except Exception:
        return None
