"""Runtime provenance and deployment attestation.

The missing-SELL investigation stalled on a question nobody could answer from
the box itself: *which source was this process actually running?* The working
tree, the files on disk and the modules already loaded into the interpreter
had all drifted apart, so a fix that was "obviously present in the code" could
not be shown to be present in the running trader.

Everything here therefore describes the **loaded interpreter**, never merely
what happens to be on disk when someone later asks. ``source_hash`` is
computed from the files backing the modules in ``sys.modules``; a later edit
to those files changes the working tree and the *disk* hash, and the mismatch
between them is exactly the signal that the process is stale.

No secret ever reaches a digest or a log line: the config digest is taken over
``LiveConfig.safe_public_dict()``, which already reduces credentials to
booleans, and is defensively re-sanitized here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable
import uuid


# A run_id ties every log line, alert and audit row of one process together.
RUN_ID_ENV = "LIVE_RUN_ID"

_GIT_TIMEOUT_SECONDS = 10.0


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _run_git(root: Path, *args: str) -> str | None:
    """Return stripped git stdout, or None when git cannot answer."""
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def repository_root(start: Path) -> Path:
    """Resolve the git work tree containing ``start``, else ``start`` itself."""
    top = _run_git(start, "rev-parse", "--show-toplevel")
    if top:
        return Path(top)
    return start


def git_state(root: Path) -> dict[str, Any]:
    """Describe the checkout: commit, branch and precise dirtiness.

    ``dirty_digest`` is deliberately taken over the *porcelain status* rather
    than the diff. It is stable, cheap on a large tree, and changes whenever a
    tracked file is modified or an untracked file appears, which is all the
    gate needs in order to refuse an unattested deployment.
    """
    sha = _run_git(root, "rev-parse", "HEAD")
    if sha is None:
        return {
            "git_available": False,
            "git_sha": "",
            "git_branch": "",
            "git_dirty": True,
            "git_dirty_files": 0,
            "git_dirty_digest": "",
            "git_describe": "",
        }
    status = _run_git(root, "status", "--porcelain=v1") or ""
    dirty_lines = [line for line in status.splitlines() if line.strip()]
    return {
        "git_available": True,
        "git_sha": sha,
        "git_branch": _run_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "",
        "git_dirty": bool(dirty_lines),
        "git_dirty_files": len(dirty_lines),
        "git_dirty_digest": (
            _sha256_hex("\n".join(sorted(dirty_lines)).encode("utf-8"))
            if dirty_lines
            else ""
        ),
        "git_describe": _run_git(root, "describe", "--always", "--dirty") or "",
    }


def _loaded_module_files(package_root: Path) -> list[Path]:
    """Files backing already-imported modules that live under our source root."""
    seen: set[Path] = set()
    for module in list(sys.modules.values()):
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        try:
            path = Path(origin).resolve()
        except (OSError, ValueError):
            continue
        if path.suffix != ".py" or not path.is_file():
            continue
        try:
            path.relative_to(package_root)
        except ValueError:
            continue
        seen.add(path)
    return sorted(seen)


def loaded_source_digest(package_root: Path) -> dict[str, Any]:
    """Hash the source of every loaded first-party module.

    This is the attestation that matters. Two processes agreeing on
    ``source_hash`` are running the same code regardless of what the working
    tree says, and a process whose ``source_hash`` no longer matches the disk
    is provably stale.
    """
    package_root = package_root.resolve()
    entries: list[str] = []
    unreadable = 0
    for path in _loaded_module_files(package_root):
        try:
            digest = _sha256_hex(path.read_bytes())
        except OSError:
            unreadable += 1
            continue
        entries.append(f"{path.relative_to(package_root).as_posix()}:{digest}")
    return {
        "source_hash": _sha256_hex("\n".join(entries).encode("utf-8")),
        "source_module_count": len(entries),
        "source_unreadable_count": unreadable,
    }


def disk_source_digest(package_root: Path, *, loaded_only: bool = True) -> str:
    """Hash the same module set as it currently exists on disk.

    Identical to :func:`loaded_source_digest` at startup. It is recomputed
    later to detect that files changed underneath a long-running process.
    """
    return loaded_source_digest(package_root)["source_hash"] if loaded_only else ""


def _sanitized_config_payload(config: Any) -> dict[str, Any]:
    from .strategy_repository import sanitize

    try:
        payload = config.safe_public_dict()
    except Exception:  # pragma: no cover - defensive
        payload = {}
    sanitized = sanitize(payload)
    return sanitized if isinstance(sanitized, dict) else {}


def config_digest(config: Any) -> dict[str, Any]:
    payload = _sanitized_config_payload(config)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return {
        "config_hash": _sha256_hex(encoded),
        "config_key_count": len(payload),
    }


@dataclass(frozen=True)
class RuntimeProvenance:
    run_id: str
    process_started_at: str
    pid: int
    python_version: str
    package_root: str
    repo_root: str
    git_available: bool
    git_sha: str
    git_branch: str
    git_dirty: bool
    git_dirty_files: int
    git_dirty_digest: str
    git_describe: str
    source_hash: str
    source_module_count: int
    source_unreadable_count: int
    config_hash: str
    config_key_count: int
    strategy_id: str
    strategy_version: str
    approved_git_sha: str
    approved_runtime_hash: str
    require_clean_runtime: bool
    gate_ok: bool = True
    gate_reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["gate_reasons"] = list(self.gate_reasons)
        return payload

    def state_rows(self) -> dict[str, str]:
        """Flatten into ``live_system_state`` rows for the dashboard/report."""
        return {
            f"provenance_{key}": (
                ",".join(value) if isinstance(value, (list, tuple)) else str(value)
            )
            for key, value in self.as_dict().items()
        }

    def summary_line(self) -> str:
        return (
            f"RUNTIME_PROVENANCE run_id={self.run_id} "
            f"git_sha={self.git_sha or 'UNKNOWN'} "
            f"dirty={str(self.git_dirty).lower()} "
            f"dirty_files={self.git_dirty_files} "
            f"source_hash={self.source_hash[:16]} "
            f"config_hash={self.config_hash[:16]} "
            f"strategy={self.strategy_id}/{self.strategy_version} "
            f"gate={'OK' if self.gate_ok else 'BLOCKED'} "
            f"reasons={'|'.join(self.gate_reasons) or '-'}"
        )


def evaluate_gate(
    *,
    git_available: bool,
    git_dirty: bool,
    git_sha: str,
    source_hash: str,
    approved_git_sha: str,
    approved_runtime_hash: str,
    require_clean_runtime: bool,
) -> tuple[bool, tuple[str, ...]]:
    """Decide whether this runtime may be trusted with real money.

    Fail-closed: anything unproven blocks. An operator who deliberately runs
    from a dirty tree must say so by clearing ``require_clean_runtime``, and
    that choice stays visible in the attestation.
    """
    reasons: list[str] = []
    if require_clean_runtime:
        if not git_available:
            reasons.append("GIT_PROVENANCE_UNAVAILABLE")
        elif git_dirty:
            reasons.append("WORKING_TREE_DIRTY")
    if approved_git_sha and git_sha and approved_git_sha != git_sha:
        reasons.append("GIT_SHA_NOT_APPROVED")
    if approved_git_sha and not git_sha:
        reasons.append("GIT_SHA_UNKNOWN")
    if approved_runtime_hash and approved_runtime_hash != source_hash:
        reasons.append("RUNTIME_HASH_NOT_APPROVED")
    return (not reasons), tuple(reasons)


def collect(
    config: Any,
    *,
    package_root: Path | None = None,
    run_id: str | None = None,
    started_at: str | None = None,
) -> RuntimeProvenance:
    root = (package_root or Path(__file__).resolve().parent.parent).resolve()
    repo_root = repository_root(root)
    git = git_state(repo_root)
    source = loaded_source_digest(root)
    cfg = config_digest(config)

    approved_git_sha = str(
        getattr(config, "approved_git_sha", "") or os.environ.get("LIVE_APPROVED_GIT_SHA", "")
    ).strip()
    approved_runtime_hash = str(
        getattr(config, "approved_runtime_hash", "")
        or os.environ.get("LIVE_APPROVED_RUNTIME_HASH", "")
    ).strip()
    require_clean = bool(getattr(config, "require_clean_runtime", True))

    gate_ok, reasons = evaluate_gate(
        git_available=bool(git["git_available"]),
        git_dirty=bool(git["git_dirty"]),
        git_sha=str(git["git_sha"]),
        source_hash=str(source["source_hash"]),
        approved_git_sha=approved_git_sha,
        approved_runtime_hash=approved_runtime_hash,
        require_clean_runtime=require_clean,
    )

    return RuntimeProvenance(
        run_id=(
            run_id
            or os.environ.get(RUN_ID_ENV)
            or uuid.uuid4().hex
        ),
        process_started_at=(
            started_at or datetime.now(timezone.utc).isoformat()
        ),
        pid=os.getpid(),
        python_version=sys.version.split()[0],
        package_root=str(root),
        repo_root=str(repo_root),
        git_available=bool(git["git_available"]),
        git_sha=str(git["git_sha"]),
        git_branch=str(git["git_branch"]),
        git_dirty=bool(git["git_dirty"]),
        git_dirty_files=int(git["git_dirty_files"]),
        git_dirty_digest=str(git["git_dirty_digest"]),
        git_describe=str(git["git_describe"]),
        source_hash=str(source["source_hash"]),
        source_module_count=int(source["source_module_count"]),
        source_unreadable_count=int(source["source_unreadable_count"]),
        config_hash=str(cfg["config_hash"]),
        config_key_count=int(cfg["config_key_count"]),
        strategy_id=str(getattr(config, "strategy_id", "") or ""),
        strategy_version=str(getattr(config, "strategy_version", "") or ""),
        approved_git_sha=approved_git_sha,
        approved_runtime_hash=approved_runtime_hash,
        require_clean_runtime=require_clean,
        gate_ok=gate_ok,
        gate_reasons=reasons,
    )
