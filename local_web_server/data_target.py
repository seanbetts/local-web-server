"""Resolve app clients to the current registered production authority."""

from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from .config import validate_registered_app
from .git_build import GitRepository
from .host_profile_store import HostProfileSnapshot
from .models import HostRegistry
from .public_origin import join_public_origin


class DataTargetError(ValueError):
    """No local-file fallback is supplied for missing or invalid registration."""


def data_target_for_repository(registry: HostRegistry, snapshot: HostProfileSnapshot,
                               repository: Path) -> dict:
    try:
        selected = Path(repository).resolve(strict=True)
        hosts = [host for host in registry.apps if host.repository.resolve(strict=True) == selected]
        if len(hosts) != 1 or not snapshot.revisions:
            raise ValueError
        host = hosts[0]
        source = GitRepository(host.repository)
        _, manifest = validate_registered_app(host, source.manifest_at(source.main_commit()), target_kind=registry.target.kind)
        revision = snapshot.revisions[-1].revision_id
        if type(revision) is not str or not re.fullmatch(r'[0-9a-f]{64}', revision):
            raise ValueError
        origin = urlsplit(registry.public_origin)
        if (origin.scheme not in ('http', 'https') or not origin.hostname
                or origin.username is not None or origin.password is not None
                or origin.path or origin.query or origin.fragment
                or registry.public_origin != f'{origin.scheme}://{origin.netloc}'
                or any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 or c == '\\'
                       for c in registry.public_origin)
                or registry.target.kind not in ('macos', 'nas-docker')
                or registry.target.kind == 'nas-docker' and origin.scheme != 'https'):
            raise ValueError
        return dict(schemaVersion=1, appId=manifest.id, targetKind=registry.target.kind,
                    baseUrl=join_public_origin(registry.public_origin, manifest.route), profileRevision=revision)
    except (OSError, ValueError, IndexError, AttributeError, KeyError, subprocess.SubprocessError) as error:
        raise DataTargetError('application data target is unavailable; check registration and host profile') from error
