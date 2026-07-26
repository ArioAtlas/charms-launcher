# charms-launcher

Run [Charms](../charms) seeds as **Runes** on your machine and earn mana.
This repo ships two entry points in one package:

- **`charms-launcher`** — the CLI: authenticate, search the seed registry,
  pull seed packages, and serve them as Runes.
- **`charms-rune-manager`** — a local Tkinter GUI that supervises runes:
  start/stop, log tails, GPU/VRAM checks, registry search and pull.

Vocabulary: a **Seed** is a module that loads a model and exposes it as a
runnable interface (docker *image*); a **Rune** is a running Seed instance
connected to a Charms server, consuming tasks (docker *container*).

## Install

```bash
# from a checkout (uv workspace: core + launcher + demo seeds)
uv sync --all-packages

# or straight from GitHub
pip install \
  "charms-core @ git+https://github.com/ArioAtlas/charms-launcher#subdirectory=core" \
  "charms-launcher @ git+https://github.com/ArioAtlas/charms-launcher#subdirectory=launcher"
```

Python ≥ 3.12. `core/` is the platform's wire/SDK contracts (`charms_core`),
vendored so the launcher needs nothing from the server monorepo.

## Quickstart

```bash
# 1. one-time: store the server URL + your rune key (web UI → My Runes)
charms-launcher login

# 2. see what you can run (your own seeds + public ones)
charms-launcher search whisper

# 3. pull a seed package and build its isolated environment
charms-launcher pull whisper

# 4. serve it as a Rune (Ctrl-C to stop)
charms-launcher run whisper
```

`run` works on two kinds of seeds:

- **Locally installed** packages exposing a `charms.seeds` entry point (the
  bundled `examples/seeds/echo` and `echo_stream` demos are dev-installed by
  `uv sync`) — served in-process.
- **Registry packages** — pulled on demand: the zip is downloaded to
  `~/.charms/packages/<seed>`, its declared dependencies are installed into a
  private virtualenv under `~/.charms/envs/<seed>`, the manifest's
  environment-variable rules are validated, and the rune runs inside that env.

Prefer clicking? `charms-rune-manager` does all of the above from a GUI and
prompts for the server URL + rune key on first use.

## Configuration

State lives under `~/.charms` (`CHARMS_HOME` overrides the location):

| File / dir | Purpose |
|---|---|
| `launcher.json` | server URL, rune key, saved seed env vars |
| `seed-cache/` | model artifact downloads (`SEED_CACHE_PATH` overrides) |
| `packages/` | extracted pulled seed packages |
| `envs/` | per-seed virtualenvs |
| `rune-logs/` | Rune Manager process logs |

Environment variables always win over `launcher.json`:

| Variable | Meaning | Default |
|---|---|---|
| `RUNE_KEY` | rune key from the web UI | from `launcher.json` |
| `SERVER_URL` | Charms server | `ws://localhost:8600` |
| `SEED_CACHE_PATH` | model download dir | `~/.charms/seed-cache` |

A `.env` in the current working directory is also read (existing environment
wins), so scripted setups keep working.

## Seed packages

A seed package is a zip with `manifest.json` (options schema, price,
environment rules), `pyproject.toml` (metadata + dependencies + the
`charms.seeds` entry point), `README.md`, and the Python code. Packages
declare their real dependencies but **never `charms-core`** — this runtime
provides it inside every seed env. Build your own from the starter template
on the Charms web UI → Seeds → *Build a seed*.

## Protocol compatibility

`core/` is a vendored copy of the `charms` monorepo's `core/` — the
WebSocket protocol and Seed SDK shared with the server. It must stay in sync
with the server you connect to: when the monorepo's core changes, copy it
over verbatim and cut a release.

## Development

```bash
uv sync --all-packages  # installs dev deps + the demo seeds
uv run pytest -v
uv run ruff check . && uv run ruff format --check .
uv run mypy launcher/src
```

Known trust note (inherited from the platform): a rune owner can technically
observe the task inputs their machine processes, and pulled seed packages run
arbitrary Python — only pull seeds you trust.
