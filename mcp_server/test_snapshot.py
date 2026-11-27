"""Create deterministic, bounded archives from an observed Git working tree."""
from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import re
import stat
import subprocess
import sys
import tarfile
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

if os.name == "nt":
    from . import _snapshot_windows as win


DEFAULT_EXCLUDES = (
    ".venv", "venv", ".virtualenv", ".conda", "__pycache__", "*.pyc", "*.pyo",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".hypothesis", ".cache",
    ".coverage", "htmlcov", ".eggs", "*.egg-info", "build", "dist", "target", "node_modules",
    ".next", ".nuxt", ".parcel-cache", ".turbo", ".gradle",
)
PROTECTED = (
    ".git", ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".ssh", ".aws", ".gnupg", ".docker",
    ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", "credentials.json",
    "*-credentials.json", "*_credentials.json", ".mcp.json", "settings.local.json",
    ".codex", ".cursor", ".a2a_link", ".a2a-link-snapshots", ".claude.json", "claude_desktop_config.json",
    "terminal-policy.json",
)
POLICY_ENV_VARS = ("A2A_LINK_POLICY", "A2A_LINK_PROFILE_CONFIG")
DEFAULT_MAX_BYTES = 1 << 30
# Bound Git metadata independently of the archive's (potentially large) payload allowance.
MAX_GIT_BYTES = 16 << 20
MAX_CANDIDATES = 100_000


class SnapshotError(RuntimeError):
    """The project or profile cannot be snapshotted safely."""


@dataclass(frozen=True)
class Snapshot:
    sha256: str
    archive_path: Path
    file_count: int
    byte_count: int
    skipped: tuple[str, ...] = ()


def _git(root, *args):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    with subprocess.Popen(["git", *args], cwd=root, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        data = bytearray()
        try:
            while block := process.stdout.read(64 * 1024):
                data.extend(block)
                if len(data) > MAX_GIT_BYTES:
                    raise SnapshotError("snapshot Git listing exceeds the metadata limit")
        except BaseException:
            process.kill()
            raise
        if process.wait():
            raise SnapshotError(f"Git {args[0]} failed in {root}")
    return bytes(data)


_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])$", re.I)
_SHORT_NAME = re.compile(r"[^.]{1,6}~[0-9]+(?:\.[^.]{1,3})?", re.I)


def _portable(part):
    return (part and part not in (".", "..") and not part.endswith((" ", "."))
            and not _RESERVED.fullmatch(part.partition(".")[0].rstrip(" "))
            and not _SHORT_NAME.fullmatch(part)
            and len(part.encode("utf-8", "surrogatepass")) <= 255
            and not any(c in '<>:"\\|?*' or ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF
                        for c in part))


def _relative(path):
    if not isinstance(path, str) or len(path) > 4096 or path.count("/") >= 128:
        raise SnapshotError(f"invalid snapshot path: {path!r}")
    parts = path.split("/")
    if not all(_portable(p) and p.casefold() != ".git" for p in parts):
        raise SnapshotError(f"nonportable or protected snapshot path: {path!r}")
    return path


def _safe_link_target(rel, target):
    # Permit leading '..' only. A later '..' could cross an intermediate symlink.
    parts = target.split("/")
    depth = rel.count("/")
    seen_name = False
    for part in parts:
        if part == ".":
            continue
        if part == ".." and not seen_name and depth:
            depth -= 1
        elif _portable(part) and part.casefold() != ".git":
            seen_name = True
        else:
            return None
    return target


def _string_list(config, key):
    value = config.get(key, [])
    if not isinstance(value, list) or not all(isinstance(s, str) and s for s in value):
        raise SnapshotError(f"snapshot.{key} must be a list of non-empty strings")
    return value


