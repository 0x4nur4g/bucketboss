import os
import pytest

from bucketboss.commands.recon import do_enum
from bucketboss.commands.transfer import do_mirror
from bucketboss.parallel import parallel_download, safe_filename_component, safe_local_path


class _FakeProvider:
    def __init__(self, bucket_name="test-bucket", tree=None):
        self.bucket_name = bucket_name
        self.tree = tree or {}
        self.downloaded = []

    def get_prompt_prefix(self):
        return f"s3://{self.bucket_name}/"

    def resolve_path(self, current_prefix, input_path, is_directory=False):
        path = (current_prefix + input_path).lstrip("/")
        if is_directory and path and not path.endswith("/"):
            path += "/"
        return path

    def list_objects(self, prefix, sort_key="name", limit=None, next_token=None):
        return self.tree.get(prefix, ([], [], None))

    def download_file(self, key, local_path):
        self.downloaded.append((key, local_path))
        with open(local_path, "wb") as f:
            f.write(b"payload:" + key.encode())


class _FakeApp:
    def __init__(self, provider):
        self.provider = provider
        self.current_prefix = ""
        self.config = {"general": {"workers": 2}}

    def list_objects(self, prefix, sort_key="name", limit=None, next_token=None):
        return self.provider.list_objects(prefix, sort_key, limit=limit, next_token=next_token)


@pytest.mark.parametrize(
    "bad_key",
    [
        "../outside.txt",
        "../../etc/passwd",
        "subdir/../../outside.txt",
        "/etc/passwd",
        "\\windows\\system32\\cmd.exe",
        "..\\..\\outside.txt",
        "C:evil.txt",
        "dir/C:evil.txt",
        "good\x00evil.txt",
        "",
        ".",
        "..",
        "/",
    ],
)
def test_safe_local_path_rejects_traversal(tmp_path, bad_key):
    with pytest.raises(ValueError):
        safe_local_path(str(tmp_path), bad_key, flat=False)


def test_safe_local_path_rejects_flat_traversal(tmp_path):
    for bad_key in ("../secret", "..", "/etc/passwd", "..\\secret", "C:file.txt"):
        with pytest.raises(ValueError):
            safe_local_path(str(tmp_path), bad_key, flat=True)


def test_safe_local_path_allows_valid_keys(tmp_path):
    nested = safe_local_path(str(tmp_path), "configs/prod/.env", flat=False)
    assert nested == os.path.realpath(os.path.join(str(tmp_path), "configs", "prod", ".env"))

    flat = safe_local_path(str(tmp_path), "configs/prod/.env", flat=True)
    assert flat == os.path.realpath(os.path.join(str(tmp_path), ".env"))


def test_safe_filename_component():
    assert safe_filename_component("myaccount/mycontainer") == "myaccount_mycontainer"
    assert safe_filename_component("../../evil") == "evil"
    assert safe_filename_component("///", fallback="default") == "default"


def test_parallel_download_blocks_traversal_and_writes_safe_files(tmp_path):
    target_dir = tmp_path / "downloads"
    target_dir.mkdir()
    escaped_file = tmp_path / "pwned.txt"

    provider = _FakeProvider()
    app = _FakeApp(provider)

    downloaded, skipped, errors = parallel_download(
        app,
        [
            ("safe/file.txt", 10),
            ("../pwned.txt", 10),
            ("a/../../pwned2.txt", 10),
            ("/etc/passwd", 10),
        ],
        str(target_dir),
        workers=2,
    )

    assert len(downloaded) == 1
    assert downloaded[0][0] == "safe/file.txt"
    assert (target_dir / "safe" / "file.txt").read_bytes() == b"payload:safe/file.txt"
    assert len(errors) == 3
    assert not escaped_file.exists()
    assert not (tmp_path / "pwned2.txt").exists()


def test_mirror_strips_prefix_without_duplication(tmp_path):
    provider = _FakeProvider(
        tree={
            "configs/": (
                ["prod"],
                [{"name": "app.yml", "size": 12}],
                None,
            ),
            "configs/prod/": (
                [],
                [{"name": ".env", "size": 8}, {"name": "../../evil.txt", "size": 5}],
                None,
            ),
        }
    )
    app = _FakeApp(provider)
    out_dir = tmp_path / "out"

    do_mirror(app, "configs/", str(out_dir))

    # Prefix 'configs/' is stripped (not duplicated as out/configs/app.yml)
    assert (out_dir / "app.yml").exists()
    assert (out_dir / "prod" / ".env").exists()
    assert not (out_dir / "configs").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_enum_download_blocks_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = _FakeProvider(
        bucket_name="acct/cont",
        tree={
            "": (
                [],
                [
                    {"name": ".env", "size": 10},
                    {"name": "../.env", "size": 10},
                ],
                None,
            )
        },
    )
    app = _FakeApp(provider)

    do_enum(app, "--download")

    assert (tmp_path / "bb-enum-acct_cont" / ".env").exists()
    assert not (tmp_path / ".env").exists()
