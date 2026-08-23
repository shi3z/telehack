"""LiveKit 連携: アクセストークン発行・ルーム状態取得・録画(Egress)制御"""
import datetime
import logging
import time

from livekit import api

import config

log = logging.getLogger("telehack.livekit")

_lkapi: api.LiveKitAPI | None = None


def get_api() -> api.LiveKitAPI:
    global _lkapi
    if _lkapi is None:
        _lkapi = api.LiveKitAPI(
            config.LIVEKIT_URL, config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET
        )
    return _lkapi


async def close_api():
    global _lkapi
    if _lkapi is not None:
        await _lkapi.aclose()
        _lkapi = None


def create_join_token(room: str, identity: str, name: str, ttl_hours: int = 6) -> str:
    return (
        api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(name)
        .with_ttl(datetime.timedelta(hours=ttl_hours))
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )


async def list_active_rooms() -> dict[str, int]:
    """稼働中ルーム名 -> 参加者数"""
    res = await get_api().room.list_rooms(api.ListRoomsRequest())
    return {r.name: r.num_participants for r in res.rooms}


async def list_online_identities() -> dict[str, str]:
    """全ルームの在室者: identity(=メールアドレス) -> ルーム名"""
    online: dict[str, str] = {}
    res = await get_api().room.list_rooms(api.ListRoomsRequest())
    for r in res.rooms:
        if r.num_participants == 0:
            continue
        ps = await get_api().room.list_participants(
            api.ListParticipantsRequest(room=r.name)
        )
        for p in ps.participants:
            online[p.identity] = r.name
    return online


async def start_room_recording(room_name: str) -> tuple[str, str]:
    """ルームのグリッド合成録画を開始し (egress_id, ファイル名) を返す"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{room_name}-{ts}.mp4"
    req = api.RoomCompositeEgressRequest(
        room_name=room_name,
        layout="grid",
        file_outputs=[
            api.EncodedFileOutput(
                file_type=api.EncodedFileType.MP4,
                filepath=f"{config.EGRESS_OUT_DIR}/{filename}",
            )
        ],
    )
    info = await get_api().egress.start_room_composite_egress(req)
    log.info("録画開始 room=%s egress=%s file=%s", room_name, info.egress_id, filename)
    return info.egress_id, filename


async def stop_recording(egress_id: str):
    await get_api().egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
    log.info("録画停止 egress=%s", egress_id)


def make_webhook_receiver() -> api.WebhookReceiver:
    return api.WebhookReceiver(
        api.TokenVerifier(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
    )