class _Selection:
    def __init__(self, config, root, destination):
        self.includes = [_relative(s) for s in _string_list(config, "include")]
        self.protected = {_relative(s).casefold() for s in _string_list(config, "policy_files")}
        for var in POLICY_ENV_VARS:
            if value := os.environ.get(var):
                # Protect the configured name and its canonical target.
                for path in (Path(value).absolute(), Path(value).resolve()):
                    if path.is_relative_to(root):
                        self.protected.add(path.relative_to(root).as_posix().casefold())
        if destination.is_relative_to(root):
            self.protected.add(destination.relative_to(root).as_posix().casefold())
        extra = [p.replace("\\", "/").strip("/").casefold() for p in _string_list(config, "exclude")]
        self.names = [*DEFAULT_EXCLUDES, *(p for p in extra if "/" not in p)]
        self.paths = [p for p in extra if "/" in p]

    def allows(self, path):
        parts = path.casefold().split("/")
        # An include can override ordinary filters only through its named prefix.
        start = max((s.count("/") + 1 for s in self.includes
                     if path == s or path.startswith(s + "/")), default=0)
        for i, part in enumerate(parts):
            prefix = "/".join(parts[:i + 1])
            if i and (parts[i - 1], part) in {(".vscode", "mcp.json"), (".claude", "settings.json")}:
                return False
            if prefix in self.protected or any(fnmatch.fnmatchcase(part, p) for p in PROTECTED):
                return False
            if i >= start and (any(fnmatch.fnmatchcase(part, p) for p in self.names)
                               or any(fnmatch.fnmatchcase(prefix, p) for p in self.paths)):
                return False
        return True

    def candidates(self, root):
        selected = {}

        def add(path, index):
            if not self.allows(path):
                return
            _relative(path)
            if path in selected and index != selected[path]:
                raise SnapshotError(f"conflicting Git entries: {path!r}")
            selected[path] = index
            if len(selected) > MAX_CANDIDATES:
                raise SnapshotError("snapshot exceeds the file count limit")

        # A tagged, NUL-delimited listing disambiguates tracked and untracked names
        # even when an untracked filename looks exactly like index metadata.
        listing = _git(root, "ls-files", "-z", "-t", "--stage", "--cached", "--others",
                       "--exclude-standard")
        for record in _records(listing):
            tag, _, data = record.partition(b" ")
            if tag == b"?":
                if not data.endswith(b"/"):  # Git reports nested repositories as directories.
                    add(os.fsdecode(data), None)
            else:
                index, path = data.split(b"\t", 1)
                if index.split(b" ", 1)[0] != b"160000":  # gitlink
                    add(os.fsdecode(path), index)
        if self.includes:
            ignored = _git(root, "--literal-pathspecs", "ls-files", "-z", "--others", "--ignored",
                           "--exclude-standard", "--", *self.includes)
            for record in _records(ignored):
                if not record.endswith(b"/"):
                    add(os.fsdecode(record), None)
        return selected


def _records(data):
    start = 0
    while start < len(data):
        end = data.index(b"\0", start)
        yield data[start:end]
        start = end + 1


def _mount_id(fd):
    # Linux bind mounts can alias another directory even on the same device.
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
        for line in info:
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise SnapshotError("cannot verify snapshot source mount")


@contextmanager
def _directory(root_fd, parts, mount=None):
    """Retain the verified ancestors until the caller has finished using the leaf."""
    with ExitStack() as stack:
        parent = root_fd
        for part in parts:
            if os.name == "nt":
                child = win.open_child(parent, part)
            else:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                except NotADirectoryError:
                    st = os.stat(part, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        raise FileNotFoundError(part) from None
                    raise SnapshotError(f"unsafe snapshot directory: {part!r}") from None
            stack.callback(os.close, child)
            if mount is not None and _mount_id(child) != mount:
                raise SnapshotError(f"snapshot directory crosses a mount: {part!r}")
            if os.name == "nt":
                attrs, _ = win.basic_info(child)
                if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise SnapshotError(f"unsafe snapshot directory: {part!r}")
                if not attrs & stat.FILE_ATTRIBUTE_DIRECTORY:
                    raise FileNotFoundError(part)
            parent = child
        yield parent


@contextmanager
def _root_handle(root):
    anchor = root.anchor
    fd = win.open_volume(anchor) if os.name == "nt" else os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with _directory(fd, root.parts[1:]) as root_fd:
            mount = _mount_id(root_fd) if sys.platform.startswith("linux") else None
            yield root_fd, mount
    finally:
        os.close(fd)


@dataclass(frozen=True)
class _Entry:
    mode: int
    device: int
    inode: int
    size: int
    modified: int
    changed: int
    target: str | None


@contextmanager
def _entry(root, rel):
    root_fd, mount = root
    parts = rel.split("/")
    leaf = parts[-1]
    with ExitStack() as stack:
        parent = stack.enter_context(_directory(root_fd, parts[:-1], mount))
        fd = None
        target = None
        if os.name == "nt":
            fd = win.open_child(parent, leaf)
            stack.callback(os.close, fd)
            st = os.fstat(fd)
            attrs, changed = win.basic_info(fd)
            mode = st.st_mode
            if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                target = win.relative_link(fd)
                mode = stat.S_IFLNK
        else:
            st = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                try:
                    fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                except FileNotFoundError as exc:
                    raise SnapshotError(f"file disappeared while opening {rel!r}") from exc
                stack.callback(os.close, fd)
                if mount is not None and _mount_id(fd) != mount:
                    raise SnapshotError(f"snapshot file crosses a mount: {rel!r}")
                opened = os.fstat(fd)
                if not os.path.samestat(st, opened):
                    raise SnapshotError(f"file changed while opening {rel!r}")
                st = opened
            elif stat.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(leaf, dir_fd=parent)
                except FileNotFoundError as exc:
                    raise SnapshotError(f"link disappeared while inspecting {rel!r}") from exc
            mode = st.st_mode
            changed = st.st_ctime_ns
        if stat.S_ISREG(mode) and st.st_nlink != 1:
            raise SnapshotError(f"hardlinked snapshot source: {rel!r}")
        yield _Entry(mode, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, changed, target), fd


def _observe(source, candidates):
    result = {}
    for rel, index in candidates.items():
        try:
            with _entry(source, rel) as (entry, _):
                result[rel] = entry
        except FileNotFoundError:
            if index is None:
                raise SnapshotError(f"untracked file disappeared during capture: {rel!r}") from None
            result[rel] = None  # Already deleted tracked path; rechecked before publication.
    return result


def _portable_tree(entries):
    spellings = {}
    regular = set()
    directories = set()
    for rel, entry in entries.items():
        if entry is None:
            continue
        path = ""
        for part in rel.split("/"):
            path = f"{path}/{part}" if path else part
            key = unicodedata.normalize("NFC", path).casefold()
            if spellings.setdefault(key, path) != path:
                raise SnapshotError(f"nonportable path collision: {path!r}")
            if path != rel:
                directories.add(path)
        if stat.S_ISREG(entry.mode):
            regular.add(rel)
    return regular, directories


class _ArchiveWriter:
    def __init__(self, file, limit):
        self.file = file
        self.limit = limit
        self.size = 0
        self.digest = hashlib.sha256()

    def tell(self):
        return self.size

    def write(self, chunk):
        if self.size + len(chunk) > self.limit:
            raise SnapshotError("snapshot archive exceeds snapshot.max_bytes")
        self.file.write(chunk)
        self.digest.update(chunk)
        self.size += len(chunk)
        return len(chunk)


def _write_archive(writer, source, candidates, entries):
    regular, directories = _portable_tree(entries)
    skipped = []
    count = size = 0
    with tarfile.open(fileobj=writer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(entries):
            expected = entries[rel]
            if expected is None or stat.S_ISDIR(expected.mode):
                continue
            info = tarfile.TarInfo(rel)  # TarInfo defaults to mtime/uid/gid 0, empty names.
            if expected.target is not None:
                target = _safe_link_target(rel, expected.target)
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target or ""))
                # Only direct, included targets with real directory prefixes are portable.
                if not target or (resolved not in regular and resolved not in directories):
                    skipped.append(rel)
                    continue
                info.type, info.mode, info.linkname = tarfile.SYMTYPE, 0o777, target
                tar.addfile(info)
            elif rel in regular:
                if writer.size + expected.size > writer.limit:
                    raise SnapshotError("snapshot archive exceeds snapshot.max_bytes")
                with _entry(source, rel) as (current, fd):
                    if current != expected:
                        raise SnapshotError(f"file changed during capture: {rel!r}")
                    mode = (int((candidates[rel] or b"0").split()[0], 8)
                            if os.name == "nt" else current.mode)
                    info.mode = 0o755 if mode & 0o111 else 0o644
                    info.size = current.size
                    with os.fdopen(fd, "rb", closefd=False) as payload:
                        tar.addfile(info, payload)
                    size += current.size
            else:
                skipped.append(rel)
                continue
            count += 1
    return count, size, tuple(skipped)


