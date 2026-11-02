"""PreToolUse hook: stop the agent from pulling whole Unity YAML files into its context.

Reads the hook event from stdin. A full Read of a large scene/prefab/controller/asset, or a `cat`
of one through Bash, is denied with a reason that names the unitygraph command to run instead.
Targeted reads (Read with offset/limit, grep, head, sed -n) pass, so line-level edits still work.
Set UNITYGRAPH_GUARD=off to disable, or UNITYGRAPH_GUARD_KB to change the size threshold.
"""
import json
import os
import re
import sys

YAML_EXTS = {'.unity': 'scene', '.prefab': 'prefab', '.controller': 'anim', '.overrideController': 'anim',
             '.asset': 'asset', '.anim': None, '.mat': 'material', '.mask': None, '.playable': None}
WHOLE_FILE_CMD = re.compile(r'(?:^|[|;&(]\s*|\s)(cat|type|more|less|bat|Get-Content|gc)\s+(?P<args>[^|;&]+)', re.I)
BOUNDED = re.compile(r'-(?:TotalCount|Head|Tail|First|Last)\b', re.I)


def _suggest(path):
    ext = os.path.splitext(path)[1]
    name = os.path.splitext(os.path.basename(path))[0]
    cmd = YAML_EXTS.get(ext)
    if cmd in ('scene', 'prefab'):
        return (f'unitygraph {cmd} "{name}" --depth 2 (then --path "A/B" to zoom in), '
                f'unitygraph find "<regex>", unitygraph query "Class.field=value"')
    if cmd:
        return f'unitygraph {cmd} "{name}" or unitygraph refs "{os.path.basename(path)}"'
    return f'unitygraph refs "{os.path.basename(path)}"'


def check(event, threshold_kb=32):
    """Returns a deny reason, or None to let the call through."""
    tool, inp = event.get('tool_name'), event.get('tool_input') or {}
    cwd = event.get('cwd') or os.getcwd()
    targets = []
    if tool == 'Read':
        if inp.get('offset') or inp.get('limit'):
            return None
        targets = [inp.get('file_path', '')]
    elif tool == 'Bash':
        cmd = inp.get('command', '')
        for m in WHOLE_FILE_CMD.finditer(cmd):
            if BOUNDED.search(m.group('args')):
                continue
            targets += [a.strip('"\'') for a in re.findall(r'"[^"]+"|\'[^\']+\'|\S+', m.group('args'))]
    for t in targets:
        if os.path.splitext(t)[1] not in YAML_EXTS:
            continue
        path = t if os.path.isabs(t) else os.path.join(cwd, t)
        try:
            size = os.path.getsize(path)
            with open(path, 'rb') as f:
                if not f.read(5) == b'%YAML':
                    continue
        except OSError:
            continue
        if size < threshold_kb * 1024:
            continue
        kb = size // 1024
        return (f'{os.path.basename(t)} is {kb} KB of Unity YAML (~{size // 4 // 1000}k tokens of fileIDs). '
                f'Query it instead: {_suggest(t)}. To edit specific lines, locate them with Grep and Read '
                f'with offset/limit.')
    return None


def main():
    if os.environ.get('UNITYGRAPH_GUARD', '').lower() in ('0', 'off', 'false', 'no'):
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    reason = check(event, int(os.environ.get('UNITYGRAPH_GUARD_KB', '32') or 32))
    if reason:
        json.dump({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                          'permissionDecisionReason': reason}}, sys.stdout)


if __name__ == '__main__':
    main()
