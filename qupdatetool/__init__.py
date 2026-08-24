"""
QUpdateTool - a universal updater for desktop applications.

One tool, branded per application at build time, that checks a git release
backend for a newer version, verifies its OpenPGP signature, installs it
using the right mechanism for the host platform, and restarts the app.
"""

__version__ = "0.0.1"

__all__ = ["__version__"]
