#!/usr/bin/env python3
"""unitygraph: a compact, queryable index of a Unity project, made for AI agents.

Reads scenes, prefabs, .meta files and C# sources straight from disk (no editor needed),
resolves GUIDs and fileIDs to names, and prints token-cheap summaries instead of raw YAML.
Every query refreshes the index incrementally, so results always match the files on disk.

    unitygraph build                    # (re)index, write .unitygraph/*.md
    unitygraph map                      # project overview
    unitygraph scene Level1 --depth 2 [--path "World/Props"] [--full]
    unitygraph prefab Player
    unitygraph class PlayerController   # fields, methods, uses, used by, where attached
    unitygraph where PlayerController   # where a script, component or asset is used
    unitygraph refs Enemy.prefab        # what an asset uses and what uses it
    unitygraph find "Door|Gate"         # GameObjects by name across scenes/prefabs
    unitygraph anim Player              # animator controller states and transitions
    unitygraph graph                    # .unitygraph/graph.html, an interactive graph
"""
import argparse
import json
import os
import pickle
import re
import sys
import time
from collections import Counter, defaultdict

__version__ = '0.2.2'
CACHE_VERSION = 6
OUT_DIR = '.unitygraph'
THIRD_PARTY = ['Assets/Packages/', 'Assets/Plugins/', 'Assets/ThirdParty/', 'Assets/Samples/',
               'Assets/TextMesh Pro/', 'Assets/AssetStoreTools/', 'Assets/_Recovery/']
YAML_DEP_EXTS = {'.unity', '.prefab', '.asset', '.mat', '.controller', '.overrideController', '.anim',
                 '.mask', '.mixer', '.playable', '.signal', '.preset', '.lighting', '.spriteatlas',
                 '.spriteatlasv2', '.terrainlayer', '.physicMaterial', '.physicsMaterial', '.renderTexture',
                 '.guiskin', '.fontsettings', '.flare', '.brush', '.cubemap'}
MODEL_ROOT_GO = '919132149155446097'  # ModelImporter's fixed fileID for a model's root GameObject
MODEL_ROOT_TR = '-8679921383154817045'  # ... and for its root Transform
MODEL_EXTS = {'.fbx', '.obj', '.blend', '.dae', '.3ds', '.max', '.ma', '.mb', '.gltf', '.glb'}
REF_KEYS = {'m_Controller': 'controller', 'm_audioClip': 'clip', 'm_Avatar': None}  # asset refs worth showing on built-ins
GUID_RE = re.compile(rb'guid: ([0-9a-f]{32})')
DEP_RE = re.compile(rb'(?:guid: |m_AssetGUID: |guid=)([0-9a-f]{32})')  # + AssetReference fields, UXML/USS urls
HDR_RE = re.compile(r'--- !u!(\d+) &(-?\d+)( stripped)?')
FULL_CLASSES = {'1', '4', '224', '1001', '114', '1660057539'}
TRIVIAL_MOD = re.compile(r'^m_(Local(Position|Rotation|Scale|EulerAnglesHint)|Name|RootOrder|'
                         r'ConstrainProportionsScale|AnchoredPosition|SizeDelta|AnchorM(in|ax)|Pivot)(\.|$)')
MIRROR_BASE_FIELDS = {'syncMode', 'syncDirection', 'syncInterval', 'syncMethod', 'serializedVersion',
                      'sceneId', '_assetId'}  # per-object ids, noise for reading; shown with --full


# ---------------------------------------------------------------- Unity YAML subset parser

def _closed(v):
    q, body = v[0], v[1:]
    if q == "'":
        return "'" in body.replace("''", '')
    k = 0
    while k < len(body):
        if body[k] == '\\':
            k += 2
            continue
        if body[k] == '"':
            return True
        k += 1
    return False


def _balanced(v):
    depth = 0
    for ch in v:
        if ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
    return depth <= 0


def _value_part(s):
    v = s[2:] if s.startswith('- ') else s
    j = v.find(': ')
    if j >= 0:
        return v[j + 2:].lstrip()
    return v if s.startswith('- ') else ''


def _toks(lines):
    out, k, n = [], 0, len(lines)
    while k < n:
        ln = lines[k]
        k += 1
        s = ln.lstrip(' ')
        if not s:
            continue
        ind = len(ln) - len(s)
        val = _value_part(s)
        if val and ((val[0] in '\'"' and not _closed(val)) or (val[0] in '{[' and not _balanced(val))):
            parts = [s]
            while k < n:
                nxt = lines[k].strip()
                k += 1
                parts.append(nxt)
                val += ' ' + nxt
                if (_closed(val) if val[0] in '\'"' else _balanced(val)):
                    break
            s = ' '.join(parts)
        out.append([ind, s])
    return out


def _kv(tx):
    j = tx.find(': ')
    if j >= 0:
        return tx[:j], tx[j + 2:].strip()
    if tx.endswith(':'):
        return tx[:-1], ''
    return tx, None


def _isitem(tx):
    return tx.startswith('- ') or tx == '-'


def _unquote(v):
    if v[0] == "'":
        return v[1:-1].replace("''", "'")
    try:
        return json.loads(v)
    except ValueError:
        return v[1:-1]


