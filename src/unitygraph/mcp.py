"""Minimal MCP server (stdio, newline-delimited JSON-RPC 2.0) exposing unitygraph as one tool.

One tool keeps the per-session schema cost small; its `args` are the CLI arguments, so every command
(map, scene, prefab, class, calls, where, refs, find, query, diff, anim, input, ui, material, addr, asset)
works the same way as on the command line. Register it with:

    claude mcp add unitygraph -- unitygraph mcp
"""
import io
import json
import os
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout

PROTOCOL = '2025-06-18'
TOOL = {
    'name': 'unitygraph',
    'description': (
        'Query a Unity project without reading its YAML. `args` are unitygraph CLI arguments, e.g. '
        '"map" (start here), "scene Level1 --depth 2", "scene Level1 --path World/Props", "prefab Player", '
        '"class PlayerController", "calls Player.Jump", "where Enemy", "refs Door.prefab", "find ^Door", '
        '"query \\"Enemy.health>50\\"", "diff --rev HEAD~1", "anim Player", "input", "ui MainMenu", '
        '"material Rock", "addr ui_", "asset GameConfig".'),
    'inputSchema': {
        'type': 'object',
        'properties': {
            'args': {'type': 'string', 'description': 'unitygraph command and its arguments'},
            'root': {'type': 'string', 'description': 'Unity project folder (default: the session project)'},
        },
        'required': ['args'],
    },
}


def run(args, root):
    from unitygraph import core
    core._uses_cache.clear()  # module-level caches would go stale in a long-lived process
    core._attach_cache.clear()
    try:
        argv = shlex.split(args)
    except ValueError as e:
        return f'cannot parse arguments: {e}', True
    if argv and argv[0] in ('mcp', 'guard'):
        return f'"{argv[0]}" is not available through MCP', True
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            core.main(['--root', root] + argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            if isinstance(e.code, str):
                err.write(e.code)
        except Exception as e:  # report, keep serving
            code = 1
            err.write(f'{type(e).__name__}: {e}')
    text = out.getvalue()
    if err.getvalue():
        text += ('\n' if text else '') + err.getvalue()
    return text.strip() or '(no output)', code != 0


def handle(msg, default_root):
    method, mid = msg.get('method'), msg.get('id')
    if mid is None:  # notification
        return None
    if method == 'initialize':
        from unitygraph.core import __version__
        req = (msg.get('params') or {}).get('protocolVersion') or PROTOCOL
        result = {'protocolVersion': req, 'capabilities': {'tools': {'listChanged': False}},
                  'serverInfo': {'name': 'unitygraph', 'version': __version__}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [TOOL]}
    elif method == 'tools/call':
        params = msg.get('params') or {}
        if params.get('name') != 'unitygraph':
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': f'unknown tool {params.get("name")}'}}
        a = params.get('arguments') or {}
        text, is_error = run(str(a.get('args', '')), a.get('root') or default_root)
        result = {'content': [{'type': 'text', 'text': text}], 'isError': is_error}
    else:
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'method not found: {method}'}}
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def serve():
    for s in (sys.stdin, sys.stdout):
        if hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8')
    root = os.environ.get('CLAUDE_PROJECT_DIR') or os.getcwd()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            reply = handle(msg, root) if isinstance(msg, dict) else None
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False) + '\n')
            out.flush()


if __name__ == '__main__':
    serve()
