"""Public HTTPS requests stay on the origin whose health is being verified."""

import urllib.error
import urllib.request
from urllib.parse import urlsplit


class _SameHTTPSOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.host = origin.hostname
        self.port = 443 if origin.port is None else origin.port

    def redirect_request(self, request, response, code, message, headers, new_url):
        target = urlsplit(new_url)
        if (
            target.scheme != "https"
            or target.hostname != self.host
            or (443 if target.port is None else target.port) != self.port
            or target.username is not None
            or target.password is not None
        ):
            raise urllib.error.HTTPError(
                request.full_url, code, "redirect leaves canonical HTTPS origin", headers, response,
            )
        return super().redirect_request(request, response, code, message, headers, new_url)


def open_public_request(request: urllib.request.Request, *, timeout: float):
    origin = urlsplit(request.full_url)
    if origin.scheme == "https":
        opener = urllib.request.build_opener(_SameHTTPSOriginRedirectHandler(origin))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)
