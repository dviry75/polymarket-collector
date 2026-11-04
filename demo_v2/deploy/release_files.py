"""Public immutable release permissions, independent of the administrator umask.

Only code/interpreter/static files belong here. Private config, DB and backups
are outside this tree and must retain their separate private permissions.
"""
import os
from pathlib import Path


def grant_release_access(root,release):
    root=Path(root);release=Path(release)
    if release.parent!=root/'releases' or release.is_symlink() or not release.is_dir():
        raise ValueError('Expected a real directory directly under DEMO releases')
    if root.is_symlink() or (root/'releases').is_symlink():
        raise ValueError('Release parents must be real directories')
    for path in (root,root/'releases',release):path.chmod(0o755)
    for current,dirs,files in os.walk(release,followlinks=False):
        for name in dirs:
            path=Path(current)/name
            if not path.is_symlink():path.chmod(0o755)
        for name in files:
            path=Path(current)/name
            if not path.is_symlink():path.chmod(0o755 if path.stat().st_mode&0o111 else 0o644)
