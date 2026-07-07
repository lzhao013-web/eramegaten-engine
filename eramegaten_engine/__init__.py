"""Modern EraBasic/Emuera-compatible runtime for the local eraMegaten tree."""

from .loader import load_program
from .runtime import EraRuntime

__all__ = ["load_program", "EraRuntime"]
