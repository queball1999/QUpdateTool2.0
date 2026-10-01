"""
In-memory state for the `--setup` wizard.

Deliberately has no PySide6 import. The wizard pages are a thin Qt shell
around this module plus validators.py and generator.py, which keeps the
actual logic testable without a display or the GUI extra installed - the
same split cli.py already relies on to run headless when PySide6 is absent.

The wrapped dict is shaped exactly like config.DEFAULTS, so a WizardState can
be hydrated straight from an existing config.yaml and turned back into a
real Config for the Validate/Demo pages to exercise unmodified.
"""

from __future__ import annotations

import copy

from ... import config as config_module

# Wizard-only fields: not part of config.yaml, but needed to render the brand
# file and the CI workflow in generator.py.
WIZARD_DEFAULTS = {
    "branding_enabled": False,
    "icon_path": "",
    "sign_command": "",
    "target_platforms": ["windows-latest"],
    "qupdatetool_repo": "queball1999/QUpdateTool2.0",
    "qupdatetool_ref": "main",
    "key_secret_name": "RELEASE_PUBLIC_KEY",
    "build_gui": False,
    "extra_lock_fields": [],
    "detected_fingerprints": [],
}


class WizardState:
    """Everything the wizard collects, in one place."""

    def __init__(self, data: dict | None = None, wizard: dict | None = None):
        self.data = config_module.deep_merge(config_module.DEFAULTS, data or {})
        self.wizard = copy.deepcopy(WIZARD_DEFAULTS)
        if wizard:
            self.wizard.update(wizard)

    @classmethod
    def from_config_file(cls, path: str) -> WizardState:
        """Prefill from an existing config.yaml, e.g. `--setup --config existing.yaml`."""
        file_data = config_module.load_yaml_file(path)
        file_data = {
            key: value for key, value in file_data.items()
            if key not in ("lock", "unlock")
        }
        return cls(file_data)

    # --- dotted-path access, same shape as Config ---

    def get(self, path: str, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    def set(self, path: str, value) -> None:
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    # --- conversion ---

    def to_config(self) -> config_module.Config:
        """A real Config, for feeding to Updater()/UpdaterWindow() as-is."""
        return config_module.Config(copy.deepcopy(self.data))

    def to_config_dict(self) -> dict:
        return copy.deepcopy(self.data)
