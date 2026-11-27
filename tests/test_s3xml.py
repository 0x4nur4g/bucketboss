import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import urllib.error

import pytest

from bucketboss.providers.s3xml import S3XMLProvider, parse_s3_url


def test_parse_s3_url_variants():
    assert parse_s3_url("https://mybucket.s3.amazonaws.com/") == (
        "https://mybucket.s3.amazonaws.com",
        "mybucket",
    )
    assert parse_s3_url("https://mybucket.s3.us-west-2.amazonaws.com/") == (
        "https://mybucket.s3.us-west-2.amazonaws.com",
        "mybucket",
    )
    assert parse_s3_url("https://s3.amazonaws.com/mybucket/") == (
        "https://s3.amazonaws.com/mybucket",
        "mybucket",
    )
    assert parse_s3_url("https://s3.ap-south-1.amazonaws.com/mybucket/") == (
        "https://s3.ap-south-1.amazonaws.com/mybucket",
        "mybucket",
    )
    assert parse_s3_url("http://127.0.0.1:9000/mybucket/") == (
        "http://127.0.0.1:9000/mybucket",
        "mybucket",
    )


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://mybucket.s3.amazonaws.com/",
        "https://s3.amazonaws.com/",
        "mybucket.s3.amazonaws.com",
    ],
)
def test_parse_s3_url_rejects_invalid(bad_url):
    with pytest.raises(ValueError):
        parse_s3_url(bad_url)


@pytest.fixture
def s3_mock_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_HEAD(self):
            if self.path == "/testbucket/hello.txt":
                self.send_response(200)
                self.send_header("Content-Length", "11")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Last-Modified", "Tue, 10 Feb 2026 12:00:00 GMT")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)

            # Simulate redirect to disallowed file:// scheme
            if parsed.path == "/testbucket/evil-redirect":
                self.send_response(302)
                self.send_header("Location", "file:///etc/passwd")
                self.end_headers()
                return

            if parsed.path == "/testbucket":
                if qs.get("max-keys") == ["0"]:
                    self.send_response(200)
                    self.end_headers()
                    return
                # Force v2 -> v1 fallback on 400
                if qs.get("list-type") == ["2"]:
                    self.send_response(400)
                    self.end_headers()
                    return
                xml = (
                    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                    "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
                    "<Name>testbucket</Name>"
                    "<IsTruncated>false</IsTruncated>"
                    "<CommonPrefixes><Prefix>secrets/</Prefix></CommonPrefixes>"
                    "<Contents>"
                    "<Key>hello.txt</Key>"
                    "<Size>11</Size>"
                    "<LastModified>2026-02-10T12:00:00.000Z</LastModified>"
                    "</Contents>"
                    "</ListBucketResult>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(xml)))
                self.end_headers()
                self.wfile.write(xml)
                return

            if parsed.path == "/testbucket/hello.txt":
                data = b"hello world"
                rng = self.headers.get("Range")
                if rng == "bytes=0-4":
                    data = data[:5]
                    self.send_response(206)
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_response(404)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/testbucket"
    finally:
        server.shutdown()
        server.server_close()


def test_s3xml_v1_fallback_and_redirect_blocking(s3_mock_server):
    base_url, bucket = parse_s3_url(s3_mock_server)
    provider = S3XMLProvider(base_url, bucket)

    provider.head_bucket()
    dirs, files, token = provider.list_objects("")
    assert dirs == ["secrets"]
    assert [f["name"] for f in files] == ["hello.txt"]
    assert token is None
    assert provider._use_list_type_2 is False

    assert provider.get_object("hello.txt") == b"hello world"
    assert provider.read_object_range("hello.txt", 5) == b"hello"
    assert provider.get_object_metadata("hello.txt")["size"] == 11

    # Redirect to file:// must be blocked
    with pytest.raises(urllib.error.URLError):
        provider.get_object("evil-redirect")
