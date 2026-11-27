import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from bucketboss import cli
from bucketboss.providers.azureblob import (
    AzureBlobProvider,
    is_azure_blob_url,
    parse_azure_blob_url,
)


def test_is_azure_blob_url_detection():
    assert is_azure_blob_url("https://myacct.blob.core.windows.net/mycontainer")
    assert is_azure_blob_url("https://myacct.blob.core.usgovcloudapi.net/mycontainer")
    assert is_azure_blob_url("https://custom.example.com/mycontainer?restype=container&comp=list")
    assert not is_azure_blob_url("https://mybucket.s3.us-west-2.amazonaws.com/")
    assert not is_azure_blob_url("https://minio.local:9000/mybucket/")


def test_parse_azure_blob_url_variants():
    base, acct, cont, sas = parse_azure_blob_url(
        "https://myacct.blob.core.windows.net/public?restype=container&comp=list"
    )
    assert base == "https://myacct.blob.core.windows.net/public"
    assert acct == "myacct"
    assert cont == "public"
    assert sas == []

    # Preserves SAS params while stripping list-operation query params
    base, acct, cont, sas = parse_azure_blob_url(
        "https://myacct.blob.core.windows.net/logs?restype=container&comp=list&sv=2024-11-04&sp=rl&sig=abc%2Fdef%3D"
    )
    assert base == "https://myacct.blob.core.windows.net/logs"
    assert acct == "myacct"
    assert cont == "logs"
    assert dict(sas) == {"sv": "2024-11-04", "sp": "rl", "sig": "abc/def="}

    # Azurite path-style
    base, acct, cont, sas = parse_azure_blob_url("http://127.0.0.1:10000/devstoreaccount1/mycontainer")
    assert base == "http://127.0.0.1:10000/devstoreaccount1/mycontainer"
    assert acct == "devstoreaccount1"
    assert cont == "mycontainer"

    # Custom host
    base, acct, cont, sas = parse_azure_blob_url("https://files.example.com/public")
    assert base == "https://files.example.com/public"
    assert cont == "public"


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://myacct.blob.core.windows.net/public",
        "https://myacct.blob.core.windows.net/",
        "myacct.blob.core.windows.net/public",
    ],
)
def test_parse_azure_blob_url_rejects_invalid(bad_url):
    with pytest.raises(ValueError):
        parse_azure_blob_url(bad_url)


@pytest.fixture
def azure_mock_server():
    blobs = {
        ".env": b"SECRET_KEY=supersecretvalue\n",
        "readme.txt": b"hello azure blob world",
        "configs/app.yml": b"debug: true\n",
    }
    recorded_queries = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_HEAD(self):
            parsed = urlparse(self.path)
            recorded_queries.append(("HEAD", parsed.path, parse_qs(parsed.query)))
            if parsed.path.startswith("/myacct/mycontainer/"):
                key = parsed.path[len("/myacct/mycontainer/"):]
                if key in blobs:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(blobs[key])))
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Last-Modified", "Tue, 10 Feb 2026 12:00:00 GMT")
                    self.end_headers()
                    return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            recorded_queries.append(("GET", parsed.path, qs))

            # Verify SAS token if requested on /myacct/sascontainer
            if parsed.path.startswith("/myacct/sascontainer"):
                if qs.get("sig") != ["valid-token"]:
                    self.send_response(403)
                    self.end_headers()
                    return

            # Container listing via ?restype=container&comp=list
            if parsed.path in ("/myacct/mycontainer", "/myacct/sascontainer"):
                if qs.get("restype") != ["container"] or qs.get("comp") != ["list"]:
                    self.send_response(400)
                    self.end_headers()
                    return

                prefix = qs.get("prefix", [""])[0]
                marker = qs.get("marker", [""])[0]

                if prefix == "":
                    if marker == "":
                        # Page 1 with UTF-8 BOM + BlobPrefix + NextMarker + ADLS directory marker
                        xml = (
                            "\ufeff<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                            "<EnumerationResults ContainerName=\"mycontainer\">"
                            "<Blobs>"
                            "<Blob>"
                            "<Name>.env</Name>"
                            "<Properties>"
                            f"<Content-Length>{len(blobs['.env'])}</Content-Length>"
                            "<Last-Modified>Tue, 10 Feb 2026 12:00:00 GMT</Last-Modified>"
                            "</Properties>"
                            "</Blob>"
                            "<BlobPrefix><Name>configs/</Name></BlobPrefix>"
                            "<Blob>"
                            "<Name>empty_adls_dir</Name>"
                            "<Properties><ResourceType>directory</ResourceType><Content-Length>0</Content-Length></Properties>"
                            "</Blob>"
                            "</Blobs>"
                            "<NextMarker>page2</NextMarker>"
                            "</EnumerationResults>"
                        )
                    else:
                        xml = (
                            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                            "<EnumerationResults ContainerName=\"mycontainer\">"
                            "<Blobs>"
                            "<Blob>"
                            "<Name>readme.txt</Name>"
                            "<Properties>"
                            f"<Content-Length>{len(blobs['readme.txt'])}</Content-Length>"
                            "<Last-Modified>Wed, 11 Feb 2026 08:30:00 GMT</Last-Modified>"
                            "</Properties>"
                            "</Blob>"
                            "</Blobs>"
                            "<NextMarker />"
                            "</EnumerationResults>"
                        )
                elif prefix == "configs/":
                    xml = (
                        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                        "<EnumerationResults ContainerName=\"mycontainer\">"
                        "<Blobs>"
                        "<Blob>"
                        "<Name>configs/app.yml</Name>"
                        "<Properties>"
                        f"<Content-Length>{len(blobs['configs/app.yml'])}</Content-Length>"
                        "<Last-Modified>Tue, 10 Feb 2026 12:00:00 GMT</Last-Modified>"
                        "</Properties>"
                        "</Blob>"
                        "</Blobs>"
                        "<NextMarker />"
                        "</EnumerationResults>"
                    )
                else:
                    xml = (
                        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                        "<EnumerationResults ContainerName=\"mycontainer\"><Blobs /><NextMarker /></EnumerationResults>"
                    )

                body = xml.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # Blob download / range read
            for container_prefix in ("/myacct/mycontainer/", "/myacct/sascontainer/"):
                if parsed.path.startswith(container_prefix):
                    key = parsed.path[len(container_prefix):]
                    if key in blobs:
                        data = blobs[key]
                        rng = self.headers.get("Range")
                        if rng and rng.startswith("bytes=0-"):
                            end = int(rng.split("-")[1])
                            data = data[: end + 1]
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
        yield f"http://127.0.0.1:{server.server_port}", recorded_queries
    finally:
        server.shutdown()
        server.server_close()


