"""Shared state for File Swipe: config, the sqlite db, the drive index, the item list and target paths.

An *item* is one card: a folder or file the user decides about as a unit (a movie folder, a game, a zip).
Folders whose names look like category containers ("Games", "Movies") are split into their children
automatically; the user can split or re-merge any folder from the UI.
"""
import json, os, re, sqlite3, threading, time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, 'config.json')))
DB = os.environ.get('FILESWIPE_DB', os.path.join(HERE, 'fileswipe.db'))
LOCK = threading.RLock()
ACTIONS = ('ai', 'delete', 'archive', 'leave') + tuple(CFG['destinations'])
SKIP = set(CFG['skip_names'])
AUTO_SPLIT = re.compile(CFG['auto_split'], re.I)

VIDEO = {'mkv', 'mp4', 'avi', 'mov', 'm4v', 'wmv', 'ts', 'webm', 'mpg', 'mpeg', 'flv', 'vob', 'm2ts'}
AUDIO = {'mp3', 'flac', 'm4a', 'ogg', 'wav', 'aac', 'wma', 'opus', 'alac'}
IMAGE = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'heic', 'tif', 'tiff'}
DOC = {'pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'txt', 'odt', 'csv', 'ipynb', 'md', 'rtf'}
ARCH = {'zip', 'rar', '7z', 'tar', 'gz', 'iso', 'bin', 'cue'}
EXE = {'exe', 'msi', 'dll', 'apk', 'deb', 'dmg', 'appimage', 'jar', 'bat'}


def db():
    c = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript('''
    pragma journal_mode=wal;
    create table if not exists entry(path text primary key, root text, parent text, name text, is_dir int,
        size int, nfiles int, exts text, samples text, children int, mtime int);
    create index if not exists entry_parent on entry(parent);
    create table if not exists expand(path text primary key, state int);      -- 1 split, 0 keep whole
    create table if not exists decision(path text primary key, action text, target text, at int);
    create table if not exists history(id integer primary key, kind text, data text, at int);
    create table if not exists ai(path text primary key, category text, title text, year int, season int,
        dest text, junk int, reason text, model text, at int);
    create table if not exists applied(path text primary key, action text, target text, status text,
        detail text, at int);
    ''')
    return c


CONN = db()


def q(sql, *a):
    with LOCK:
        return CONN.execute(sql, a).fetchall()


def ext(name):
    return name.rsplit('.', 1)[-1].lower() if '.' in name[1:] else ''


# ---------------------------------------------------------------- index
def index(progress=None):
    """Walk every root bottom-up and store per-entry recursive stats. Metadata only; no file contents read."""
    rows, done = [], []
    for root in CFG['roots']:
        if not os.path.isdir(root) or not os.listdir(root):
            continue      # drive not mounted: keep its old index rather than wiping it
        done.append(root)
        agg = {}   # dir -> [size, nfiles, Counter(ext->bytes), [(size, rel)]]
        for d, dirs, files in os.walk(root, topdown=False, onerror=lambda e: None):
            if os.path.basename(d) in SKIP and d != root:
                continue
            size, n, ex, big = 0, 0, Counter(), []
            for f in files:
                p = os.path.join(d, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                e = ext(f)
                size += st.st_size; n += 1; ex[e] += st.st_size; big.append((st.st_size, f))
                if d != root or f not in SKIP:
                    rows.append((p, root, d, f, 0, st.st_size, 1, None, None, 0, int(st.st_mtime)))
            for sub in dirs:
                sp = os.path.join(d, sub)
                if sp not in agg:
                    continue
                s2, n2, e2, b2 = agg.pop(sp)
                size += s2; n += n2; ex.update(e2)
                big += [(b, sub + '/' + r) for b, r in b2]
            big = sorted(big, reverse=True)[:10]
            agg[d] = [size, n, ex, big]
            try:
                mt = int(os.lstat(d).st_mtime)
            except OSError:
                mt = 0
            rows.append((d, root, os.path.dirname(d) if d != root else None, os.path.basename(d) or d, 1, size, n,
                         json.dumps(dict(ex.most_common(8))), json.dumps([[b, r] for b, r in big]),
                         len(dirs) + len(files), mt))
            if progress and len(rows) % 5000 < 50:
                progress(len(rows))
    with LOCK:
        for root in done:
            CONN.execute('delete from entry where root=?', (root,))
        CONN.executemany('insert or replace into entry values(?,?,?,?,?,?,?,?,?,?,?)', rows)
        CONN.commit()
    return len(rows)


# ---------------------------------------------------------------- items
def expanded():
    return {r['path']: r['state'] for r in q('select * from expand')}


def is_split(row, exp):
    if row['path'] in exp:
        return bool(exp[row['path']])
    return row['parent'] is None or bool(AUTO_SPLIT.match(row['name'].strip()))


def items():
    """The current cards: walk down from the roots through split folders."""
    with LOCK:
        exp = expanded()
        out = []
        kids = {}
        for r in CONN.execute('select * from entry'):
            kids.setdefault(r['parent'], []).append(r)
        stack = [r for r in kids.get(None, [])]
        while stack:
            r = stack.pop()
            if r['is_dir'] and is_split(r, exp) and kids.get(r['path']):
                stack += kids[r['path']]
            elif r['parent'] is not None and r['name'] not in SKIP:
                out.append(r)
        return sorted(out, key=lambda r: r['path'].lower())


def describe(r):
    d = dict(p=r['path'], root=r['root'], name=r['name'], dir=bool(r['is_dir']), size=r['size'], nfiles=r['nfiles'],
             rel=os.path.relpath(r['path'], r['root']), folder=os.path.relpath(r['parent'], r['root']))
    if d['folder'] == '.':
        d['folder'] = '(top level)'
    if r['is_dir']:
        d['exts'] = json.loads(r['exts'] or '{}'); d['samples'] = json.loads(r['samples'] or '[]')
        d['children'] = r['children']
    else:
        d['exts'] = {ext(r['name']): r['size']}; d['samples'] = []
    d['kind'] = kind_of(d['exts'])
    return d


def kind_of(exts):
    tot = sum(exts.values()) or 1
    share = lambda S: sum(v for k, v in exts.items() if k in S) / tot
    for k, S in (('video', VIDEO), ('audio', AUDIO), ('image', IMAGE), ('doc', DOC)):
        if share(S) > .6:
            return k
    if share(ARCH | EXE) > .5:
        return 'package'
    return 'mixed'


# ---------------------------------------------------------------- targets
BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def clean(name):
    name = BAD.sub(' ', name)
    return re.sub(r'\s+', ' ', name).strip(' .') or 'unnamed'


def suggestion(path):
    r = q('select * from ai where path=?', path)
    return dict(r[0]) if r else None


def target(it, action, override=None):
    """Destination folder for an item. A folder's *contents* land in it; a file lands inside it."""
    if action in ('delete', 'leave'):
        return None
    ai = suggestion(it['p']) or {}
    if action == 'ai':
        action = ai.get('dest') or 'archive'
    dest = CFG['destinations'][action]['dir']
    if it['p'] == dest or it['p'].startswith(dest.rstrip('/') + '/'):
        return it['p'] if it['dir'] else os.path.dirname(it['p'])    # already where it belongs: stays put
    base = it['name'] if it['dir'] else os.path.splitext(it['name'])[0]
    if override:
        return os.path.join(dest, *[clean(x) for x in override.split('/') if x.strip()])   # 'Show/Season 3' allowed
    if CFG['destinations'][action].get('keep_path'):
        rel = it['rel'] if it['dir'] else os.path.dirname(it['rel'])
        return os.path.join(dest, *[clean(x) for x in rel.split(os.sep) if x])
    if action == 'movies':
        t = clean(re.sub(r'\s*[\(\[]\d{4}[\)\]]\s*$', '', ai.get('title') or base))   # no "(2012) (2012)"
        return os.path.join(dest, f"{t} ({ai['year']})" if ai.get('year') else t)
    if action == 'shows':
        t = os.path.join(dest, clean(ai.get('title') or base))
        return os.path.join(t, f"Season {ai['season']}") if ai.get('season') else t
    if not it['dir']:
        return dest
    return os.path.join(dest, clean(it['name']))


def decisions():
    return {r['path']: dict(r) for r in q('select * from decision')}


def hist_push(kind, data):
    with LOCK:
        CONN.execute('insert into history(kind,data,at) values(?,?,?)', (kind, json.dumps(data), int(time.time())))
        CONN.commit()
