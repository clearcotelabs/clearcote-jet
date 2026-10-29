"""Goal-driven browser agent on Clearcote."""

__version__ = "0.3.0"

from .agent import run  # noqa: E402
from .browser import Session  # noqa: E402

__all__ = ["run", "Session", "__version__"]
