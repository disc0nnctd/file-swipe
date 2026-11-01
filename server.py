#!/usr/bin/env python3
"""File Swipe: swipe-to-sort whole folders/files on a drive. Decisions are only recorded here;
apply.py does the copying/moving, after you review the plan."""
import hashlib, json, os, shutil, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import core, ai

HERE = core.HERE
THUMBS = os.path.join(HERE, 'thumbs'); os.makedirs(THUMBS, exist_ok=True)
STATUS = os.path.join(HERE, 'apply_status.json')
AI_STATE = dict(running=False, done=0, total=0, at=0)
q = core.q


def item_map():
    return {r['path']: core.describe(r) for r in core.items()}


def listing():
    decs = core.decisions()
    sugg = {r['path']: dict(r) for r in q('select * from ai')}
    out = []
    for d in item_map().values():
        d['ai'] = sugg.get(d['p'])
        if d['ai']:
            d['ai']['target'] = core.target(d, 'ai')
        dec = decs.get(d['p'])
        d['action'] = dec and dec['action']; d['target'] = dec and dec['target']
        out.append(d)
    roots = [dict(p=r, mounted=os.path.isdir(r) and bool(os.listdir(r))) for r in core.CFG['roots']]
    return dict(items=out, dests=core.CFG['destinations'], roots=roots, ai=AI_STATE,
                undo=q('select count(*) n from history')[0]['n'])


def set_decision(p, action, override=None, record=True):
    it = item_map().get(p)
    if not it:
        return None
    prev = q('select * from decision where path=?', p)
    prev = dict(prev[0]) if prev else None
    with core.LOCK:
        if action in (None, 'clear'):
            core.CONN.execute('delete from decision where path=?', (p,))
        else:
            core.CONN.execute('insert or replace into decision values(?,?,?,?)',
                              (p, action, core.target(it, action, override), int(time.time())))
        core.CONN.commit()
    if record:
        core.hist_push('set', dict(p=p, prev=prev))
    return prev


def set_expand(p, state):
    prev = q('select state from expand where path=?', p)
    with core.LOCK:
        core.CONN.execute('insert or replace into expand values(?,?)', (p, state)); core.CONN.commit()
    return prev[0]['state'] if prev else None


def undo():
    h = q('select * from history order by id desc limit 1')
    if not h:
        return dict(ok=False)
    h = h[0]; d = json.loads(h['data'])
    with core.LOCK:
        core.CONN.execute('delete from history where id=?', (h['id'],))
        for x in d.get('items', [d]) if h['kind'] in ('set', 'bulk') else []:
            core.CONN.execute('delete from decision where path=?', (x['p'],))
            if x['prev']:
                pv = x['prev']
                core.CONN.execute('insert into decision values(?,?,?,?)', (pv['path'], pv['action'], pv['target'], pv['at']))
        if h['kind'] in ('split', 'merge'):
            if d['prev'] is None:
                core.CONN.execute('delete from expand where path=?', (d['p'],))
            else:
                core.CONN.execute('insert or replace into expand values(?,?)', (d['p'], d['prev']))
            for x in d.get('cleared', []):
                core.CONN.execute('insert or replace into decision values(?,?,?,?)', (x['path'], x['action'], x['target'], x['at']))
        core.CONN.commit()
    return dict(ok=True, kind=h['kind'], p=d.get('p'), n=len(d.get('items', [])), left=q('select count(*) n from history')[0]['n'])


def ai_thread(redo=False):
    if AI_STATE['running']:
        return
    AI_STATE.update(running=True, done=0, total=0)

    def st(a, b):
        AI_STATE.update(done=a, total=b, at=int(time.time()))
    try:
        ai.run(redo, status=st)
    finally:
        AI_STATE['running'] = False


THUMB_LOCK = threading.Lock()


def thumb(p):
    """A preview picture: a frame from the main video, or the first image. None if neither."""
    it = item_map().get(p)
    if not it:
        return None
    c = os.path.join(THUMBS, hashlib.md5(p.encode()).hexdigest() + '.jpg')
    if os.path.exists(c):
        return c if os.path.getsize(c) else None
    files = [p] if not it['dir'] else [os.path.join(p, r) for _, r in it['samples']]
    vid = [f for f in files if core.ext(f) in core.VIDEO]
    img = [f for f in files if core.ext(f) in core.IMAGE]
    with THUMB_LOCK:
        try:
            if vid:
                dur = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', vid[0]],
                                     capture_output=True, text=True, timeout=25).stdout.strip()
                at = float(dur) * .3 if dur and dur != 'N/A' else 60
                subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', str(int(at)), '-i', vid[0], '-frames:v', '1',
                                '-vf', 'scale=640:-2', c], timeout=40)
            elif img:
                from PIL import Image, ImageOps
                im = ImageOps.exif_transpose(Image.open(img[0])); im.thumbnail((640, 640)); im.convert('RGB').save(c, quality=80)
        except Exception:
            pass
        if not os.path.exists(c):
            open(c, 'w').close()        # remember "no preview"
    return c if os.path.getsize(c) else None


def peek(p):
    rows = q('select name, is_dir, size, nfiles from entry where parent=? order by size desc limit 60', p)
    return [dict(r) for r in rows]


