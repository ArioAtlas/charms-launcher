"""Artifact downloads into SEED_CACHE_PATH (url with sha256 check, hf_repo)."""

import asyncio
import hashlib
from pathlib import Path

import httpx

from charms_core.seed import SeedArtifact
from charms_core.types import ConfigurationError

_CHUNK = 1 << 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(path: Path, sha256: str | None) -> bool:
    return sha256 is None or _sha256(path) == sha256


async def download_artifact(artifact: SeedArtifact, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if artifact.kind == "url":
        filename = artifact.ref.rstrip("/").rpartition("/")[2] or artifact.name
        target = dest_dir / filename
        if target.exists() and _verified(target, artifact.sha256):
            return target
        partial = target.with_name(target.name + ".part")
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=None) as client,
            client.stream("GET", artifact.ref) as response,
        ):
            response.raise_for_status()
            with partial.open("wb") as fh:
                async for chunk in response.aiter_bytes(_CHUNK):
                    fh.write(chunk)
        if not _verified(partial, artifact.sha256):
            partial.unlink(missing_ok=True)
            raise ConfigurationError(f"artifact '{artifact.name}' failed sha256 verification")
        partial.replace(target)
        return target

    # hf_repo — heavy dependency stays optional until a real model seed needs it.
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise ConfigurationError(
            "huggingface_hub is required for hf_repo artifacts — add it to the seed package"
        ) from exc
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(
        None,
        lambda: snapshot_download(
            artifact.ref, revision=artifact.revision, cache_dir=str(dest_dir)
        ),
    )
    return Path(path)
