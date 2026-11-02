"""Entry point: `python -m unitygraph ...`, or run this file directly (the plugin's bin/ wrappers do)."""
import os
import sys

if not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unitygraph.cli import main  # noqa: E402

main()
