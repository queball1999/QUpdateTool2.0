"""
PyInstaller entry point.

A frozen build cannot use `qupdatetool/__main__.py` as its entry script: when
PyInstaller runs that file directly it is loaded as a top-level module with no
parent package, so the relative imports inside it fail at startup. This module
sits outside the package and uses an absolute import instead, which keeps the
package structure intact in the frozen binary.

`python -m qupdatetool` still works through `qupdatetool/__main__.py`; this
file exists only for the frozen build.
"""

import warnings

# requests probes for chardet/charset_normalizer at import time and warns on
# stderr when neither is importable. In a frozen build the mypyc-compiled
# submodules of charset_normalizer do not always survive bundling, and the
# warning then appears on every single run of a shipped binary.
#
# It is safe to silence here specifically because this tool never relies on
# charset detection: every response it parses is either JSON (decoded by the
# json module) or decoded explicitly as UTF-8. Nothing reads
# response.apparent_encoding, which is the only thing the missing dependency
# affects.
warnings.filterwarnings("ignore", message=".*character detection dependency.*")

try:  # pragma: no cover - import side effect only
    from requests.exceptions import RequestsDependencyWarning

    warnings.simplefilter("ignore", RequestsDependencyWarning)
except Exception:
    pass

from qupdatetool.cli import run  # noqa: E402

if __name__ == "__main__":
    run()