def plan():
    its = item_map(); decs = core.decisions(); done = {r['path']: dict(r) for r in q('select * from applied')}
    rows, free = [], {}
    for p, dc in decs.items():
        it = its.get(p)
        if not it:
            continue
        a = done.get(p)
        rows.append(dict(p=p, rel=it['rel'], size=it['size'], action=dc['action'], target=dc['target'],
                         status=a['status'] if a and a['target'] == dc['target'] else None))
    for d in core.CFG['destinations'].values():
        top = d['dir']
        while not os.path.exists(top):
            top = os.path.dirname(top)
        free[d['dir']] = shutil.disk_usage(top).free
    st = json.load(open(STATUS)) if os.path.exists(STATUS) else None
    return dict(rows=rows, free=free, status=st, unsorted=len(its) - len([r for r in rows]))


def start_apply(dry):
    st = json.load(open(STATUS)) if os.path.exists(STATUS) else {}
    if st.get('running') and st.get('pid') and os.path.exists(f"/proc/{st['pid']}"):
        return dict(ok=False, error='already running')
    args = [sys.executable, os.path.join(HERE, 'apply.py')] + (['--dry-run'] if dry else [])
    subprocess.Popen(args, cwd=HERE, start_new_session=True, stdout=open(os.path.join(HERE, 'apply.log'), 'a'),
                     stderr=subprocess.STDOUT)
    return dict(ok=True)


class H(BaseHTTPRequestHandler):
    def log_message(s, *a):
        pass

    def send(s, code, body, ct='application/json', extra=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        s.send_response(code); s.send_header('Content-Type', ct); s.send_header('Content-Length', str(len(body)))
        for k, v in extra:
            s.send_header(k, v)
        s.end_headers(); s.wfile.write(body)

    def do_GET(s):
        u = urlparse(s.path); a = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == '/':
            return s.send(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8',
                          [('Cache-Control', 'no-store')])
        if u.path == '/list':
            return s.send(200, listing())
        if u.path == '/thumb':
            c = thumb(a.get('p', ''))
            return s.send(200, open(c, 'rb').read(), 'image/jpeg', [('Cache-Control', 'max-age=86400')]) if c else s.send(404, b'', 'text/plain')
        if u.path == '/peek':
            return s.send(200, peek(a.get('p', '')))
        if u.path == '/plan':
            return s.send(200, plan())
        s.send(404, b'', 'text/plain')

    def do_POST(s):
        n = int(s.headers.get('Content-Length') or 0)
        d = json.loads(s.rfile.read(n) or b'{}')
        with core.LOCK:
            return s.post(d)

    def post(s, d):
        P = s.path
        if P == '/set':
            act = d.get('action')
            if act not in core.ACTIONS + ('clear',):
                return s.send(400, {})
            set_decision(d['p'], act, d.get('target'))
            return s.send(200, dict(ok=True, undo=q('select count(*) n from history')[0]['n']))
        if P == '/bulk':
            act = d['action']; f = d['folder']
            if act not in core.ACTIONS:
                return s.send(400, {})
            decs = core.decisions()
            todo = [x for x in item_map().values() if x['folder'] == f and (d.get('all') or x['p'] not in decs)]
            items = [dict(p=x['p'], prev=set_decision(x['p'], act, record=False)) for x in todo]
            if items:
                core.hist_push('bulk', dict(items=items, folder=f, action=act))
            return s.send(200, dict(ok=True, n=len(items)))
        if P == '/split':                         # one folder card -> one card per child
            p = d['p']; prev = set_expand(p, 1)
            cleared = [dict(r) for r in q('select * from decision where path=?', p)]
            with core.LOCK:
                core.CONN.execute('delete from decision where path=?', (p,)); core.CONN.commit()
            core.hist_push('split', dict(p=p, prev=prev, cleared=cleared))
            ai_bg()
            return s.send(200, dict(ok=True, n=len(peek(p))))
        if P == '/merge':                         # all cards of a folder -> one card for the folder
            p = d['p']; prev = set_expand(p, 0)
            cleared = [dict(r) for r in q("select * from decision where path like ?", p.rstrip('/') + '/%')]
            with core.LOCK:
                core.CONN.execute("delete from decision where path like ?", (p.rstrip('/') + '/%',)); core.CONN.commit()
            core.hist_push('merge', dict(p=p, prev=prev, cleared=cleared))
            ai_bg()
            return s.send(200, dict(ok=True))
        if P == '/undo':
            return s.send(200, undo())
        if P == '/ai/run':
            threading.Thread(target=ai_thread, args=(bool(d.get('redo')),), daemon=True).start()
            return s.send(200, dict(ok=True))
        if P == '/rescan':
            n = core.index(); ai_bg()
            return s.send(200, dict(ok=True, n=n))
        if P == '/apply/start':
            return s.send(200, start_apply(bool(d.get('dry'))))
        s.send(404, b'', 'text/plain')


def ai_bg():
    threading.Thread(target=ai_thread, daemon=True).start()


if __name__ == '__main__':
    if not q('select count(*) n from entry')[0]['n']:
        core.index()
    ai_bg()
    ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1]) if len(sys.argv) > 1 else core.CFG['port']), H).serve_forever()
