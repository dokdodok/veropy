from dataclasses import dataclass
from typing import Literal


DeviceType = Literal["ANDROID_MAIN", "ANDROID_SUB"]
NetworkType = Literal["4G", "WIFI"]
UserType = Literal["STAFF", "WIFI"]

APP_VER = "26.3.1"

DEVICE_TYPE_CODES: dict[DeviceType, tuple[int, int]] = {
    "ANDROID_MAIN": (1, 2),
    "ANDROID_SUB": (4, 7),
}

NETWORK_TYPE_CODES: dict[NetworkType, int] = {
    "4G": 1,
    "WIFI": 2,
}


@dataclass
class VoxConfig:
    user_id: int
    device_type: DeviceType
    network: NetworkType
    device_lang: str
    account_country: str
    carrier_id: str
    model: str
    sdk_version: str
    build_id: str

    @property
    def dev_type(self) -> int:
        return DEVICE_TYPE_CODES[self.device_type][0]

    @property
    def dev_sub_type(self) -> int:
        return DEVICE_TYPE_CODES[self.device_type][1]

    @property
    def net_type(self) -> int:
        return NETWORK_TYPE_CODES[self.network]