def _flow(s, i):
    n = len(s)
    while i < n and s[i] == ' ':
        i += 1
    if i >= n:
        return '', i
    c = s[i]
    if c == '{':
        d = {}
        i += 1
        while True:
            while i < n and s[i] in ' ,':
                i += 1
            if i >= n or s[i] == '}':
                return d, i + 1
            j = s.index(':', i)
            key = s[i:j].strip()
            v, i = _flow(s, j + 1)
            d[key] = v
    if c == '[':
        lst = []
        i += 1
        while True:
            while i < n and s[i] in ' ,':
                i += 1
            if i >= n or s[i] == ']':
                return lst, i + 1
            v, i = _flow(s, i)
            lst.append(v)
    if c in '\'"':
        j = i + 1
        while j < n:
            if s[j] == '\\' and c == '"':
                j += 2
                continue
            if s[j] == c:
                if c == "'" and j + 1 < n and s[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        return _unquote(s[i:j + 1]), j + 1
    j = i
    while j < n and s[j] not in ',}]':
        j += 1
    return s[i:j].strip(), j


def _scalar(v):
    if not v:
        return ''
    if v[0] in '{[':
        try:
            return _flow(v, 0)[0]
        except (ValueError, IndexError):
            return v
    if v[0] in '\'"' and len(v) > 1:
        return _unquote(v)
    return v


class _Yaml:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def block(self, ind):
        return self.seq(ind) if _isitem(self.t[self.i][1]) else self.map(ind)

    def map(self, ind):
        d, t = {}, self.t
        while self.i < len(t):
            ci, tx = t[self.i]
            if ci < ind or (ci == ind and _isitem(tx)):
                break
            self.i += 1
            if ci > ind:
                continue
            k, v = _kv(tx)
            if v is None:
                continue
            d[k] = self.child(ind) if v == '' else self.plain(v, ind)
        return d

    def child(self, ind):
        t = self.t
        if self.i < len(t):
            ni, nt = t[self.i]
            if ni > ind:
                return self.block(ni)
            if ni == ind and _isitem(nt):
                return self.seq(ind)
        return ''

    def plain(self, v, ind):
        t = self.t
        if v[0] not in '{[\'"':
            while self.i < len(t) and t[self.i][0] > ind:
                v += ' ' + t[self.i][1]
                self.i += 1
        return _scalar(v)

    def seq(self, ind):
        lst, t = [], self.t
        while self.i < len(t):
            ci, tx = t[self.i]
            if ci != ind or not _isitem(tx):
                break
            rest = tx[2:].lstrip() if len(tx) > 1 else ''
            if rest == '':
                # "- " alone: a nested block if the next line is deeper, otherwise an empty item
                self.i += 1
                deeper = self.i < len(t) and t[self.i][0] > ind
                lst.append(self.block(t[self.i][0]) if deeper else '')
            elif rest[0] in '{[\'"' or _kv(rest)[1] is None:
                self.i += 1
                lst.append(self.plain(rest, ind))
            else:
                t[self.i] = [ind + 2, rest]
                lst.append(self.map(ind + 2))
        return lst


def parse_yaml_block(lines):
    toks = _toks(lines)
    return _Yaml(toks).block(toks[0][0]) if toks else {}


def iter_docs(text):
    """Yields (classID, fileID, stripped, typeName, bodyLines) for each document of a Unity YAML file."""
    lines = text.split('\n')
    starts = [k for k, ln in enumerate(lines) if ln.startswith('--- !u!')]
    starts.append(len(lines))
    for a, b in zip(starts, starts[1:]):
        m = HDR_RE.match(lines[a])
        if not m or a + 1 >= len(lines):
            continue
        yield m.group(1), m.group(2), bool(m.group(3)), lines[a + 1].strip().rstrip(':'), lines[a + 2:b]


# ---------------------------------------------------------------- scenes and prefabs

def _fid(ref):
    return ref.get('fileID', '0') if isinstance(ref, dict) else '0'


def parse_unity_file(text):
    """Turns a .unity/.prefab/.asset file into a name-free object model (cacheable per file)."""
    fm = {'gos': {}, 'trs': {}, 'st': {}, 'comps': {}, 'pis': {}, 'roots': None, 'main': []}
    for cls, fid, stripped, typ, body in iter_docs(text):
        if stripped:
            d = parse_yaml_block(body)
            fm['st'][fid] = {'pi': _fid(d.get('m_PrefabInstance')), 'src': _fid(d.get('m_CorrespondingSourceObject')),
                             'type': typ}
            continue
        if cls not in FULL_CLASSES:
            go, en, refs = '0', '1', {}
            for k, ln in enumerate(body):
                if ln.startswith('  m_GameObject: '):
                    go = ln[ln.find('fileID: ') + 8:].rstrip('}').strip()
                elif ln.startswith('  m_Enabled: '):
                    en = ln[13:].strip()
                elif ln.startswith('  m_') and ln[2:ln.find(':')] in REF_KEYS and REF_KEYS[ln[2:ln.find(':')]]:
                    ref = _scalar(ln[ln.find(':') + 1:].strip())
                    if isinstance(ref, dict) and ref.get('guid'):
                        refs[REF_KEYS[ln[2:ln.find(':')]]] = ref
                elif k > 12 and not refs and typ not in ('Animator', 'AudioSource'):
                    break
            if go != '0':
                fm['comps'][fid] = {'type': typ, 'go': go, 'en': en, 'refs': refs}
            else:
                fm['main'].append((fid, typ))
            continue
        d = parse_yaml_block(body)
        if not isinstance(d, dict):
            continue
        if cls == '1':
            fm['gos'][fid] = {'name': str(d.get('m_Name', '')), 'active': d.get('m_IsActive', '1'),
                              'tag': d.get('m_TagString', 'Untagged'),
                              'comps': [_fid(c.get('component')) for c in d.get('m_Component') or []
                                        if isinstance(c, dict)]}
        elif cls in ('4', '224'):
            fm['trs'][fid] = {'go': _fid(d.get('m_GameObject')), 'father': _fid(d.get('m_Father')),
                              'children': [_fid(c) for c in d.get('m_Children') or []]}
        elif cls == '1001':
            mod = d.get('m_Modification') or {}
            mods = []
            for m in mod.get('m_Modifications') or []:
                if isinstance(m, dict):
                    tgt = m.get('target') or {}
                    mods.append((_fid(tgt), str(m.get('propertyPath', '')), m.get('value', ''),
                                 m.get('objectReference') or {}))
            src = d.get('m_SourcePrefab') or {}
            fm['pis'][fid] = {'parent': _fid(mod.get('m_TransformParent')), 'src': src.get('guid', ''),
                              'mods': mods, 'removed': len(mod.get('m_RemovedComponents') or []),
                              'removed_go': len(mod.get('m_RemovedGameObjects') or [])}
        elif cls == '114':
            go = _fid(d.get('m_GameObject'))
            script = d.get('m_Script') or {}
            fields = {k: v for k, v in d.items() if not k.startswith('m_')}
            entry = {'type': 'MonoBehaviour', 'go': go, 'en': d.get('m_Enabled', '1'),
                     'script': script.get('guid', '') if isinstance(script, dict) else '',
                     'ident': str(d.get('m_EditorClassIdentifier', '')), 'fields': fields,
                     'name': str(d.get('m_Name', ''))}
            if go != '0':
                fm['comps'][fid] = entry
            else:
                fm['main'].append((fid, entry))
        elif cls == '1660057539':
            fm['roots'] = [_fid(r) for r in d.get('m_Roots') or []]
    return fm


def parse_controller(text):
    """AnimatorController / AnimatorOverrideController → params, layers, state machines, states, transitions, trees."""
    c = {'params': [], 'layers': [], 'sms': {}, 'states': {}, 'trans': {}, 'trees': {}, 'behaviours': {},
         'override': None}
    for cls, fid, stripped, typ, body in iter_docs(text):
        d = parse_yaml_block(body)
        if not isinstance(d, dict):
            continue
        if typ == 'AnimatorController':
            c['name'] = d.get('m_Name', '')
            for prm in d.get('m_AnimatorParameters') or []:
                c['params'].append((prm.get('m_Name', ''), prm.get('m_Type', ''), prm.get('m_DefaultFloat', '0'),
                                    prm.get('m_DefaultInt', '0'), prm.get('m_DefaultBool', '0')))
            for lay in d.get('m_AnimatorLayers') or []:
                c['layers'].append((lay.get('m_Name', ''), _fid(lay.get('m_StateMachine')),
                                    _fid(lay.get('m_Mask')), lay.get('m_BlendingMode', '0'),
                                    lay.get('m_SyncedLayerIndex', '-1')))
        elif typ == 'AnimatorOverrideController':
            c['name'] = d.get('m_Name', '')
            c['override'] = d.get('m_Controller') or {}
            c['clips'] = [(x.get('m_OriginalClip') or {}, x.get('m_OverrideClip') or {}) for x in d.get('m_Clips') or []]
        elif typ == 'AnimatorStateMachine':
            c['sms'][fid] = {'name': d.get('m_Name', ''),
                             'states': [_fid(s.get('m_State')) for s in d.get('m_ChildStates') or []],
                             'subs': [_fid(s.get('m_StateMachine')) for s in d.get('m_ChildStateMachines') or []],
                             'default': _fid(d.get('m_DefaultState')),
                             'any': [_fid(t) for t in d.get('m_AnyStateTransitions') or []],
                             'beh': [_fid(b) for b in d.get('m_StateMachineBehaviours') or []]}
        elif typ == 'AnimatorState':
            c['states'][fid] = {'name': d.get('m_Name', ''), 'motion': d.get('m_Motion') or {},
                                'trans': [_fid(t) for t in d.get('m_Transitions') or []],
                                'speed': d.get('m_Speed', '1'), 'speedp': d.get('m_SpeedParameterActive', '0') == '1'
                                and d.get('m_SpeedParameter', ''), 'tag': d.get('m_Tag', ''),
                                'beh': [_fid(b) for b in d.get('m_StateMachineBehaviours') or []]}
        elif typ in ('AnimatorStateTransition', 'AnimatorTransition'):
            c['trans'][fid] = {'dst': _fid(d.get('m_DstState')), 'dst_sm': _fid(d.get('m_DstStateMachine')),
                               'exit': d.get('m_IsExit', '0') == '1',
                               'conds': [(x.get('m_ConditionMode', ''), x.get('m_ConditionEvent', ''),
                                          x.get('m_EventTreshold', '0')) for x in d.get('m_Conditions') or []],
                               'has_exit': d.get('m_HasExitTime', '0') == '1', 'exit_time': d.get('m_ExitTime', '')}
        elif typ == 'BlendTree':
            c['trees'][fid] = {'name': d.get('m_Name', ''), 'type': d.get('m_BlendType', '0'),
                               'param': d.get('m_BlendParameter', ''), 'paramY': d.get('m_BlendParameterY', ''),
                               'childs': [(x.get('m_Motion') or {}, x.get('m_Threshold', '0'), x.get('m_Position') or {})
                                          for x in d.get('m_Childs') or []]}
        elif typ == 'MonoBehaviour':
            script = d.get('m_Script') or {}
            c['behaviours'][fid] = {'script': script.get('guid', '') if isinstance(script, dict) else '',
                                    'ident': str(d.get('m_EditorClassIdentifier', '')),
                                    'fields': {k: v for k, v in d.items() if not k.startswith('m_')}}
    return c


# ---------------------------------------------------------------- C# sources

_CS_NOISE = re.compile(r'//[^\n]*|/\*.*?\*/|@"(?:""|[^"])*"|\$?@?"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])+\'', re.S)
_NS_RE = re.compile(r'\bnamespace\s+([\w.]+)\s*([{;])')
_TYPE_RE = re.compile(
    r'((?:\[[^\[\]]*(?:\[[^\]]*\][^\[\]]*)*\]\s*)*)'
    r'((?:(?:public|internal|private|protected|static|abstract|sealed|partial|readonly|unsafe|new|ref)\s+)*)'
    r'(class|struct|enum|interface|record(?:\s+struct|\s+class)?)\s+(\w+)\s*(<[^>{;]*>)?\s*(?:\([^)]*\))?\s*'
    r'(?::\s*([^{;]+?))?\s*(?:where\s[^{;]*)?\{')
_DOC_RE = re.compile(r'((?:[ \t]*///[^\n]*\n)+)\s*(?:\[[^\n]*\]\s*)*(?:\w+\s+)*'
                     r'(?:class|struct|enum|interface|record)\s+(\w+)')
_ATTR_PREFIX = re.compile(r'^\s*\[((?:[^\[\]]|\[[^\]]*\])*)\]')
_MODS = {'public', 'private', 'protected', 'internal', 'static', 'readonly', 'const', 'virtual', 'override',
         'abstract', 'sealed', 'new', 'extern', 'unsafe', 'volatile', 'async', 'event', 'partial', 'ref'}
UNITY_MESSAGES = {'Awake', 'Start', 'Update', 'FixedUpdate', 'LateUpdate', 'OnEnable', 'OnDisable', 'OnDestroy',
                  'OnValidate', 'Reset', 'OnGUI', 'OnTriggerEnter', 'OnTriggerExit', 'OnTriggerStay',
                  'OnCollisionEnter', 'OnCollisionExit', 'OnCollisionStay', 'OnDrawGizmos',
                  'OnDrawGizmosSelected', 'OnApplicationQuit', 'OnApplicationFocus', 'OnApplicationPause',
                  'OnAnimatorMove', 'OnBecameVisible', 'OnBecameInvisible'}
RPC_ATTRS = {'Command', 'ClientRpc', 'TargetRpc', 'Server', 'Client', 'ServerCallback', 'ClientCallback',
             'ContextMenu', 'MenuItem', 'RuntimeInitializeOnLoadMethod'}


def _match_brace(s, i):
    depth = 0
    for k in range(i, len(s)):
        if s[k] == '{':
            depth += 1
        elif s[k] == '}':
            depth -= 1
            if depth == 0:
                return k
    return len(s) - 1


def _flatten(body):
    out, depth = [], 0
    for ch in body:
        if ch == '{':
            if depth == 0:
                out.append('{')
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                out.append('}')
        elif depth == 0:
            out.append(ch)
    return ''.join(out)


def _summary(xml):
    txt = re.sub(r'^\s*///\s?', '', xml, flags=re.M)
    m = re.search(r'<summary>(.*?)</summary>', txt, re.S)
    txt = m.group(1) if m else txt
    txt = re.sub(r'<see\s+cref="([^"]+)"\s*/>', r'\1', txt)
    txt = re.sub(r'<[^>]+>', '', txt)
    txt = ' '.join(txt.split())
    m = re.match(r'(.+?[.!?])(\s|$)', txt)
    return (m.group(1) if m else txt)[:160]


def _split_attrs(seg):
    attrs = []
    while True:
        m = _ATTR_PREFIX.match(seg)
        if not m:
            return attrs, seg.strip()
        attrs += re.findall(r'(?:^|,)\s*([\w.]+)', m.group(1))
        seg = seg[m.end():]


def _norm_decl(decl):
    return re.sub(r'\s*([<>,\[\]?])\s*', r'\1', decl.strip())


def _members(body, type_name):
    flat = _flatten(body)
    fields, props, methods = [], [], []
    for seg, delim in re.findall(r'([^;{}]*)(;|\{\}|\{|\})', flat):
        attrs, seg = _split_attrs(seg)
        if not seg or seg[0] in '=:' or re.search(r'\b(class|struct|enum|interface|record|delegate)\b', seg):
            continue
        paren, arrow = seg.find('('), seg.find('=>')
        eq = next((m.start() for m in re.finditer(r'(?<![=!<>])=(?![=>])', seg)), -1)
        if eq >= 0 and (paren < 0 or eq < paren) and (arrow < 0 or eq < arrow):
            kind, decl = 'field', seg[:eq]
        elif arrow >= 0 and (paren < 0 or arrow < paren):
            kind, decl = 'prop', seg[:arrow]
        elif paren >= 0:
            kind, decl = 'method', seg[:paren]
        else:
            kind, decl = ('prop' if delim.startswith('{') else 'field'), seg
        toks = _norm_decl(decl).split()
        if not toks:
            continue
        mods = [t for t in toks if t in _MODS]
        rest = [t for t in toks if t not in _MODS]
        vis = next((m for m in ('public', 'protected', 'internal', 'private') if m in mods), 'private')
        static = 'static' in mods or 'const' in mods
        if kind == 'method':
            if not rest:
                continue
            name = re.sub(r'<.*', '', rest[-1])
            if name in ('operator', 'this') or 'operator' in rest:
                continue
            close = seg.find(')', paren)
            params = ' '.join(seg[paren + 1:close if close > 0 else None].split())
            params = ', '.join(p.split()[-2] if len(p.split()) >= 2 else p
                               for p in re.split(r',(?![^<]*>)', _norm_decl(params).replace(',', ', ')) if p.strip())
            ret = rest[-2] if len(rest) >= 2 else ('ctor' if name == type_name else '')
            methods.append({'n': name, 'ret': ret, 'params': params, 'vis': vis, 'static': static,
                            'attrs': attrs, 'override': 'override' in mods})
            continue
        if len(rest) < 2:
            continue
        typ = rest[-2]
        for name in rest[-1].split(','):
            name = name.strip()
            if not re.match(r'^\w+$', name):
                continue
            if kind == 'prop' or 'event' in mods:
                props.append({'n': name, 't': typ, 'vis': vis, 'static': static, 'event': 'event' in mods})
            else:
                ser = (not static and 'readonly' not in mods and 'NonSerialized' not in attrs and
                       (vis == 'public' or 'SerializeField' in attrs or 'SerializeReference' in attrs))
                fields.append({'n': name, 't': typ, 'vis': vis, 'static': static, 'attrs': attrs, 'ser': ser})
    return fields, props, methods


def _enum_values(body):
    vals, nxt = [], 0
    for part in body.split(','):
        _, part = _split_attrs(part)
        if not part:
            continue
        name, _, expr = part.partition('=')
        name, expr = name.strip(), expr.strip()
        if not re.match(r'^\w+$', name):
            continue
        val = nxt
        if expr:
            known = {n: v for n, v in vals}
            try:
                val = eval(re.sub(r'\b[A-Za-z_]\w*\b', lambda m: str(known.get(m.group(0), 0)), expr),
                           {'__builtins__': {}})
            except Exception:
                val = None
        vals.append((name, val))
        nxt = (val + 1) if isinstance(val, int) else nxt + 1
    return vals


def _blank(m):
    """Blanks a comment or literal's content but keeps its length, so offsets and line numbers stay valid."""
    s = m.group(0)
    if s[0] == '/':
        return re.sub(r'[^\n]', ' ', s)
    q = s.index(s[-1]) + 1
    return s[:q] + re.sub(r'[^\n]', ' ', s[q:-1]) + s[-1]


_SHADER_CALL = re.compile(r'Shader\.Find\s*\(\s*$')
_RES_CALL = re.compile(r'Resources\.Load(?:All|Async)?\s*(?:<[^>]*>)?\s*\(\s*$')
_ADDR_CALL = re.compile(r'Addressables\.(?:Load\w*|Instantiate\w*|DownloadDependencies\w*)\s*(?:<[^>]*>)?\s*\(\s*$')


def _asset_strings(src):
    """(offset, kind, value) for string literals that name assets: paths, Resources.Load keys, shader names."""
    out = []
    for m in _CS_NOISE.finditer(src):
        s = m.group(0)
        if s[0] == '/' or s[-1] != '"':
            continue
        q = s.index('"') + 1
        body = s[q:-1]
        if '$' in s[:q]:
            body = body.split('{')[0]  # interpolated: keep the constant prefix
        before = src[max(0, m.start() - 60):m.start()]
        if _SHADER_CALL.search(before):
            kind = 'shader'
        elif _RES_CALL.search(before):
            kind = 'res'
        elif _ADDR_CALL.search(before):
            kind = 'addr'
        elif body.startswith(('Assets/', 'Packages/')):
            kind = 'path'
        else:
            continue
        if body:
            out.append((m.start(), kind, body.replace('\\\\', '/')))
    return out


_CS_KEYWORDS = {'if', 'for', 'foreach', 'while', 'switch', 'catch', 'using', 'lock', 'return', 'new', 'typeof',
                'nameof', 'sizeof', 'default', 'base', 'this', 'when', 'fixed', 'checked', 'unchecked', 'stackalloc',
                'get', 'set', 'add', 'remove', 'is', 'as', 'await', 'throw', 'else', 'do', 'in', 'out', 'ref', 'var'}
_METHOD_HEAD = re.compile(r'(\w+)\s*(?:<[^<>{};]*>)?\s*\(((?:[^()]|\([^()]*\))*)\)\s*'
                          r'(?:where\s[^{;]*)?(?::\s*(?:base|this)\s*\((?:[^()]|\([^()]*\))*\))?\s*(?:=>)?\s*$')
_CALL = re.compile(r'(?:\b(\w+)\s*(?:\?\.|\.)\s*)?\b([A-Za-z_]\w*)\s*(?:<[^<>;(){}=]*>)?\s*\(')
_GETCOMP_CALL = re.compile(r'\bGetComponent(?:InChildren|InParent)?\s*<\s*(?:\w+\.)*(\w+)\s*>\s*\(\s*\)\s*\??\.\s*(\w+)')
_NAME_REF = re.compile(r'\bnameof\s*\(\s*(\w+)\s*\)|[+-]=\s*(?:\w+\s*\.\s*)?(\w+)\s*;')
_READ = re.compile(r'(?=\b(\w+)\s*(?:\?\.|\.)\s*([A-Z]\w*)\b(?!\s*[(<\w]))')  # lookahead: a.B.C yields a.B and B.C
_LOCAL = re.compile(r'\b([A-Z]\w*)(?:<[^<>;(){}]*>)?(?:\[\])?\??\s+([a-z_]\w*)\s*(?==|;|,|\)|\bin\b)')
_VAR_NEW = re.compile(r'\bvar\s+(\w+)\s*=\s*(?:new\s+(\w+)|(?:\w+\s*\.\s*)?GetComponent\w*\s*<\s*(\w+)\s*>|'
                      r'\(\s*([A-Z]\w*)\s*\))')


def _params(sig):
    """{name: type} from a parameter list."""
    out = {}
    for part in re.split(r',(?![^<]*>)', sig):
        part = re.sub(r'=.*', '', part)
        toks = [x for x in _norm_decl(part).split() if x not in ('this', 'ref', 'out', 'in', 'params', 'readonly')]
        _, toks = _split_attrs(' '.join(toks))
        toks = toks.split()
        if len(toks) >= 2 and re.match(r'^\w+$', toks[-1]):
            out[toks[-1]] = re.sub(r'<.*|\[\]|\?', '', toks[-2])
    return out


def _calls_in(body, params=None):
    """Call sites in a method body ('Qualifier.Name' or 'Name'), capitalized member reads, names referenced
    via nameof/+=, and the types of parameters and locals it declares (to resolve qualifiers later)."""
    calls = set()
    for q, name in _GETCOMP_CALL.findall(body):
        calls.add(f'{q}.{name}')
    for q, name in _CALL.findall(body):
        if name in _CS_KEYWORDS or (q and q in ('new',)):
            continue
        calls.add(f'{q}.{name}' if q and q not in ('this', 'base') else name)
    reads = {f'{q}.{n}' if q not in ('this', 'base') else n for q, n in _READ.findall(body)} - calls
    refs = {a or b for a, b in _NAME_REF.findall(body)}
    local = dict(params or {})
    for t, n in _LOCAL.findall(body):
        if t not in ('return', 'new', 'var'):
            local.setdefault(n, t)
    for m in _VAR_NEW.finditer(body):
        local.setdefault(m.group(1), next(x for x in m.groups()[1:] if x))
    return {'calls': sorted(calls), 'reads': sorted(reads), 'refs': sorted(r for r in refs if r and r not in _CS_KEYWORDS),
            'locals': local}


def _bodies(code, open_i, close_i):
    """Methods, constructors and property accessors directly inside a type body, with their call sites."""
    out, i, seg = [], open_i + 1, open_i + 1
    while i < close_i:
        ch = code[i]
        if ch == '{':
            end = _match_brace(code, i)
            head = code[seg:i]
            if not re.search(r'\b(class|struct|enum|interface|record|namespace)\b', head):
                m = _METHOD_HEAD.search(head)
                name = m.group(1) if m else (re.findall(r'(\w+)\s*$', head) or [''])[0]  # property accessors
                if name and name not in _CS_KEYWORDS:
                    out.append(dict(_calls_in(code[i + 1:end], _params(m.group(2)) if m else None), n=name,
                                    line=code.count('\n', 0, seg + len(head) - len(head.lstrip())) + 1))
            i = seg = end + 1
            continue
        if ch == ';':
            head = code[seg:i]
            arrow = head.find('=>')
            if arrow > 0:  # expression-bodied member
                m = _METHOD_HEAD.search(head[:arrow + 2])
                name = m.group(1) if m else (re.findall(r'(\w+)\s*$', head[:arrow]) or [''])[0]
                if name and name not in _CS_KEYWORDS and not re.search(r'(?<![=!<>])=(?![=>])', head[:arrow]):
                    out.append(dict(_calls_in(head[arrow + 2:], _params(m.group(2)) if m else None), n=name,
                                    line=code.count('\n', 0, seg + len(head) - len(head.lstrip())) + 1))
            seg = i + 1
        i += 1
    return out


def parse_cs(src):
    docs = {m.group(2): _summary(m.group(1)) for m in _DOC_RE.finditer(src)}
    code = _CS_NOISE.sub(_blank, src)
    ns_spans = []
    for m in _NS_RE.finditer(code):
        end = _match_brace(code, m.end() - 1) if m.group(2) == '{' else len(code)
        ns_spans.append((m.start(), end, m.group(1)))
    types = []
    for m in _TYPE_RE.finditer(code):
        open_i = m.end() - 1
        close_i = _match_brace(code, open_i)
        name, kind = m.group(4), m.group(3).split()[0]
        ns = '.'.join(s[2] for s in ns_spans if s[0] <= m.start() <= s[1])
        attrs, _ = _split_attrs(m.group(1) or '')
        bases = [b.strip() for b in re.split(r',(?![^<]*>)', m.group(6) or '') if b.strip()]
        body = code[open_i + 1:close_i]
        t = {'name': name, 'ns': ns, 'kind': kind, 'bases': bases, 'attrs': attrs,
             'line': code.count('\n', 0, m.start(4)) + 1, 'summary': docs.get(name, ''),
             'span': (m.start(), close_i), 'abstract': 'abstract' in (m.group(2) or ''),
             'fields': [], 'props': [], 'methods': [], 'enum': []}
        if kind == 'enum':
            t['enum'] = _enum_values(body)
        else:
            t['fields'], t['props'], t['methods'] = _members(body, name)
            t['bodies'] = _bodies(code, open_i, close_i)
            # nameof() in member declarations, e.g. [SyncVar(hook = nameof(OnHolderChanged))]
            decl = _flatten(body)
            if 'nameof' in decl:
                t['bodies'].append({'n': '(declarations)', 'line': t['line'], 'calls': [], 'reads': [],
                                    'refs': sorted(set(re.findall(r'\bnameof\s*\(\s*(\w+)\s*\)', decl))), 'locals': {}})
        types.append(t)
    for t in types:
        outer = [o for o in types if o is not t and o['span'][0] < t['span'][0] and t['span'][1] <= o['span'][1]]
        t['outer'] = max(outer, key=lambda o: o['span'][0])['name'] if outer else ''
        t['idents'] = set(re.findall(r'\b[A-Z]\w*\b', code[t['span'][0]:t['span'][1] + 1]))
        t['strs'] = []
    for pos, kind, val in _asset_strings(src):
        owners = [t for t in types if t['span'][0] <= pos <= t['span'][1]]
        if owners:
            owner = max(owners, key=lambda t: t['span'][0])
            if (kind, val) not in owner['strs']:
                owner['strs'].append((kind, val))
    return types


# ---------------------------------------------------------------- project scan and cache

def find_root(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, 'Assets')) and os.path.isdir(os.path.join(d, 'ProjectSettings')):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            sys.exit('unitygraph: not inside a Unity project (no Assets/ and ProjectSettings/)')
        d = parent


