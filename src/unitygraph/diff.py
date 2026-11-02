"""Semantic diff of scenes and prefabs: objects added, removed, moved, renamed, toggled, components and
serialized values changed, prefab overrides changed — instead of a git diff of fileID soup."""
import os
import subprocess

from unitygraph import core as C


def _git(root, *args):
    res = subprocess.run(['git', '-C', root, *args], capture_output=True, stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode('utf-8', 'replace').strip() or 'git failed')
    return res.stdout


def _text_at(p, path, rev):
    """File text at a git revision (None: working tree). '' when the file does not exist there."""
    if rev is None:
        full = os.path.join(p.root, path)
        if not os.path.exists(full):
            return ''
        with open(full, 'rb') as f:
            raw = f.read()
    else:
        try:
            raw = _git(p.root, 'show', f'{rev}:./{path}')
        except RuntimeError:
            return ''
    return raw.decode('utf-8', 'replace').replace('\r\n', '\n')


def _flatten(p, fm, tree):
    """id -> (path, node, {component key: (label name, {field: value})})."""
    names = {n['id']: n['n'] for _, n in C.walk_nodes(tree)}
    ctx = C.Ctx(p, fm, names)
    out = {}
    for path, n in C.walk_nodes(tree):
        comps = {}
        if n['k'] == 'g':
            g = fm['gos'].get(n['id'], {})
            cids = g.get('comps', [])
        else:
            cids = [cid for cid, c in fm['comps'].items() if fm['st'].get(c['go'], {}).get('pi') == n['id']]
        seen = {}
        for cid in cids:
            c = fm['comps'].get(cid)
            if not c or cid in fm['trs']:
                continue
            name = p.short(p.script_name(c['script'], c['ident'])) if c['type'] == 'MonoBehaviour' else c['type']
            fields = {}
            if c['type'] == 'MonoBehaviour':
                for kv in ctx.fields(c, full=True):
                    k, _, v = kv.partition('=')
                    if k not in C.MIRROR_BASE_FIELDS:  # per-object network ids churn on every rebuild
                        fields[k] = v
            refs = c.get('refs') or {}
            for k, r in refs.items():
                fields[k] = '→' + p.asset_label(r['guid'], r.get('fileID', '0'))
            if c.get('en') == '0':
                fields['(enabled)'] = 'false'
            seen[name] = seen.get(name, 0) + 1
            comps[(cid, name, seen[name])] = (name, fields)
        out[n['id']] = (path, n, comps)
    return out


def _match(old, new):
    """Pairs old ids with new ids: same fileID first, then same hierarchy path (generated scenes get new ids)."""
    pairs = {i: i for i in old if i in new}
    left_old = [i for i in old if i not in pairs]
    used = set(pairs.values())
    by_path = {}
    for i in new:
        if i not in used:
            by_path.setdefault(new[i][0], []).append(i)
    for i in left_old:
        cands = by_path.get(old[i][0])
        if cands:
            pairs[i] = cands.pop(0)
    return pairs


def _comp_changes(old_comps, new_comps):
    out = []
    o_by_id = {k[0]: k for k in old_comps}
    n_by_id = {k[0]: k for k in new_comps}
    o_by_name = {(k[1], k[2]): k for k in old_comps}
    n_by_name = {(k[1], k[2]): k for k in new_comps}
    matched = []
    for cid, ok in o_by_id.items():
        nk = n_by_id.get(cid) or n_by_name.get((ok[1], ok[2]))
        if nk:
            matched.append((ok, nk))
    m_old = {a for a, _ in matched}
    m_new = {b for _, b in matched}
    for k in new_comps:
        if k not in m_new:
            out.append(f'+{k[1]}')
    for k in old_comps:
        if k not in m_old:
            out.append(f'-{k[1]}')
    for ok, nk in matched:
        name, of = old_comps[ok]
        _, nf = new_comps[nk]
        for f in sorted(set(of) | set(nf)):
            a, b = of.get(f), nf.get(f)
            if a != b:
                if a is None:
                    out.append(f'{name}.{f}={b}')
                elif b is None:
                    out.append(f'{name}.{f} removed (was {a})')
                else:
                    out.append(f'{name}.{f}: {a} → {b}')
    return out


