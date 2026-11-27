import os
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from .base import CloudProvider
from ._http import DEFAULT_TIMEOUT, read_bounded, safe_urlopen

_AZURE_BLOB_HOST_SUFFIXES = (
    '.blob.core.windows.net',
    '.blob.core.usgovcloudapi.net',
    '.blob.core.chinacloudapi.cn',
    '.blob.core.cloudapi.de',
)

# Query params managed by List Blobs operations (stripped from user URL, while SAS params are preserved)
_LIST_QUERY_KEYS = {'restype', 'comp', 'prefix', 'delimiter', 'marker', 'maxresults', 'include'}


def is_azure_blob_url(url: str) -> bool:
    """Return True if the URL looks like a Microsoft Azure Blob container URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if any(host.endswith(suffix) for suffix in _AZURE_BLOB_HOST_SUFFIXES):
        return True
    query_keys = {k.lower(): v.lower() for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
    return query_keys.get('restype') == 'container'


def parse_azure_blob_url(url: str) -> Tuple[str, str, str, List[Tuple[str, str]]]:
    """Parse an Azure Blob container URL into (base_url, account_name, container_name, sas_params).

    Supported formats:
      - https://ACCOUNT.blob.core.windows.net/CONTAINER
      - https://ACCOUNT.blob.core.windows.net/CONTAINER?restype=container&comp=list
      - https://ACCOUNT.blob.core.windows.net/CONTAINER?sv=...&sp=rl&sig=... (SAS token)
      - http://127.0.0.1:10000/devstoreaccount1/CONTAINER (Azurite)
      - https://custom-host/CONTAINER (custom domain / reverse proxy)
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise ValueError(f"Unsupported URL scheme '{parsed.scheme}': only http and https are allowed")

    host = parsed.hostname or ''
    if not host:
        raise ValueError(f"Missing hostname in URL: {url}")

    port_str = f":{parsed.port}" if parsed.port else ''
    path_parts = [p for p in parsed.path.split('/') if p]
    if not path_parts:
        raise ValueError(f"Cannot determine container name from URL: {url}")

    # Preserve SAS query parameters while stripping list-operation query params
    sas_params = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _LIST_QUERY_KEYS
    ]

    host_lower = host.lower()
    for suffix in _AZURE_BLOB_HOST_SUFFIXES:
        if host_lower.endswith(suffix):
            account_name = host[:-len(suffix)]
            container_name = path_parts[0]
            base_url = f"{scheme}://{host}{port_str}/{container_name}"
            return base_url, account_name, container_name, sas_params

    # Azurite path-style (e.g. http://127.0.0.1:10000/devstoreaccount1/container)
    if len(path_parts) >= 2:
        account_name = path_parts[0]
        container_name = path_parts[1]
        base_url = f"{scheme}://{host}{port_str}/{account_name}/{container_name}"
        return base_url, account_name, container_name, sas_params

    # Custom domain / single-segment container path
    account_name = host
    container_name = path_parts[0]
    base_url = f"{scheme}://{host}{port_str}/{container_name}"
    return base_url, account_name, container_name, sas_params


