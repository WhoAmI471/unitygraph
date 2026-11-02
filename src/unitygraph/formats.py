"""Non-scene formats: Input System actions, UI Toolkit (UXML/USS), Addressables groups and materials.

Parsers return small cacheable dicts (stored per file by core); reports render them with names resolved.
"""
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

PARSED_EXTS = {'.inputactions', '.uxml', '.uss', '.tss', '.mat'}


def parse(path, raw):
    """Parsed form of a file with one of PARSED_EXTS (or an Addressables group asset), or None."""
    ext = os.path.splitext(path)[1]
    text = raw.decode('utf-8-sig', 'replace').replace('\r\n', '\n')
    try:
        if ext == '.inputactions':
            return {'kind': 'input', **_parse_input(text)}
        if ext == '.uxml':
            return {'kind': 'uxml', 'tree': _parse_uxml(text)}
        if ext in ('.uss', '.tss'):
            return {'kind': 'uss', 'rules': _parse_uss(text)}
        if ext == '.mat':
            return {'kind': 'mat', **_parse_material(text)}
        if ext == '.asset' and 'm_SerializeEntries' in text and 'm_GroupName' in text:
            return {'kind': 'addr', **_parse_addr_group(text)}
    except (ValueError, ET.ParseError, KeyError, TypeError) as e:
        return {'kind': 'error', 'error': f'{type(e).__name__}: {e}'}
    return None


# ---------------------------------------------------------------- Input System

def _parse_input(text):
    d = json.loads(text)
    maps = []
    for m in d.get('maps', []):
        actions = [{'name': a.get('name', ''), 'type': a.get('type', ''), 'control': a.get('expectedControlType', ''),
                    'interactions': a.get('interactions', ''), 'bindings': []} for a in m.get('actions', [])]
        by_name = {a['name']: a for a in actions}
        composite = None
        for b in m.get('bindings', []):
            act = by_name.get(b.get('action', ''))
            if act is None:
                continue
            if b.get('isComposite'):
                composite = {'composite': b.get('path', ''), 'name': b.get('name', ''), 'parts': []}
                act['bindings'].append(composite)
            elif b.get('isPartOfComposite') and composite is not None:
                composite['parts'].append((b.get('name', ''), b.get('path', '')))
            else:
                composite = None
                act['bindings'].append({'path': b.get('path', ''), 'groups': b.get('groups', ''),
                                        'interactions': b.get('interactions', '')})
        maps.append({'name': m.get('name', ''), 'actions': actions})
    schemes = [s.get('name', '') for s in d.get('controlSchemes', [])]
    return {'name': d.get('name', ''), 'maps': maps, 'schemes': schemes}


def _ctrl(path):
    return re.sub(r'^<(\w+)>/', r'\1/', path)


def report_input(p, path, data):
    lines = [f'# {os.path.basename(path)}  ({path})']
    if data.get('schemes'):
        lines.append('control schemes: ' + ', '.join(data['schemes']))
    for m in data['maps']:
        lines.append(f'map {m["name"]}')
        for a in m['actions']:
            binds = []
            for b in a['bindings']:
                if 'composite' in b:
                    parts = defaultdict(list)
                    for n, pth in b['parts']:
                        parts[n].append(_ctrl(pth).split('/', 1)[-1])
                    device = {_ctrl(pth).split('/', 1)[0] for _, pth in b['parts']}
                    binds.append(f'{"/".join(sorted(device))}:{b["name"] or b["composite"]}(' +
                                 ', '.join(f'{n}={"|".join(v)}' for n, v in parts.items()) + ')')
                elif b['path']:
                    binds.append(_ctrl(b['path']) + (f' [{b["interactions"]}]' if b['interactions'] else ''))
            kind = a['type'] + (f' {a["control"]}' if a['control'] else '')
            extra = f' [{a["interactions"]}]' if a['interactions'] else ''
            lines.append(f'  {a["name"]} ({kind}){extra}: ' + (', '.join(binds) or '(unbound)'))
    users = _code_mentions(p, [a['name'] for m in data['maps'] for a in m['actions']])
    if users:
        lines.append('actions named in code: ' + '; '.join(f'{k}: {", ".join(v)}' for k, v in sorted(users.items())))
    return '\n'.join(lines)


def _code_mentions(p, names):
    """action name -> project classes whose source contains the literal "name" (FindAction, actions["..."])."""
    out = defaultdict(set)
    wanted = set(names)
    for path, e in p.cache['cs'].items():
        if p.classes and any(t.get('name') for t in e['types']) and not path.startswith('Assets/'):
            continue
        try:
            with open(os.path.join(p.root, path), encoding='utf-8-sig', errors='replace') as f:
                src = f.read()
        except OSError:
            continue
        for lit in set(re.findall(r'"([A-Za-z][\w ]*)"', src)) & wanted:
            out[lit].add(os.path.splitext(os.path.basename(path))[0])
    return {k: sorted(v) for k, v in out.items()}


