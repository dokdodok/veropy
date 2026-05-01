import struct
import typing
import collections.abc

from .etc import I32, I64, U64, TypeSignature, EncodeError

encoders = {}


def register(input_type: collections.abc.Sequence) -> typing.Callable:
    """
    simple decorator for type dispatch
    """

    def decorator(function):
        if input_type in encoders:
            return function
        for types in input_type:
            encoders[types] = function
        return function

    return decorator


def encode_element_name(name: str) -> bytes:
    assert isinstance(name, str)

    nameb = name.encode("utf-8")
    if b"\x00" in nameb:
        raise EncodeError("null contained in name")

    return nameb + b"\x00"


def encode_element(name: str, element) -> bytes:
    encoder = encoders.get(type(element))
    if encoder is None:
        raise EncodeError(f"No encoder for : {type(element)}")
    return encoder(name, element)


def encode_document(document):
    buffer = b"".join([encode_element(key, document[key]) for key in document.keys()])
    return struct.pack(f"<i{len(buffer)}sb", len(buffer) + 5, buffer, 0)


@register((str,))
def encode_string(name: str, value: str) -> bytes:
    valueb = value.encode("utf-8")
    return (
        TypeSignature.string
        + encode_element_name(name)
        + struct.pack(f"<i{len(valueb)}sb", len(valueb) + 1, valueb, 0)
    )


@register((bool,))
def encode_bool(name: str, value: bool) -> bytes:
    return TypeSignature.bool + encode_element_name(name) + struct.pack("<b", value)


@register((type(None),))
def encode_null(name: str, value: None) -> bytes:
    return TypeSignature.null + encode_element_name(name)


@register((I32,))
def encode_i32(name: str, value: I32) -> bytes:
    return TypeSignature.int32 + encode_element_name(name) + struct.pack("<i", value.v)


@register((I64,))
def encode_i64(name: str, value: I64) -> bytes:
    return TypeSignature.int64 + encode_element_name(name) + struct.pack("<q", value.v)


@register((U64,))
def encode_u64(name: str, value: U64) -> bytes:
    return TypeSignature.uint64 + encode_element_name(name) + struct.pack("<Q", value.v)


@register((float,))
def encode_double(name: str, value: float) -> bytes:
    return TypeSignature.double + encode_element_name(name) + struct.pack("<d", value)


@register((bytes,))
def encode_binary(name: str, value: bytes) -> bytes:
    return (
        TypeSignature.binary
        + encode_element_name(name)
        + struct.pack("<ib", len(value), 0)
        + value
    )


@register((list, tuple))
def encode_list(name: str, value: set | list) -> bytes:
    buffer = b"".join(
        [encode_element(str(index), element) for index, element in enumerate(value)]
    )
    return (
        TypeSignature.array
        + encode_element_name(name)
        + struct.pack(f"<i{len(buffer)}sb", len(buffer) + 5, buffer, 0)
    )


@register((dict,))
def encode_dict(name: str, value: dict) -> bytes:
    buffer = bytearray()
    for key in value.keys():
        if type(key) not in (bytes, str):
            key = str(key)
        buffer.extend(encode_element(key, value[key]))
    return (
        TypeSignature.document
        + encode_element_name(name)
        + struct.pack(f"<i{len(buffer)}sb", len(buffer) + 5, buffer, 0)
    )
