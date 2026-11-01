#!/usr/bin/env python3
"""Carry out the swipe decisions.  python3 apply.py --dry-run   first, then   python3 apply.py

- Kept items are COPIED to their target with rsync when the target is on another drive (the source stays
  as it was), or MOVED with a rename when it is on the same drive.
- Delete items go to <drive>/_fileswipe_trash/ (recoverable), never straight to deletion. On a read-only drive
  (any read-only source) nothing can be deleted; those items are just not copied.
- Order: irreplaceable personal stuff first, re-downloadable media and games last, in case the drive dies mid-way.
Progress goes to apply_status.json (shown in the app) and apply.log.
"""
import json, os, re, shutil, subprocess, sys, time
import core

DRY = '--dry-run' in sys.argv
ONLY = next((x.split('=', 1)[1].split(',') for x in sys.argv if x.startswith('--only=')), None)   # e.g. --only=movies,shows
STATUS = os.path.join(core.HERE, 'apply_status.json')
ORDER = ['documents', 'trading', 'photos', 'music', 'archive', 'software', 'shows', 'movies', 'games']
st = dict(running=True, pid=os.getpid(), dry=DRY, started=int(time.time()), current=None, done_bytes=0,
          total_bytes=0, ok=0, failed=0, skipped=0, errors=[], lines=[])


def save():
    tmp = STATUS + '.tmp'
    json.dump(st, open(tmp, 'w')); os.replace(tmp, STATUS)


def log(m):
    print(time.strftime('%H:%M:%S'), m, flush=True)
    st['lines'] = (st['lines'] + [m])[-40:]