# ---------------------------------------------------------------- UI Toolkit

def _parse_uxml(text):
    root = ET.fromstring(text)

    def node(el):
        tag = el.tag.split('}')[-1]
        attrs = {k.split('}')[-1]: v for k, v in el.attrib.items()}
        return {'tag': tag, 'attrs': attrs, 'kids': [node(c) for c in el]}
    return node(root)


def _uxml_line(n):
    a = n['attrs']
    head = n['tag']
    if a.get('name'):
        head += f'#{a["name"]}'
    if a.get('class'):
        head += ''.join(f'.{c}' for c in a['class'].split())
    extras = []
    for key in ('text', 'label', 'binding-path', 'template', 'src', 'value', 'tooltip'):
        if a.get(key):
            v = a[key]
            if key == 'src':
                v = re.sub(r'^project://database/', '', v).split('?')[0]
            extras.append(f'{key}="{v[:50]}"')
    if a.get('style'):
        extras.append(f'style({len(a["style"].split(";"))})')
    return head + (' ' + ' '.join(extras) if extras else '')


def report_uxml(path, data):
    lines = [f'# {os.path.basename(path)}  ({path})']

    def walk(n, ind):
        lines.append('  ' * ind + _uxml_line(n))
        for k in n['kids']:
            walk(k, ind + 1)
    root = data['tree']
    for k in root['kids'] if root['tag'] == 'UXML' else [root]:
        walk(k, 0)
    return '\n'.join(lines)