def walk(root, rel):
    stack = [os.path.join(root, rel)]
    cut = len(root) + 1
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    if not (e.name.startswith('.') or e.name.endswith('~')):
                        stack.append(e.path)
                else:
                    st = e.stat()
                    yield e.path[cut:].replace('\\', '/'), st.st_mtime_ns, st.st_size


def display_path(p):
    m = re.match(r'Library/PackageCache/([^@/]+)(?:@[^/]*)?/(.*)', p)
    return f'pkg:{m.group(1)}/{m.group(2)}' if m else p


def is_third_party(p, extra):
    return p.startswith('pkg:') or p.startswith('Library/') or p.startswith('Packages/') or \
        any(p.startswith(x) for x in THIRD_PARTY + extra)


def stem(p):
    return os.path.splitext(os.path.basename(p))[0]


class Project:
    def __init__(self, root, verbose=False):
        self.root = root
        self.out = os.path.join(root, OUT_DIR)
        self.verbose = verbose
        cfg_path = os.path.join(root, '.unitygraph.json')
        self.cfg = json.load(open(cfg_path, encoding='utf-8')) if os.path.exists(cfg_path) else {}
        self.extra_tp = self.cfg.get('third_party', [])
        self.cache_path = os.path.join(self.out, 'cache.pkl')
        # parsed data depends on the parser's code: a changed core.py (new release, local edit) reparses everything
        st = os.stat(__file__)
        key = f'{CACHE_VERSION}:{__version__}:{st.st_size}:{st.st_mtime_ns}'
        self.cache = {'v': key, 'meta': {}, 'pkgmeta': {}, 'pkgkey': None, 'files': {}, 'cs': {}}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'rb') as f:
                    c = pickle.load(f)
                if c.get('v') == key:
                    self.cache = c
            except Exception:
                pass
        self.dirty = False
        self.changed = False

    def log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr)

    # ---- refresh

    def refresh(self, force_packages=False):
        t0 = time.time()
        tool_mt = os.path.getmtime(__file__)
        if self.cache.get('tool_mt') != tool_mt:  # rendering changed: rewrite the summaries
            self.cache['tool_mt'] = tool_mt
            self.dirty = self.changed = True
        files = list(walk(self.root, 'Assets')) + list(walk(self.root, 'Packages'))
        self._refresh_meta(files)
        self._refresh_packages(force_packages)
        self._refresh_assets(files)
        self._refresh_cs(files)
        if self.dirty:
            os.makedirs(self.out, exist_ok=True)
            with open(self.cache_path, 'wb') as f:
                pickle.dump(self.cache, f, protocol=pickle.HIGHEST_PROTOCOL)
        self.log(f'refresh {time.time() - t0:.2f}s, changed={self.changed}')
        self._link()

    def _read_guid(self, p):
        with open(os.path.join(self.root, p), 'rb') as f:
            m = GUID_RE.search(f.read(400))
        return m.group(1).decode() if m else None

    def _refresh_meta(self, files):
        old, new = self.cache['meta'], {}
        for p, mt, _ in files:
            if p.endswith('.meta'):
                hit = old.get(p)
                new[p] = hit if hit and hit[0] == mt else (mt, self._read_guid(p))
                if new[p] is not hit:
                    self.changed = True
        if set(old) != set(new):
            self.changed = True
        if self.changed:
            self.dirty = True
        self.cache['meta'] = new
        # animation clips inside models: the importer records each clip's name and fileID in the .meta
        old_clips, clips = self.cache.setdefault('clips', {}), {}
        for p, (mt, g) in new.items():
            if os.path.splitext(p[:-5])[1].lower() not in MODEL_EXTS:
                continue
            hit = old_clips.get(p)
            if hit and hit[0] == mt:
                clips[p] = hit
                continue
            table, name = {}, None
            with open(os.path.join(self.root, p), encoding='utf-8', errors='replace') as f:
                for ln in f:
                    m = re.match(r'^ {6}(name|internalID): (.*)$', ln.rstrip('\r\n'))
                    if m and m.group(1) == 'name':
                        name = m.group(2).strip()
                    elif m and name:
                        table[m.group(2).strip()] = name.split('|')[-1]
                        name = None
            clips[p] = (mt, g, table)
            self.dirty = self.changed = True
        self.cache['clips'] = clips

    def _refresh_packages(self, force):
        lock = os.path.join(self.root, 'Packages', 'packages-lock.json')
        key = os.path.getmtime(lock) if os.path.exists(lock) else 0
        if not force and self.cache['pkgkey'] == key:
            return
        new = {}
        for p, mt, _ in walk(self.root, 'Library/PackageCache'):
            if p.endswith('.meta'):
                new[p] = self._read_guid(p)
        self.cache['pkgmeta'], self.cache['pkgkey'] = new, key
        self.dirty = self.changed = True

    def _refresh_assets(self, files):
        from unitygraph import formats
        old, new = self.cache['files'], {}
        for p, mt, size in files:
            ext = os.path.splitext(p)[1]
            if ext not in YAML_DEP_EXTS and ext not in formats.PARSED_EXTS:
                continue
            hit = old.get(p)
            if hit and hit['mt'] == mt and hit['size'] == size:
                new[p] = hit
                continue
            self.log('parse', p)
            with open(os.path.join(self.root, p), 'rb') as f:
                raw = f.read()
            entry = {'mt': mt, 'size': size, 'deps': sorted({g.decode() for g in DEP_RE.findall(raw)}), 'fm': None,
                     'fmt': formats.parse(p, raw) if ext in formats.PARSED_EXTS or ext == '.asset' else None}
            if raw.startswith(b'%YAML'):
                want = ext == '.prefab' or (ext == '.unity' and not is_third_party(p, self.extra_tp)) or \
                    (ext == '.asset' and b'\nMonoBehaviour:' in raw[:600])
                if want:
                    entry['fm'] = parse_unity_file(raw.decode('utf-8', 'replace').replace('\r\n', '\n'))
                elif ext in ('.controller', '.overrideController'):
                    entry['ctrl'] = parse_controller(raw.decode('utf-8', 'replace').replace('\r\n', '\n'))
            new[p] = entry
            self.dirty = self.changed = True
        if set(old) != set(new):
            self.dirty = self.changed = True
        self.cache['files'] = new

    def _refresh_cs(self, files):
        old_sh, shaders = self.cache.setdefault('shaders', {}), {}
        for p, mt, size in files:
            if p.endswith('.shader'):
                hit = old_sh.get(p)
                if hit and hit[0] == mt:
                    shaders[p] = hit
                    continue
                with open(os.path.join(self.root, p), encoding='utf-8', errors='replace') as f:
                    m = re.search(r'Shader\s+"([^"]+)"', f.read(4000))
                shaders[p] = (mt, m.group(1) if m else None)
            elif p.endswith('.shadergraph'):
                shaders[p] = (mt, 'Shader Graphs/' + stem(p))
        if shaders != old_sh:
            self.cache['shaders'] = shaders
            self.dirty = self.changed = True
        old, new = self.cache['cs'], {}
        for p, mt, size in files:
            if not p.endswith('.cs'):
                continue
            hit = old.get(p)
            if hit and hit['mt'] == mt:
                new[p] = hit
                continue
            with open(os.path.join(self.root, p), encoding='utf-8-sig', errors='replace') as f:
                src = f.read()
            new[p] = {'mt': mt, 'types': parse_cs(src.replace('\r\n', '\n'))}
            self.dirty = self.changed = True
        if set(old) != set(new):
            self.dirty = self.changed = True
        self.cache['cs'] = new

    # ---- link everything into one graph

    def _link(self):
        self.guid2path = {g: display_path(p[:-5]) for p, g in self.cache['pkgmeta'].items() if g}
        self.guid2path.update({g: p[:-5] for p, (_, g) in self.cache['meta'].items() if g})
        self.path2guid = {p: g for g, p in self.guid2path.items()}
        self.clip_names = {g: table for _, g, table in self.cache.get('clips', {}).values() if table}
        self.classes, self.by_name, self.script_cls = {}, defaultdict(list), {}
        for p, e in sorted(self.cache['cs'].items()):
            tp = is_third_party(p, self.extra_tp)
            for t in e['types']:
                full = '.'.join(x for x in (t['ns'], t['outer'], t['name']) if x)
                prev = self.classes.get(full)
                if prev and prev['file'] != p:  # partial class split over files: merge the parts
                    if stem(p) == t['name'] and stem(prev['file']) != t['name']:
                        # the file named after the class is the main part (where Unity looks for it)
                        prev['file'], prev['line'] = p, t['line']
                        prev['files'].insert(0, p)
                        for k in ('fields', 'props', 'methods', 'strs', 'bodies'):
                            prev[k] = [dict(x, file=p) if k == 'bodies' else x for x in t.get(k, [])] + \
                                [x for x in prev.get(k, []) if x not in t.get(k, [])]
                    else:
                        prev['files'].append(p)
                        for k in ('fields', 'props', 'methods', 'strs', 'bodies'):
                            prev[k] = prev.get(k, []) + [dict(x, file=p) if k == 'bodies' else x
                                                         for x in t.get(k, []) if x not in prev.get(k, [])]
                    for k in ('bases', 'attrs'):
                        prev[k] = prev[k] + [x for x in t[k] if x not in prev[k]]
                    prev['idents'] = prev['idents'] | t['idents']
                    prev['summary'] = prev['summary'] or t['summary']
                    continue
                t = dict(t, full=full, file=p, files=[p], project=not tp,
                         bodies=[dict(b, file=p) for b in t.get('bodies', [])])
                self.classes[full] = t
                self.by_name[t['name']].append(full)
            guid = self.path2guid.get(p)
            if guid:
                main = next((t for t in e['types'] if t['name'] == stem(p) and not t['outer']), None)
                if main:
                    self.script_cls[guid] = '.'.join(x for x in (main['ns'], main['name']) if x)
        self.files = self.cache['files']
        self.rdeps = defaultdict(set)
        for p, e in self.files.items():
            for g in e['deps']:
                self.rdeps[g].add(p)
        self._addresses = None
        self._link_code_strings()
        self._root_cache, self._tree_cache = {}, {}
        self._derived = defaultdict(list)
        for full, t in self.classes.items():
            for b in t['bases']:
                bc = self.find_class(b, t['ns'])
                if bc:
                    self._derived[bc].append(full)

    def _link_code_strings(self):
        """Resolves asset paths, path prefixes, Resources keys and shader names written in project code."""
        import bisect
        shader_path = {name: p for p, (_, name) in self.cache.get('shaders', {}).items() if name}
        paths = sorted(x for x in self.path2guid if x.startswith(('Assets/', 'Packages/')))
        from unitygraph import formats
        addr = formats.addresses(self)
        res_index = defaultdict(list)
        for x in paths:
            if '/Resources/' in x:
                res_index[os.path.splitext(x.split('/Resources/', 1)[1])[0]].append(x)
        self.code_loads = defaultdict(list)    # class full name -> labels
        self.code_users = defaultdict(set)     # asset path -> {(class full name, string)}
        self.code_users_by_class = defaultdict(set)  # class full name -> {(asset path, string)}
        for full, t in self.classes.items():
            if not t['project']:
                continue
            for kind, val in t.get('strs', []):
                hits = []
                if kind == 'shader':
                    sp = shader_path.get(val)
                    label = f'shader "{val}"' + (f' ({os.path.basename(sp)})' if sp else ' (builtin/package)')
                    hits = [sp] if sp else []
                elif kind == 'res':
                    hits = res_index.get(val, [])
                    label = f'Resources "{val}"' + ('' if hits else ' (missing)')
                elif kind == 'addr':
                    hits = [self.guid2path[g] for g, (a, _, labels) in addr.items()
                            if (a == val or val in labels) and g in self.guid2path]
                    label = f'Addressable "{val}"' + ('' if hits else ' (no such address or label)')
                elif val in self.path2guid:
                    hits = [val]
                    is_dir = not os.path.splitext(val)[1]
                    if is_dir:  # a folder constant: code builds its file names from it
                        i = bisect.bisect_left(paths, val + '/')
                        while i < len(paths) and paths[i].startswith(val + '/'):
                            hits.append(paths[i])
                            i += 1
                    label = val + '/' if is_dir else os.path.basename(val)
                else:
                    i = bisect.bisect_left(paths, val)
                    while i < len(paths) and paths[i].startswith(val):
                        hits.append(paths[i])
                        i += 1
                    label = f'{val}* ({len(hits)})' if hits else f'{val} (not on disk)'
                self.code_loads[full].append(label)
                for h in hits:
                    self.code_users[h].add((full, val))
                    self.code_users_by_class[full].add((h, val))

    # ---- name helpers

    def find_class(self, name, ns=''):
        name = re.sub(r'<.*', '', name).rstrip('?').split('.')[-1]
        cands = self.by_name.get(name, [])
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        for c in cands:
            if self.classes[c]['ns'] == ns:
                return c
        proj = [c for c in cands if self.classes[c]['project']]
        return (proj or cands)[0]

    def lookup_class(self, query):
        if query in self.classes:
            return query
        c = self.find_class(query)
        if c:
            return c
        low = query.lower()
        hits = [k for k in self.classes if k.lower().endswith(low)]
        return hits[0] if len(hits) == 1 else None

    def class_fields(self, full, seen=None):
        """Serialized fields of a class including its base classes, name -> (type, ns)."""
        out, seen = {}, seen or set()
        t = self.classes.get(full)
        if not t or full in seen:
            return out
        seen.add(full)
        for b in t['bases']:
            bc = self.find_class(b, t['ns'])
            if bc:
                out.update(self.class_fields(bc, seen))
        for f in t['fields']:
            out[f['n']] = (f['t'], t['ns'])
        return out

    def script_name(self, guid, ident=''):
        if guid in self.script_cls:
            return self.script_cls[guid]
        p = self.guid2path.get(guid)
        if p and p.endswith('.cs'):
            return stem(p)
        if ident:
            return ident.split('::')[-1]
        return 'MissingScript'

    def layer_mask(self, bits):
        if not hasattr(self, '_layers'):
            self._layers = {}
            tm = os.path.join(self.root, 'ProjectSettings', 'TagManager.asset')
            if os.path.exists(tm):
                with open(tm, encoding='utf-8') as f:
                    body = next(iter_docs(f.read().replace('\r\n', '\n')), (None,) * 5)[4]
                doc = parse_yaml_block(body) if body else {}
                for i, name in enumerate((doc or {}).get('layers') or []):
                    if name:
                        self._layers[i] = str(name)
        try:
            b = int(bits) & 0xFFFFFFFF
        except ValueError:
            return f'mask({bits})'
        if b == 0xFFFFFFFF:
            return 'Everything'
        if b == 0:
            return 'Nothing'
        names = [self._layers.get(i, f'#{i}') for i in range(32) if b >> i & 1]
        return '|'.join(names)

    def short(self, full):
        return full.split('.')[-1]

    def asset_label(self, guid, fid='0'):
        if guid in ('0000000000000000e000000000000000', '0000000000000000f000000000000000'):
            return 'builtin'
        p = self.guid2path.get(guid)
        if not p:
            return f'?{guid[:8]}'
        if p.endswith('.cs'):
            return self.short(self.script_name(guid))
        clip = self.clip_names.get(guid, {}).get(str(fid))
        if clip:
            return f'{os.path.basename(p)}:{clip}'
        return os.path.basename(p)

    # ---- trees

    def fm(self, path):
        e = self.files.get(path)
        return e and e['fm']

    def prefab_root_go(self, guid):
        """fileID (inside that asset) of the root GameObject a prefab instance of `guid` is named after."""
        if guid in self._root_cache:
            return self._root_cache[guid]
        self._root_cache[guid] = MODEL_ROOT_GO
        p = self.guid2path.get(guid, '')
        fm = self.fm(p) if p.endswith('.prefab') else None
        res = MODEL_ROOT_GO
        if fm:
            for tid, tr in fm['trs'].items():
                if tr['father'] == '0' and tr['go'] in fm['gos']:
                    res = tr['go']
                    break
            else:
                for pid, pi in fm['pis'].items():
                    if pi['parent'] == '0':
                        inner = self.prefab_root_go(pi['src'])
                        res = next((sid for sid, s in fm['st'].items() if s['pi'] == pid and s['src'] == inner),
                                   MODEL_ROOT_GO)
                        break
        self._root_cache[guid] = res
        return res

    def obj_in_asset(self, guid, fid, depth=0):
        """(GameObject name, component full name or type) for an object inside a prefab asset."""
        p = self.guid2path.get(guid, '')
        fm = self.fm(p) if p.endswith('.prefab') else None
        if not fm or depth > 8:
            return None, None
        if fid in fm['comps']:
            c = fm['comps'][fid]
            go = fm['gos'].get(c['go'])
            ctype = self.script_name(c['script'], c['ident']) if c['type'] == 'MonoBehaviour' else c['type']
            if go:
                return go['name'], ctype
            s = fm['st'].get(c['go'])
            name = self.obj_in_asset(fm['pis'][s['pi']]['src'], s['src'], depth + 1)[0] if s and s['pi'] in fm['pis'] else None
            return name, ctype
        if fid in fm['gos']:
            return fm['gos'][fid]['name'], None
        if fid in fm['trs']:
            go = fm['gos'].get(fm['trs'][fid]['go'])
            return (go['name'] if go else None), 'Transform'
        s = fm['st'].get(fid)
        if s and s['pi'] in fm['pis']:
            return self.obj_in_asset(fm['pis'][s['pi']]['src'], s['src'], depth + 1)
        return None, None

    def tree(self, path):
        if path not in self._tree_cache:
            self._tree_cache[path] = build_tree(self, path, self.fm(path))
        return self._tree_cache[path]


