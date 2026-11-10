"""
MiniCut — banque de médias libres (Pexels, Pixabay, Jamendo).

Les clés API restent côté serveur (fichier .env à côté de server.py) ; le
navigateur ne parle qu'à ces routes :

  GET /api/stock/status                       sources configurées
  GET /api/stock/search?source=&kind=&q=&orientation=&page=
                                              photos / vidéos normalisées
  GET /api/stock/music?q=&page=               musiques Jamendo (Creative Commons)
  GET /api/stock/fetch?url=                   téléchargement proxifié (évite CORS),
                                              limité aux domaines des banques
"""
import json
import os
import time
import urllib.parse
import urllib.request
from flask import Blueprint, Response, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = 'MiniCut/1.0 (local video editor)'
MAX_FETCH = 400 * 1024 * 1024   # 400 Mo par média

bp = Blueprint('stock', __name__)


def _load_env():
    """Lit .env (KEY=valeur) sans dépendance ; les variables d'environnement
    déjà définies sont prioritaires."""
    env = {}
    p = os.path.join(HERE, '.env')
    if os.path.exists(p):
        with open(p, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ('PEXELS_API_KEY', 'PIXABAY_API_KEY', 'JAMENDO_CLIENT_ID'):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _key(name):
    return _load_env().get(name, '')


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def _err(msg, code=400):
    return jsonify(error=msg), code


# ---------------------------------------------------------------- status
@bp.route('/api/stock/status')
def status():
    return jsonify(pexels=bool(_key('PEXELS_API_KEY')),
                   pixabay=bool(_key('PIXABAY_API_KEY')),
                   jamendo=bool(_key('JAMENDO_CLIENT_ID')))


# ---------------------------------------------------------------- Pexels
def _pexels_best_video(files, target_h):
    """Choisit le fichier MP4 le plus proche de la résolution visée (sans
    descendre en dessous si possible, plafonné pour limiter le poids)."""
    mp4 = [f for f in files if (f.get('file_type') or '').endswith('mp4')
           and f.get('width') and f.get('height')]
    if not mp4:
        return None
    short = lambda f: min(f['width'], f['height'])
    ok = [f for f in mp4 if target_h <= short(f) <= 2160]
    if ok:
        return min(ok, key=short)
    return max(mp4, key=short)


def pexels_search(kind, q, orientation, page, target_h):
    key = _key('PEXELS_API_KEY')
    if not key:
        raise RuntimeError('PEXELS_API_KEY manquante dans .env')
    params = {'query': q, 'per_page': 24, 'page': page}
    if orientation:
        params['orientation'] = orientation
    base = ('https://api.pexels.com/videos/search' if kind == 'video'
            else 'https://api.pexels.com/v1/search')
    data = _get_json(base + '?' + urllib.parse.urlencode(params),
                     {'Authorization': key})
    out = []
    if kind == 'video':
        for v in data.get('videos', []):
            f = _pexels_best_video(v.get('video_files', []), target_h)
            if not f:
                continue
            user = v.get('user') or {}
            out.append({'id': f"pexels-v{v['id']}", 'source': 'Pexels', 'kind': 'video',
                        'thumb': v.get('image'), 'url': f['link'],
                        'width': f['width'], 'height': f['height'],
                        'duration': v.get('duration'),
                        'author': user.get('name', ''), 'page': v.get('url', '')})
    else:
        for p in data.get('photos', []):
            src = p.get('src') or {}
            big = src.get('original', '')
            if big:
                big += ('&' if '?' in big else '?') + 'auto=compress&cs=tinysrgb&w=2400'
            out.append({'id': f"pexels-p{p['id']}", 'source': 'Pexels', 'kind': 'photo',
                        'thumb': src.get('medium') or src.get('small'), 'url': big or src.get('large2x'),
                        'width': p.get('width'), 'height': p.get('height'),
                        'author': p.get('photographer', ''), 'page': p.get('url', '')})
    return out


# ---------------------------------------------------------------- Pixabay
def pixabay_search(kind, q, orientation, page, target_h):
    key = _key('PIXABAY_API_KEY')
    if not key:
        raise RuntimeError('PIXABAY_API_KEY manquante dans .env')
    # les vidéos verticales sont rares : on en demande davantage puis on filtre
    per = 200 if (kind == 'video' and orientation in ('landscape', 'portrait')) else 24
    params = {'key': key, 'q': q[:100], 'per_page': per, 'page': page, 'safesearch': 'true'}
    if kind == 'video':
        url = 'https://pixabay.com/api/videos/?' + urllib.parse.urlencode(params)
    else:
        params['image_type'] = 'photo'
        if orientation in ('landscape', 'portrait'):
            params['orientation'] = 'horizontal' if orientation == 'landscape' else 'vertical'
        url = 'https://pixabay.com/api/?' + urllib.parse.urlencode(params)
    data = _get_json(url)
    out = []
    for h in data.get('hits', []):
        if kind == 'video':
            vids = h.get('videos') or {}
            cands = [vids[k] for k in ('tiny', 'small', 'medium', 'large')
                     if vids.get(k, {}).get('url')]
            if not cands:
                continue
            short = lambda f: min(f.get('width') or 0, f.get('height') or 0)
            ok = [f for f in cands if short(f) >= target_h]
            f = min(ok, key=short) if ok else max(cands, key=short)
            w, hh = f.get('width') or 0, f.get('height') or 0
            if orientation == 'portrait' and w > hh:
                continue
            if orientation == 'landscape' and hh > w:
                continue
            out.append({'id': f"pixabay-v{h['id']}", 'source': 'Pixabay', 'kind': 'video',
                        'thumb': f.get('thumbnail') or vids.get('tiny', {}).get('thumbnail'),
                        'url': f['url'], 'width': w, 'height': hh,
                        'duration': h.get('duration'),
                        'author': h.get('user', ''), 'page': h.get('pageURL', '')})
            if len(out) >= 24:
                break
        else:
            out.append({'id': f"pixabay-p{h['id']}", 'source': 'Pixabay', 'kind': 'photo',
                        'thumb': h.get('webformatURL'), 'url': h.get('largeImageURL'),
                        'width': h.get('imageWidth'), 'height': h.get('imageHeight'),
                        'author': h.get('user', ''), 'page': h.get('pageURL', '')})
    return out


@bp.route('/api/stock/search')
def search():
    source = request.args.get('source', 'pexels')
    kind = 'video' if request.args.get('kind') == 'video' else 'photo'
    q = (request.args.get('q') or '').strip()
    orientation = request.args.get('orientation', '')
    if orientation not in ('', 'landscape', 'portrait', 'square'):
        orientation = ''
    page = max(1, int(request.args.get('page', 1) or 1))
    target_h = int(request.args.get('target', 1080) or 1080)
    if not q:
        return _err('Tape un mot-clé')
    try:
        fn = pixabay_search if source == 'pixabay' else pexels_search
        return jsonify(results=fn(kind, q, orientation, page, target_h))
    except RuntimeError as e:
        return _err(str(e))
    except urllib.error.HTTPError as e:
        return _err(f'{source} a répondu HTTP {e.code}', 502)
    except Exception as e:
        return _err(f'{source} : {e}', 502)


# ---------------------------------------------------------------- Jamendo
@bp.route('/api/stock/music')
def music():
    cid = _key('JAMENDO_CLIENT_ID')
    if not cid:
        return _err('JAMENDO_CLIENT_ID manquant dans .env')
    q = (request.args.get('q') or '').strip()
    page = max(1, int(request.args.get('page', 1) or 1))
    params = {'client_id': cid, 'format': 'json', 'limit': 20,
              'offset': (page - 1) * 20,
              'include': 'licenses', 'order': 'popularity_total',
              'vocalinstrumental': request.args.get('vocal', 'instrumental')}
    if request.args.get('nonc') == '1':      # exclut les licences « non commerciales »
        params['ccnc'] = 'false'
    # L'API Jamendo renvoie parfois 0 résultat pour une requête qui en a
    # (instabilité côté serveur) : on réessaie, puis on élargit la recherche.
    variants = [{}]
    if q:
        tags = '+'.join(q.lower().split())
        variants = [{'fuzzytags': tags}, {'search': q},
                    {'fuzzytags': tags, 'vocalinstrumental': None}]
    data = {}
    try:
        for v in variants:
            pv = {**params, **v}
            pv = {k: val for k, val in pv.items() if val is not None}
            for attempt in range(3):
                data = _get_json('https://api.jamendo.com/v3.0/tracks/?'
                                 + urllib.parse.urlencode(pv))
                if data.get('results'):
                    break
                time.sleep(0.4)
            if data.get('results'):
                break
    except Exception as e:
        return _err(f'Jamendo : {e}', 502)
    out = []
    for t in data.get('results', []):
        if not t.get('audio'):
            continue
        out.append({'id': f"jamendo-{t['id']}", 'source': 'Jamendo', 'kind': 'audio',
                    'name': t.get('name', ''), 'author': t.get('artist_name', ''),
                    'duration': t.get('duration'),
                    # MP3 haute qualité si l'artiste l'autorise, sinon flux standard
                    'url': (t.get('audiodownload') if t.get('audiodownload_allowed')
                            and t.get('audiodownload') else t['audio']),
                    'preview': t['audio'],
                    'thumb': t.get('image') or t.get('album_image'),
                    'license': t.get('license_ccurl', ''), 'page': t.get('shareurl', '')})
    return jsonify(results=out)


# ---------------------------------------------------------------- proxy
ALLOWED_HOSTS = ('pexels.com', 'pixabay.com', 'jamendo.com')


def _host_ok(url):
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return False
    h = (u.hostname or '').lower()
    return u.scheme == 'https' and any(h == d or h.endswith('.' + d) for d in ALLOWED_HOSTS)


@bp.route('/api/stock/fetch')
def fetch():
    url = request.args.get('url', '')
    if not _host_ok(url):
        return _err('URL non autorisée', 403)
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA}),
                                   timeout=60)
    except Exception as e:
        return _err(f'Téléchargement impossible : {e}', 502)
    if not _host_ok(r.geturl()):          # redirection hors des banques autorisées
        r.close()
        return _err('Redirection non autorisée', 403)
    size = int(r.headers.get('Content-Length') or 0)
    if size > MAX_FETCH:
        r.close()
        return _err('Fichier trop lourd', 413)
    ctype = r.headers.get('Content-Type', 'application/octet-stream')

    def gen():
        sent = 0
        try:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                sent += len(chunk)
                if sent > MAX_FETCH:
                    break
                yield chunk
        finally:
            r.close()

    headers = {'Cache-Control': 'no-store'}
    if size:
        headers['Content-Length'] = str(size)
    return Response(gen(), mimetype=ctype, headers=headers)