def _parse_uss(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    rules = []
    for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', text):
        props = [x.split(':', 1)[0].strip() for x in body.split(';') if ':' in x]
        rules.append((' '.join(sel.split()), props))
    return rules


def report_uss(path, data):
    lines = [f'# {os.path.basename(path)}  ({path})  {len(data["rules"])} rules']
    for sel, props in data['rules']:
        lines.append(f'  {sel}  {{{", ".join(props)}}}')
    return '\n'.join(lines)


# ---------------------------------------------------------------- Addressables

def _parse_addr_group(text):
    from unitygraph.core import iter_docs, parse_yaml_block
    for cls, fid, stripped, typ, body in iter_docs(text):
        d = parse_yaml_block(body)
        if isinstance(d, dict) and 'm_SerializeEntries' in d:
            entries = []
            for e in d.get('m_SerializeEntries') or []:
                if isinstance(e, dict):
                    labels = e.get('m_SerializedLabels') or []
                    entries.append((e.get('m_GUID', ''), str(e.get('m_Address', '')),
                                    [str(x) for x in labels] if isinstance(labels, list) else []))
            return {'group': str(d.get('m_GroupName', '')), 'entries': entries}
    return {'group': '', 'entries': []}


def addresses(p):
    """guid -> (address, group, labels) for every Addressables entry."""
    out = {}
    for path, e in p.files.items():
        f = e.get('fmt')
        if f and f.get('kind') == 'addr':
            for guid, addr, labels in f['entries']:
                out[guid] = (addr, f['group'], labels)
    return out


def report_addr(p, pattern=''):
    table = addresses(p)
    if not table:
        return 'no Addressables groups in this project (Assets/AddressableAssetsData)'
    rx = re.compile(pattern, re.I) if pattern else None
    by_group = defaultdict(list)
    for guid, (addr, group, labels) in table.items():
        asset = p.guid2path.get(guid, f'?{guid[:8]}')
        if rx and not (rx.search(addr) or rx.search(asset) or any(rx.search(lb) for lb in labels)):
            continue
        by_group[group].append(f'  "{addr}" → {asset}' + (f'  [{", ".join(labels)}]' if labels else ''))
    lines = []
    for group in sorted(by_group):
        lines.append(f'group {group} ({len(by_group[group])})')
        lines += sorted(by_group[group])[:80]
    return '\n'.join(lines) or f'no address matches "{pattern}"'


# ---------------------------------------------------------------- materials

def _parse_material(text):
    from unitygraph.core import iter_docs, parse_yaml_block
    for cls, fid, stripped, typ, body in iter_docs(text):
        if typ != 'Material':
            continue
        d = parse_yaml_block(body)
        props = d.get('m_SavedProperties') or {}

        def pairs(key):
            out = {}
            for item in props.get(key) or []:
                if isinstance(item, dict):
                    out.update(item)
            return out
        tex = {}
        for name, v in pairs('m_TexEnvs').items():
            t = (v or {}).get('m_Texture') or {}
            if isinstance(t, dict) and t.get('fileID', '0') != '0':
                tex[name] = {'guid': t.get('guid', ''), 'fileID': t.get('fileID', '0'),
                             'scale': v.get('m_Scale'), 'offset': v.get('m_Offset')}
        kw = d.get('m_ValidKeywords') or d.get('m_ShaderKeywords') or []
        if isinstance(kw, str):
            kw = kw.split()
        return {'name': str(d.get('m_Name', '')), 'shader': d.get('m_Shader') or {}, 'tex': tex,
                'floats': {k: str(v) for k, v in pairs('m_Floats').items()},
                'ints': {k: str(v) for k, v in pairs('m_Ints').items()},
                'colors': pairs('m_Colors'), 'keywords': [str(k) for k in kw],
                'queue': str(d.get('m_CustomRenderQueue', '-1'))}
    return {'name': '', 'shader': {}, 'tex': {}, 'floats': {}, 'ints': {}, 'colors': {}, 'keywords': [], 'queue': '-1'}


def shader_label(p, ref):
    guid = (ref or {}).get('guid', '')
    if not guid:
        return '(none)'
    path = p.guid2path.get(guid, '')
    name = p.cache.get('shaders', {}).get(path, (None, None))[1]
    return f'"{name}"' if name else p.asset_label(guid)


def report_material(p, path, data):
    from unitygraph.core import Ctx, _fmt_num
    ctx = Ctx(p, {'gos': {}, 'trs': {}, 'st': {}, 'comps': {}, 'pis': {}}, {})
    lines = [f'# {os.path.basename(path)}  ({path})', f'shader: {shader_label(p, data["shader"])}']
    if data['keywords']:
        lines.append('keywords: ' + ', '.join(data['keywords']))
    if data['queue'] not in ('-1', ''):
        lines.append(f'render queue: {data["queue"]}')
    for name, t in sorted(data['tex'].items()):
        extra = ''
        if t.get('scale') and ctx.value(t['scale']) != '(1,1)':
            extra += f' scale{ctx.value(t["scale"])}'
        if t.get('offset') and ctx.value(t['offset']) != '(0,0)':
            extra += f' offset{ctx.value(t["offset"])}'
        lines.append(f'  {name} = →{p.asset_label(t["guid"], t["fileID"])}{extra}')
    for name, c in sorted(data['colors'].items()):
        lines.append(f'  {name} = {ctx.value(c)}')
    # floats: only those that differ from what most materials with this shader use
    same = [e['fmt'] for e in p.files.values() if e.get('fmt') and e['fmt'].get('kind') == 'mat'
            and e['fmt']['shader'].get('guid') == data['shader'].get('guid')]
    typical = {}
    for k in data['floats']:
        vals = Counter(m['floats'].get(k) for m in same if k in m['floats'])
        typical[k] = vals.most_common(1)[0][0] if len(same) > 1 else None
    odd = [f'{k}={_fmt_num(v)}' for k, v in sorted(data['floats'].items()) if typical.get(k) != v]
    if odd:
        lines.append(f'  floats{" unlike other materials on this shader" if len(same) > 1 else ""}: ' + ', '.join(odd))
    lines.append(f'  ({len(data["floats"])} floats total; {len(same)} project materials use this shader)')
    return '\n'.join(lines)


# ---------------------------------------------------------------- dispatch for the CLI

def find(p, kind, name):
    q = name.lower()
    cands = [x for x, e in p.files.items() if e.get('fmt') and e['fmt'].get('kind') in kind]
    cands.sort(key=lambda x: (not x.startswith('Assets/'), os.path.splitext(os.path.basename(x))[0].lower() != q, len(x)))
    return next((x for x in cands if q in x.lower()), None)


def report(p, cmd, name):
    kinds = {'input': ('input',), 'ui': ('uxml', 'uss'), 'material': ('mat',)}[cmd]
    if cmd == 'input' and not name:
        found = [x for x, e in p.files.items() if e.get('fmt') and e['fmt'].get('kind') == 'input']
        if not found:
            return 'no .inputactions files'
        return '\n\n'.join(report_input(p, x, p.files[x]['fmt']) for x in sorted(found))
    path = find(p, kinds, name or '')
    if not path:
        return f'no {cmd} asset matches "{name}"'
    data = p.files[path]['fmt']
    if data.get('kind') == 'error':
        return f'{path}: could not parse ({data["error"]})'
    if data['kind'] == 'input':
        return report_input(p, path, data)
    if data['kind'] == 'uxml':
        return report_uxml(path, data)
    if data['kind'] == 'uss':
        return report_uss(path, data)
    return report_material(p, path, data)