# ---------------------------------------------------------------- value rendering

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _fmt_num(v):
    f = _num(v)
    if f is None:
        return str(v)
    # range check first: int() of Infinity (serialized as "Infinity") raises, and NaN fails it too
    return str(int(f)) if abs(f) < 1e15 and f == int(f) else f'{f:.4g}'


def _is_empty(v):
    if v in ('', '0', '0.0', None, [], {}):
        return True
    if isinstance(v, dict):
        if 'fileID' in v:
            return v.get('fileID') == '0'
        return all(_is_empty(x) for x in v.values())
    return False


class Ctx:
    """Rendering context for one file: resolves local fileIDs to object names."""

    def __init__(self, proj, fm, names):
        self.p, self.fm, self.names = proj, fm, names

    def local(self, fid):
        fm = self.fm
        if fid in self.names:
            return self.names[fid]
        if fid in fm['comps']:
            c = fm['comps'][fid]
            t = self.p.short(self.p.script_name(c['script'], c['ident'])) if c['type'] == 'MonoBehaviour' else c['type']
            return f"{self.local(c['go'])}.{t}"
        if fid in fm['trs']:
            return self.local(fm['trs'][fid]['go'])
        s = fm['st'].get(fid)
        if s:
            pi = self.names.get(s['pi'], '?')
            src = fm['pis'][s['pi']]['src'] if s['pi'] in fm['pis'] else ''
            go, comp = self.p.obj_in_asset(src, s['src']) if src else (None, None)
            root = self.p.obj_in_asset(src, self.p.prefab_root_go(src))[0] if src else None
            inner = go if go and go not in (pi, root) else ''  # the root may be renamed on the instance
            return pi + (f'/{inner}' if inner else '') + (f'.{self.p.short(comp)}' if comp and comp != 'Transform' else '')
        return f'#{fid}'

    def value(self, v, ftype='', ns='', depth=0):
        p = self.p
        if isinstance(v, dict):
            if 'fileID' in v:
                if v.get('fileID') == '0':
                    return 'null'
                g = v.get('guid')
                return '→' + p.asset_label(g, v['fileID']) if g else '@' + self.local(v['fileID'])
            keys = set(v)
            if 'm_Bits' in keys:
                return p.layer_mask(v['m_Bits'])
            if 'm_AssetGUID' in keys:  # Addressables AssetReference
                g = v.get('m_AssetGUID') or ''
                if not g:
                    return 'null'
                from unitygraph import formats
                a = formats.addresses(p).get(g)
                sub = f':{v["m_SubObjectName"]}' if v.get('m_SubObjectName') else ''
                return '→' + p.asset_label(g) + sub + (f' (addr "{a[0]}")' if a else '')
            if keys and keys <= {'x', 'y', 'z', 'w'}:
                return '(' + ','.join(_fmt_num(v[k]) for k in 'xyzw' if k in v) + ')'
            if keys == {'r', 'g', 'b', 'a'}:
                try:
                    rgb = ''.join(f'{max(0, min(255, round(float(v[k]) * 255))):02X}' for k in 'rgb')
                    a = float(v['a'])
                    return '#' + rgb + ('' if a == 1 else f'{max(0, min(255, round(a * 255))):02X}')
                except (ValueError, OverflowError):  # Infinity/NaN channels: fall back to {r=…, …}
                    pass
            if depth > 2:
                return '{…}'
            sub = p.class_fields(p.find_class(ftype, ns)) if ftype else {}
            parts = [f'{k}={self.value(x, *sub.get(k, ("", "")), depth + 1)}' for k, x in v.items()
                     if not _is_empty(x)]
            return '{' + ', '.join(parts) + '}'
        if isinstance(v, list):
            inner = re.sub(r'^(List|IList)<(.*)>$', r'\2', ftype)[:-2] if ftype.endswith('[]') else \
                re.sub(r'^(?:List|IList)<(.*)>$', r'\1', ftype)
            items = [self.value(x, inner, ns, depth + 1) for x in v[:6]]
            more = f', …+{len(v) - 6}' if len(v) > 6 else ''
            return '[' + ', '.join(items) + more + ']'
        s = str(v)
        if ftype:
            base = ftype.rstrip('?')
            if base == 'bool':
                return 'true' if s == '1' else 'false'
            ec = p.find_class(base, ns)
            if ec and p.classes[ec]['kind'] == 'enum':
                vals = p.classes[ec]['enum']
                try:
                    n = int(s)
                except ValueError:
                    return s
                hit = [nm for nm, val in vals if val == n]
                if hit:
                    return hit[0]
                if 'Flags' in p.classes[ec]['attrs'] and n > 0:
                    flags = [nm for nm, val in vals if isinstance(val, int) and val > 0 and n & val == val]
                    return '|'.join(flags) or s
                return s
        if _num(s) is not None:
            return _fmt_num(s)
        return json.dumps(s, ensure_ascii=False) if len(s) <= 60 else json.dumps(s[:57] + '…', ensure_ascii=False)

    def fields(self, comp, full=False):
        cls = self.p.script_name(comp['script'], comp['ident'])
        ftypes = self.p.class_fields(cls) if cls in self.p.classes else {}
        out = []
        for k, v in comp.get('fields', {}).items():
            if k in MIRROR_BASE_FIELDS and not full:
                continue
            ftype, ns = ftypes.get(k, ('', ''))
            # a project enum's first value is a real choice (ItemType.Plank), a third-party one is just its default
            ec = self.p.find_class(ftype, ns) if ftype else None
            keep_zero = ec and self.p.classes[ec]['kind'] == 'enum' and self.p.classes[ec]['project']
            if not full and _is_empty(v) and not keep_zero:
                continue
            out.append(f'{k}={self.value(v, ftype, ns)}')
        return out


