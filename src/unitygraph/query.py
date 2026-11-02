"""Search serialized values: which objects have a component whose fields match conditions.

    unitygraph query "Carryable.itemType=Hook"
    unitygraph query "Leak.nailsRequired>3, boardedFlow<0.5"
    unitygraph query "PlayerController.moveSpeed"        # distribution of values

Values are compared as rendered (enum names, @Object, →Asset); numbers numerically. Prefab instances
report their effective value: the prefab's own value unless the instance overrides it.
"""
import os
import re
from collections import Counter

from unitygraph import core as C

COND = re.compile(r'^\s*(?:(\w+)\.)?(\w+(?:\.\w+)*)\s*(?:(!=|>=|<=|=|>|<|~)\s*(.*?))?\s*$')


def _parse(expr):
    cls, conds = None, []
    for part in expr.split(','):
        m = COND.match(part)
        if not m:
            raise ValueError(f'cannot read condition "{part.strip()}"')
        c, field, op, val = m.groups()
        if c:
            if cls and c != cls:
                raise ValueError('all conditions must be on one component type')
            cls = c
        conds.append((field, op, (val or '').strip().strip('"\'')))
    if not cls:
        raise ValueError('start with the component type: Class.field=value')
    return cls, conds


def _match(value, op, want):
    if op is None:
        return True
    if value is None:
        return op == '!=' and want != ''
    v = value.strip('"')
    a, b = C._num(v), C._num(want)
    if op == '~':
        return re.search(want, v, re.I) is not None
    if op in ('=', '!='):
        same = (a == b) if a is not None and b is not None else v.lower() == want.lower()
        return same if op == '=' else not same
    if a is None or b is None:
        return False
    return {'>': a > b, '<': a < b, '>=': a >= b, '<=': a <= b}[op]


def _values(ctx, comp, ftypes):
    out = {}
    for k, v in comp.get('fields', {}).items():
        ftype, ns = ftypes.get(k, ('', ''))
        out[k] = ctx.value(v, ftype, ns)
    return out


def _plain(path):
    return re.sub(r'\s*\(\d+\)', '', path)


def _sub(values, field):
    """Dotted field path into a rendered nested value like {speed=3, ...} is not re-parsed; look up flat keys."""
    return values.get(field)


def _rows(p, full):
    """(file, object path, {field: rendered value}) for every component of class `full` in project files."""
    ftypes = p.class_fields(full)
    for path, e in sorted(p.files.items()):
        fm = e['fm']
        if not fm or not path.endswith(('.unity', '.prefab')) or not C.is_project_asset(p, path):
            continue
        tree = p.tree(path)
        where = {n['id']: npath for npath, n in C.walk_nodes(tree)}
        names = {n['id']: n['n'] for _, n in C.walk_nodes(tree)}
        ctx = C.Ctx(p, fm, names)
        for cid, c in fm['comps'].items():
            if c['type'] != 'MonoBehaviour' or p.script_name(c['script'], c['ident']) != full:
                continue
            go = c['go']
            obj = where.get(go) or where.get(fm['st'].get(go, {}).get('pi'), '?')
            yield path, obj, _values(ctx, c, ftypes)
        if not path.endswith('.unity'):
            continue  # nested instances inside prefabs are reported through the prefab itself
        for pid, pi in fm['pis'].items():
            src = p.guid2path.get(pi['src'], '')
            sfm = p.fm(src) if src.endswith('.prefab') else None
            if not sfm:
                continue
            stree = p.tree(src)
            snames = {n['id']: n['n'] for _, n in C.walk_nodes(stree)}
            spaths = {n['id']: npath for npath, n in C.walk_nodes(stree)}
            sctx = C.Ctx(p, sfm, snames)
            for cid, c in sfm['comps'].items():
                if c['type'] != 'MonoBehaviour' or p.script_name(c['script'], c['ident']) != full:
                    continue
                values = _values(sctx, c, ftypes)
                for tgt, prop, val, ref in pi['mods']:
                    if tgt != cid:
                        continue
                    key = prop.split('.')[0]
                    ftype, ns = ftypes.get(key, ('', ''))
                    if ref and ref.get('fileID', '0') != '0':
                        values[key] = ctx.value(ref)
                    elif '.' not in prop:
                        values[key] = ctx.value(val, ftype, ns)
                inner = spaths.get(c['go'], '')
                inner = inner.split('/', 1)[1] if '/' in inner else ''
                obj = where.get(pid, '?') + (f'/{inner}' if inner else '')
                yield path, obj, values


def report(p, expr, limit=60):
    try:
        cls, conds = _parse(expr)
    except ValueError as e:
        return str(e)
    full = p.lookup_class(cls)
    if not full:
        return f'no type "{cls}"'
    rows = sorted(_rows(p, full), key=lambda r: (r[0], r[1]))
    if not rows:
        return f'{p.short(full)} is not on any object in project scenes or prefabs'
    known = {k for _, _, v in rows for k in v}  # what is actually serialized
    bad = [f for f, _, _ in conds if f.split('.')[0] not in known]
    if bad:
        fields = sorted(k for k in known if k not in C.MIRROR_BASE_FIELDS and not k.startswith('sync'))
        return f'{p.short(full)} has no serialized field {", ".join(bad)}; fields: {", ".join(fields)}'
    if len(conds) == 1 and conds[0][1] is None:  # distribution of one field
        field = conds[0][0]
        dist = Counter(_sub(v, field) for _, _, v in rows)
        lines = [f'{p.short(full)}.{field} over {len(rows)} components:']
        for value, n in dist.most_common(30):
            sample = list(dict.fromkeys(f'{C.stem(f)}{os.path.splitext(f)[1]}:{_plain(o)}'
                                        for f, o, v in rows if _sub(v, field) == value))[:3]
            lines.append(f'  {value if value is not None else "(default)"} ×{n}  e.g. ' + ', '.join(sample))
        return '\n'.join(lines)
    hits = [(f, o, v) for f, o, v in rows if all(_match(_sub(v, fld), op, want) for fld, op, want in conds)]
    if not hits:
        return f'no {p.short(full)} matches {expr} ({len(rows)} checked)'
    lines = [f'{len(hits)} of {len(rows)} {p.short(full)} components match {expr}:']
    shown = [fld for fld, _, _ in conds]
    grouped = Counter()
    order = []
    for f, o, v in hits:
        key = (f, re.sub(r'\s*\(\d+\)', '', o), tuple(f'{fld}={_sub(v, fld)}' for fld in shown))
        if key not in grouped:
            order.append(key)
        grouped[key] += 1
    for key in order[:limit]:
        f, o, vals = key
        n = grouped[key]
        lines.append(f'  {C.stem(f)}{os.path.splitext(f)[1]}: {o}' + (f' ×{n}' if n > 1 else '') + '  ' + ', '.join(vals))
    if len(order) > limit:
        lines.append(f'  …+{len(order) - limit} more')
    return '\n'.join(lines)
