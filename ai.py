"""Suggest where each item goes. Rules give an instant first guess; the local LLM (llama.cpp on :8084) then
reads names, sizes and file lists in batches and fills in category, clean title/year/season and a junk hint.
Only metadata is sent, and only to the local model.

  python3 ai.py            # classify items that have no LLM suggestion yet
  python3 ai.py --redo     # classify everything again
"""
import json, os, re, sys, time, urllib.request
import core

CATS = list(core.CFG['destinations'])
SXE = re.compile(r'\bS(\d{1,2})(?:E\d{1,3})?\b|\bSeason[ ._-]?(\d{1,2})\b', re.I)
YEAR = re.compile(r'(?<!\d)(19[3-9]\d|20[0-3]\d)(?!\d)')
GAME = re.compile(r'fitgirl|repack|reloaded|codex|skidrow|plaza|hoodlum|r\.g\.|dodi|gog|nosteam|3dm|cracked|pcgames', re.I)
TRADE = re.compile(r'backtest|nifty|banknifty|options?|trading|ohlc|zerodha|kite', re.I)
JUNK = re.compile(r'^\$recycle\.bin$|^found\.\d+$|^seagate$|\.ico$|\.tmp$', re.I)


def rules(d):
    """Fast first guess, used until the LLM has answered (and as its fallback)."""
    n, k = d['name'], d['kind']
    title, year = guess_title(n)
    s = SXE.search(n)
    out = dict(category='archive', title=title, year=year, season=None, junk=0, reason='rule: no strong signal')
    if JUNK.search(n):
        out.update(junk=1, reason='rule: system/vendor leftovers')
    elif GAME.search(n) or (d['dir'] and any(r.lower().endswith(('setup.exe', 'steam_api.dll', 'steam_api64.dll'))
                                                 for _, r in d['samples'])):
        out.update(category='games', reason='rule: game release/installer markers')
    elif k == 'video':
        if s or d['nfiles'] > 6:
            out.update(category='shows', season=int(s.group(1) or s.group(2)) if s else None, reason='rule: episodes')
        else:
            out.update(category='movies', reason='rule: one big video')
    elif k == 'audio':
        out.update(category='music', reason='rule: mostly audio')
    elif TRADE.search(n):
        out.update(category='trading', reason='rule: trading words in name')
    elif k == 'image':
        out.update(category='photos', reason='rule: mostly images')
    elif k == 'doc':
        out.update(category='documents', reason='rule: mostly documents')
    elif k == 'package':
        out.update(category='software', reason='rule: installers/archives')
    return out


def guess_title(n):
    base = re.sub(r'\.(mkv|mp4|avi|mov|m4v|wmv|srt)$', '', n, flags=re.I)
    y = YEAR.search(base)
    cut = min([m.start() for m in [y, SXE.search(base), re.search(r'\b(720p|1080p|2160p|brrip|bluray|web-?dl|webrip|dvd|x26[45]|hdrip)\b', base, re.I)] if m] or [len(base)])
    t = re.sub(r'[._]+', ' ', base[:cut])
    t = re.sub(r'[\[\(].*?[\]\)]', '', t).strip(' -[(')
    return (t or base), (int(y.group(1)) if y else None)