# ---------------------------------------------------------------- hierarchy trees

def comp_label(ctx, comp, full=False):
    p = ctx.p
    if comp['type'] != 'MonoBehaviour':
        refs = comp.get('refs') or {}
        shown = ', '.join('→' + p.asset_label(r['guid'], r.get('fileID', '0')) for r in refs.values())
        return comp['type'] + ('(off)' if comp.get('en') == '0' else '') + (f'({shown})' if shown else '')
    name = p.short(p.script_name(comp['script'], comp['ident']))
    f = ctx.fields(comp, full)
    off = '(off)' if comp.get('en') == '0' else ''
    return name + off + (f'({", ".join(f)})' if f else '')


def build_tree(p, path, fm):
    """Builds the GameObject hierarchy of a scene/prefab as nested dicts with resolved names."""
    if not fm:
        return []
    gos, trs, st, comps, pis = fm['gos'], fm['trs'], fm['st'], fm['comps'], fm['pis']
    names = {}
    for gid, g in gos.items():
        names[gid] = g['name']
    pi_root = {}
    for pid, pi in pis.items():
        root_go = p.prefab_root_go(pi['src'])
        nm = next((m[2] for m in pi['mods'] if m[1] == 'm_Name' and m[0] == root_go), None)
        if nm is None:
            nm = next((m[2] for m in pi['mods'] if m[1] == 'm_Name'), None) if not p.guid2path.get(pi['src'], '').endswith('.prefab') else None
        if nm is None:
            nm = stem(p.guid2path.get(pi['src'], '?missing'))
        names[pid] = str(nm)
        pi_root[pid] = root_go
    ctx = Ctx(p, fm, names)

    st_pi = {sid: s['pi'] for sid, s in st.items()}
    pi_kids = defaultdict(list)       # parent transform id -> prefab instance ids
    for pid, pi in pis.items():
        pi_kids[pi['parent']].append(pid)
    extra_kids = defaultdict(list)    # prefab instance -> GameObjects/instances added under its objects
    added_comps = defaultdict(list)
    for tid, tr in trs.items():
        if tr['father'] in st_pi:
            extra_kids[st_pi[tr['father']]].append(('g', tr['go']))
    for pid, pi in pis.items():
        if pi['parent'] in st_pi:
            extra_kids[st_pi[pi['parent']]].append(('p', pid))
    for cid, c in comps.items():
        if c['go'] in st_pi:
            added_comps[st_pi[c['go']]].append(c)
    seen = set()

    def go_node(gid):
        if gid in seen or gid not in gos:
            return None
        seen.add(gid)
        g = gos[gid]
        tid = next((c for c in g['comps'] if c in trs), None)
        labels, types = [], set()
        for cid in g['comps']:
            c = comps.get(cid)
            if c:
                types.add(c['type'])
        for cid in g['comps']:
            c = comps.get(cid)
            if not c or (c['type'] == 'MeshFilter' and 'MeshRenderer' in types):
                continue
            labels.append(comp_label(ctx, c))
        kids = []
        if tid:
            for ch in trs[tid]['children']:
                kids.append(tr_node(ch))
            for pid in pi_kids.get(tid, []):
                kids.append(pi_node(pid))
        node = {'n': g['name'], 'k': 'g', 'id': gid, 'off': g['active'] == '0', 'c': labels,
                'ch': [k for k in kids if k], 'tag': g['tag'] if g['tag'] != 'Untagged' else '',
                'scripts': [p.script_name(comps[c]['script'], comps[c]['ident']) for c in g['comps']
                            if c in comps and comps[c]['type'] == 'MonoBehaviour']}
        return node

    def tr_node(tid):
        if tid in trs:
            return go_node(trs[tid]['go'])
        if tid in st_pi:
            return pi_node(st_pi[tid])
        if tid in pis:  # SceneRoots lists root prefab instances by their PrefabInstance id
            return pi_node(tid)
        return None

    def pi_node(pid):
        if pid in seen or pid not in pis:
            return None
        seen.add(pid)
        pi = pis[pid]
        ov, off = [], False
        src_path = p.guid2path.get(pi['src'], '')
        for tgt, prop, val, ref in pi['mods']:
            if prop == 'm_IsActive' and tgt == pi_root[pid]:
                off = str(val) == '0'
                continue
            if TRIVIAL_MOD.match(prop):
                continue
            goname, ctype = p.obj_in_asset(pi['src'], tgt)
            rootname = p.obj_in_asset(pi['src'], pi_root[pid])[0]
            prefix = f'{goname}:' if goname and goname != rootname else ''
            if not goname and not ctype and tgt not in (pi_root[pid], MODEL_ROOT_TR):
                prefix = 'inner:'  # a node inside a model; modern importers keep no name table for it
            shown = prop.replace('.Array.data[', '[').replace('.Array.size', '.size')
            shown = re.sub(r'(^|\.)m_', r'\1', shown)
            if ctype:
                prefix += p.short(ctype) + '.'
            if ref and ref.get('fileID', '0') != '0':
                vs = ctx.value(ref)
            else:
                ftype, ns = ('', '')
                if ctype and '.' not in prop:
                    ftype, ns = p.class_fields(ctype).get(prop, ('', ''))
                vs = ctx.value(val, ftype, ns)
            ov.append(f'{prefix}{shown}={vs}')
        if pi['removed']:
            ov.append(f'-{pi["removed"]} comp')
        if pi['removed_go']:
            ov.append(f'-{pi["removed_go"]} obj')
        kids = []
        for kind, x in extra_kids.get(pid, []):
            kids.append(go_node(x) if kind == 'g' else pi_node(x))
        added = [comp_label(ctx, c) for c in added_comps.get(pid, [])]
        return {'n': names[pid], 'k': 'p', 'id': pid, 'src': pi['src'], 'srcp': src_path, 'off': off,
                'ov': ov, 'c': ['+' + a for a in added], 'ch': [k for k in kids if k],
                'scripts': [p.script_name(c['script'], c['ident']) for c in added_comps.get(pid, [])
                            if c['type'] == 'MonoBehaviour']}

    roots = []
    if fm['roots']:
        for tid in fm['roots']:
            roots.append(tr_node(tid))
    else:
        for tid, tr in trs.items():
            if tr['father'] == '0':
                roots.append(go_node(tr['go']))
        for pid, pi in pis.items():
            if pi['parent'] == '0':
                roots.append(pi_node(pid))
    roots = [r for r in roots if r]

    def fold(n):
        # "Rock_3 [SphereCollider]" holding just its model instance reads as one line: Rock_3 [..] › ⇐Rock_3.fbx
        for c in n['ch']:
            fold(c)
        if len(n['ch']) == 1 and not n['ch'][0]['ch'] and not n['ch'][0].get('only'):
            n['only'] = n['ch'][0]
            n['ch'] = []

    def count(n):
        n['cnt'] = sum(1 + count(c) for c in n['ch']) + (1 if n.get('only') else 0)
        return n['cnt']
    for r in roots:
        fold(r)
        count(r)
    return roots


def _norm_name(n):
    return re.sub(r'(\s*\(\d+\)|[ _]?\d+)$', '', n)


def _sig(n):
    """Structural signature: siblings with equal signatures render once as "Name ×N"."""
    if 'sig' not in n:
        kids = tuple(_sig(c) for c in n['ch'])
        only = _sig(n['only']) if n.get('only') else None
        bad = n.get('ov') or None in kids or (n.get('only') and only is None)
        n['sig'] = None if bad else (n['k'], n.get('src'), _norm_name(n['n']), tuple(n['c']), n['off'], only, kids)
    return n['sig']


def _is_decor(n):
    """A leaf with no scripts or overrides: trees, rocks, props."""
    return not n['ch'] and not n.get('ov') and not n['scripts'] and \
        not (n.get('only') and (n['only']['scripts'] or n['only'].get('ov')))


def group_nodes(nodes):
    """Collapses identical sibling leaves (same prefab/components, numbered names) into one entry."""
    order, groups = [], {}
    for n in nodes:
        s = _sig(n)
        if s is None:
            order.append([n])
            continue
        if s in groups:
            groups[s].append(n)
        else:
            groups[s] = [n]
            order.append(groups[s])
    return order


def node_line(n, count=1, names=None, full=False):
    if count > 1:
        uniq = sorted(set(names))
        name = uniq[0] if len(uniq) == 1 else _norm_name(n['n']) + '*'
        head = f'{name} ×{count}'
    else:
        head = n['n']
    if n['off']:
        head += ' (off)'
    if n.get('tag'):
        head += f' #{n["tag"]}'
    if n['k'] == 'p':
        src = n['srcp'] or '?missing'
        head += ' ⇐' + (src if full else os.path.basename(src))
    if n['c']:
        head += ' [' + ', '.join(n['c']) + ']'
    if n.get('ov'):
        shown = n['ov'] if full else n['ov'][:8]
        head += ' {' + '; '.join(shown) + (f'; …+{len(n["ov"]) - 8}' if len(n['ov']) > len(shown) else '') + '}'
    only = n.get('only')
    if only:
        child = node_line(only, full=full)
        if _norm_name(only['n']) == _norm_name(n['n']):
            child = child[len(only['n']):].lstrip()
        head += ' › ' + child
    return head


def decor_summary(n, kinds=6):
    groups = group_nodes(n['ch'])
    groups.sort(key=lambda g: -len(g))
    label = lambda g: re.sub(r'\s*\(\d+\)$', '', g[0]['n'])
    shown = ', '.join(f'{label(g)}×{len(g)}' if len(g) > 1 else label(g) for g in groups[:kinds])
    more = f' …+{len(groups) - kinds} kinds' if len(groups) > kinds else ''
    return f'  ({n["cnt"]} decor: {shown}{more})'


def render_tree(nodes, depth=None, ind=0, out=None, full=False, expand=False):
    """expand: list the first level in full even if it is all decor (for an explicit --path)."""
    out = [] if out is None else out
    for grp in group_nodes(nodes):
        n = grp[0]
        line = '  ' * ind + node_line(n, len(grp), [g['n'] for g in grp], full)
        if n['ch'] and not full and not expand and len(n['ch']) > 8 and all(_is_decor(c) for c in n['ch']):
            out.append(line + decor_summary(n))
            continue
        if n['ch'] and depth is not None and ind + 1 >= depth:
            out.append(line + f'  …+{n["cnt"]}')
            continue
        out.append(line)
        if n['ch']:
            render_tree(n['ch'], depth, ind + 1, out, full)
    return out


def walk_nodes(nodes, prefix=''):
    for n in nodes:
        path = f'{prefix}/{n["n"]}' if prefix else n['n']
        yield path, n
        yield from walk_nodes(n['ch'] + ([n['only']] if n.get('only') else []), path)


def find_node(nodes, path):
    parts = [x for x in path.strip('/').split('/') if x]
    cur = nodes
    node = None
    for part in parts:
        node = next((n for n in cur if n['n'] == part), None) or \
            next((n for n in cur if n['n'].lower() == part.lower()), None)
        if not node:
            return None
        cur = node['ch'] + ([node['only']] if node.get('only') else [])
    return node


# ---------------------------------------------------------------- reports

def is_project_asset(p, path):
    return path.startswith('Assets/') and not is_third_party(path, p.extra_tp)


