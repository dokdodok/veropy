from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
import ipaddress

from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription

from .config import APP_VER, VoxConfig
from .event_emitter import EventEmitter
from .simple_bson.etc import I32, I64
from .track import VOX_STREAM_ID, VOX_TRACK_ID
from .vox_connection import VoxConnection, VoxPayload


VOICEROOM_SERVICE_TYPE = 11
MEMBER_OPTION_MIC = 1 << 0
MEMBER_OPTION_SPEAKER_REQUEST = 1 << 1
MEMBER_OPTION_VOICE_FILTER_SHIFT = 24


def bson_int(value: I32 | I64 | int) -> int:
    if isinstance(value, I32 | I64):
        return value.v
    return int(value)


class VoiceRoomEvent(StrEnum):
    RAW = "raw"
    JOIN = "join"
    LEAVE = "leave"
    SPEAKER_REQUEST = "speaker_request"
    SPEAKER_REQUEST_CANCELLED = "speaker_request_cancelled"


class UserType(IntEnum):
    NORMAL = 1
    STAFF = 2
    HOST = 3


@dataclass(frozen=True, slots=True)
class VoiceRoomMember:
    user_id: int
    user_type: int
    changes: int | None = None
    option: int | None = None
    mic: bool | None = None
    speaker_requested: bool | None = None
    cam: bool | None = None
    voice_filter: int | None = None
    video_alt: str | None = None
    audio_effect: str | None = None
    raw: VoxPayload = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: VoxPayload) -> VoiceRoomMember:
        option = bson_int(payload["option"]) if "option" in payload else None
        return cls(
            user_id=bson_int(payload["userId"]),
            user_type=bson_int(payload.get("userType", 0)),
            changes=bson_int(payload["changes"]) if "changes" in payload else None,
            option=option,
            mic=(
                payload["mic"]
                if "mic" in payload
                else cls._option_flag(option, MEMBER_OPTION_MIC)
            ),
            speaker_requested=cls._option_flag(
                option,
                MEMBER_OPTION_SPEAKER_REQUEST,
            ),
            cam=payload.get("cam"),
            voice_filter=cls._voice_filter_from_payload(payload, option),
            video_alt=payload.get("videoAlt"),
            audio_effect=payload.get("audioFEmo"),
            raw=payload,
        )

    @staticmethod
    def _option_flag(option: int | None, flag: int) -> bool | None:
        if option is None:
            return None
        return bool(option & flag)

    @staticmethod
    def _option_voice_filter(option: int | None) -> int | None:
        if option is None:
            return None
        return (option >> MEMBER_OPTION_VOICE_FILTER_SHIFT) & 0xFF

    @staticmethod
    def _voice_filter_from_payload(
        payload: VoxPayload,
        option: int | None,
    ) -> int | None:
        if "voiceFilter" in payload:
            return bson_int(payload["voiceFilter"])
        return VoiceRoomMember._option_voice_filter(option)


@dataclass(frozen=True, slots=True)
class VoiceRoomServerEvent:
    event: int
    body: VoxPayload


@dataclass(frozen=True, slots=True)
class VeroSdp:
    type: int
    sdp: str
    user_id: int

    @classmethod
    def from_payload(cls, payload: VoxPayload) -> VeroSdp:
        return cls(
            type=bson_int(payload["sdpType"]),
            sdp=payload["sdp"],
            user_id=bson_int(payload["sdpUserId"]),
        )


