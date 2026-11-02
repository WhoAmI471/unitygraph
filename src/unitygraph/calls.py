"""Method-level call graph: who calls a method and what it calls, resolved through field/property types,
static calls, GetComponent<T>() and base classes. Calls through locals or parameters can't be typed by a
regex parser, so they are reported as "likely" when the calling class uses the target class."""
import os
from collections import defaultdict

from unitygraph import core as C


def _chain(p, full):
    """The class and its project base classes."""
    out, seen = [], set()
    while full and full not in seen and full in p.classes:
        seen.add(full)
        out.append(full)
        t = p.classes[full]
        full = next((b for b in (p.find_class(x, t['ns']) for x in t['bases']) if b and b in p.classes), None)
    return out


def _member_type(p, full, name):
    for cls in _chain(p, full):
        t = p.classes[cls]
        for m in t['fields'] + t['props']:
            if m['n'] == name:
                return p.find_class(m['t'], t['ns'])
    return None


def _owners(p, name):
    return [full for full, t in p.classes.items() if t['project'] and any(b['n'] == name for b in t.get('bodies', []))]


def _resolve(p, caller, call, body=None):
    """Class a call site targets, '' when unknown (an untyped local), None when it is not project code."""
    q, _, name = call.rpartition('.')
    if not q:
        for cls in _chain(p, caller):
            t = p.classes[cls]
            if any(b['n'] == name for b in t.get('bodies', [])) or any(m['n'] == name for m in t['fields'] + t['props']):
                return cls
        return None
    ns = p.classes[caller]['ns']
    local = (body or {}).get('locals', {}).get(q)
    if local:
        c = p.find_class(local, ns)
        return c if c and p.classes[c]['project'] else None
    mt = _member_type(p, caller, q)
    if mt:
        return mt if p.classes[mt]['project'] else None
    c = p.find_class(q, ns) if q[:1].isupper() else None
    if c and p.classes[c]['project']:
        return c
    return ''


def _where(p, full, body):
    return f'{p.short(full)}.{body["n"]} ({os.path.basename(body.get("file", p.classes[full]["file"]))}:{body["line"]})'


def method_report(p, full, name):
    t = p.classes[full]
    bodies = [b for b in t.get('bodies', []) if b['n'] == name]
    member = next((m for m in t['fields'] + t['props'] if m['n'] == name), None)
    if not bodies and not member:
        return f'{p.short(full)} has no method or property "{name}"'
    b0 = bodies[0] if bodies else {'n': name, 'line': t['line'], 'calls': [], 'reads': [], 'refs': []}
    tags = [a for m in t['methods'] if m['n'] == name for a in m['attrs'] if a in C.RPC_ATTRS]
    head = f'{full}.{name}  ({os.path.basename(b0.get("file", t["file"]))}:{b0["line"]}' + \
           (f', {len(bodies)} overloads' if len(bodies) > 1 else '') + ')'
    lines = [head]
    if name in C.UNITY_MESSAGES:
        lines.append('  called by Unity (message)')
    if tags:
        lines.append(f'  attributes: {", ".join(sorted(set(tags)))}')
    calls, other = set(), 0
    for b in bodies:
        for call in b['calls']:
            target = _resolve(p, full, call, b)
            if target:
                calls.add(f'{p.short(target)}.{call.rpartition(".")[2]}')
            elif target == '' and _owners(p, call.rpartition('.')[2]):
                calls.add(f'?.{call.rpartition(".")[2]}')
            else:
                other += 1
    if calls:
        lines.append('  calls: ' + ', '.join(sorted(calls)) + (f'  (+{other} engine/library calls)' if other else ''))
    elif other:
        lines.append(f'  calls: only engine/library code ({other} calls)')
    family = set(_chain(p, full)) | {d for d in p.classes if full in _chain(p, d)}
    sure, likely, refs = [], [], []
    for caller, ct in p.classes.items():
        if not ct['project']:
            continue
        uses = full in C.class_uses(p, caller) or caller in family
        for b in ct.get('bodies', []):
            for call in b['calls'] + b.get('reads', []):
                q, _, n = call.rpartition('.')
                if n != name or (caller == full and b['n'] == name):
                    continue
                target = _resolve(p, caller, call, b)
                if target and target in family:
                    sure.append(_where(p, caller, b))
                elif target == '' and uses:
                    likely.append(_where(p, caller, b))
            if name in b['refs'] and uses:
                refs.append(_where(p, caller, b))
    verb = 'used' if member and not bodies or (member and member in t['props']) else 'called'
    for label, items in ((f'{verb} by', sure), (f'likely {verb} by (untyped local)', likely),
                         ('referenced by (nameof / event +=)', refs)):
        items = sorted(set(items))
        if items:
            lines.append(f'  {label}: ' + ', '.join(items[:25]) + (f', …+{len(items) - 25}' if len(items) > 25 else ''))
    if not (sure or likely or refs) and name not in C.UNITY_MESSAGES:
        lines.append('  no callers found in project code (maybe UnityEvent, reflection, SendMessage or unused)')
    return '\n'.join(lines)


def class_calls(p, full):
    """Per-method fan-in for a whole class."""
    t = p.classes[full]
    names = []
    for b in t.get('bodies', []):
        if b['n'] not in names and b['n'] not in (t['name'], '(declarations)'):
            names.append(b['n'])
    counts = defaultdict(set)
    family = set(_chain(p, full)) | {d for d in p.classes if full in _chain(p, d)}
    for caller, ct in p.classes.items():
        if not ct['project'] or caller in family:
            continue
        uses = full in C.class_uses(p, caller)
        for b in ct.get('bodies', []):
            for call in b['calls'] + b.get('reads', []):
                n = call.rpartition('.')[2]
                if n in names:
                    target = _resolve(p, caller, call, b)
                    if (target and target in family) or (target == '' and uses):
                        counts[n].add(p.short(caller))
    lines = [f'{full}: calls into each method from other classes  (unitygraph calls {t["name"]}.<Method> for details)']
    for n in names:
        users = sorted(counts.get(n, ()))
        msg = ' (Unity message)' if n in C.UNITY_MESSAGES else ''
        lines.append(f'  {n}{msg}: ' + (', '.join(users) if users else '-'))
    return '\n'.join(lines)


def report(p, query):
    cls, _, name = query.rpartition('.')
    if cls:
        full = p.lookup_class(cls)
        if not full:
            return f'no type "{cls}"'
        return method_report(p, full, name)
    full = p.lookup_class(query)
    if full and p.classes[full]['project']:
        return class_calls(p, full)
    owners = _owners(p, query)
    if not owners:
        return f'no project method or type named "{query}"'
    return '\n\n'.join(method_report(p, o, query) for o in owners[:6])
