import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed


def safe_filename_component(name: str, fallback: str = 'bucket') -> str:
    """Sanitize an identifier (such as bucket/container name) for safe use in a single filename."""
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', (name or '').strip()).strip('._-')
    return cleaned or fallback


def safe_local_path(base_dir: str, relative_key: str, flat: bool = False) -> str:
    """Resolve a remote object key safely inside base_dir, rejecting path traversal.

    Raises ValueError if the key is empty, contains NUL bytes, drive specifiers,
    parent traversal ('..'), or resolves outside base_dir.
    """
    if not relative_key or '\x00' in relative_key:
        raise ValueError(f"Unsafe remote key: {relative_key!r}")

    normalized = relative_key.replace('\\', '/')
    if normalized.startswith('/'):
        raise ValueError(f"Unsafe absolute remote key: {relative_key!r}")

    raw_parts = [p for p in normalized.split('/') if p and p != '.']
    if not raw_parts:
        raise ValueError(f"Unsafe remote key: {relative_key!r}")

    for part in raw_parts:
        if part == '..' or ':' in part:
            raise ValueError(f"Unsafe remote key escapes target directory: {relative_key!r}")

    parts = [raw_parts[-1]] if flat else raw_parts

    base_real = os.path.realpath(os.path.abspath(base_dir))
    target_real = os.path.realpath(os.path.join(base_real, *parts))

    if os.path.commonpath([base_real, target_real]) != base_real or target_real == base_real:
        raise ValueError(f"Unsafe remote key escapes target directory: {relative_key!r}")

    return target_real


def get_workers_from_app(app):
    """Get worker count from app config, with fallback."""
    config = getattr(app, 'config', None)
    if config:
        return config.get("general", {}).get("workers", 16)
    return 16


def parallel_list(app, prefixes, sort_key='name'):
    """List multiple prefixes in parallel.

    Returns a dict mapping prefix → (dirs, files).
    """
    workers = get_workers_from_app(app)
    results = {}

    if workers <= 1 or len(prefixes) <= 1:
        for prefix in prefixes:
            dirs, files, _ = app.list_objects(prefix, sort_key)
            results[prefix] = (dirs, files)
        return results

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_prefix = {}
        for prefix in prefixes:
            future = executor.submit(app.list_objects, prefix, sort_key)
            future_to_prefix[future] = prefix

        for future in as_completed(future_to_prefix):
            prefix = future_to_prefix[future]
            try:
                dirs, files, _ = future.result()
                results[prefix] = (dirs, files)
            except Exception:
                results[prefix] = ([], [])

    return results


def parallel_walk(app, root_prefix, max_depth=5, workers=16, progress_callback=None):
    """Parallel breadth-first directory walk.

    Returns:
        all_files: list of (full_key, file_info_dict)
        all_dirs: list of (full_prefix, depth)
        total_size: int — sum of all file sizes
    """
    all_files = []
    all_dirs = []
    total_size = 0

    if workers <= 1:
        return _sequential_walk(app, root_prefix, max_depth, progress_callback)

    # BFS with thread pool
    current_level = [(root_prefix, 1)]  # (prefix, depth)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while current_level:
            next_level = []
            # Submit all prefixes at current level in parallel
            future_to_prefix = {}
            for prefix, depth in current_level:
                future = executor.submit(app.list_objects, prefix)
                future_to_prefix[future] = (prefix, depth)

            for future in as_completed(future_to_prefix):
                prefix, depth = future_to_prefix[future]
                try:
                    dirs, files, _ = future.result()
                except Exception:
                    continue

                # Collect files
                for f in files:
                    full_key = prefix + f['name']
                    all_files.append((full_key, f))
                    total_size += f.get('size', 0)

                # Collect dirs and queue next level
                for d in dirs:
                    full_dir = prefix + d + '/'
                    all_dirs.append((full_dir, depth))
                    if depth < max_depth:
                        next_level.append((full_dir, depth + 1))

                if progress_callback:
                    progress_callback(len(all_files), len(all_dirs), total_size)

            current_level = next_level

    return all_files, all_dirs, total_size


def _sequential_walk(app, root_prefix, max_depth, progress_callback=None):
    """Sequential fallback for parallel_walk (workers=1)."""
    all_files = []
    all_dirs = []
    total_size = 0

    def _walk(prefix, depth):
        nonlocal total_size
        dirs, files, _ = app.list_objects(prefix)

        for f in files:
            full_key = prefix + f['name']
            all_files.append((full_key, f))
            total_size += f.get('size', 0)

        if depth < max_depth:
            for d in dirs:
                full_dir = prefix + d + '/'
                all_dirs.append((full_dir, depth))
                _walk(full_dir, depth + 1)

        if progress_callback:
            progress_callback(len(all_files), len(all_dirs), total_size)

    _walk(root_prefix, 1)
    return all_files, all_dirs, total_size


def parallel_download(app, file_keys, local_base_dir, workers=16, flat=False,
                      max_size=None, progress_callback=None, strip_prefix=''):
    """Download multiple files in parallel.

    Args:
        file_keys: list of (remote_key, file_size) tuples
        local_base_dir: local directory to download into
        workers: thread count
        flat: if True, all files go into local_base_dir directly (no subdirs)
        max_size: skip files larger than this (bytes)
        progress_callback: called with (completed_count, total_count, current_file)
        strip_prefix: optional remote prefix to strip when mapping to local paths

    Returns:
        downloaded: list of (remote_key, local_path) tuples
        skipped: list of (remote_key, reason) tuples
        errors: list of (remote_key, error_msg) tuples
    """
    downloaded = []
    skipped = []
    errors = []

    # Filter by max_size upfront
    to_download = []
    for remote_key, file_size in file_keys:
        if max_size is not None and file_size > max_size:
            skipped.append((remote_key, 'exceeds max-size'))
            continue
        to_download.append((remote_key, file_size))

    total_count = len(to_download)

    if not to_download:
        return downloaded, skipped, errors

    def _download_one(remote_key, file_size):
        """Download a single file. Returns (remote_key, local_path) or raises."""
        rel_key = (
            remote_key[len(strip_prefix):]
            if strip_prefix and remote_key.startswith(strip_prefix)
            else remote_key
        )
        local_path = safe_local_path(local_base_dir, rel_key, flat=flat)

        local_parent = os.path.dirname(local_path)
        if local_parent:
            os.makedirs(local_parent, exist_ok=True)

        app.provider.download_file(remote_key, local_path)
        return remote_key, local_path

    if workers <= 1:
        for idx, (remote_key, file_size) in enumerate(to_download):
            try:
                rk, lp = _download_one(remote_key, file_size)
                downloaded.append((rk, lp))
            except Exception as e:
                errors.append((remote_key, str(e)))
            if progress_callback:
                progress_callback(len(downloaded) + len(errors), total_count, remote_key)
        return downloaded, skipped, errors

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_key = {}
        for remote_key, file_size in to_download:
            future = executor.submit(_download_one, remote_key, file_size)
            future_to_key[future] = remote_key

        for future in as_completed(future_to_key):
            remote_key = future_to_key[future]
            try:
                rk, lp = future.result()
                downloaded.append((rk, lp))
            except Exception as e:
                errors.append((remote_key, str(e)))

            if progress_callback:
                progress_callback(len(downloaded) + len(errors), total_count, remote_key)

    return downloaded, skipped, errors