def file_header(p, path):
    tree = p.tree(path)
    nodes = list(walk_nodes(tree))
    gos = sum(1 for _, n in nodes if n['k'] == 'g')
    pis = sum(1 for _, n in nodes if n['k'] == 'p')
    scripts = Counter(p.short(s) for _, n in nodes for s in n['scripts'])
    prefabs = Counter(os.path.basename(n['srcp'] or '?') for _, n in nodes if n['k'] == 'p')
    lines = [f'# {stem(path)}  ({path})', f'{gos} GameObjects, {pis} prefab instances']
    if scripts:
        lines.append('scripts: ' + ', '.join(f'{k}×{v}' if v > 1 else k for k, v in scripts.most_common()))
    if prefabs:
        top = prefabs.most_common(20)
        lines.append('instances: ' + ', '.join(f'{k}×{v}' if v > 1 else k for k, v in top) +
                     (f', …+{len(prefabs) - 20} kinds' if len(prefabs) > 20 else ''))
    return lines


def report_file(p, path, depth=None, sub=None, full=False):
    tree = p.tree(path)
    lines = file_header(p, path)
    if sub:
        node = find_node(tree, sub)
        if not node:
            return f'no object at path "{sub}" in {path}'
        lines.append(f'@ {sub}')
        tree = [node]
    lines.append('')
    lines += render_tree(tree, depth, full=full, expand=bool(sub))
    return '\n'.join(lines)


def report_data_asset(p, path):
    fm = p.fm(path)
    out = []
    for fid, entry in fm['main']:
        if isinstance(entry, dict):
            ctx = Ctx(p, fm, {})
            cls = p.short(p.script_name(entry['script'], entry['ident']))
            f = ctx.fields(entry)
            out.append(f'{stem(path)} : {cls}' + (f' {{{"; ".join(f)}}}' if f else ''))
    return '\n'.join(out)


PARAM_TYPES = {'1': 'float', '3': 'int', '4': 'bool', '9': 'trigger'}
COND_OPS = {'1': '{}', '2': '!{}', '3': '{}>{}', '4': '{}<{}', '6': '{}=={}', '7': '{}!={}'}


def report_controller(p, path):
    c = p.files[path].get('ctrl')
    if not c:
        return f'{path}: not an animator controller'
    lines = [f'# {stem(path)}  ({path})']
    if c['override'] is not None:
        base = c['override']
        lines.append('overrides ' + ('→' + p.asset_label(base['guid']) if base.get('guid') else '(no base)'))
        for orig, new in c.get('clips', []):
            if new.get('guid'):
                lines.append(f"  {p.asset_label(orig.get('guid', ''), orig.get('fileID', '0'))} → "
                             f"{p.asset_label(new['guid'], new.get('fileID', '0'))}")
        return '\n'.join(lines)

    def motion(ref, depth=0):
        if not isinstance(ref, dict) or ref.get('fileID', '0') == '0':
            return '(none)'
        if ref.get('guid'):
            return p.asset_label(ref['guid'], ref['fileID'])
        tree = c['trees'].get(ref['fileID'])
        if not tree or depth > 3:
            return '?'
        param = tree['param'] + (f',{tree["paramY"]}' if tree['type'] != '0' and tree['paramY'] else '')
        kids = []
        for m, thr, pos in tree['childs']:
            at = f'({_fmt_num(pos.get("x"))},{_fmt_num(pos.get("y"))})' if tree['type'] != '0' and pos else _fmt_num(thr)
            kids.append(f'{motion(m, depth + 1)}@{at}')
        return f'blend {param} [' + ', '.join(kids) + ']'

    def cond(t):
        parts = [COND_OPS.get(mode, '{}?{}').format(ev, _fmt_num(thr)) for mode, ev, thr in t['conds']]
        if t['has_exit']:
            parts.append(f'exit {_fmt_num(t["exit_time"])}')
        return ' & '.join(parts) or 'always'

    def target(t):
        if t['exit']:
            return 'Exit'
        if t['dst'] in c['states']:
            return c['states'][t['dst']]['name']
        return c['sms'].get(t['dst_sm'], {}).get('name', '?') + '/' if t['dst_sm'] != '0' else '?'

    def behaviours(ids):
        names = [p.short(p.script_name(c['behaviours'][b]['script'], c['behaviours'][b]['ident']))
                 for b in ids if b in c['behaviours']]
        return f' +{"+".join(names)}' if names else ''

    def sm_lines(fid, ind, seen):
        sm = c['sms'].get(fid)
        if not sm or fid in seen:
            return []
        seen.add(fid)
        out = []
        for t in sm['any']:
            if t in c['trans']:
                out.append('  ' * ind + f'Any → {target(c["trans"][t])} if {cond(c["trans"][t])}')
        for sid in sm['states']:
            s = c['states'].get(sid)
            if not s:
                continue
            head = ('*' if sid == sm['default'] else '') + s['name']
            extra = ''
            if s['speedp']:
                extra += f' speed×{s["speedp"]}'
            elif s['speed'] not in ('1', 1):
                extra += f' speed {_fmt_num(s["speed"])}'
            if s['tag']:
                extra += f' #{s["tag"]}'
            trans = [f'→ {target(c["trans"][t])} if {cond(c["trans"][t])}' for t in s['trans'] if t in c['trans']]
            out.append('  ' * ind + f'{head} = {motion(s["motion"])}{extra}{behaviours(s["beh"])}' +
                       (('  ' + '; '.join(trans)) if trans else ''))
        for sub in sm['subs']:
            out.append('  ' * ind + f'{c["sms"].get(sub, {}).get("name", "?")}/')
            out += sm_lines(sub, ind + 1, seen)
        return out

    if c['params']:
        prm = []
        for name, typ, df, di, db in c['params']:
            t = PARAM_TYPES.get(str(typ), str(typ))
            default = {'float': df, 'int': di, 'bool': db}.get(t, '0')
            prm.append(f'{name}:{t}' + (f'={_fmt_num(default)}' if str(default) not in ('0', '0.0', '') else ''))
        lines.append('params: ' + ', '.join(prm))
    for i, (name, sm, mask, blend, synced) in enumerate(c['layers']):
        notes = []
        if synced not in ('-1', -1):
            notes.append(f'synced to layer {synced}')
        if blend == '1':
            notes.append('additive')
        lines.append(f'layer {name}' + (f' ({", ".join(notes)})' if notes else '') +
                     behaviours(c['sms'].get(sm, {}).get('beh', [])))
        lines += sm_lines(sm, 1, set())
    lines.append('(* = default state)')
    return '\n'.join(lines)


def class_report(p, full):
    t = p.classes[full]
    lines = []
    base = f' : {", ".join(t["bases"])}' if t['bases'] else ''
    kind = t['kind'] if t['kind'] != 'class' else ('abstract class' if t['abstract'] else 'class')
    parts = t.get('files', [t['file']])
    where = f'{t["file"]}:{t["line"]}' + (f' + {", ".join(os.path.basename(f) for f in parts[1:])}' if len(parts) > 1 else '')
    lines.append(f'{kind} {full}{base}   ({where})')
    if t['summary']:
        lines.append(f'  {t["summary"]}')
    if t['enum']:
        lines.append('  values: ' + ', '.join(f'{n}={v}' for n, v in t['enum']))
    ser = [f for f in t['fields'] if f['ser']]
    other = [f for f in t['fields'] if not f['ser']]
    if ser:
        lines.append('  serialized: ' + '; '.join(f"{f['n']}: {f['t']}" + (' [SyncVar]' if 'SyncVar' in f['attrs'] else '')
                                                 for f in ser))
    if other:
        lines.append('  fields: ' + '; '.join(f"{f['n']}: {f['t']}" + (' [SyncVar]' if 'SyncVar' in f['attrs'] else '') +
                                             (' static' if f['static'] else '') for f in other))
    pub_props = [x for x in t['props'] if x['vis'] in ('public', 'internal', 'protected')]
    if pub_props:
        lines.append('  props: ' + ', '.join(('event ' if x['event'] else '') + f"{x['n']}: {x['t']}" for x in pub_props))
    pub, msgs, rpc, priv = [], [], [], 0
    for m in t['methods']:
        sig = f"{m['n']}({m['params']})" + (f": {m['ret']}" if m['ret'] not in ('void', 'ctor', '') else '')
        tags = [a for a in m['attrs'] if a in RPC_ATTRS]
        if tags:
            rpc.append(f"[{','.join(tags)}] {sig}")
        elif m['n'] in UNITY_MESSAGES:
            msgs.append(m['n'])
        elif m['vis'] in ('public', 'internal', 'protected'):
            pub.append(('static ' if m['static'] else '') + sig)
        else:
            priv += 1
    if pub:
        lines.append('  methods: ' + '; '.join(pub))
    if rpc:
        lines.append('  net/attr: ' + '; '.join(rpc))
    if msgs:
        lines.append('  unity: ' + ', '.join(msgs) + (f'   (+{priv} private)' if priv else ''))
    elif priv:
        lines.append(f'  (+{priv} private methods)')
    uses = sorted({p.short(c) for c in class_uses(p, full)})
    if uses:
        lines.append('  uses: ' + ', '.join(uses))
    users = sorted({p.short(c) for c in p.classes if c != full and full in class_uses(p, c)})
    if users:
        lines.append('  used by: ' + ', '.join(users))
    loads = p.code_loads.get(full)
    if loads:
        lines.append('  assets in code: ' + ', '.join(loads[:20]) + (f', …+{len(loads) - 20}' if len(loads) > 20 else ''))
    derived = sorted(p.short(d) for d in p._derived.get(full, []))
    if derived:
        lines.append('  derived: ' + ', '.join(derived))
    att = attachments(p).get(full)
    if att:
        lines.append('  attached: ' + '; '.join(_fmt_attach(att)))
    return '\n'.join(lines)


def _fmt_attach(att):
    by_file = defaultdict(list)
    for f, path in att:
        by_file[f].append(path)
    out = []
    for f, paths in by_file.items():
        c = Counter(paths)
        shown = [f'{k}×{v}' if v > 1 else k for k, v in c.most_common(4)]
        more = f', …+{len(c) - 4}' if len(c) > 4 else ''
        out.append(f'{stem(f)}{os.path.splitext(f)[1]}: ' + ', '.join(shown) + more)
    return out


_uses_cache = {}


def class_uses(p, full):
    if full not in _uses_cache:
        t = p.classes[full]
        res = set()
        for ident in t['idents']:
            if ident == t['name']:
                continue
            c = p.find_class(ident, t['ns'])
            if c and p.classes[c]['project'] and not c.startswith(full + '.'):
                res.add(c)
        _uses_cache[full] = res if t['project'] and t['kind'] != 'enum' else set()
    return _uses_cache[full]


_attach_cache = {}


def attachments(p):
    """class full name -> [(file, GameObject path)] for every direct attachment in scenes/prefabs/assets."""
    if 'v' not in _attach_cache:
        res = defaultdict(list)
        for path, e in p.files.items():
            if not e['fm']:
                continue
            if path.endswith('.asset'):
                for fid, entry in e['fm']['main']:
                    if isinstance(entry, dict):
                        res[p.script_name(entry['script'], entry['ident'])].append((path, '(asset)'))
                continue
            for npath, n in walk_nodes(p.tree(path)):
                for s in n['scripts']:
                    res[s].append((path, re.sub(r'\s*\(\d+\)', '', npath)))
        _attach_cache['v'] = res
    return _attach_cache['v']


