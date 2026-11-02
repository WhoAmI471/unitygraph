"""Command dispatch shared by `python -m unitygraph`, the plugin's bin/ wrappers and the pip entry point."""
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'guard':  # PreToolUse hook: stay light, don't load the indexer
        from unitygraph.guard import main as run
    elif cmd == 'mcp':
        from unitygraph.mcp import serve as run
    else:
        from unitygraph.core import main as run
    run()
