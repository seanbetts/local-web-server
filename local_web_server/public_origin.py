from dataclasses import dataclass
from urllib.parse import urlsplit


TRUSTED_LAN = "trusted-lan"
TAILSCALE_SERVE = "tailscale-serve"
TAILSCALE_CADDY_PORT = 8080


@dataclass(frozen=True)
class CanonicalPublicOrigin:
    value: str
    scheme: str
    host: str
    effective_port: int


def _valid_dns_label(label: str) -> bool:
    return (
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(
            character.isdigit() or "a" <= character <= "z" or character == "-"
            for character in label
        )
    )


def parse_tailscale_public_origin(value: object) -> CanonicalPublicOrigin:
    if not isinstance(value, str) or not value:
        raise ValueError("public origin is invalid")
    if any(
        character.isspace()
        or ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise ValueError("public origin is invalid")
    if (
        any(ord(character) > 0x7E for character in value)
        or "\\" in value
        or "%" in value
    ):
        raise ValueError("public origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("public origin is invalid") from error
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not host
        or len(host) > 253
        or host != host.lower()
        or not host.endswith(".ts.net")
        or parsed.netloc != host
        or port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public origin is invalid")
    labels = host.split(".")
    if (
        len(labels) < 4
        or labels[-2:] != ["ts", "net"]
        or any(not _valid_dns_label(label) for label in labels)
    ):
        raise ValueError("public origin is invalid")
    canonical = f"https://{host}"
    if canonical != value:
        raise ValueError("public origin is invalid")
    return CanonicalPublicOrigin(canonical, "https", host, 443)


def join_public_origin(origin: str, path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("public path must be absolute")
    return f"{origin}{path}"