def project_map(p):
    lines = []
    ver = ''
    vp = os.path.join(p.root, 'ProjectSettings', 'ProjectVersion.txt')
    if os.path.exists(vp):
        m = re.search(r'm_EditorVersion: (\S+)', open(vp, encoding='utf-8').read())
        ver = m.group(1) if m else ''
    lines.append(f'# {os.path.basename(p.root)} — unitygraph map (Unity {ver})')
    lines.append('Query, don\'t read raw YAML: unitygraph scene|prefab|class|where|refs|find|anim <name>')
    lines.append('Legend: Name ⇐Source.prefab {overrides}  [Components(field=value)]  @SceneObject  →Asset  ×N identical siblings  A › B = B is the only child of A')
    # assemblies
    asm = []
    for path, (_, g) in p.cache['meta'].items():
        if path.endswith('.asmdef.meta') and not is_third_party(path, p.extra_tp):
            try:
                d = json.load(open(os.path.join(p.root, path[:-5]), encoding='utf-8-sig'))
                asm.append(f"{d.get('name')} ({os.path.dirname(path)}) → {', '.join(d.get('references', []))}")
            except Exception:
                pass
    if asm:
        lines += ['', '## Assemblies'] + asm
    # code
    lines += ['', '## Code (project)']
    by_ns = defaultdict(list)
    for full, t in sorted(p.classes.items()):
        if t['project'] and not t['outer']:
            by_ns[t['ns'] or '(global)'].append(t)
    for ns, ts in sorted(by_ns.items()):
        lines.append(f'### {ns}')
        enums = [t['name'] for t in ts if t['kind'] == 'enum']
        for t in ts:
            if t['kind'] == 'enum':
                continue
            base = f' : {t["bases"][0]}' if t['bases'] else ''
            kind = '' if t['kind'] == 'class' else t['kind'] + ' '
            summ = f' — {t["summary"][:90]}' if t['summary'] else ''
            lines.append(f'- {kind}{t["name"]}{base}{summ}')
        if enums:
            lines.append('- enums: ' + ', '.join(enums))
    # scenes
    lines += ['', '## Scenes']
    for path in sorted(x for x, e in p.files.items() if x.endswith('.unity') and e['fm'] and is_project_asset(p, x)):
        tree = p.tree(path)
        nodes = list(walk_nodes(tree))
        roots = ', '.join(r['n'] for r in tree[:14]) + (f', …+{len(tree) - 14}' if len(tree) > 14 else '')
        lines.append(f'- {stem(path)} ({len(nodes)} objects): {roots}')
    # prefabs
    lines += ['', '## Prefabs (project)']
    for path in sorted(x for x, e in p.files.items() if x.endswith('.prefab') and e['fm'] and is_project_asset(p, x)):
        tree = p.tree(path)
        if not tree:
            continue
        r = tree[0]
        scripts = sorted({p.short(s) for _, n in walk_nodes(tree) for s in n['scripts']})
        variant = f' ⇐{os.path.basename(r["srcp"])}' if r['k'] == 'p' else ''
        lines.append(f'- {path[7:]}{variant}: ' + (', '.join(scripts) if scripts else '(no scripts)'))
    data = [x for x, e in p.files.items() if x.endswith('.asset') and e['fm'] and is_project_asset(p, x)]
    if data:
        lines += ['', '## Data assets (ScriptableObjects)']
        by_cls = defaultdict(list)
        for path in data:
            for fid, entry in p.fm(path)['main']:
                if isinstance(entry, dict):
                    by_cls[p.short(p.script_name(entry['script'], entry['ident']))].append(stem(path))
        for cls, names in sorted(by_cls.items()):
            lines.append(f'- {cls}: ' + ', '.join(sorted(names)[:12]) + (f', …+{len(names) - 12}' if len(names) > 12 else ''))
    ctrls = defaultdict(list)  # same params and states → one line
    for path, e in sorted(p.files.items()):
        c = e.get('ctrl')
        if c and is_project_asset(p, path):
            if c['override'] is not None:
                key = ('override of ' + p.asset_label(c['override'].get('guid', '')),)
            else:
                key = ('params ' + (', '.join(x[0] for x in c['params']) or '-'),
                       'states ' + ', '.join(sorted(s['name'] for s in c['states'].values())))
            ctrls[key].append(stem(path))
    if ctrls:
        lines += ['', '## Animator controllers  (unitygraph anim <name>)']
        for key, names in ctrls.items():
            lines.append(f'- {"; ".join(key)}: ' + ', '.join(names[:8]) + (f', …+{len(names) - 8}' if len(names) > 8 else ''))
    fmts = defaultdict(list)
    for path, e in sorted(p.files.items()):
        f = e.get('fmt')
        if f and is_project_asset(p, path):
            fmts[f['kind']].append((path, f))
    if fmts.get('input'):
        lines += ['', '## Input actions  (unitygraph input)']
        for path, f in fmts['input']:
            maps = '; '.join(f'{m["name"]}: ' + ', '.join(a['name'] for a in m['actions']) for m in f['maps'])
            lines.append(f'- {path[7:]}: {maps}')
    if fmts.get('uxml') or fmts.get('uss'):
        lines += ['', '## UI Toolkit  (unitygraph ui <name>)']
        lines.append('- ' + ', '.join(os.path.basename(x) for x, _ in fmts.get('uxml', []) + fmts.get('uss', [])))
    if fmts.get('addr'):
        lines += ['', '## Addressables  (unitygraph addr [regex])']
        for path, f in fmts['addr']:
            lines.append(f'- group {f["group"]}: {len(f["entries"])} entries')
    if fmts.get('mat'):
        by_shader = Counter()
        from unitygraph import formats
        for _, f in fmts['mat']:
            by_shader[formats.shader_label(p, f['shader'])] += 1
        lines += ['', f'## Materials ({len(fmts["mat"])}, unitygraph material <name>)',
                  '- ' + ', '.join(f'{s}×{n}' for s, n in by_shader.most_common(8))]
    return '\n'.join(lines)


def where_report(p, query):
    full = p.lookup_class(query)
    lines = []
    if full:
        att = attachments(p).get(full, [])
        lines.append(f'script {full}' + ('' if att else ' — not attached anywhere'))
        lines += ['  ' + x for x in _fmt_attach(att)]
        guid = next((g for g, c in p.script_cls.items() if c == full), None)
        prefabs = {f for f, _ in att if f.endswith('.prefab')}
        via = defaultdict(set)
        for pf in prefabs:
            g = p.path2guid.get(pf)
            for user in p.rdeps.get(g, ()):
                if user.endswith('.unity') or user.endswith('.prefab'):
                    via[user].add(stem(pf))
        for user, pfs in sorted(via.items()):
            lines.append(f'  via prefab in {stem(user)}{os.path.splitext(user)[1]}: ' + ', '.join(sorted(pfs)))
        if guid is None and not att:
            lines.append('  (no MonoBehaviour script file named after this class)')
        return '\n'.join(lines)
    builtin = []  # a built-in component type such as Animator or Light
    for path, e in sorted(p.files.items()):
        if e['fm'] and path.endswith(('.unity', '.prefab')) and is_project_asset(p, path):
            for npath, n in walk_nodes(p.tree(path)):
                if any(re.sub(r'\(.*', '', c).lstrip('+') == query for c in n['c']):
                    builtin.append((path, re.sub(r'\s*\(\d+\)', '', npath)))
    if builtin:
        return f'component {query}\n' + '\n'.join('  ' + x for x in _fmt_attach(builtin))
    return refs_report(p, query, only_users=True)


def resolve_asset(p, query):
    if query in p.path2guid:
        return query
    q = query.lower()
    hits = [x for x in p.path2guid if x.lower().endswith('/' + q) or x.lower() == q]
    if not hits:
        hits = [x for x in p.path2guid if stem(x).lower() == q and not x.endswith('.meta')]
    hits.sort(key=lambda x: (not x.startswith('Assets/'), len(x)))
    return hits[0] if hits else None


def refs_report(p, query, only_users=False):
    path = resolve_asset(p, query)
    if not path:
        return f'no asset matches "{query}"'
    guid = p.path2guid[path]
    lines = [f'{path}  (guid {guid})']
    users = sorted(p.rdeps.get(guid, ()))
    if path in p.files:
        e = p.files[path]
        if not only_users:
            deps = Counter(p.guid2path.get(g, f'?{g[:8]}') for g in e['deps'] if g != guid and not g.startswith('0000000000000000'))
            deps = sorted(deps)
            kinds = defaultdict(list)
            for d in deps:
                ext = os.path.splitext(d)[1] or '(other)'
                kinds[ext].append(os.path.basename(d) if not d.endswith('.cs') else stem(d))
            lines.append(f'uses ({len(deps)}):')
            for ext, items in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
                lines.append(f'  {ext}: ' + ', '.join(items[:25]) + (f', …+{len(items) - 25}' if len(items) > 25 else ''))
    lines.append(f'used by ({len(users)}):' + ('' if users or path in p.code_users else
                                                ' nothing in scenes/assets/code (Addressables are not indexed)'))
    for u in users[:60]:
        detail = ''
        if u.endswith('.unity') or u.endswith('.prefab'):
            hits = [np for np, n in walk_nodes(p.tree(u)) if n.get('src') == guid]
            if hits:
                c = Counter(re.sub(r'\s*\(\d+\)', '', h) for h in hits)
                detail = ' — ' + ', '.join(f'{k}×{v}' if v > 1 else k for k, v in c.most_common(5)) + \
                    (f', …+{len(c) - 5}' if len(c) > 5 else '')
        lines.append(f'  {u}{detail}')
    if len(users) > 60:
        lines.append(f'  …+{len(users) - 60}')
    from unitygraph import formats
    a = formats.addresses(p).get(guid)
    if a:
        lines.append(f'addressable: "{a[0]}" in group {a[1]}' + (f', labels {", ".join(a[2])}' if a[2] else ''))
    named = p.code_users.get(path)
    if named:
        by_cls = defaultdict(set)
        for full, val in named:
            by_cls[p.short(full)].add(val if val != path else '')
        lines.append('named in code by: ' + ', '.join(
            k + (' (' + ', '.join(f'"{v}"' for v in sorted(vs) if v) + ')' if any(vs) else '') for k, vs in sorted(by_cls.items())))
    if path.endswith('.cs'):
        name = stem(path)
        code_users = sorted(c for c in p.classes if p.classes[c]['project'] and name in p.classes[c]['idents']
                            and p.classes[c]['name'] != name)
        if code_users:
            lines.append('referenced in code by: ' + ', '.join(p.short(c) for c in code_users))
    return '\n'.join(lines)


def find_report(p, pattern, limit=60):
    rx = re.compile(pattern, re.I)
    hits = []
    for path in sorted(x for x, e in p.files.items() if e['fm'] and (x.endswith('.unity') or x.endswith('.prefab'))
                       and is_project_asset(p, x)):
        for npath, n in walk_nodes(p.tree(path)):
            if rx.search(n['n']):
                hits.append((path, npath, n))
    lines = []
    grouped = Counter((h[0], re.sub(r'\s*\(\d+\)', '', h[1])) for h in hits)
    for (path, npath), cnt in list(grouped.items())[:limit]:
        n = next(h[2] for h in hits if h[0] == path and re.sub(r'\s*\(\d+\)', '', h[1]) == npath)
        lines.append(f'{stem(path)}{os.path.splitext(path)[1]}: {npath}' + (f' ×{cnt}' if cnt > 1 else '') +
                     (' [' + ', '.join(n['c'][:4]) + ']' if n['c'] else '') +
                     (f' ⇐{os.path.basename(n["srcp"])}' if n['k'] == 'p' else ''))
    if len(grouped) > limit:
        lines.append(f'…+{len(grouped) - limit} more (narrow the pattern)')
    return '\n'.join(lines) or f'nothing named like "{pattern}"'


# ---------------------------------------------------------------- interactive graph

def graph_data(p):
    """Nodes (project types, scenes, prefabs, controllers, data assets) and the edges between them."""
    nodes, edges = {}, set()

    def add_file(path):
        e = p.files.get(path)
        if not e or not is_project_asset(p, path):
            return None
        if path.endswith('.unity') and e['fm']:
            kind, text = 'scene', '\n'.join(file_header(p, path) + [''] + render_tree(p.tree(path), 2))
        elif path.endswith('.prefab') and e['fm']:
            kind, text = 'prefab', report_file(p, path, 3)
        elif e.get('ctrl'):
            kind, text = 'anim', report_controller(p, path)
        elif path.endswith('.asset') and e['fm'] and report_data_asset(p, path):
            kind, text = 'data', report_data_asset(p, path)
        else:
            return None
        nid = 'f:' + path
        if nid not in nodes:
            nodes[nid] = {'id': nid, 'label': stem(path), 'kind': kind, 'group': path.split('/')[1],
                          'text': text + '\n\n' + refs_report(p, path)}
        return nid

    for full, t in p.classes.items():
        if t['project'] and not t['outer']:
            nodes['c:' + full] = {'id': 'c:' + full, 'label': t['name'],
                                  'kind': 'enum' if t['kind'] == 'enum' else 'class',
                                  'group': t['ns'] or '(global)', 'text': class_report(p, full)}
    for full, t in p.classes.items():
        a = 'c:' + full
        if a not in nodes:
            continue
        for u in class_uses(p, full):
            if 'c:' + u in nodes:
                edges.add((a, 'c:' + u, 'uses'))
        for b in t['bases']:
            bc = p.find_class(b, t['ns'])
            if bc and 'c:' + bc in nodes:
                edges.add((a, 'c:' + bc, 'inherits'))
        for path, _ in p.code_users_by_class.get(full, ()):
            f = add_file(path)
            if f:
                edges.add((a, f, 'names'))
    for full, att in attachments(p).items():
        if 'c:' + full in nodes:
            for path, _ in att:
                f = add_file(path)
                if f:
                    edges.add((f, 'c:' + full, 'attached'))
    for path, e in p.files.items():
        src = add_file(path)
        if not src:
            continue
        for g in e['deps']:
            dst = p.guid2path.get(g)
            if dst and dst != path:
                d = add_file(dst)
                if d:
                    edges.add((src, d, 'refs'))
    return list(nodes.values()), [{'s': a, 't': b, 'k': k} for a, b, k in sorted(edges)]


