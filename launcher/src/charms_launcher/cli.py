"""
`charms-launcher` CLI.

- ``login``    — authenticate against a Charms server (stores the rune key).
- ``search``   — find seed packages you may pull (yours + public ones).
- ``pull``     — download a seed package and build its isolated environment.
- ``run``      — serve a seed as a Rune: locally installed seeds run
  in-process; pulled seeds run inside their own environment (pulling on
  demand when needed).
- ``describe`` — print a locally installed seed's SeedDescriptor JSON.
"""

import argparse
import asyncio
import getpass
import logging
import os
import socket
import subprocess
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from charms_core.package import validate_environment
from charms_core.seed import Seed, SeedContext, SeedDescriptor
from charms_core.types import CharmsError
from dotenv import load_dotenv

from charms_launcher import config as cfg
from charms_launcher import registry, seedenv
from charms_launcher.client import LauncherClient
from charms_launcher.download import download_artifact


def load_env_file() -> None:
    """Read `.env` from the current working directory, if present.

    Lets RUNE_KEY / SEED_CACHE_PATH / provider API keys live in a local
    `.env` next to where the launcher is started. Variables already set in
    the environment always win (load_dotenv never overrides).
    """
    load_dotenv(Path.cwd() / ".env")


def find_seed_class(name: str) -> type[Seed[Any, Any, Any]] | None:
    for entry in entry_points(group="charms.seeds"):
        if entry.name == name:
            seed_cls: type[Seed[Any, Any, Any]] = entry.load()
            return seed_cls
    return None


def load_seed_class(name: str) -> type[Seed[Any, Any, Any]]:
    seed_cls = find_seed_class(name)
    if seed_cls is None:
        installed = sorted(entry.name for entry in entry_points(group="charms.seeds"))
        raise SystemExit(f"unknown seed '{name}' — installed seeds: {installed or 'none'}")
    return seed_cls


def _ws_url(server: str) -> str:
    return server.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")


async def _serve(seed_name: str, server: str, name: str, rune_key: str) -> None:
    """Load a locally importable seed and serve it as a Rune (in-process)."""
    cache = cfg.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    seed = load_seed_class(seed_name)()
    context = SeedContext(cache_dir=cache, downloader=download_artifact)
    print(f"loading seed '{seed_name}' …")
    await seed.load(context)
    client = LauncherClient(seed, server_url=_ws_url(server), rune_key=rune_key, name=name)
    print(f"serving '{seed_name}' against {server} — Ctrl-C to stop")
    try:
        await client.run_forever()
    finally:
        await seed.unload()


def _check_environment(pulled: seedenv.PulledSeed, config: cfg.LauncherConfig) -> None:
    env = {**config.env, **os.environ}
    problems = validate_environment(pulled.package.manifest, env)
    if problems:
        raise SystemExit(
            f"'{pulled.seed_id}' is missing required environment configuration:\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )


def _run_pulled(pulled: seedenv.PulledSeed, config: cfg.LauncherConfig, args: Any) -> None:
    """Re-invoke `run` inside the pulled seed's environment (blocking)."""
    _check_environment(pulled, config)
    seedenv.ensure_env(pulled)
    env = {
        **config.env,
        **os.environ,
        "RUNE_KEY": config.rune_key,
        "SERVER_URL": args.server,
        "SEED_CACHE_PATH": str(cfg.cache_dir()),
    }
    command = [
        str(pulled.python),
        "-m",
        "charms_launcher.cli",
        "run",
        pulled.seed_id,
        "--server",
        args.server,
        "--name",
        args.name,
    ]
    process = subprocess.Popen(command, env=env)
    try:
        raise SystemExit(process.wait())
    except KeyboardInterrupt:
        process.terminate()
        process.wait(timeout=15)


def cmd_run(args: Any) -> None:
    config = cfg.load_config()
    if args.server == cfg.DEFAULT_SERVER_URL and config.server_url:
        args.server = config.server_url
    config.server_url = args.server  # an explicit --server wins for registry pulls too
    if find_seed_class(args.seed) is not None:
        cfg.require_auth(config)
        try:
            asyncio.run(_serve(args.seed, args.server, args.name, config.rune_key))
        except KeyboardInterrupt:
            print("\nstopped")
        return
    pulled = seedenv.load_pulled(args.seed)
    if pulled is None:
        cfg.require_auth(config)
        print(f"seed '{args.seed}' is not installed locally — pulling from the registry")
        pulled = seedenv.pull(config, args.seed)
    _run_pulled(pulled, config, args)


def cmd_login(args: Any) -> None:
    config = cfg.load_config()
    server = args.server or input(f"Server URL [{config.server_url}]: ").strip()
    if server:
        config.server_url = server
    key = args.key or getpass.getpass("Rune key (from the web UI → My Runes): ").strip()
    if key:
        config.rune_key = key
    cfg.require_auth(config)
    count = registry.verify_login(config)
    path = cfg.save_config(config)
    print(f"authenticated against {config.server_url} ({count} seed(s) available)")
    print(f"saved to {path}")


def cmd_search(args: Any) -> None:
    config = cfg.require_auth(cfg.load_config())
    packages = registry.search_packages(config, query=args.query)
    if not packages:
        print("no seed packages found")
        return
    print(f"{'SEED':<20} {'VERSION':<10} {'PRICE':<16} {'RUNES':<6} {'VISIBILITY':<11} NAME")
    for package in packages:
        visibility = "mine" if package.mine else package.visibility
        price = f"{package.price_value:g} {package.price_unit.removeprefix('mana_per_')}"
        print(
            f"{package.id:<20} {package.version:<10} {price:<16} "
            f"{package.online_runes:<6} {visibility:<11} {package.name}"
        )


def cmd_pull(args: Any) -> None:
    config = cfg.require_auth(cfg.load_config())
    pulled = seedenv.pull(config, args.seed)
    print(f"pulled '{pulled.seed_id}' v{pulled.package.manifest.version}")
    print(f"run it with: charms-launcher run {pulled.seed_id}")


def cmd_describe(args: Any) -> None:
    descriptor = SeedDescriptor.from_seed(load_seed_class(args.seed))
    print(descriptor.model_dump_json(indent=2))


def main() -> None:
    load_env_file()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="charms-launcher")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="serve a seed as a Rune (pulls it if needed)")
    run_parser.add_argument("seed", help="seed id (installed entry point or registry package)")
    run_parser.add_argument(
        "--server", default=os.environ.get("SERVER_URL", cfg.DEFAULT_SERVER_URL)
    )
    run_parser.add_argument("--name", default=socket.gethostname())

    login_parser = sub.add_parser("login", help="store the server URL and rune key")
    login_parser.add_argument("--server", default="")
    login_parser.add_argument("--key", default="")

    search_parser = sub.add_parser("search", help="search pullable seed packages")
    search_parser.add_argument("query", nargs="?", default="")

    pull_parser = sub.add_parser("pull", help="download a seed package and build its env")
    pull_parser.add_argument("seed")

    describe_parser = sub.add_parser(
        "describe", help="print the SeedDescriptor JSON (for POST /api/seeds)"
    )
    describe_parser.add_argument("seed")

    args = parser.parse_args()
    commands = {
        "run": cmd_run,
        "login": cmd_login,
        "search": cmd_search,
        "pull": cmd_pull,
        "describe": cmd_describe,
    }
    try:
        commands[args.command](args)
    except CharmsError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
