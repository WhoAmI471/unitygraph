"""Entry point: `python -m unitygraph ...`, or run this file directly (the plugin's bin/ wrappers do)."""
import os
import sys

if not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if len(sys.argv) > 1 and sys.argv[1] == 'guard':  # hook path: keep it light, skip loading the indexer
    from unitygraph.guard import main
else:
    from unitygraph.core import main

main()