def dev(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return os.stat(path).st_dev


def existing(path):
    while not os.path.exists(path):
        path = os.path.dirname(path)
    return path


def readonly(path):
    return bool(os.statvfs(path).f_flag & os.ST_RDONLY)


def mark(p, action, target, status, detail=''):
    with core.LOCK:
        core.CONN.execute('insert or replace into applied values(?,?,?,?,?,?)', (p, action, target, status, detail[:2000], int(time.time())))
        core.CONN.commit()
    st[{'done': 'ok', 'failed': 'failed', 'skipped': 'skipped'}[status]] += 1
    if status == 'failed':
        st['errors'] = (st['errors'] + [f"{os.path.basename(p)}: {detail[:300]}"])[-20:]


def rsync(src, target, is_dir, base):
    os.makedirs(target, exist_ok=True)
    cmd = ['rsync', '-rt', '--no-perms', '--no-owner', '--no-group', '--modify-window=2', '--partial',
           '--info=progress2', '--no-inc-recursive', (src.rstrip('/') + '/') if is_dir else src, target.rstrip('/') + '/']
    pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    last = 0
    for line in iter(lambda: pr.stdout.read(200), ''):
        m = re.findall(r'([\d,]+)\s+\d+%', line)
        if m and time.time() - last > 2:
            st['done_bytes'] = base + int(m[-1].replace(',', '')); save(); last = time.time()
    err = pr.stderr.read(); pr.wait()
    return pr.returncode, err


def move_same(src, target, is_dir):
    if not is_dir:
        os.makedirs(target, exist_ok=True)
        dst = os.path.join(target, os.path.basename(src))
        if os.path.exists(dst):
            raise RuntimeError(f'already exists: {dst}')
        os.rename(src, dst); return
    if not os.path.exists(target):
        os.makedirs(os.path.dirname(target), exist_ok=True); os.rename(src, target); return
    for c in os.listdir(src):                     # merge into an existing folder, never overwrite
        if os.path.exists(os.path.join(target, c)):
            raise RuntimeError(f'already exists: {os.path.join(target, c)}')
    for c in os.listdir(src):
        os.rename(os.path.join(src, c), os.path.join(target, c))
    os.rmdir(src)


def main():
    its = {r['path']: core.describe(r) for r in core.items()}
    done = {r['path']: r['target'] for r in core.q("select * from applied where status='done'")}
    jobs = []
    for p, dc in core.decisions().items():
        it = its.get(p)
        if ONLY and not any(dc['target'] and dc['target'].startswith(core.CFG['destinations'][k]['dir']) for k in ONLY):
            continue
        if not it or dc['action'] == 'leave' or (p in done and done[p] == dc['target']):
            continue
        if not os.path.exists(p):
            continue
        if dc['target'] and dc['target'] == (p if it['dir'] else os.path.dirname(p)):
            if not DRY:
                mark(p, dc['action'], dc['target'], 'done', 'already in place')
            log(f"stays: {it['rel']}")
            continue
        key = 'delete' if dc['action'] == 'delete' else next((k for k, d in core.CFG['destinations'].items()
                                                                if dc['target'].startswith(d['dir'])), 'archive')
        jobs.append((ORDER.index(key) if key in ORDER else -1, p, it, dc))
    jobs.sort(key=lambda j: (j[0], j[1].lower()))
    copies = [j for j in jobs if j[3]['action'] != 'delete' and dev(j[1]) != dev(j[3]['target'])]
    st['total_bytes'] = sum(j[2]['size'] for j in copies)
    need = {}
    for j in copies:
        need[dev(j[3]['target'])] = need.get(dev(j[3]['target']), 0) + j[2]['size']
    for d, b in need.items():
        tgt = next(j[3]['target'] for j in copies if dev(j[3]['target']) == d)
        top = tgt
        while not os.path.exists(top):
            top = os.path.dirname(top)
        free = shutil.disk_usage(top).free
        log(f'target drive {top}: needs {b / 1e9:.1f} GB, free {free / 1e9:.1f} GB')
        if b > free * .98:
            st['errors'].append(f'Not enough space on {top}: need {b / 1e9:.1f} GB, free {free / 1e9:.1f} GB')
            st['running'] = False; save(); return
    log(f"{'DRY RUN: ' if DRY else ''}{len(jobs)} items, {st['total_bytes'] / 1e9:.1f} GB to copy")
    save()
    base = 0
    for _, p, it, dc in jobs:
        st['current'] = it['rel']; save()
        if dc['action'] == 'delete':
            if readonly(p):
                log(f'skip delete (drive is read-only): {it["rel"]}')
                if not DRY:
                    mark(p, 'delete', None, 'skipped', 'read-only drive: not copied, gone when the drive is wiped')
                continue
            trash = os.path.join(it['root'], '_fileswipe_trash', it['rel'])
            log(f'trash: {it["rel"]} -> {trash}')
            if not DRY:
                try:
                    os.makedirs(os.path.dirname(trash), exist_ok=True); os.rename(p, trash)
                    mark(p, 'delete', trash, 'done')
                except Exception as e:
                    mark(p, 'delete', trash, 'failed', str(e))
            continue
        if readonly(existing(dc['target'])):
            log(f"⚠ can't put {it['rel']} on a read-only drive: {dc['target']}")
            if not DRY:
                mark(p, dc['action'], dc['target'], 'failed', 'destination drive is read-only')
            continue
        same = dev(p) == dev(dc['target'])
        log(f"{'move' if same else 'copy'} {it['size'] / 1e9:.2f} GB  {it['rel']}  ->  {dc['target']}")
        if DRY:
            continue
        try:
            if same:
                move_same(p, dc['target'], it['dir']); mark(p, dc['action'], dc['target'], 'done')
            else:
                rc, err = rsync(p, dc['target'], it['dir'], base)
                base += it['size']; st['done_bytes'] = base
                if rc == 0:
                    mark(p, dc['action'], dc['target'], 'done')
                else:
                    mark(p, dc['action'], dc['target'], 'failed', f'rsync exit {rc}: ' + err.strip()[-1500:])
                    log(f'  ⚠ rsync exit {rc} (unreadable files are listed in the log)')
                    print(err, flush=True)
        except Exception as e:
            mark(p, dc['action'], dc['target'], 'failed', str(e)); log(f'  ⚠ {e}')
        save()
    st.update(running=False, current=None, finished=int(time.time()))
    log('finished' + (' (dry run, nothing changed)' if DRY else ''))
    save()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        st.update(running=False); st['errors'].append(repr(e)); save(); raise