SYSTEM = f"""You sort the contents of an old external hard drive. For each item you get its path, size, number of
files, the main file types and the largest files. Decide where it belongs.

Categories (pick exactly one):
- movies: one feature film (a video file or a folder with one main video + subtitles)
- shows: TV series, anime, documentary series, season packs, episode batches
- games: PC games, game installers, repacks (FitGirl, RELOADED, R.G. ...), game zips/rars
- music: songs and albums
- trading: market data, backtests, trading notebooks and spreadsheets
- photos: personal photos, pictures, wallpapers, wallpaper engine content, comics (they go on to a photo sorter)
- documents: personal papers, school/college/exam files, reports, phone/laptop backups
- software: application installers, OS ISOs, tools, cracks, programming setups
- archive: anything else worth keeping as-is

Also give:
- title: clean human name. Movies: the official film title (e.g. "Deadpool 2", no edition/cut/quality words); shows: the series name
  (e.g. "The Office (US)", "Big Mouth"); others: a short readable name.
- year: release year for movies if you know it or it is in the name, else null.
- season: season number if the item is a single season of a show, else null.
- junk: true only if it is clearly worthless (Windows recycle bin, chkdsk fragments with no value, vendor
  promo files, empty folders, temp files). Old installers or cracks are NOT junk by themselves.
- reason: at most 10 words.

Reply with JSON only: {{"items":[{{"id":1,"category":"...","title":"...","year":null,"season":null,"junk":false,"reason":"..."}}]}}"""


def card(i, d):
    mb = d['size'] / 2**20
    ex = ', '.join(f"{k or 'noext'} {v / 2**20:.0f}MB" for k, v in list(d['exts'].items())[:5])
    big = '; '.join(r for _, r in d['samples'][:6])
    return f"#{i} {'folder' if d['dir'] else 'file'}: {d['rel']}\n   {mb:.0f} MB, {d['nfiles']} files; types: {ex}" + \
        (f"\n   largest: {big}" if big else '')


def ask(batch):
    body = dict(messages=[dict(role='system', content=SYSTEM),
                          dict(role='user', content='\n'.join(card(i + 1, d) for i, d in enumerate(batch)))],
                temperature=0.1, max_tokens=180 * len(batch), response_format=dict(type='json_object'))
    req = urllib.request.Request(core.CFG['llm_url'], json.dumps(body).encode(), {'Content-Type': 'application/json'})
    txt = json.load(urllib.request.urlopen(req, timeout=600))['choices'][0]['message']['content']
    txt = txt[txt.find('{'):txt.rfind('}') + 1]
    return {int(x['id']): x for x in json.loads(txt).get('items', []) if str(x.get('id', '')).isdigit()}


def save(path, r, model):
    cat = r.get('category') if r.get('category') in CATS else 'archive'
    yr = r.get('year'); se = r.get('season')
    yr = int(yr) if str(yr).isdigit() and 1900 < int(yr) < 2100 else None
    se = int(se) if str(se).isdigit() and 0 < int(se) < 100 else None
    with core.LOCK:
        core.CONN.execute('insert or replace into ai values(?,?,?,?,?,?,?,?,?,?)',
                          (path, cat, (r.get('title') or '').strip()[:120] or None, yr, se, cat,
                           1 if r.get('junk') in (True, 1, 'true') else 0, str(r.get('reason') or '')[:160], model,
                           int(time.time())))
        core.CONN.commit()


def run(redo=False, batch=6, status=None):
    its = [core.describe(r) for r in core.items()]
    have = {r['path']: r['model'] for r in core.q('select path, model from ai')}
    for d in its:                                   # instant rule guesses for anything new
        if d['p'] not in have:
            save(d['p'], rules(d), 'rules')
    todo = [d for d in its if redo or have.get(d['p'], 'rules') == 'rules']
    for k in range(0, len(todo), batch):
        b = todo[k:k + batch]
        if status:
            status(k, len(todo))
        try:
            res = ask(b)
        except Exception as e:
            print('LLM batch failed:', e, file=sys.stderr)
            continue
        for i, d in enumerate(b):
            if i + 1 in res:
                r = res[i + 1]
                if rules(d)['junk']:
                    r['junk'] = True        # rules know the Windows/vendor leftovers better than the model
                save(d['p'], r, 'llm')
    if status:
        status(len(todo), len(todo))
    return len(todo)


if __name__ == '__main__':
    t = time.time()
    n = run('--redo' in sys.argv, status=lambda a, b: print(f'{a}/{b}  {time.time() - t:.0f}s', flush=True))
    print('classified', n)