def test_azure_blob_provider_end_to_end(azure_mock_server, tmp_path):
    base_host, recorded = azure_mock_server
    url = f"{base_host}/myacct/mycontainer?restype=container&comp=list"
    base_url, acct, cont, sas = parse_azure_blob_url(url)
    provider = AzureBlobProvider(base_url, acct, cont, sas_params=sas)

    assert provider.get_prompt_prefix() == "az://myacct/mycontainer/"
    provider.head_bucket()

    # Unbounded list automatically follows NextMarker across both pages
    dirs, files, token = provider.list_objects("")
    assert dirs == ["configs", "empty_adls_dir"]
    assert [f["name"] for f in files] == [".env", "readme.txt"]
    assert token is None

    # Paginated list returns NextMarker
    dirs_p1, files_p1, next_tok = provider.list_objects("", limit=1)
    assert next_tok == "page2"
    assert [f["name"] for f in files_p1] == [".env"]

    # Sub-prefix list
    sub_dirs, sub_files, _ = provider.list_objects("configs/")
    assert sub_dirs == []
    assert [f["name"] for f in sub_files] == ["app.yml"]

    # Object read, range read, metadata, download, read-only upload
    assert provider.get_object("readme.txt") == b"hello azure blob world"
    assert provider.read_object_range("readme.txt", 5) == b"hello"

    meta = provider.get_object_metadata("readme.txt")
    assert meta["size"] == 22
    assert meta["content_type"] == "text/plain"

    dest = tmp_path / "out.txt"
    provider.download_file("readme.txt", str(dest))
    assert dest.read_bytes() == b"hello azure blob world"

    with pytest.raises(NotImplementedError):
        provider.upload_file(str(dest), "readme.txt")


def test_azure_blob_sas_forwarding_and_redaction(azure_mock_server):
    base_host, _ = azure_mock_server

    # Without SAS token -> 403 PermissionError
    base_url, acct, cont, sas = parse_azure_blob_url(f"{base_host}/myacct/sascontainer")
    denied_provider = AzureBlobProvider(base_url, acct, cont, sas_params=sas)
    with pytest.raises(PermissionError):
        denied_provider.head_bucket()

    # With SAS token -> succeeds, and token never leaks in prompt/stats
    sas_url = f"{base_host}/myacct/sascontainer?sv=2024-11-04&sp=rl&sig=valid-token"
    base_url, acct, cont, sas = parse_azure_blob_url(sas_url)
    provider = AzureBlobProvider(base_url, acct, cont, sas_params=sas)
    provider.head_bucket()
    assert provider.get_object("readme.txt") == b"hello azure blob world"
    assert "valid-token" not in provider.get_prompt_prefix()
    assert "valid-token" not in str(provider.get_bucket_stats())


def test_cli_one_shot_azure_enum(azure_mock_server, capsys):
    base_host, _ = azure_mock_server
    url = f"{base_host}/myacct/mycontainer?restype=container&comp=list"

    cli.main(["--url", url, "-c", "enum --min-severity high"])
    out = capsys.readouterr().out
    assert ".env" in out
    assert "CRITICAL" in out
