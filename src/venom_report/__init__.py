"""Turn a venom output directory into a single static HTML report."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("venom-report")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"