GRAPH_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ graph</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
<style>
:root{--bg:#f6f5f2;--panel:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#d9d7d2;--accent:#2f6fde}
@media (prefers-color-scheme:dark){:root{--bg:#141518;--panel:#1d1f23;--ink:#e8e8ea;--muted:#9a9ca3;--line:#33363c;--accent:#6aa0ff}}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font:13px/1.45 system-ui,sans-serif}
#app{display:grid;grid-template-columns:1fr 420px;height:100vh}
#cy{min-height:0}
aside{background:var(--panel);border-left:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
header{padding:14px 16px;border-bottom:1px solid var(--line)}
h1{font-size:15px;margin:0 0 8px}
input[type=search]{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
.row{display:flex;flex-wrap:wrap;gap:6px 12px;margin-top:10px}
.row label{display:flex;align-items:center;gap:5px;cursor:pointer;color:var(--muted)}
.sw{width:10px;height:10px;border-radius:50%;display:inline-block}
.hint{color:var(--muted);margin-top:8px;font-size:12px}
pre{margin:0;padding:14px 16px;overflow:auto;flex:1;white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,Consolas,monospace}
@media (max-width:800px){#app{grid-template-columns:1fr;grid-template-rows:55vh 45vh}aside{border-left:0;border-top:1px solid var(--line)}}
</style></head><body><div id="app"><div id="cy"></div><aside><header>
<h1>__TITLE__</h1><input id="q" type="search" placeholder="Find a node (Enter to focus)">
<div class="row" id="kinds"></div><div class="row" id="ekinds"></div>
<div class="hint">Click a node: its neighbours stay lit and its summary shows below. Click empty space to reset.</div>
</header><pre id="detail">__STATS__</pre></aside></div>
<script>
const DATA = __DATA__;
const KIND = {class:'#4f7cff',enum:'#9aa0ad',scene:'#e5484d',prefab:'#30a46c',anim:'#f5a524',data:'#a855f7'};
const EDGE = {uses:'#8a8f98',inherits:'#4f7cff',attached:'#30a46c',refs:'#e5484d',names:'#f5a524'};
const deg = {}; DATA.edges.forEach(e => {deg[e.s]=(deg[e.s]||0)+1; deg[e.t]=(deg[e.t]||0)+1;});
const els = DATA.nodes.map(n => ({data:{...n, deg:deg[n.id]||0}}))
  .concat(DATA.edges.map((e,i) => ({data:{id:'e'+i, source:e.s, target:e.t, k:e.k}})));
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
// Zooming in spreads nodes apart but barely enlarges them: sizes are divided by zoom^0.85 above zoom 1,
// so a crowded cluster opens up instead of staying a blob of overlapping circles.
let S = 1;
const size = n => (10 + Math.sqrt(n.data('deg')) * 5) * S;
const cy = cytoscape({container:document.getElementById('cy'), elements:els, wheelSensitivity:0.2,
  style:[
    {selector:'node', style:{'background-color':n=>KIND[n.data('kind')]||'#888', label:'data(label)',
      width:size, height:size, 'font-size':()=>10*S, color:dark?'#ddd':'#222', 'text-valign':'bottom',
      'text-margin-y':()=>3*S, 'text-outline-width':()=>2*S, 'text-outline-color':dark?'#141518':'#f6f5f2',
      'min-zoomed-font-size':7}},
    {selector:'node[kind="scene"]', style:{shape:'round-rectangle'}},
    {selector:'node[kind="prefab"]', style:{shape:'diamond'}},
    {selector:'node[kind="anim"]', style:{shape:'triangle'}},
    {selector:'edge', style:{width:()=>1*S, 'line-color':e=>EDGE[e.data('k')], 'target-arrow-color':e=>EDGE[e.data('k')],
      'target-arrow-shape':'triangle', 'arrow-scale':0.6, 'curve-style':'bezier', opacity:0.35}},
    {selector:'edge[k="inherits"]', style:{'line-style':'dashed', width:()=>1.5*S}},
    {selector:'.faded', style:{opacity:0.07}},
    {selector:'edge.lit', style:{opacity:0.9, width:()=>1.6*S}},
    {selector:'node:selected', style:{'border-width':()=>3*S, 'border-color':dark?'#fff':'#000'}}],
  layout:{name:'cose', animate:false, nodeRepulsion:()=>20000, idealEdgeLength:()=>110, nodeOverlap:20,
          componentSpacing:80, gravity:0.25, numIter:2000}});
let pending = false;
cy.on('zoom', () => {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    const s = cy.zoom() > 1 ? Math.pow(cy.zoom(), -0.85) : 1;
    if (Math.abs(s - S) > 0.01) { S = s; cy.style().update(); }
  });
});
const detail = document.getElementById('detail');
function focus(n){ cy.elements().addClass('faded').removeClass('lit');
  const hood = n.closedNeighborhood(); hood.removeClass('faded'); hood.edges().addClass('lit');
  detail.textContent = n.data('text'); }
cy.on('tap','node', e => focus(e.target));
cy.on('tap', e => { if (e.target === cy){ cy.elements().removeClass('faded lit'); detail.textContent = DATA.stats; }});
function toggles(id, map, attr, sel){ const box = document.getElementById(id);
  Object.keys(map).forEach(k => { const l = document.createElement('label');
    l.innerHTML = `<input type="checkbox" checked><span class="sw" style="background:${map[k]}"></span>${k}`;
    l.querySelector('input').onchange = ev => cy.$(sel + `[${attr}="${k}"]`).style('display', ev.target.checked?'element':'none');
    box.appendChild(l); }); }
toggles('kinds', KIND, 'kind', 'node'); toggles('ekinds', EDGE, 'k', 'edge');
document.getElementById('q').addEventListener('keydown', ev => { if (ev.key !== 'Enter') return;
  const q = ev.target.value.trim().toLowerCase(); if (!q) return;
  const hit = cy.nodes().filter(n => n.data('label').toLowerCase().includes(q)).sort((a,b)=>a.data('label').length-b.data('label').length)[0];
  if (hit){ cy.$(':selected').unselect(); hit.select(); focus(hit); cy.animate({center:{eles:hit}, zoom:1.4}, {duration:300}); }});
</script></body></html>'''


def write_graph_html(p):
    nodes, edges = graph_data(p)
    kinds = Counter(n['kind'] for n in nodes)
    stats = (f'{len(nodes)} nodes, {len(edges)} edges\n' + ', '.join(f'{k}: {v}' for k, v in kinds.most_common()) +
             '\n\nShapes: circle = C# type, square = scene, diamond = prefab, triangle = animator.\n'
             'Edges: uses (code), inherits (dashed), attached (component on object), refs (asset → asset), '
             'names (code names an asset path or shader).')
    data = json.dumps({'nodes': nodes, 'edges': edges, 'stats': stats}, ensure_ascii=False).replace('</', '<\\/')
    title = os.path.basename(p.root)
    html = GRAPH_HTML.replace('__TITLE__', title).replace('__STATS__', stats).replace('__DATA__', data)
    path = os.path.join(p.out, 'graph.html')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(html)
    return path, len(nodes), len(edges)


# ---------------------------------------------------------------- build output

def write_outputs(p):
    out = p.out
    for sub in ('scenes', 'prefabs', 'code', 'data', 'anim'):
        d = os.path.join(out, sub)
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))

    def w(rel, text):
        with open(os.path.join(out, rel), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text + '\n')
    w('MAP.md', project_map(p))
    for path, e in p.files.items():
        if not is_project_asset(p, path):
            continue
        safe = re.sub(r'[\\/:*?"<>|]', '_', path[7:])
        if e.get('ctrl'):
            w(f'anim/{os.path.splitext(safe)[0]}.md', report_controller(p, path))
        if not e['fm']:
            continue
        if path.endswith('.unity'):
            w(f'scenes/{stem(path)}.md', report_file(p, path))
        elif path.endswith('.prefab'):
            w(f'prefabs/{os.path.splitext(safe)[0]}.md', report_file(p, path))
        elif path.endswith('.asset'):
            txt = report_data_asset(p, path)
            if txt:
                w(f'data/{os.path.splitext(safe)[0]}.md', txt)
    by_ns = defaultdict(list)
    for full, t in sorted(p.classes.items()):
        if t['project']:
            by_ns[t['ns'] or 'global'].append(full)
    for ns, fulls in by_ns.items():
        w(f'code/{ns}.md', '\n\n'.join(class_report(p, f) for f in fulls))


# ---------------------------------------------------------------- CLI

def pick_file(p, query, ext):
    cands = [x for x, e in p.files.items() if x.endswith(ext) and e['fm']]
    q = query.lower().removesuffix(ext)
    exact = [x for x in cands if stem(x).lower() == q or x.lower().removesuffix(ext).endswith(q)]
    exact.sort(key=lambda x: (not is_project_asset(p, x), len(x)))
    if exact:
        return exact[0], None
    fuzzy = sorted(x for x in cands if q in x.lower())
    if len(fuzzy) == 1:
        return fuzzy[0], None
    return None, fuzzy


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(prog='unitygraph', description=__doc__.split('\n')[0])
    ap.add_argument('--root', default='.', help='Unity project root (default: search upward from cwd)')
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--version', action='version', version=f'unitygraph {__version__}')
    sp = ap.add_subparsers(dest='cmd', required=True)
    b = sp.add_parser('build', help='refresh the index and write .unitygraph/*.md')
    b.add_argument('--packages', action='store_true', help='rescan Library/PackageCache GUIDs')
    sp.add_parser('map', help='print the project map')
    for name in ('scene', 'prefab'):
        s = sp.add_parser(name, help=f'{name} hierarchy with components and overrides')
        s.add_argument('name')
        s.add_argument('--depth', type=int, help='levels to expand (deeper subtrees show …+N)')
        s.add_argument('--path', help='only the subtree at this GameObject path, e.g. "World Tilt/World"')
        s.add_argument('--full', action='store_true', help='show default/empty fields and all overrides')
    c = sp.add_parser('class', help='C# type: fields, methods, uses, used by, where attached')
    c.add_argument('name')
    wh = sp.add_parser('where', help='where a script class or asset is used')
    wh.add_argument('name')
    r = sp.add_parser('refs', help='what an asset references and what references it')
    r.add_argument('name')
    f = sp.add_parser('find', help='GameObjects whose name matches a regex, across project scenes/prefabs')
    f.add_argument('pattern')
    f.add_argument('--limit', type=int, default=60)
    a = sp.add_parser('asset', help='fields of a ScriptableObject asset')
    a.add_argument('name')
    an = sp.add_parser('anim', help='animator controller: params, layers, states, blend trees, transitions')
    an.add_argument('name')
    sp.add_parser('graph', help='write .unitygraph/graph.html, an interactive graph to open in a browser')
    ca = sp.add_parser('calls', help='call graph: Class.Method (callers and callees), Method, or Class (fan-in)')
    ca.add_argument('name')
    qu = sp.add_parser('query', help='objects whose component fields match, e.g. "Carryable.itemType=Hook" '
                                     'or "Leak.nailsRequired>3, boardedFlow<1"; "Class.field" shows the distribution')
    qu.add_argument('expr')
    qu.add_argument('--limit', type=int, default=60)
    ip = sp.add_parser('input', help='Input System actions: maps, actions, bindings')
    ip.add_argument('name', nargs='?', default='')
    ui = sp.add_parser('ui', help='UI Toolkit: UXML element tree or USS selectors')
    ui.add_argument('name')
    ma = sp.add_parser('material', help='material: shader, textures, colors, unusual floats')
    ma.add_argument('name')
    ad = sp.add_parser('addr', help='Addressables: addresses, groups and labels (optional regex filter)')
    ad.add_argument('pattern', nargs='?', default='')
    d = sp.add_parser('diff', help='semantic diff of scenes/prefabs against a git revision')
    d.add_argument('files', nargs='*', help='scene/prefab names or paths (default: every changed one)')
    d.add_argument('--rev', default='HEAD', help='base revision (default HEAD)')
    d.add_argument('--to', help='target revision (default: working tree)')
    args = ap.parse_args(argv)

    p = Project(find_root(args.root), args.verbose)
    p.refresh(force_packages=getattr(args, 'packages', False))

    if args.cmd == 'build':
        write_outputs(p)
        n_sc = sum(1 for x, e in p.files.items() if x.endswith('.unity') and e['fm'] and is_project_asset(p, x))
        n_pf = sum(1 for x, e in p.files.items() if x.endswith('.prefab') and is_project_asset(p, x))
        n_cl = sum(1 for t in p.classes.values() if t['project'])
        print(f'indexed {len(p.guid2path)} guids, {n_sc} scenes, {n_pf} project prefabs, {n_cl} project types '
              f'→ {OUT_DIR}/MAP.md')
        return
    # keep the written summaries in step with queries when something changed
    if p.changed and os.path.exists(os.path.join(p.out, 'MAP.md')):
        write_outputs(p)
    if args.cmd == 'map':
        print(project_map(p))
    elif args.cmd in ('scene', 'prefab'):
        ext = '.unity' if args.cmd == 'scene' else '.prefab'
        path, cands = pick_file(p, args.name, ext)
        if not path:
            print(f'no {args.cmd} matches "{args.name}"' + (': ' + ', '.join(cands[:20]) if cands else ''))
            return
        print(report_file(p, path, args.depth, args.path, args.full))
    elif args.cmd == 'class':
        full = p.lookup_class(args.name)
        if not full:
            low = args.name.lower()
            sugg = sorted({p.short(k) for k in p.classes if low in k.lower() and p.classes[k]['project']})
            print(f'no type "{args.name}"' + (': did you mean ' + ', '.join(sugg[:15]) if sugg else ''))
            return
        print(class_report(p, full))
    elif args.cmd == 'where':
        print(where_report(p, args.name))
    elif args.cmd == 'refs':
        print(refs_report(p, args.name))
    elif args.cmd == 'find':
        print(find_report(p, args.pattern, args.limit))
    elif args.cmd == 'asset':
        path = resolve_asset(p, args.name if args.name.endswith('.asset') else args.name + '.asset')
        print(report_data_asset(p, path) if path and p.fm(path) else f'no ScriptableObject asset "{args.name}"')
    elif args.cmd == 'graph':
        path, n, m = write_graph_html(p)
        print(f'{n} nodes, {m} edges → {path}')
    elif args.cmd == 'diff':
        from unitygraph import diff
        print(diff.report(p, args.files, args.rev, args.to))
    elif args.cmd == 'calls':
        from unitygraph import calls
        print(calls.report(p, args.name))
    elif args.cmd == 'query':
        from unitygraph import query
        print(query.report(p, args.expr, args.limit))
    elif args.cmd in ('input', 'ui', 'material'):
        from unitygraph import formats
        print(formats.report(p, args.cmd, args.name))
    elif args.cmd == 'addr':
        from unitygraph import formats
        print(formats.report_addr(p, args.pattern))
    elif args.cmd == 'anim':
        q = args.name.lower()
        cands = sorted((x for x, e in p.files.items() if e.get('ctrl')),
                       key=lambda x: (not is_project_asset(p, x), stem(x).lower() != q, len(x)))
        hits = [x for x in cands if q == stem(x).lower() or q in x.lower()]
        print(report_controller(p, hits[0]) if hits else f'no animator controller matches "{args.name}"')


if __name__ == '__main__':  # `python core.py ...`: run as the package so sibling modules share one core
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from unitygraph.core import main as _main
    _main()