class AzureBlobProvider(CloudProvider):
    """Microsoft Azure Blob container provider using raw HTTP/XML (?restype=container&comp=list)."""

    def __init__(
        self,
        base_url: str,
        account_name: str,
        container_name: str,
        sas_params: Optional[List[Tuple[str, str]]] = None,
    ):
        self.base_url = base_url.rstrip('/')
        self.account_name = account_name
        self.container_name = container_name
        self.bucket_name = f"{account_name}/{container_name}" if account_name else container_name
        self.sas_params = list(sas_params) if sas_params else []

    def get_prompt_prefix(self) -> str:
        if self.account_name:
            return f"az://{self.account_name}/{self.container_name}/"
        return f"az://{self.container_name}/"

    def _build_container_url(self, params: List[Tuple[str, str]]) -> str:
        all_params = list(params) + self.sas_params
        return f"{self.base_url}?{urlencode(all_params)}"

    def _build_blob_url(self, key: str) -> str:
        url = f"{self.base_url}/{quote(key, safe='/')}"
        if self.sas_params:
            url = f"{url}?{urlencode(self.sas_params)}"
        return url

    def head_bucket(self):
        # Azure List Blobs requires maxresults >= 1 (0 returns HTTP 400)
        url = self._build_container_url([
            ('restype', 'container'),
            ('comp', 'list'),
            ('maxresults', '1'),
        ])
        try:
            req = urllib.request.Request(url, method='GET')
            with safe_urlopen(req, timeout=DEFAULT_TIMEOUT):
                pass
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise PermissionError(f"Access denied to Azure Blob container '{self.container_name}'")
            elif e.code == 404:
                raise FileNotFoundError(f"Azure Blob container '{self.container_name}' not found")
            else:
                raise ConnectionError(f"HTTP {e.code}: {e.reason}")

    def list_objects(
        self,
        prefix: str,
        sort_key: str = 'name',
        limit: Optional[int] = None,
        next_token: Optional[str] = None,
    ) -> Tuple[List[str], List[dict], Optional[str]]:
        directories: List[str] = []
        files: List[dict] = []
        next_marker: Optional[str] = None

        try:
            directories, files, next_marker = self._list_blobs_page(prefix, limit, next_token)
        except urllib.error.HTTPError as e:
            self._handle_http_error(e, f"listing blobs at '{prefix}'")
            return [], [], None
        except urllib.error.URLError as e:
            print(f"Error listing blobs at '{prefix}': {e.reason}", file=sys.stderr)
            return [], [], None

        if limit is None and next_marker:
            seen_markers = {next_marker}
            while next_marker:
                try:
                    more_dirs, more_files, next_marker = self._list_blobs_page(prefix, None, next_marker)
                    directories.extend(more_dirs)
                    files.extend(more_files)
                    if next_marker:
                        if next_marker in seen_markers:
                            break
                        seen_markers.add(next_marker)
                except Exception:
                    break
            next_marker = None

        directories = sorted(set(directories))
        if sort_key == 'name':
            files.sort(key=lambda x: x['name'])
        elif sort_key == 'date':
            files.sort(key=lambda x: x['last_modified'], reverse=True)
        elif sort_key == 'size':
            files.sort(key=lambda x: x['size'], reverse=True)

        return directories, files, next_marker

    def _list_blobs_page(
        self, prefix: str, limit: Optional[int], marker: Optional[str]
    ) -> Tuple[List[str], List[dict], Optional[str]]:
        params: List[Tuple[str, str]] = [
            ('restype', 'container'),
            ('comp', 'list'),
            ('delimiter', '/'),
        ]
        if prefix:
            params.append(('prefix', prefix))
        if limit is not None:
            params.append(('maxresults', str(max(1, min(limit, 5000)))))
        if marker:
            params.append(('marker', marker))

        url = self._build_container_url(params)
        req = urllib.request.Request(url, method='GET')
        with safe_urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            body = read_bounded(resp)
        return self._parse_list_response(body, prefix)

    def _parse_list_response(
        self, body: bytes, prefix: str
    ) -> Tuple[List[str], List[dict], Optional[str]]:
        directories: List[str] = []
        files: List[dict] = []
        next_marker: Optional[str] = None

        # Azure Storage XML responses frequently include a UTF-8 BOM
        cleaned_body = body.lstrip(b'\xef\xbb\xbf')
        root = ET.fromstring(cleaned_body)

        ns = ''
        if root.tag.startswith('{'):
            ns = root.tag.split('}')[0] + '}'

        blobs_elem = root.find(f'{ns}Blobs')
        if blobs_elem is not None:
            for bp in blobs_elem.findall(f'{ns}BlobPrefix'):
                name_elem = bp.find(f'{ns}Name')
                if name_elem is not None and name_elem.text:
                    dir_path = name_elem.text
                    dir_name = dir_path[len(prefix):].rstrip('/') if dir_path.startswith(prefix) else dir_path.rstrip('/')
                    if dir_name:
                        directories.append(dir_name)

            for blob in blobs_elem.findall(f'{ns}Blob'):
                name_elem = blob.find(f'{ns}Name')
                if name_elem is None or not name_elem.text:
                    continue
                blob_name = name_elem.text
                if blob_name == prefix:
                    continue

                props = blob.find(f'{ns}Properties')
                resource_type = None
                size = 0
                last_modified_text = None
                if props is not None:
                    rt_elem = props.find(f'{ns}ResourceType')
                    if rt_elem is not None and rt_elem.text:
                        resource_type = rt_elem.text.strip().lower()
                    size_elem = props.find(f'{ns}Content-Length')
                    if size_elem is not None and size_elem.text:
                        try:
                            size = int(size_elem.text.strip())
                        except ValueError:
                            size = 0
                    lm_elem = props.find(f'{ns}Last-Modified')
                    if lm_elem is not None and lm_elem.text:
                        last_modified_text = lm_elem.text.strip()

                rel_name = blob_name[len(prefix):] if blob_name.startswith(prefix) else blob_name
                if not rel_name:
                    continue

                # ADLS Gen2 / directory marker blobs
                if resource_type == 'directory' or rel_name.endswith('/'):
                    dir_name = rel_name.rstrip('/')
                    if dir_name:
                        directories.append(dir_name)
                    continue

                files.append({
                    'name': rel_name,
                    'size': size,
                    'last_modified': self._parse_datetime(last_modified_text),
                    'extension': os.path.splitext(rel_name)[1].lower(),
                })

        marker_elem = root.find(f'{ns}NextMarker')
        if marker_elem is not None and marker_elem.text and marker_elem.text.strip():
            next_marker = marker_elem.text.strip()

        return directories, files, next_marker

    def _parse_datetime(self, dt_str: Optional[str]) -> datetime:
        if not dt_str:
            return datetime.now(timezone.utc)
        try:
            return parsedate_to_datetime(dt_str)
        except Exception:
            pass
        try:
            return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        except ValueError:
            return datetime.now(timezone.utc)

    def resolve_path(self, current_prefix: str, input_path: str, is_directory: bool = False) -> str:
        if input_path.startswith('/'):
            path_parts = input_path.lstrip('/').split('/')
        else:
            current_parts = current_prefix.rstrip('/').split('/') if current_prefix else []
            input_parts = input_path.split('/')
            path_parts = current_parts + input_parts

        normalized_parts = []
        for part in path_parts:
            if part == '..':
                if normalized_parts:
                    normalized_parts.pop()
            elif part and part != '.':
                normalized_parts.append(part)

        normalized_path = '/'.join(normalized_parts)

        if is_directory and normalized_path:
            normalized_path += '/'
        elif not is_directory and normalized_path.endswith('/') and normalized_path != '/':
            normalized_path = normalized_path.rstrip('/')

        return normalized_path

    def get_object(self, key: str) -> bytes:
        url = self._build_blob_url(key)
        try:
            req = urllib.request.Request(url, method='GET')
            with safe_urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            self._handle_http_error(e, f"getting blob '{key}'")
            raise
        except urllib.error.URLError as e:
            print(f"Error getting blob '{key}': {e.reason}", file=sys.stderr)
            raise

    def download_file(self, key: str, local_path: str):
        url = self._build_blob_url(key)
        try:
            req = urllib.request.Request(url, method='GET')
            with safe_urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                with open(local_path, 'wb') as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
        except urllib.error.HTTPError as e:
            self._handle_http_error(e, f"downloading '{key}'")
            raise
        except urllib.error.URLError as e:
            print(f"Error downloading '{key}': {e.reason}", file=sys.stderr)
            raise

    def upload_file(self, local_path: str, key: str):
        raise NotImplementedError("Azure Blob HTTP/XML provider is read-only")

    def read_object_range(self, key: str, size: int) -> bytes:
        if size <= 0:
            raise ValueError(f"Size must be positive, got: {size}")
        url = self._build_blob_url(key)
        try:
            req = urllib.request.Request(url, method='GET')
            req.add_header('Range', f'bytes=0-{size - 1}')
            with safe_urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            self._handle_http_error(e, f"reading range of '{key}'")
            raise
        except urllib.error.URLError as e:
            print(f"Error reading range of '{key}': {e.reason}", file=sys.stderr)
            raise

    def get_object_metadata(self, key: str) -> dict:
        url = self._build_blob_url(key)
        try:
            req = urllib.request.Request(url, method='HEAD')
            with safe_urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                headers = resp.headers

            size = int(headers.get('Content-Length', 0))
            content_type = headers.get('Content-Type', 'application/octet-stream')
            last_modified = self._parse_datetime(headers.get('Last-Modified'))

            return {
                'size': size,
                'last_modified': last_modified,
                'content_type': content_type,
            }
        except urllib.error.HTTPError as e:
            self._handle_http_error(e, f"getting metadata for '{key}'")
            raise
        except urllib.error.URLError as e:
            print(f"Error getting metadata for '{key}': {e.reason}", file=sys.stderr)
            raise

    def get_bucket_stats(self) -> dict:
        return {
            "Account": self.account_name or "unknown",
            "Container": self.container_name,
            "Transport": "Azure Blob HTTP/XML (SAS)" if self.sas_params else "Azure Blob HTTP/XML (public)",
            "Size": "N/A",
        }

    def _handle_http_error(self, e: urllib.error.HTTPError, context: str):
        if e.code in (401, 403):
            print(f"Access denied ({context})", file=sys.stderr)
        elif e.code == 404:
            print(f"Not found ({context})", file=sys.stderr)
        else:
            print(f"HTTP {e.code}: {e.reason} ({context})", file=sys.stderr)
