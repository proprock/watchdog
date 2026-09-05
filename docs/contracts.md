# WD-003 configuration, registry, and events

Implemented as a small Python library, now backed by WD-004 [storage](storage.md).
The [daemon](daemon.md) now coordinates collection and registry mutations; the
metadata [hook adapter and installer](hooks.md) are available; project CLI remains WD-008. Importing these modules does
not register projects, create directories, install hooks, or start processes.

## Configuration

`config.user_paths()` returns platformdirs user config/data/runtime paths without
creating them. The configuration file is `config.toml`; project data is located
under `data/projects/<project UUID>`. Paths are local to the host, not portable
configuration to copy between operating systems.

`load_config(path)` returns defaults when the file is absent. Existing files must
be valid TOML with schema version 1. Unknown fields, invalid UUIDs, relative
registered paths, duplicate projects, invalid overrides, and incompatible quotas
raise `ConfigError`. Invalid files are never replaced with defaults.

Minimal configuration, with all other limits inherited from the architecture:

```toml
schema_version = 1
projects = []

[defaults]
content_days = 30
metrics_days = 180
```

Projects contain `id`, absolute `root`, optional `git_common_dir`, and partial
`overrides`. Overrides apply only to that project. Limits must be positive integers,
and `payload_bytes <= inbox_bytes <= project_bytes`; numeric strings and booleans
are rejected. TOML serialization omits optional values rather than inventing nulls.

`save_config(path, config)` validates, writes a sibling temporary file, and replaces
the destination. An invalid existing configuration is protected from overwrite.
This low-level function does not coordinate concurrent writers. Use
`daemon.mutate_registry(paths, callback)` for serialized load/edit/save operations.
No config migration or general settings framework is provided.

## Registry

```python
from pathlib import Path
from agent_watchdog.config import load_config, save_config, user_paths
from agent_watchdog.registry import Registry

paths = user_paths()
registry = Registry(load_config(paths.config))
project = registry.add(Path.cwd())
save_config(paths.config, registry.config)
```

- Registration assigns a random project UUID. Re-registering a Git worktree or
  the same canonical root returns the existing project.
- Git discovery uses `rev-parse` for the checkout root and common directory.
  Worktrees share a project UUID; clones remain separate regardless of remote URL.
  Git environment overrides are removed from the discovery subprocess. Invalid
  Git metadata raises `RegistryError`; it is not treated as an ordinary directory.
- Non-Git registration uses the explicit directory root. Descendants resolve to
  it, but overlapping non-Git registrations are rejected. An unregistered nested
  Git repository is not collected through an enclosing non-Git registration.
- Resolve symlinks and respect host filesystem identity. Checkout IDs are UUIDv5
  values derived from the project UUID and canonical checkout path. Different
  worktrees have different checkout IDs; moving a checkout changes that ID.
- `resolve(path)` returns a resolution or `None` for an unregistered existing
  directory. Invalid/missing paths raise `RegistryError`.
- `relocate(id, path)` requires the old root to be absent, preserves the project
  UUID and overrides, and rejects a change between Git and non-Git or a registry
  collision. Repair moved Git worktrees with Git itself before registering their
  new locations. Relocation never rewrites Git metadata.
- `remove(id)` disables registration only; it deletes neither source nor data.

## Event envelope

`events.Envelope` implements schema version 1 from the architecture. It validates
project/event UUIDs, event kinds, source/surface, timezone-aware timestamps, and
JSON payload values. Native IDs, provider version, checkout ID, and source event
time remain nullable when unavailable. Received time and a new event UUID are
assigned at construction; JSON round trips retain both for replay.

Payloads are empty or namespaced by the provider, for example
`{"codex": {"hook_event_name": "FutureEvent"}}`. Unknown provider event names can
be retained with kind `unknown`; that is not a support claim. Unknown envelope
schema versions are rejected and quarantined by WD-004 ingestion. Availability
uses `observed`, `inferred`, `unknown`, or `unavailable`, never an invented zero.
Serialization does not redact content; redaction and input-size limits belong to
the ingestion boundary in WD-006/WD-007.

Implementation references: [Pydantic strict validation](https://docs.pydantic.dev/latest/concepts/strict_mode/),
[platformdirs paths](https://platformdirs.readthedocs.io/en/latest/api.html), and
[ty configuration](https://docs.astral.sh/ty/configuration/).
