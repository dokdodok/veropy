import asyncio
import random

from veropy import (
    AudioSourceTrack,
    UserType,
    Vero,
    VoiceRoomEvent,
    VoiceRoomMember,
    VoiceRoomServerEvent,
    VoxConfig,
)

# fill config
DEFAULT_CONFIG = VoxConfig(
    user_id=0,
    device_type="ANDROID_SUB",
    network="WIFI",
    device_lang="ko",
    account_country="KR",
    carrier_id="",  # mccmnc
    model="",
    sdk_version="",
    build_id="",
)

# fill
CHAT_ID = 0
USER_TYPE = UserType.NORMAL  # 일반:NORMAL, 부방장:STAFF, 방장:HOST
OAUTH_TOKEN = ""
DEVICE_UUID = ""

PLAYLIST_SOURCES = [
    "test_data/1.ogg",
    "test_data/2.ogg",
    "test_data/3.ogg",
]


async def main() -> None:
    # vsshost, loco port
    vero = Vero(DEFAULT_CONFIG, "211.242.12.121", 9282)
    audio_track = AudioSourceTrack()
    room = await vero.create_voice_room(
        chat_id=CHAT_ID,
        title="Hi There",
        account_token=OAUTH_TOKEN,
        device_uuid=DEVICE_UUID,
        audio_track=audio_track,
        user_type=USER_TYPE,
    )

    async def add_random_audio_source() -> None:
        while True:
            source = random.choice(PLAYLIST_SOURCES)
            print("add audio source", source)
            audio_track.add_source(source)
            await asyncio.sleep(13)

    audio_task = asyncio.create_task(add_random_audio_source())

    @room.on(VoiceRoomEvent.JOIN)
    async def on_join(member: VoiceRoomMember) -> None:
        print("join", member.user_id, member.user_type)

    @room.on(VoiceRoomEvent.LEAVE)
    async def on_leave(member: VoiceRoomMember) -> None:
        print("leave", member.user_id, member.user_type)

    @room.on(VoiceRoomEvent.SPEAKER_REQUEST)
    async def on_speaker_request(
        user_id: int,
        event: VoiceRoomServerEvent,
    ) -> None:
        print("speaker request", user_id, event.event)
        await asyncio.sleep(1)
        await room.accept_speaker_request(user_id)
        print("speaker request accepted", user_id)

    print("voice room created. press Ctrl-C to close.")

    try:
        await asyncio.Event().wait()
    finally:
        audio_task.cancel()
        await asyncio.gather(audio_task, return_exceptions=True)
        audio_track.stop()
        await room.close()


if __name__ == "__main__":
    asyncio.run(main())
