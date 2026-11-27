import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 30
MAX_XML_BYTES = 50 * 1024 * 1024  # 50 MiB cap for XML listing responses


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow only http/https redirects; block redirects to file:// or other schemes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = (urlparse(newurl).scheme or '').lower()
        if scheme not in ('http', 'https'):
            raise urllib.error.URLError(f"Blocked redirect to unsupported scheme: {scheme!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def safe_urlopen(req: urllib.request.Request, timeout: int = DEFAULT_TIMEOUT):
    """Open an HTTP/HTTPS request with safe redirect validation."""
    scheme = (urlparse(req.full_url).scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise ValueError(f"Unsupported URL scheme '{scheme}': only http and https are allowed")
    return _OPENER.open(req, timeout=timeout)


def read_bounded(resp, max_bytes: int = MAX_XML_BYTES) -> bytes:
    """Read up to max_bytes from an HTTP response, raising ValueError if exceeded."""
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"Response exceeded maximum allowed size of {max_bytes} bytes")
    return data
