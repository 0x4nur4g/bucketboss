# BucketBoss

**Interactive cloud storage shell and security recon CLI for AWS S3 and Microsoft Azure Blob containers.**

Navigate, inspect, classify, diff, mirror, and hunt for secrets inside cloud buckets as smoothly as a local shell — interactively with fuzzy tab-completion or non-interactively in scripts with `-c`.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](pyproject.toml)

---

## Why BucketBoss

When triaging exposed or misconfigured cloud storage, bouncing between `aws s3 ls`, `curl`, `xmllint`, and throwaway download scripts is slow and error-prone. BucketBoss gives security researchers and cloud engineers a single fast interface across AWS S3 and Azure Blob Storage:

- **AWS S3 (`s3` & `s3xml`)**: Full boto3 support (ambient AWS creds/SSO/IMDS, `--profile`, `--access-key`/`--secret-key`/`--session-token`, `--no-sign-request`, multi-bucket mode) plus zero-SDK raw HTTP/XML for public S3 and S3-compatible endpoints (MinIO, R2, Spaces) with automatic ListObjectsV2 → V1 fallback.
- **Microsoft Azure Blob Containers (`azure`)**: Zero-SDK container enumeration via `?restype=container&comp=list` with automatic URL detection (`*.blob.core.windows.net`, sovereign clouds, or `?restype=container`), full `NextMarker` pagination, UTF-8 BOM handling, ADLS Gen2 directory markers, Azurite/custom host support (`--provider azure`), and container SAS token forwarding (redacted from prompts, banners, and reports).
- **Security Recon Built In**:
  - `enum` — parallel BFS crawl + 4-tier severity classification (`critical`, `high`, `medium`, `info`) backed by 100+ filename/path/directory rules (`.env`, private keys, `terraform.tfstate`, backups, CI configs, etc.) + `enum report` (`text` / `json` / `md`).
  - `th` — built-in [TruffleHog](https://github.com/trufflesecurity/trufflehog) integration that downloads targets to an isolated temp directory, runs secret scanning, maps findings back to remote keys, and cleans up.
  - `tag`, `findings`, `export` — track annotations alongside `enum` and `th` hits and export Markdown, JSON, or text reports (`--output FILE` or `-` for stdout).
- **Fast Shell UX + Pipeline Mode**:
  - Parallel thread-pool walks and downloads (`workers` configurable), local TTL directory cache + background pre-crawl, fuzzy Tab completion, `tree`, `du`, `find`, `diff`, `peek`, `head`, `mirror`.
  - Non-interactive one-shot mode (`-c "<command>"`) for scripting and piping into `jq` or other tools.
- **Hardened Against Adversarial Buckets**:
  - Remote-key path traversal containment on all downloads (`mirror`, `get`, `enum --download`, `th`, `diff`).
  - Strict `http`/`https` scheme and redirect validation on HTTP/XML providers plus bounded XML listing reads.

---

## Installation

### With `uv` (recommended)

```bash
uv tool install git+https://github.com/0x4nur4g/bucketboss.git
```

### With `pip`

```bash
pip install git+https://github.com/0x4nur4g/bucketboss.git
```

### From source (development)

```bash
git clone https://github.com/0x4nur4g/bucketboss.git
cd bucketboss
uv pip install -e ".[dev]"
```

---

## Quick Start

### AWS S3 (boto3)

```bash
# Public / anonymous bucket (falls back to unsigned if no ambient AWS creds exist)
bb --bucket my-public-bucket

# Force unsigned requests even if AWS credentials are configured locally
bb --bucket my-public-bucket --no-sign-request

# Using an AWS CLI profile (omit --bucket for multi-bucket shell)
bb --profile prod-readonly
bb --profile prod-readonly --bucket app-artifacts

# Explicit or temporary STS credentials
bb --bucket app-artifacts --access-key AKIA... --secret-key ... --session-token ...
```

### AWS S3 / S3-Compatible (zero-SDK HTTP/XML)

```bash
bb --url https://my-bucket.s3.us-west-2.amazonaws.com/
bb --url https://s3.amazonaws.com/my-bucket/
bb --url https://minio.example.com:9000/public-assets/
```

### Microsoft Azure Blob Container (`?restype=container&comp=list`)

```bash
# Public container (auto-detected from *.blob.core.windows.net)
bb --url https://myaccount.blob.core.windows.net/mycontainer

# Pasting a full listing URL works directly
bb --url "https://myaccount.blob.core.windows.net/mycontainer?restype=container&comp=list"

# Container SAS URL (SAS query parameters are forwarded on all list/HEAD/GET/Range calls)
bb --url "https://myaccount.blob.core.windows.net/mycontainer?sp=rl&sv=2024-11-04&sr=c&sig=..."

# Azurite or custom reverse-proxy hostname
bb --provider azure --url http://127.0.0.1:10000/devstoreaccount1/mycontainer
```

### Non-Interactive One-Shot Mode (`-c`)

```bash
# Classify high/critical objects in a public Azure container
bb --url https://myaccount.blob.core.windows.net/public -c "enum --min-severity high"

# Search for backup/env files in an S3 bucket
bb --url https://my-bucket.s3.amazonaws.com/ -c "find *.env*"

# Quick scope overview
bb --bucket my-public-bucket --no-sign-request -c "scope"
```

---

## CLI Options

```text
bb [options]

  --bucket BUCKET              S3 bucket name (omit for authenticated multi-bucket mode)
  --url URL                    Direct HTTP/XML URL (S3 bucket or Azure Blob container)
  --provider {s3,s3xml,azure}  Force backend (auto-detected from --url when omitted)
  --config CONFIG_PATH         Config file path (default: ~/.bucketboss/config.json)
  -c, --command COMMAND        Run a single command non-interactively and exit

S3 Authentication:
  --profile PROFILE            AWS CLI profile name
  --access-key ACCESS_KEY      AWS access key ID
  --secret-key SECRET_KEY      AWS secret access key
  --session-token TOKEN        AWS STS temporary session token
  --no-sign-request            Force anonymous/unsigned S3 requests
```

---

## Interactive Commands

Run `help` or `help <command>` inside the shell for detailed syntax.

### Navigation
| Command | Description |
|---|---|
| `ls [-l] [--sort=name\|date\|size] [path]` | List directories and files (paginated) |
| `cd <path>` | Change remote prefix (`cd ..`, `cd /`, `cd dir/`) |
| `tree [path] [--depth N]` | Visual directory tree (default depth: 3) |
| `pwd` | Print full current remote URI |

### Reading & Inspection
| Command | Description |
|---|---|
| `cat <file>` | Display full contents of a remote file (paged when large) |
| `head <file> [lines]` | Show first `N` lines via HTTP Range read (default: 10) |
| `peek <file> [bytes]` | Preview text or hex dump of first `N` bytes (default: 2 KB, max: 10 MB) |
| `open <file>` | Download to a temp file and open with the OS default viewer |
| `info <file>` | Show object metadata (`HEAD`: size, last-modified, content-type, full key) |

### Transfer & Diff
| Command | Description |
|---|---|
| `get <remote_path> [local_path]` | Download a file or wildcard glob (`get "*.pem"`) in parallel |
| `put <local_path> <remote_path>` | Upload a local file (authenticated S3 provider) |
| `mirror <prefix/> [local_dir] [opts]` | Recursive parallel download (`--depth`, `--max-size`, `--dry-run`, `--include`, `--exclude`, `--flat`) |
| `diff <file_a> <file_b>` | Colorized unified diff (text) or SHA-256 comparison (binary) between remote/local files (`./`, `~/`, `/` = local) |

### Search & Security Recon
| Command | Description |
|---|---|
| `find <pattern> [--path prefix] [--depth N]` | Parallel recursive glob search by filename |
| `scope [path]` | Bucket-wide survey: object count, total size, deepest path, top extensions/prefixes, oldest/newest object |
| `enum [path] [--depth N] [--download] [--min-severity LEVEL] [--no-classify]` | Recursive crawl with 4-tier security classification (`critical`, `high`, `medium`, `info`) |
| `enum report [--format text\|json\|md]` | Full report of the last `enum` run (alias: `enum_report`) |
| `th <file_or_dir> [--depth N] [--verified-only] [--json] [--keep] [--max-size N]` | Scan remote file or prefix with TruffleHog (`th status` checks installation) |

### Findings & Reporting
| Command | Description |
|---|---|
| `tag <file> <note> [--severity LEVEL]` | Attach a manual finding note to a remote object |
| `findings [--severity LEVEL] [--source enum\|th\|tag] [--json]` | Unified table/JSON of all `enum`, `th`, and manual findings |
| `export [--format md\|json\|text] [--output FILE\|-]` | Export assessment report to a file (owner-only permissions) or stdout (`-`) |

### Info & Shell
| Command | Description |
|---|---|
| `du [path] [--depth N]` | Parallel disk usage breakdown per directory up to depth `N` |
| `stats` | Provider info + cached content extension/size summary |
| `crawlstatus` | Background cache pre-crawl progress |
| `help [command]`, `clear`, `exit` / `quit` | Shell utilities |

---

## Configuration

Optional configuration lives at `~/.bucketboss/config.json` (or pass `--config <path>`):

```json
{
  "general": {
    "workers": 16,
    "crawl_depth": 2,
    "cache_ttl": 21600,
    "max_download_size": "100MB"
  },
  "trufflehog": {
    "binary": "trufflehog",
    "default_timeout": 300
  }
}
```

- Directory caches are stored per target under `~/.bucketboss_cache/` (`0700` dir / `0600` files).
- Shell history is saved in `~/.bucketboss_history`.

---

## Development & Testing

```bash
ruff check bucketboss tests
pytest -v
```

---

## Responsible Use

BucketBoss is built for authorized security assessments, cloud configuration auditing, bug bounty research within program scope, and managing your own cloud storage. Only inspect buckets and containers you own or have explicit authorization to test.

---

## License

[MIT](LICENSE)
