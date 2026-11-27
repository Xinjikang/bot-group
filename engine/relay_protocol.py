"""Relay wire revisions. Release tags are diagnostic metadata."""

import base64

VERSIONS = (3, 2, 1)
CURRENT = VERSIONS[0]
REJECTION = "supported relay protocols: " + ",".join(map(str, VERSIONS))


class ProtocolMismatch(ValueError):
    pass


def offer():
    # A legacy responder reads only the single-version field.
    return {"protocol_version": 1, "protocol_versions": list(VERSIONS)}


def profile_fields(values, *, decode=False):
    """Keep UTF-8 field bytes bounded independently of JSON escape expansion."""
    if decode:
        return {k: base64.b64decode(v, validate=True).decode('utf-8') for k, v in values.items()}
    return {k: base64.b64encode(v.encode('utf-8')).decode('ascii') for k, v in values.items()
            if v is not None}


def negotiate(hello):
    fallback = hello.get("protocol_version")
    versions = hello.get("protocol_versions", [fallback])
    if (type(fallback) is not int or not isinstance(versions, list)
            or not all(type(v) is int for v in versions) or fallback not in versions):
        return None
    return next((v for v in VERSIONS if v in versions), None)


def selected(receipt):
    version = receipt.get("protocol_version", 1)
    if type(version) is not int or version not in VERSIONS:
        raise ProtocolMismatch("unsupported relay protocol in handshake receipt")
    return version


def refusal(close_reason):
    """Report only numeric revisions supplied by the peer."""
    prefix = "supported relay protocols: "
    offered = (close_reason or "").removeprefix(prefix).split(",")
    remote = ",".join(offered) if all(v.isascii() and v.isdecimal() and len(v) <= 5 for v in offered) else "未知"
    return f"远程通信协议不兼容（本端 {','.join(map(str, VERSIONS))}，对端 {remote}）"
