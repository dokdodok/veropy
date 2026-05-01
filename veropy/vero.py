from __future__ import annotations

from aiortc import AudioStreamTrack

from veropy.config import VoxConfig
from veropy.voiceroom import UserType, VoiceRoom


class Vero:
    def __init__(
        self,
        config: VoxConfig,
        host: str,
        port: int,
    ) -> None:
        self._config = config
        self._host = host
        self._port = port

    async def create_voice_room(
        self,
        *,
        chat_id: int,
        title: str,
        account_token: str,
        device_uuid: str,
        audio_track: AudioStreamTrack,
        user_type: UserType,
    ) -> VoiceRoom:
        return await VoiceRoom.create(
            config=self._config,
            chat_id=chat_id,
            title=title,
            account_token=account_token,
            device_uuid=device_uuid,
            audio_track=audio_track,
            user_type=user_type,
            host=self._host,
            port=self._port,
        )
