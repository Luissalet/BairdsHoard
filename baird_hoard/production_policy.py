"""Bind an installed profile to the operator-selected Talos database and Hub library."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
import logging
from pathlib import Path
import sqlite3
from types import ModuleType

from .hoard_link.atomic import read_json, write_json_atomic
from .monitor_privacy import PolicySnapshot

CONFIG_NAME = "capture-policy.json"
log = logging.getLogger("baird.production_policy")


def _file(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"An existing absolute file path is required: {path}")
    return path.resolve(strict=True)


@dataclass(frozen=True)
class Binding:
    database: Path
    provider_file: Path
    provider: ModuleType

    @classmethod
    def open(cls, config: Path) -> Binding:
        data = read_json(config)
        if not isinstance(data, dict) or not all(isinstance(data.get(key), str) for key in ("database", "provider")):
            raise ValueError("The capture policy binding is missing or invalid.")
        return cls.from_paths(data["database"], data["provider"])

    @classmethod
    def from_paths(cls, database: str | Path, provider: str | Path) -> Binding:
        database, provider = _file(database), _file(provider)
        spec = spec_from_file_location("_baird_installed_capture_policy", provider)
        if spec is None or spec.loader is None:
            raise ImportError("The Hub capture policy library is unavailable.")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        if not all(callable(getattr(module, key, None)) for key in ("ScreenCapturePolicy", "provision", "remove")):
            raise ImportError("The Hub capture policy library does not support this integration.")
        return cls(database, provider, module)

    def authority(self):
        return self.provider.ScreenCapturePolicy(self.database, PolicySnapshot)

    def prepare(self, monitor: str, layout: str) -> None:
        self.provider.provision(self.database, monitor, layout)

    def remove(self) -> None:
        with closing(sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True)) as conn:
            installed = conn.execute("SELECT 1 FROM sqlite_master WHERE name GLOB 'capture_policy_*' LIMIT 1").fetchone()
        if installed:
            self.provider.remove(self.database)


def configured_binding(config: Path | None) -> Binding | None:
    if config is None or not config.is_file():
        return None
    try:
        return Binding.open(config)
    except (ImportError, OSError, SyntaxError, ValueError):
        log.warning("The installed screen policy binding is unavailable.", exc_info=True)
        return None


def worker_binding(database: Path, provider: Path) -> Binding | None:
    try:
        return Binding.from_paths(database, provider)
    except (ImportError, OSError, SyntaxError, ValueError):
        log.warning("The installed screen policy binding is unavailable.", exc_info=True)
        return None


def _backup(database: Path, destination: Path) -> None:
    if not destination.is_absolute() or destination.resolve() == database:
        raise ValueError("The SQLite backup must use a different absolute path.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb"):
        pass  # Reserve a fresh destination without overwriting an earlier backup.
    with (closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source,
          closing(sqlite3.connect(destination)) as target):
        source.backup(target, pages=256)
        if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("The SQLite backup failed its integrity check.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True, help="Absolute path to the Baird data profile")
    actions = parser.add_subparsers(dest="action", required=True)
    bind = actions.add_parser("bind")
    bind.add_argument("--talos-db", type=Path, required=True)
    bind.add_argument("--hub-root", type=Path, required=True)
    bind.add_argument("--backup", type=Path, required=True)
    actions.add_parser("remove")
    args = parser.parse_args()
    if not args.profile.is_absolute():
        parser.error("--profile must be absolute")
    config = args.profile / CONFIG_NAME
    try:
        if args.action == "bind":
            if config.exists():
                raise ValueError("Remove the previous capture policy binding first.")
            binding = Binding.from_paths(args.talos_db, args.hub_root / "hoard_link" / "capture_policy.py")
            _backup(binding.database, args.backup)
            write_json_atomic(config, {"database": str(binding.database), "provider": str(binding.provider_file)})
        elif config.exists():
            Binding.open(config).remove()
            config.unlink()
    except (ImportError, OSError, SyntaxError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