class VoiceRoom(EventEmitter):
    def __init__(
        self,
        *,
        connection: VoxConnection,
        config: VoxConfig,
        chat_id: int,
        create_response: VoxPayload | None = None,
        peer_connection: RTCPeerConnection,
    ) -> None:
        super().__init__()
        self.connection = connection
        self.config = config
        self.chat_id = chat_id
        self.create_response = create_response
        self.peer_connection = peer_connection
        self._remote_peer_connections: dict[int, RTCPeerConnection] = {}
        self._ping_task: asyncio.Task[None] | None = None
        self._members: dict[int, VoiceRoomMember] = {}

    @staticmethod
    async def create(
        *,
        config: VoxConfig,
        chat_id: int,
        title: str,
        account_token: str,
        device_uuid: str,
        audio_track: AudioStreamTrack,
        user_type: UserType,
        host: str,
        port: int,
    ) -> VoiceRoom:
        cs_host, cs_port = await VoiceRoom._get_call_server(
            config=config,
            chat_id=chat_id,
            host=host,
            port=port,
        )
        connection = await VoxConnection.connect(cs_host, cs_port)
        peer_connection = None

        try:
            peer_connection = await VoiceRoom._create_peer_connection(audio_track)
            local_sdp = peer_connection.localDescription.sdp

            room = VoiceRoom(
                connection=connection,
                config=config,
                chat_id=chat_id,
                peer_connection=peer_connection,
            )
            connection.set_on_packet(room.handle_packet)

            vr_created_fut = connection.wait_event(11003)

            create_response = await connection.request(
                11028,
                VoiceRoom._build_create_request(
                    config=config,
                    chat_id=chat_id,
                    title=title,
                    account_token=account_token,
                    device_uuid=device_uuid,
                    sdp=local_sdp,
                    user_type=user_type,
                ),
            )
            room.attach_create_response(create_response)

            vr_created_event = await vr_created_fut
            await room.ack_event(vr_created_event.method)

            await room.connect_webrtc(
                VeroSdp.from_payload(vr_created_event.body["sdp"])
            )
            room.start_ping()
            return room
        except Exception:
            if peer_connection is not None:
                await peer_connection.close()
            await connection.close()
            raise

    @property
    def user_id(self) -> int:
        return self.config.user_id

    @property
    def call_id(self) -> int:
        return bson_int(self._require_create_response()["callId"])

    @property
    def cs_id(self) -> int:
        return bson_int(self._require_create_response()["csId"])

    @property
    def interval(self) -> int:
        return bson_int(self._require_create_response().get("interval", 30))

    def attach_create_response(self, create_response: VoxPayload) -> None:
        self.create_response = create_response

    async def ack_event(self, method: int) -> None:
        await self.connection.send(
            method,
            {
                "userId": I64(self.user_id),
                "callId": I64(self.call_id),
                "resCode": I32(0),
                "devType": I32(self.config.dev_type),
            },
        )

    async def handle_packet(self, method: int, body: VoxPayload) -> None:
        # Event
        if method == 12011:
            await self.ack_event(method)
            await self._handle_server_event(body)
        elif method == 11030:
            await self.ack_event(method)
            await self._handle_remote_offer(body)

    @property
    def members(self) -> tuple[VoiceRoomMember, ...]:
        return tuple(self._members.values())

    async def connect_webrtc(self, sdp: VeroSdp) -> None:
        if self.peer_connection is None:
            raise Exception("peer connection is none")

        await self.peer_connection.setRemoteDescription(
            RTCSessionDescription(
                sdp=sdp.sdp,
                type=self._rtc_sdp_type(sdp),
            )
        )

    async def ping(self) -> VoxPayload:
        return await self.connection.request(
            11015,
            {
                "userId": I64(self.user_id),
                "csId": I64(self.cs_id),
                "callId": I64(self.call_id),
                "devType": I32(self.config.dev_type),
                "voiceFilter": I32(0),
                "audioState": I32(0),
                "videoState": I32(0),
                "recInfo": {
                    "recSessionId": "",
                    "recordCount": I32(0),
                    "recordDuration": I64(0),
                    "recordIds": [],
                },
            },
        )

    async def accept_speaker_request(self, user_id: int) -> VoxPayload:
        return await self.connection.request(
            12041,
            self._build_speaker_request_control(user_id),
        )

    async def reject_speaker_request(self, user_id: int) -> VoxPayload:
        return await self.connection.request(
            12042,
            self._build_speaker_request_control(user_id),
        )

    def start_ping(self) -> None:
        if self._ping_task is None or self._ping_task.done():
            self._ping_task = asyncio.create_task(self._ping_loop())

    async def stop_ping(self) -> None:
        if self._ping_task is None:
            return

        self._ping_task.cancel()
        await asyncio.gather(self._ping_task, return_exceptions=True)
        self._ping_task = None

    async def close(self) -> None:
        await self.stop_ping()
        for id, peer_connection in self._remote_peer_connections.items():
            await peer_connection.close()
        self._remote_peer_connections.clear()
        if self.peer_connection is not None:
            await self.peer_connection.close()
            self.peer_connection = None
        await self.connection.close()

    async def _ping_loop(self) -> None:
        interval = self.interval
        while True:
            response = await self.ping()
            interval = bson_int(response.get("interval", interval))
            await asyncio.sleep(interval)

    def _require_create_response(self) -> VoxPayload:
        if self.create_response is None:
            raise RuntimeError("voice room create response is not attached yet")
        return self.create_response

    def _build_speaker_request_control(self, user_id: int) -> VoxPayload:
        return {
            "userId": I64(self.user_id),
            "callId": I64(self.call_id),
            "devType": I32(self.config.dev_type),
            "destId": I64(user_id),
        }

    async def _handle_remote_offer(self, body: VoxPayload) -> None:
        sdp = VeroSdp.from_payload(body["sdp"])
        previous = self._remote_peer_connections.pop(sdp.user_id, None)
        if previous is not None:
            await previous.close()

        peer_connection = RTCPeerConnection()
        self._remote_peer_connections[sdp.user_id] = peer_connection

        await peer_connection.setRemoteDescription(
            RTCSessionDescription(
                sdp=sdp.sdp,
                type=self._rtc_sdp_type(sdp),
            )
        )

        answer = await peer_connection.createAnswer()
        await peer_connection.setLocalDescription(answer)

        if "moderators" in body or "speakers" in body:
            await self._handle_full_members(
                {
                    "moderators": body.get("moderators", []),
                    "speakers": body.get("speakers", []),
                    "listeners": body.get("listeners", []),
                }
            )

        partial = body.get("partial")
        if isinstance(partial, dict):
            await self._handle_partial_members(partial)

        await self.connection.request(
            11032,
            {
                "userId": I64(self.user_id),
                "devType": I32(self.config.dev_type),
                "callId": I64(self.call_id),
                "sdp": {
                    "sdp": peer_connection.localDescription.sdp,
                    "sdpType": I32(2),
                    "sdpUserId": I64(sdp.user_id),
                },
            },
        )

    async def _handle_server_event(self, body: VoxPayload) -> None:
        event = bson_int(body.get("event", 0))
        server_event = VoiceRoomServerEvent(event=event, body=body)
        await self.emit(VoiceRoomEvent.RAW, server_event)

        members = body.get("members")
        if isinstance(members, dict):
            await self._handle_members(members)

        if event == 11 and "destId" in body:
            await self.emit(
                VoiceRoomEvent.SPEAKER_REQUEST,
                bson_int(body["destId"]),
                server_event,
            )
            return
        elif event == 12 and "destId" in body:
            await self.emit(
                VoiceRoomEvent.SPEAKER_REQUEST_CANCELLED,
                bson_int(body["destId"]),
                server_event,
            )
            return

    async def _handle_members(self, members: VoxPayload) -> None:
        full = members.get("full")
        if isinstance(full, dict):
            await self._handle_full_members(full)

        partial = members.get("partial")
        if isinstance(partial, dict):
            await self._handle_partial_members(partial)

    async def _handle_full_members(self, full: VoxPayload) -> None:
        next_members: dict[int, VoiceRoomMember] = {}
        for key in ("moderators", "speakers", "listeners"):
            for payload in full.get(key, []):
                member = VoiceRoomMember.from_payload(payload)
                next_members[member.user_id] = member

        for user_id, member in next_members.items():
            if user_id not in self._members:
                await self.emit(VoiceRoomEvent.JOIN, member)

        for user_id, member in list(self._members.items()):
            if user_id not in next_members:
                await self.emit(VoiceRoomEvent.LEAVE, member)

        self._members = next_members

    async def _handle_partial_members(self, partial: VoxPayload) -> None:
        for payload in partial.get("events", []):
            member = VoiceRoomMember.from_payload(payload)
            previous = self._members.get(member.user_id)

            if self._is_leave_payload(payload):
                left = (
                    self._merge_member(previous, member)
                    if previous is not None
                    else member
                )
                self._members.pop(member.user_id, None)
                await self._close_remote_peer_connection(member.user_id)
                await self.emit(VoiceRoomEvent.LEAVE, left)
                continue

            if previous is None:
                self._members[member.user_id] = member
                await self.emit(VoiceRoomEvent.JOIN, member)
            else:
                self._members[member.user_id] = self._merge_member(previous, member)

    def _merge_member(
        self,
        previous: VoiceRoomMember,
        update: VoiceRoomMember,
    ) -> VoiceRoomMember:
        return VoiceRoomMember(
            user_id=previous.user_id,
            user_type=update.user_type or previous.user_type,
            changes=update.changes,
            option=update.option,
            mic=update.mic if update.mic is not None else previous.mic,
            speaker_requested=update.speaker_requested
            if update.speaker_requested is not None
            else previous.speaker_requested,
            cam=update.cam if update.cam is not None else previous.cam,
            voice_filter=(
                update.voice_filter
                if update.voice_filter is not None
                else previous.voice_filter
            ),
            video_alt=update.video_alt
            if update.video_alt is not None
            else previous.video_alt,
            audio_effect=update.audio_effect,
            raw=update.raw,
        )

    async def _close_remote_peer_connection(self, user_id: int) -> None:
        peer_connection = self._remote_peer_connections.pop(user_id, None)
        if peer_connection is not None:
            await peer_connection.close()

    def _is_leave_payload(self, payload: VoxPayload) -> bool:
        changes = bson_int(payload.get("changes", 0))
        return changes in (3, 6, 9)

    def _rtc_sdp_type(self, sdp: VeroSdp) -> str:
        if sdp.type == 1:
            return "offer"
        if sdp.type == 2:
            return "answer"
        raise ValueError(f"unknown sdp type: {sdp.type}")

    @staticmethod
    async def _create_peer_connection(
        audio_track: AudioStreamTrack,
    ) -> RTCPeerConnection:
        peer_connection = RTCPeerConnection()
        peer_connection._RTCPeerConnection__stream_id = VOX_STREAM_ID  # type: ignore[attr-defined]

        track = audio_track
        track._id = VOX_TRACK_ID

        peer_connection.addTransceiver(track, direction="sendonly")
        offer = await peer_connection.createOffer()
        await peer_connection.setLocalDescription(offer)
        return peer_connection

    @staticmethod
    def _build_callable_request(
        *,
        config: VoxConfig,
        chat_id: int,
    ) -> VoxPayload:
        return {
            "userId": I64(config.user_id),
            "devType": I32(config.dev_type),
            "chatId": I64(chat_id),
            "appVer": APP_VER,
            "lang": config.device_lang,
            "country": config.account_country,
            "voxVer": I32(5),
            "carrierId": config.carrier_id,
            "netType": I32(config.net_type),
            "checkOnCall": False,
            "devInfo": VoiceRoom._build_dev_info(
                config=config,
                include_sub_type=False,
            ),
            "serviceType": I32(VOICEROOM_SERVICE_TYPE),
        }

    @staticmethod
    async def _get_call_server(
        *,
        config: VoxConfig,
        chat_id: int,
        host: str,
        port: int,
    ) -> tuple[str, int]:
        async with await VoxConnection.connect(host, port) as connection:
            callable_response = await connection.request(
                30000,
                VoiceRoom._build_callable_request(
                    config=config,
                    chat_id=chat_id,
                ),
            )
            if bson_int(callable_response["callable"]) != 1:
                raise RuntimeError(f"voice room is not callable: {callable_response!r}")

            return VoiceRoom._parse_call_server(callable_response)

    @staticmethod
    def _parse_call_server(callable_response: VoxPayload) -> tuple[str, int]:
        ssl_addr = callable_response["csSslAddr"]
        ip = bson_int(ssl_addr["ip"])
        port = bson_int(callable_response.get("csSslPort", ssl_addr["port"]))
        return str(ipaddress.IPv4Address(ip & 0xFFFFFFFF)), port

    @staticmethod
    def _build_create_request(
        *,
        config: VoxConfig,
        chat_id: int,
        title: str,
        account_token: str,
        device_uuid: str,
        sdp: str,
        user_type: UserType,
    ) -> VoxPayload:
        return {
            "userId": I64(config.user_id),
            "devType": I32(config.dev_type),
            "mediaType": I32(1),
            "sKey": "",
            "oauthToken": account_token,
            "duuid": device_uuid,
            "appVer": APP_VER,
            "lang": config.device_lang,
            "country": config.account_country,
            "voxVer": I32(5),
            "serviceType": I32(VOICEROOM_SERVICE_TYPE),
            "chatId": I64(chat_id),
            "title": title,
            "userType": I32(user_type.value),
            "defaultMicOnOff": I32(1),
            "destId": I64(0),
            "netType": I32(config.net_type),
            "carrierId": config.carrier_id,
            "simOperator": config.carrier_id,
            "isRoaming": I32(0),
            "mobileNetType": I32(config.data_net_type),
            "devInfo": VoiceRoom._build_dev_info(
                config=config,
                include_sub_type=True,
            ),
            "sdp": {
                "sdp": sdp,
                "sdpType": I32(1),
                "sdpUserId": I64(config.user_id),
            },
            "isMultiChat": False,
        }

    @staticmethod
    def _build_dev_info(
        *,
        config: VoxConfig,
        include_sub_type: bool,
    ) -> VoxPayload:
        dev_info: VoxPayload = {
            "model": config.model,
            "os": "android",
            "osver": config.sdk_version,
            "buildId": config.build_id,
        }
        if include_sub_type:
            dev_info["devSubType"] = I32(config.dev_sub_type)
        return dev_info
