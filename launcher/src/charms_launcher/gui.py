"""
Charms Rune Manager — a local Tkinter GUI for starting and stopping Runes.

Run it as ``charms-rune-manager`` (or ``python -m charms_launcher.gui``).

What it does
------------
- Authenticates on first use: prompts for the server URL and a rune key
  (created in the web UI → My Runes) and stores them in ``~/.charms``.
- Lists seeds from three sources: locally installed (``charms.seeds`` entry
  points), pulled registry packages (``~/.charms/packages``), and — via the
  search box — remote packages you may pull (yours + public ones).
- Checks per-seed prerequisites before a start: environment variables
  (static map for bundled seeds, the package manifest's rules for pulled
  ones), GPU free VRAM vs the manifest requirement, and the rune key.
- Starts a rune as ``python -m charms_launcher.cli run <seed>`` — the CLI
  pulls the seed and builds its isolated environment on demand — with output
  logged to ``~/.charms/rune-logs/<seed>-<timestamp>.log``.
- Lists running runes; select one and press Delete (or the Kill button) to
  stop it — the whole process tree is terminated.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from charms_core.package import EnvVarSpec, SeedPackageManifest, validate_environment
from charms_launcher import config as cfg
from charms_launcher import registry, seedenv

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# ---------------------------------------------------------------------------
# Environment / prerequisites
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvReq:
    """One env-var prerequisite; any name in ``names`` satisfies it."""

    names: tuple[str, ...]
    is_path: bool = False

    def unmet(self) -> str | None:
        for name in self.names:
            value = os.environ.get(name)
            if value:
                if self.is_path and not Path(value).exists():
                    return f"{name} points to a missing path"
                return None
        if len(self.names) == 1:
            return f"{self.names[0]} is not set"
        return f"{self.names[0]} (or {', '.join(self.names[1:])}) is not set"

    def missing_names(self) -> list[str]:
        return [n for n in self.names if not os.environ.get(n)]


# Bundled seeds whose env needs predate manifest environment rules.
SEED_ENV_REQS: dict[str, list[EnvReq]] = {
    "gemini": [EnvReq(("GEMINI_API_KEY", "GOOGLE_API_KEY"))],
    "openai": [EnvReq(("OPENAI_API_KEY",))],
    "so_vits_svc": [EnvReq(("SO_VITS_CHECKPOINT_PATH",), is_path=True)],
    "gpt_sovits": [EnvReq(("GPT_SOVITS_REPO_PATH",), is_path=True)],
}

SECRET_MARKERS = ("KEY", "TOKEN", "SECRET")


# ---------------------------------------------------------------------------
# GPU probing (nvidia-smi: fast, no torch import)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuInfo:
    name: str
    total_mb: int
    free_mb: int


def probe_gpu() -> GpuInfo | None:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    # First GPU only — matches launcher detect_hardware() (device 0).
    parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
    try:
        return GpuInfo(name=parts[0], total_mb=int(parts[1]), free_mb=int(parts[2]))
    except (IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Seed discovery: local entry points ∪ pulled packages ∪ remote search
# ---------------------------------------------------------------------------


@dataclass
class SeedInfo:
    id: str
    source: str = "local"  # "local" | "pulled" | "remote"
    title: str = ""
    description: str = ""
    vram_mb: int = 0
    notes: str = ""
    load_error: str | None = None
    env_specs: list[EnvVarSpec] = field(default_factory=list)


def discover_seeds() -> list[SeedInfo]:
    seeds: dict[str, SeedInfo] = {}
    for entry in entry_points(group="charms.seeds"):
        try:
            manifest = entry.load().manifest
            seeds[manifest.id] = SeedInfo(
                id=manifest.id,
                source="local",
                title=manifest.name,
                description=manifest.description,
                vram_mb=manifest.resources.vram_mb,
                notes=manifest.resources.notes,
            )
        except Exception as exc:  # a broken seed must not hide the others
            seeds[entry.name] = SeedInfo(
                id=entry.name, source="local", load_error=f"{type(exc).__name__}: {exc}"
            )
    for pulled in seedenv.list_pulled():
        if pulled.seed_id in seeds:
            continue
        manifest = pulled.package.manifest
        seeds[pulled.seed_id] = SeedInfo(
            id=pulled.seed_id,
            source="pulled",
            title=manifest.name,
            description=manifest.description,
            vram_mb=manifest.resources.vram_mb,
            notes=manifest.resources.notes,
            env_specs=list(manifest.environment),
        )
    return sorted(seeds.values(), key=lambda s: s.id)


def seed_issues(seed: SeedInfo, gpu: GpuInfo | None) -> list[str]:
    """Unmet prerequisites; empty list means the seed is startable."""
    if seed.load_error:
        return [f"failed to import: {seed.load_error}"]
    issues = [msg for req in SEED_ENV_REQS.get(seed.id, []) if (msg := req.unmet())]
    if seed.env_specs:
        manifest = SeedPackageManifest(id=seed.id, name=seed.id, environment=seed.env_specs)
        issues.extend(validate_environment(manifest, dict(os.environ)))
    if seed.vram_mb > 0:
        if gpu is None:
            issues.append(f"needs a GPU with {seed.vram_mb} MB VRAM — no NVIDIA GPU detected")
        elif gpu.free_mb < seed.vram_mb:
            issues.append(f"needs {seed.vram_mb} MB free VRAM (only {gpu.free_mb} MB free)")
    return issues


# ---------------------------------------------------------------------------
# Rune processes
# ---------------------------------------------------------------------------


@dataclass
class RuneProc:
    uid: str
    seed_id: str
    proc: subprocess.Popen[bytes]
    log_path: Path
    started: float = field(default_factory=time.monotonic)

    def status(self) -> str:
        code = self.proc.poll()
        if code is not None:
            return f"exited ({code})"
        tail = read_tail(self.log_path, 8192)
        if "serving '" in tail:
            return "serving"
        if "pulling '" in tail or "installing" in tail:
            return "pulling"
        return "loading"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def uptime(self) -> str:
        if not self.alive():
            return "—"
        total = int(time.monotonic() - self.started)
        minutes, seconds = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"


def read_tail(path: Path, nbytes: int) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the rune and its children (pulled seeds re-exec a child)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":  # literal check so mypy accepts os.killpg below
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        import signal

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class RuneManagerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = cfg.load_config()
        self._apply_saved_env()
        self.gpu = probe_gpu()
        self.seeds = discover_seeds()
        self.remote_seeds: list[SeedInfo] = []
        self.runes: dict[str, RuneProc] = {}
        self._uid = 0
        self._log_rendered_for: tuple[str, int] | None = None
        self._build_ui()
        self._refresh_seeds()
        self._tick()
        self._slow_refresh()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self.config.rune_key:
            root.after(200, self._server_settings)

    def _apply_saved_env(self) -> None:
        """Saved seed env vars become process env (already-set values win)."""
        for name, value in self.config.env.items():
            os.environ.setdefault(name, value)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.root.title("Charms Rune Manager")
        self.root.geometry("1020x680")

        panes = ttk.Panedwindow(self.root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        # -- seeds ------------------------------------------------------
        seed_frame = ttk.Labelframe(panes, text="Seeds (local, pulled, and registry search)")
        panes.add(seed_frame, weight=3)

        search_bar = ttk.Frame(seed_frame)
        search_bar.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(search_bar, text="Registry search:").pack(side="left")
        self.search_entry = ttk.Entry(search_bar, width=32)
        self.search_entry.pack(side="left", padx=(6, 0))
        self.search_entry.bind("<Return>", lambda _e: self._search_registry())
        self.search_button = ttk.Button(search_bar, text="Search", command=self._search_registry)
        self.search_button.pack(side="left", padx=(6, 0))
        self.search_status = ttk.Label(search_bar, text="", foreground="#888888")
        self.search_status.pack(side="left", padx=(10, 0))

        columns = ("seed", "source", "vram", "status", "description")
        self.seed_tree = ttk.Treeview(
            seed_frame, columns=columns, show="headings", selectmode="browse", height=8
        )
        for col, title, width, stretch in (
            ("seed", "Seed", 130, False),
            ("source", "Source", 70, False),
            ("vram", "VRAM (MB)", 90, False),
            ("status", "Requirements", 340, True),
            ("description", "Description", 300, True),
        ):
            self.seed_tree.heading(col, text=title)
            self.seed_tree.column(col, width=width, stretch=stretch, anchor="w")
        self.seed_tree.tag_configure("unavailable", foreground="#888888")
        self.seed_tree.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        self.seed_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.seed_tree.bind("<Double-1>", lambda _e: self._start_selected())

        seed_buttons = ttk.Frame(seed_frame)
        seed_buttons.pack(fill="x", padx=6, pady=6)
        self.start_button = ttk.Button(
            seed_buttons, text="Start Rune", command=self._start_selected
        )
        self.start_button.pack(side="left")
        self.vars_button = ttk.Button(
            seed_buttons, text="Set variables…", command=self._set_variables
        )
        self.vars_button.pack(side="left", padx=(6, 0))
        ttk.Button(seed_buttons, text="Server settings…", command=self._server_settings).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(seed_buttons, text="Refresh", command=self._slow_refresh_now).pack(
            side="left", padx=(6, 0)
        )

        # -- runes ------------------------------------------------------
        rune_frame = ttk.Labelframe(
            panes, text="Running runes  (select a row and press Delete to kill)"
        )
        panes.add(rune_frame, weight=3)
        columns = ("seed", "pid", "status", "uptime", "log")
        self.rune_tree = ttk.Treeview(
            rune_frame, columns=columns, show="headings", selectmode="browse", height=6
        )
        for col, title, width, stretch in (
            ("seed", "Seed", 130, False),
            ("pid", "PID", 80, False),
            ("status", "Status", 120, False),
            ("uptime", "Uptime", 100, False),
            ("log", "Log file", 420, True),
        ):
            self.rune_tree.heading(col, text=title)
            self.rune_tree.column(col, width=width, stretch=stretch, anchor="w")
        self.rune_tree.tag_configure("dead", foreground="#888888")
        self.rune_tree.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        self.rune_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.rune_tree.bind("<Delete>", lambda _e: self._kill_selected())

        rune_buttons = ttk.Frame(rune_frame)
        rune_buttons.pack(fill="x", padx=6, pady=6)
        self.kill_button = ttk.Button(
            rune_buttons, text="Kill selected (Del)", command=self._kill_selected
        )
        self.kill_button.pack(side="left")
        ttk.Button(rune_buttons, text="Clear finished", command=self._clear_finished).pack(
            side="left", padx=(6, 0)
        )
        self.log_button = ttk.Button(rune_buttons, text="Open log", command=self._open_log)
        self.log_button.pack(side="left", padx=(6, 0))

        # -- log tail ---------------------------------------------------
        log_frame = ttk.Labelframe(panes, text="Log (selected rune)")
        panes.add(log_frame, weight=2)
        self.log_text = tk.Text(
            log_frame, height=8, wrap="none", state="disabled", font=("Consolas", 9)
        )
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

        # -- status bar -------------------------------------------------
        self.status_label = ttk.Label(self.root, anchor="w")
        self.status_label.pack(fill="x", padx=8, pady=(4, 6))
        self._update_status_bar()
        self._update_buttons()

    # ------------------------------------------------------ seeds table

    def _all_seeds(self) -> list[SeedInfo]:
        known = {seed.id for seed in self.seeds}
        return self.seeds + [s for s in self.remote_seeds if s.id not in known]

    def _refresh_seeds(self) -> None:
        selected = self.seed_tree.selection()
        self.seed_tree.delete(*self.seed_tree.get_children())
        for seed in self._all_seeds():
            issues = seed_issues(seed, self.gpu)
            status = "✓ available" if not issues else "✗ " + "; ".join(issues)
            if seed.source == "remote":
                status = "not pulled — Start pulls it first" if not issues else status
            self.seed_tree.insert(
                "",
                "end",
                iid=seed.id,
                values=(seed.id, seed.source, seed.vram_mb or "—", status, seed.description),
                tags=() if not issues else ("unavailable",),
            )
        for iid in selected:
            if self.seed_tree.exists(iid):
                self.seed_tree.selection_set(iid)
        self._update_buttons()

    def _selected_seed(self) -> SeedInfo | None:
        selection = self.seed_tree.selection()
        if not selection:
            return None
        return next((s for s in self._all_seeds() if s.id == selection[0]), None)

    def _selected_rune(self) -> RuneProc | None:
        selection = self.rune_tree.selection()
        return self.runes.get(selection[0]) if selection else None

    def _update_buttons(self) -> None:
        seed = self._selected_seed()
        startable = seed is not None and not seed_issues(seed, self.gpu)
        self.start_button.state(["!disabled"] if startable else ["disabled"])
        rune = self._selected_rune()
        self.kill_button.state(["!disabled"] if rune else ["disabled"])
        self.log_button.state(["!disabled"] if rune else ["disabled"])

    # ------------------------------------------------- registry search

    def _search_registry(self) -> None:
        if not self.config.rune_key:
            self._server_settings()
            return
        query = self.search_entry.get().strip()
        self.search_button.state(["disabled"])
        self.search_status.configure(text="searching…")

        def worker() -> None:
            try:
                packages = registry.search_packages(self.config, query=query)
                results = [
                    SeedInfo(
                        id=p.id,
                        source="remote",
                        title=p.name,
                        description=p.description or p.name,
                    )
                    for p in packages
                ]
                message = f"{len(results)} result(s)"
            except Exception as exc:
                results = []
                message = str(exc)
            self.root.after(0, lambda: self._search_done(results, message))

        threading.Thread(target=worker, daemon=True).start()

    def _search_done(self, results: list[SeedInfo], message: str) -> None:
        self.remote_seeds = results
        self.search_button.state(["!disabled"])
        self.search_status.configure(text=message)
        self._refresh_seeds()

    # ---------------------------------------------------------- actions

    def _start_selected(self) -> None:
        seed = self._selected_seed()
        if seed is None:
            return
        issues = seed_issues(seed, self.gpu)
        if issues:
            messagebox.showerror(
                "Seed unavailable",
                f"'{seed.id}' cannot start:\n\n" + "\n".join(f"• {i}" for i in issues),
                parent=self.root,
            )
            return
        if not self.config.rune_key:
            self._server_settings()
            return
        cache = cfg.cache_dir()
        cache.mkdir(parents=True, exist_ok=True)

        running = [r for r in self.runes.values() if r.seed_id == seed.id and r.alive()]
        if running and not messagebox.askyesno(
            "Already running",
            f"A '{seed.id}' rune is already running (pid {running[0].proc.pid}).\n"
            "Start another one?",
            parent=self.root,
        ):
            return

        log_dir = cfg.logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{seed.id}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        popen_kwargs: dict[str, Any] = (
            {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {"start_new_session": True}
        )
        env = {
            **os.environ,
            "RUNE_KEY": self.config.rune_key,
            "SERVER_URL": self.config.server_url,
            "SEED_CACHE_PATH": str(cache),
            # UTF-8 stdio: redirected output would otherwise use the legacy
            # codepage on Windows, garbling the log tail.
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        with open(log_path, "ab") as log_file:
            # The CLI resolves local vs pulled seeds itself and pulls from the
            # registry when needed — the manager just supervises the process.
            proc = subprocess.Popen(
                [sys.executable, "-m", "charms_launcher.cli", "run", seed.id],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                **popen_kwargs,
            )

        self._uid += 1
        uid = f"rune{self._uid}"
        rune = RuneProc(uid=uid, seed_id=seed.id, proc=proc, log_path=log_path)
        self.runes[uid] = rune
        self.rune_tree.insert(
            "",
            "end",
            iid=uid,
            values=(seed.id, proc.pid, "loading", rune.uptime(), str(log_path)),
        )
        self.rune_tree.selection_set(uid)
        self._update_buttons()

    def _kill_selected(self) -> None:
        rune = self._selected_rune()
        if rune is None:
            return
        if not rune.alive():  # Delete on a finished row just removes it
            self.rune_tree.delete(rune.uid)
            del self.runes[rune.uid]
            self._update_buttons()
            return
        if not messagebox.askyesno(
            "Kill rune",
            f"Kill rune '{rune.seed_id}' (pid {rune.proc.pid})?",
            parent=self.root,
        ):
            return
        kill_tree(rune.proc)

    def _clear_finished(self) -> None:
        for uid, rune in list(self.runes.items()):
            if not rune.alive():
                self.rune_tree.delete(uid)
                del self.runes[uid]
        self._update_buttons()

    def _open_log(self) -> None:
        rune = self._selected_rune()
        if rune is None:
            return
        if IS_WINDOWS:
            os.startfile(rune.log_path)  # noqa: S606
        else:
            opener = "xdg-open" if sys.platform == "linux" else "open"
            subprocess.Popen([opener, str(rune.log_path)])

    # ---------------------------------------------------------- dialogs

    def _server_settings(self) -> None:
        """First-use authentication / server settings dialog."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Charms server")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Server URL").grid(row=0, column=0, sticky="w", pady=2)
        server_entry = ttk.Entry(body, width=44)
        server_entry.insert(0, self.config.server_url)
        server_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=2)

        ttk.Label(body, text="Rune key").grid(row=1, column=0, sticky="w", pady=2)
        key_entry = ttk.Entry(body, width=44, show="•")
        key_entry.insert(0, self.config.rune_key)
        key_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)

        ttk.Label(
            body,
            text="Create a rune key in the web UI → My Runes. It authenticates\n"
            "this machine for running runes and pulling seed packages.",
            foreground="#888888",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        status = ttk.Label(body, text="")
        status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        save_button = ttk.Button(buttons, text="Verify && save")

        def apply() -> None:
            candidate = cfg.LauncherConfig(
                server_url=server_entry.get().strip() or cfg.DEFAULT_SERVER_URL,
                rune_key=key_entry.get().strip(),
                env=self.config.env,
            )
            if not candidate.rune_key:
                status.configure(text="a rune key is required", foreground="#b00020")
                return
            save_button.state(["disabled"])
            status.configure(text="verifying…", foreground="")

            def worker() -> None:
                try:
                    count = registry.verify_login(candidate)
                    error = ""
                except Exception as exc:
                    count, error = 0, str(exc)

                def done() -> None:
                    save_button.state(["!disabled"])
                    if error:
                        status.configure(text=error, foreground="#b00020")
                        return
                    self.config = candidate
                    cfg.save_config(candidate)
                    self._update_status_bar()
                    dialog.destroy()
                    messagebox.showinfo(
                        "Connected",
                        f"Authenticated against {candidate.server_url}\n"
                        f"({count} seed package(s) available to pull).",
                        parent=self.root,
                    )

                self.root.after(0, done)

            threading.Thread(target=worker, daemon=True).start()

        save_button.configure(command=apply)
        save_button.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 6))
        key_entry.focus_set()

    def _set_variables(self) -> None:
        seed = self._selected_seed()
        missing: list[str] = []
        if seed is not None:
            for req in SEED_ENV_REQS.get(seed.id, []):
                if req.unmet():
                    missing.extend(req.missing_names())
            # Offer every unset manifest variable, optional ones included.
            for spec in seed.env_specs:
                if not os.environ.get(spec.name):
                    missing.append(spec.name)
        if not missing:
            messagebox.showinfo(
                "Nothing to set",
                "No missing variables for the selected seed.",
                parent=self.root,
            )
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Set variables")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        entries: dict[str, tk.Entry] = {}
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        secret_names = {spec.name for spec in (seed.env_specs if seed else []) if spec.secret}
        missing = list(dict.fromkeys(missing))
        for row, name in enumerate(missing):
            ttk.Label(body, text=name).grid(row=row, column=0, sticky="w", pady=2)
            secret = name in secret_names or any(marker in name for marker in SECRET_MARKERS)
            entry = ttk.Entry(body, width=52, show="•" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=2)
            entries[name] = entry
        persist = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            body, text="Save to ~/.charms/launcher.json for future sessions", variable=persist
        ).grid(row=len(missing), column=0, columnspan=2, sticky="w", pady=(10, 0))

        def apply() -> None:
            changed = False
            for name, entry in entries.items():
                value = entry.get().strip()
                if not value:
                    continue
                os.environ[name] = value
                if persist.get():
                    self.config.env[name] = value
                    changed = True
            if changed:
                cfg.save_config(self.config)
            dialog.destroy()
            self._refresh_seeds()
            self._update_status_bar()

        buttons = ttk.Frame(body)
        buttons.grid(row=len(missing) + 1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Apply", command=apply).pack(side="right", padx=(0, 6))
        next(iter(entries.values())).focus_set()

    # ------------------------------------------------------- background

    def _tick(self) -> None:
        for uid, rune in self.runes.items():
            self.rune_tree.set(uid, "status", rune.status())
            self.rune_tree.set(uid, "uptime", rune.uptime())
            self.rune_tree.item(uid, tags=() if rune.alive() else ("dead",))
        self._render_log()
        self.root.after(1000, self._tick)

    def _slow_refresh(self) -> None:
        self._slow_refresh_now()
        self.root.after(5000, self._slow_refresh)

    def _slow_refresh_now(self) -> None:
        self.gpu = probe_gpu()
        self.seeds = discover_seeds()
        self._refresh_seeds()
        self._update_status_bar()

    def _render_log(self) -> None:
        rune = self._selected_rune()
        if rune is None:
            key = None
            text = ""
        else:
            text = read_tail(rune.log_path, 16384)
            key = (rune.uid, len(text))
        if key == self._log_rendered_for:
            return
        self._log_rendered_for = key
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _update_status_bar(self) -> None:
        rune_key = "rune key ✓" if self.config.rune_key else "RUNE KEY MISSING — Server settings…"
        gpu = (
            f"GPU: {self.gpu.name} ({self.gpu.free_mb} MB free / {self.gpu.total_mb} MB)"
            if self.gpu
            else "GPU: none detected"
        )
        self.status_label.configure(
            text=f"{rune_key}   ·   cache: {cfg.cache_dir()}   ·   {gpu}   ·   "
            f"server: {self.config.server_url}",
            foreground="" if self.config.rune_key else "#b00020",
        )

    def _on_close(self) -> None:
        alive = [r for r in self.runes.values() if r.alive()]
        if alive:
            answer = messagebox.askyesnocancel(
                "Quit",
                f"{len(alive)} rune(s) still running.\n\n"
                "Yes — kill them and quit\n"
                "No — leave them running and quit",
                parent=self.root,
            )
            if answer is None:
                return
            if answer:
                for rune in alive:
                    kill_tree(rune.proc)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    with contextlib.suppress(tk.TclError):
        ttk.Style(root).theme_use("vista" if IS_WINDOWS else "clam")
    RuneManagerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