def create_snapshot(cwd: Path, profile: dict, destination: Path) -> Snapshot:
    """Publish a snapshot only after Git membership, file identity and version revalidate."""
    try:
        raw_root = _git(cwd, "rev-parse", "--show-toplevel").removesuffix(b"\n").removesuffix(b"\r")
        root = Path(os.fsdecode(raw_root)).absolute()
        destination = Path(destination).resolve()
        # Never place capture artifacts beside project files: doing so both risks
        # colliding with legitimate source names and changes the captured tree.
        if destination == root:
            destination = root / ".a2a-link-snapshots"
        config = (profile or {}).get("snapshot", {})
        if not isinstance(config, dict):
            raise SnapshotError("profile snapshot settings must be an object")
        limit = config.get("max_bytes", DEFAULT_MAX_BYTES)
        if type(limit) is not int or limit <= 0:
            raise SnapshotError("snapshot.max_bytes must be a positive integer")
        selection = _Selection(config, root, destination)
        with ExitStack() as cleanup:
            with _root_handle(root) as source:
                for path in selection.includes:
                    if not selection.allows(path):
                        continue
                    try:
                        with _entry(source, path):
                            pass
                    except FileNotFoundError:
                        pass
                destination.mkdir(parents=True, exist_ok=True)
                candidates = selection.candidates(root)
                entries = _observe(source, candidates)
                temp_path = destination / f".snapshot-{uuid.uuid4().hex}.tmp"
                with open(temp_path, "xb", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
                    cleanup.callback(temp_path.unlink, missing_ok=True)
                    writer = _ArchiveWriter(handle, limit)
                    count, size, skipped = _write_archive(writer, source, candidates, entries)
                if (_Selection(config, root, destination).protected != selection.protected
                        or selection.candidates(root) != candidates
                        or _observe(source, candidates) != entries):
                    raise SnapshotError("working tree or index changed during snapshot capture")
                with _root_handle(root) as final_source:
                    if (not os.path.samestat(os.fstat(source[0]), os.fstat(final_source[0]))
                            or source[1] != final_source[1]):
                        raise SnapshotError("project root changed during snapshot capture")
            # Windows requires the root directory's read handle to close before a rename into it.
            digest = writer.digest.hexdigest()
            path = destination / f"snapshot-{digest}.tar"
            os.replace(temp_path, path)
        return Snapshot(digest, path, count, size, skipped)
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"cannot capture snapshot: {exc}") from exc
