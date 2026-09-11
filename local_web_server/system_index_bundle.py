"""Load one completed, structurally trusted System Index build."""

import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_MAX_BUNDLE_BYTES = 8 * 1024 * 1024
_INVALID_BUNDLE = "System Index bundle is invalid"
_INVALID_GALLERY_BUNDLE = "UI Gallery bundle is invalid"


@dataclass(frozen=True)
class SystemIndexBundle:
    home: bytes
    assets: tuple[tuple[PurePosixPath, bytes], ...]


def load_system_index_bundle(dist: Path) -> SystemIndexBundle:
    """Read only the immediate HTML, JavaScript, and CSS build artifacts."""

    try:
        dist = Path(dist)
        if not stat.S_ISDIR(dist.lstat().st_mode):
            raise ValueError
        entries = {entry.name: entry for entry in dist.iterdir()}
        if set(entries) != {"assets", "index.html"}:
            raise ValueError

        index = entries["index.html"]
        assets_directory = entries["assets"]
        index_metadata = index.lstat()
        if not stat.S_ISREG(index_metadata.st_mode) or not stat.S_ISDIR(
            assets_directory.lstat().st_mode
        ):
            raise ValueError

        asset_paths = tuple(assets_directory.iterdir())
        if not asset_paths:
            raise ValueError
        asset_metadata = tuple((asset, asset.lstat()) for asset in asset_paths)
        if any(
            not stat.S_ISREG(metadata.st_mode) or asset.suffix not in {".css", ".js"}
            for asset, metadata in asset_metadata
        ):
            raise ValueError
        if not any(asset.suffix == ".js" for asset in asset_paths):
            raise ValueError
        if (
            index_metadata.st_size
            + sum(metadata.st_size for _asset, metadata in asset_metadata)
            > _MAX_BUNDLE_BYTES
        ):
            raise ValueError

        home = index.read_bytes()
        assets = tuple(
            sorted(
                (
                    (PurePosixPath("assets", asset.name), asset.read_bytes())
                    for asset in asset_paths
                ),
                key=lambda item: item[0].as_posix(),
            )
        )
        if len(home) + sum(len(content) for _path, content in assets) > _MAX_BUNDLE_BYTES:
            raise ValueError
        return SystemIndexBundle(home, assets)
    except (OSError, ValueError) as error:
        raise ValueError(_INVALID_BUNDLE) from error


def load_ui_gallery_bundle(dist: Path) -> tuple[tuple[PurePosixPath, bytes], ...]:
    """Read the gallery's static HTML and hashed assets for atomic publication."""

    try:
        dist = Path(dist)
        if not stat.S_ISDIR(dist.lstat().st_mode):
            raise ValueError
        files: list[tuple[PurePosixPath, Path, int]] = []
        for entry in dist.iterdir():
            metadata = entry.lstat()
            if entry.name == "assets":
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError
                for asset in entry.iterdir():
                    asset_metadata = asset.lstat()
                    if not stat.S_ISREG(asset_metadata.st_mode):
                        raise ValueError
                    if asset.suffix not in {".css", ".js"}:
                        raise ValueError
                    files.append((PurePosixPath("assets", asset.name), asset, asset_metadata.st_size))
            elif entry.suffix == ".html":
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError
                files.append((PurePosixPath(entry.name), entry, metadata.st_size))
            else:
                raise ValueError
        if not any(path.suffix == ".js" for path, _entry, _size in files):
            raise ValueError
        if not any(path == PurePosixPath("index.html") for path, _entry, _size in files):
            raise ValueError
        if sum(size for _path, _entry, size in files) > _MAX_BUNDLE_BYTES:
            raise ValueError
        result = tuple(
            (path, entry.read_bytes())
            for path, entry, _size in sorted(files, key=lambda item: item[0].as_posix())
        )
        if sum(len(content) for _path, content in result) > _MAX_BUNDLE_BYTES:
            raise ValueError
        return result
    except (OSError, ValueError) as error:
        raise ValueError(_INVALID_GALLERY_BUNDLE) from error