def diff_file(p, path, rev='HEAD', to=None):
    """Lines describing how `path` changed from `rev` to `to` (None = working tree)."""
    texts = [_text_at(p, path, rev), _text_at(p, path, to)]
    if texts[0] == texts[1]:
        return []
    side = []
    for text in texts:
        fm = C.parse_unity_file(text) if text.startswith('%YAML') else None
        tree = C.build_tree(p, path, fm) if fm else []
        side.append(_flatten(p, fm, tree) if fm else {})
    old, new = side
    pairs = _match(old, new)
    matched_new = set(pairs.values())
    added = [i for i in new if i not in matched_new]
    removed = [i for i in old if i not in pairs]
    lines = []

    def top_only(ids, table):
        # report a whole added/removed subtree once, at its root
        paths = {table[i][0] for i in ids}
        return [i for i in ids if not any(table[i][0].startswith(q + '/') for q in paths)]

    groups = {}  # identical changes on similarly named objects → one line listing where

    def emit(sign, path_, text):
        parent, _, leaf = path_.rpartition('/')
        key = (sign, C._norm_name(leaf), text)
        groups.setdefault(key, []).append((parent, leaf))

    for i in top_only(added, new):
        path_, n, _ = new[i]
        size = f'({n["cnt"] + 1} objects) ' if n.get('cnt') else ''
        emit('+', path_, (size + C.node_line(n)[len(n['n']):].strip()).strip())
    for i in top_only(removed, old):
        path_, n, _ = old[i]
        emit('-', path_, f'({n["cnt"] + 1} objects)' if n.get('cnt') else '')
    for oi, ni in pairs.items():
        opath, on, ocomps = old[oi]
        npath, nn, ncomps = new[ni]
        changes = []
        if on['n'] != nn['n']:
            changes.append(f'renamed from "{on["n"]}"')
        elif opath.rsplit('/', 1)[0] != npath.rsplit('/', 1)[0]:
            changes.append(f'moved from {opath.rsplit("/", 1)[0] if "/" in opath else "(root)"}')
        if on['off'] != nn['off']:
            changes.append('deactivated' if nn['off'] else 'activated')
        if on.get('tag', '') != nn.get('tag', ''):
            changes.append(f'tag {on.get("tag") or "Untagged"} → {nn.get("tag") or "Untagged"}')
        if on.get('src') != nn.get('src'):
            changes.append(f'source {os.path.basename(on.get("srcp") or "-")} → {os.path.basename(nn.get("srcp") or "-")}')
        oov, nov = set(on.get('ov') or []), set(nn.get('ov') or [])
        changes += [f'override +{x}' for x in sorted(nov - oov)] + [f'override -{x}' for x in sorted(oov - nov)]
        changes += _comp_changes(ocomps, ncomps)
        if changes:
            emit('~', npath, '; '.join(changes))
    for (sign, base, text), where in groups.items():
        if len(where) == 1:
            parent, leaf = where[0]
            line = f'{sign} {parent + "/" if parent else ""}{leaf}'
        else:
            leaves = {leaf for _, leaf in where}
            name = next(iter(leaves)) if len(leaves) == 1 else base + '*'
            parents = sorted({parent or '(root)' for parent, _ in where})
            shown = ', '.join(parents[:3]) + (f', …+{len(parents) - 3}' if len(parents) > 3 else '')
            line = f'{sign} {name} ×{len(where)} in {shown}'
        lines.append(line + ((': ' if sign == '~' else '  ') + text if text else ''))
    if not lines:
        lines.append('(only scene settings, transforms or ordering changed)')
    return lines


def changed_files(p, rev='HEAD', to=None):
    args = ['diff', '--name-only', rev] + ([to] if to else []) + ['--', '.']
    names = _git(p.root, *args).decode('utf-8', 'replace').split()
    if not to:
        names += _git(p.root, 'ls-files', '--others', '--exclude-standard', '--', '.').decode('utf-8', 'replace').split()
    prefix = _git(p.root, 'rev-parse', '--show-prefix').decode().strip()
    out = []
    for n in names:
        rel = n[len(prefix):] if prefix and n.startswith(prefix) else n
        if rel.endswith(('.unity', '.prefab')) and C.is_project_asset(p, rel):
            out.append(rel)
    return sorted(set(out))


def report(p, targets, rev='HEAD', to=None, limit=80):
    try:
        if targets:
            files = []
            for t in targets:
                if t in p.files or os.path.exists(os.path.join(p.root, t)):
                    files.append(t.replace('\\', '/'))
                    continue
                path, _ = C.pick_file(p, t, '.unity')
                path = path or C.pick_file(p, t, '.prefab')[0]
                if not path:
                    return f'no scene or prefab matches "{t}"'
                files.append(path)
        else:
            files = changed_files(p, rev, to)
    except RuntimeError as e:
        return f'git: {e}'
    if not files:
        return f'no scene or prefab changed since {rev}'
    out = []
    for f in files:
        try:
            lines = diff_file(p, f, rev, to)
        except RuntimeError as e:
            lines = [f'git: {e}']
        if not lines:
            continue
        out.append(f'## {f}  ({rev} → {to or "working tree"})')
        out += lines[:limit]
        if len(lines) > limit:
            out.append(f'…+{len(lines) - limit} more changes')
        out.append('')
    return '\n'.join(out).rstrip() or f'no changes in {", ".join(files)}'
