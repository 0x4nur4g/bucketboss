import json
import subprocess

import pytest

from bucketboss.app import BucketBossApp
from bucketboss.cli import _resolve_url_provider, parse_args
from bucketboss.commands.findings import do_export, do_tag
from bucketboss.commands.info import do_du
from bucketboss.commands.recon import do_enum, do_th
from bucketboss.providers.azureblob import AzureBlobProvider
from bucketboss.providers.s3xml import S3XMLProvider


class _TreeProvider:
    def __init__(self, tree):
        self.bucket_name = "demo-bucket"
        self.tree = tree

    def get_prompt_prefix(self):
        return "s3://demo-bucket/"

    def head_bucket(self):
        pass

    def list_objects(self, prefix, sort_key="name", limit=None, next_token=None):
        return self.tree.get(prefix, ([], [], None))

    def resolve_path(self, current_prefix, input_path, is_directory=False):
        path = (current_prefix + input_path).lstrip("/")
        if is_directory and path and not path.endswith("/"):
            path += "/"
        return path

    def get_object(self, key):
        return b"content"

    def download_file(self, key, local_path):
        with open(local_path, "wb") as f:
            f.write(b"content")

    def upload_file(self, local_path, key):
        pass

    def read_object_range(self, key, size):
        return b"content"[:size]

    def get_object_metadata(self, key):
        return {"size": 7, "last_modified": None, "content_type": "text/plain"}

    def get_bucket_stats(self):
        return {"Size": "N/A"}


def test_parse_args_and_url_provider_resolution():
    args = parse_args(["--url", "https://acct.blob.core.windows.net/cont", "-c", "ls"])
    assert args.provider is None
    assert args.command == "ls"
    provider = _resolve_url_provider(args.url, args.provider)
    assert isinstance(provider, AzureBlobProvider)

    s3_provider = _resolve_url_provider("https://bucket.s3.amazonaws.com/", None)
    assert isinstance(s3_provider, S3XMLProvider)

    forced_azure = _resolve_url_provider("http://127.0.0.1:10000/devstoreaccount1/c1", "azure")
    assert isinstance(forced_azure, AzureBlobProvider)

    with pytest.raises(SystemExit):
        parse_args(["--session-token", "tok-only"])

    with pytest.raises(SystemExit):
        parse_args(["--profile", "p", "--no-sign-request"])


def test_non_interactive_app_and_no_audit_placeholder():
    app = BucketBossApp(_TreeProvider({}), interactive=False)
    assert app.session is None
    assert "audit" not in app.commands


def test_enum_report_subcommand_and_export_output(tmp_path, capsys):
    provider = _TreeProvider(
        {
            "": (
                [],
                [{"name": ".env", "size": 42}, {"name": "logo.png", "size": 100}],
                None,
            )
        }
    )
    app = BucketBossApp(provider, interactive=False)
    app.config = {"general": {"workers": 1}}

    do_enum(app)
    capsys.readouterr()

    # Both 'enum report' and 'enum_report' work
    do_enum(app, "report", "--format", "json")
    report_json = json.loads(capsys.readouterr().out)
    assert report_json["total_files"] == 2
    assert report_json["classified"]["critical"][0]["key"] == ".env"

    # Manual tag + export --output FILE and --output -
    do_tag(app, ".env", "confirmed secret", "--severity", "critical")
    capsys.readouterr()

    out_file = tmp_path / "sub" / "report.json"
    do_export(app, "--format", "json", "--output", str(out_file))
    exported = json.loads(out_file.read_text())
    assert exported["summary"]["total"] >= 2

    capsys.readouterr()
    do_export(app, "--format", "json", "--output", "-")
    stdout_json = json.loads(capsys.readouterr().out)
    assert stdout_json["bucket"] == "demo-bucket"


def test_du_respects_depth_flag(capsys):
    provider = _TreeProvider(
        {
            "": (["a"], [], None),
            "a/": (["b"], [{"name": "top.txt", "size": 100}], None),
            "a/b/": ([], [{"name": "deep.txt", "size": 250}], None),
        }
    )
    app = BucketBossApp(provider, interactive=False)
    app.config = {"general": {"workers": 1}}

    do_du(app, "--depth", "1")
    out_d1 = capsys.readouterr().out
    assert "a/" in out_d1
    assert "a/b/" not in out_d1

    do_du(app, "--depth", "2")
    out_d2 = capsys.readouterr().out
    assert "a/" in out_d2
    assert "a/b/" in out_d2


def test_th_reports_nonzero_exit_code_as_error(monkeypatch, capsys):
    provider = _TreeProvider({"": ([], [{"name": ".env", "size": 10}], None)})
    app = BucketBossApp(provider, interactive=False)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/trufflehog")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="boom failure")

    monkeypatch.setattr("subprocess.run", fake_run)

    do_th(app, ".env")
    out = capsys.readouterr().out
    assert "TruffleHog failed: boom failure" in out
    assert "No secrets found" not in out
