# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in BucketBoss (such as path traversal, unsafe URL/redirect handling, command injection, or credential leakage), please report it privately rather than opening a public issue:

- Use **GitHub Security Advisories** (`Security` -> `Advisories` -> `Report a vulnerability`) on this repository.

Please include reproduction steps, affected version, and impact. Verified vulnerabilities will be patched promptly and credited if desired.

## Security Design Notes

BucketBoss is often pointed at untrusted or adversarial cloud buckets during security research. By design:

- All remote-key downloads (`get`, `mirror`, `enum --download`, `th`, `diff`) validate resolved local paths against the target directory to block path traversal (`../`, absolute paths, Windows drive/backslash tricks).
- Direct HTTP/XML providers (`s3xml`, `azure`) restrict URLs and HTTP redirects to `http`/`https` schemes and bound XML listing response sizes.
- SAS tokens supplied via Azure container URLs are never rendered in shell prompts, banners, or exported reports.
- Local caches and exported reports are written with owner-only permissions (`0600` / `0700`) where supported by the OS.
