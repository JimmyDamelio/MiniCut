#!/usr/bin/env python3
"""
MiniCut Studio — serveur local de rendu vidéo via FFmpeg.

Sert l'interface (index.html) et expose /render qui reçoit les photos,
l'audio et les durées, puis génère un vrai MP4 H.264 haute qualité.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from flask import Flask, request, send_file, send_from_directory, jsonify, after_this_request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765

from stock import bp as stock_bp

app = Flask(__name__)
app.register_blueprint(stock_bp)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2 Go


@app.route('/')
def index():
    return send_from_directory(HERE, 'index.html')


FPS = 30
KB_AMT = 0.12          # amplitude du Ken Burns (12 %)
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

# transitions xfade proposées dans l'interface (le preview les reproduit)
TRANSITIONS = {'fade', 'fadeblack', 'slideleft', 'slideup', 'wipeleft',
               'circleopen', 'zoomin'}
# qualité d'export -> (preset x264, CRF)
QUALITY = {'draft': ('veryfast', 26), 'standard': ('medium', 20),
           'high': ('slow', 17)}
TEXT_SIZES = {'s': 0.045, 'm': 0.06, 'l': 0.08}


def _kb_filter(kb, w, h, frames, fps=FPS):
    """Renvoie le segment de filtre Ken Burns (zoompan) ou None.

    L'image est pré-agrandie 2x pour éviter le tremblement de zoompan, puis
    zoompan recadre en WxH. Les expressions utilisent `on` (numéro de frame de
    sortie 0..frames-1), ce qui reproduit l'animation linéaire du preview.
    """
    if kb in (None, '', 'none'):
        return None
    fr = max(1, frames - 1)
    cx = 'iw/2-(iw/zoom/2)'
    cy = 'ih/2-(ih/zoom/2)'
    if kb == 'zoomIn':
        z, x, y = f'1+{KB_AMT}*on/{fr}', cx, cy
    elif kb == 'zoomOut':
        z, x, y = f'{1+KB_AMT}-{KB_AMT}*on/{fr}', cx, cy
    elif kb == 'panR':
        z, x, y = f'{1+KB_AMT}', f'(iw-iw/zoom)*on/{fr}', cy
    elif kb == 'panL':
        z, x, y = f'{1+KB_AMT}', f'(iw-iw/zoom)*(1-on/{fr})', cy
    else:
        return None
    return (f'scale={2*w}:{2*h},'
            f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps}")


def _hex_color(c, default='FFFFFF'):
    c = (c or '').lstrip('#')
    return c.upper() if len(c) == 6 and all(ch in '0123456789abcdefABCDEF' for ch in c) else default


def _drawtext(textfile, pos, h, size='m', style='outline', color='#ffffff'):
    """Segment drawtext centré horizontalement, lecture du texte depuis un
    fichier (gère accents / guillemets / sauts de ligne sans échappement).
    style : outline (contour noir) | box (bandeau sombre) | shadow (ombre portée)."""
    fs = max(12, int(h * TEXT_SIZES.get(size, 0.06)))
    if pos == 'top':
        y = f'{int(h * 0.10)}'
    elif pos == 'center':
        y = '(h-text_h)/2'
    else:  # bottom
        y = f'h-text_h-{int(h * 0.10)}'
    if style == 'box':
        deco = f'box=1:boxcolor=black@0.55:boxborderw={max(4, int(fs * 0.35))}'
    elif style == 'shadow':
        so = max(1, int(fs * 0.06))
        deco = f'shadowcolor=black@0.7:shadowx={so}:shadowy={so}'
    else:
        deco = f'borderw={max(1, int(fs * 0.12))}:bordercolor=black@0.85'
    return (f"drawtext=fontfile={FONT}:textfile={textfile}:"
            f"fontcolor=0x{_hex_color(color)}:fontsize={fs}:{deco}:"
            f"line_spacing={int(fs*0.2)}:x=(w-text_w)/2:y={y}")


def probe_media(path):
    """Renvoie (duree_secondes, a_de_l_audio) pour un fichier vidéo/audio."""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', '0', '-show_entries',
             'format=duration:stream=codec_type', '-of', 'json', path],
            capture_output=True, text=True)
        info = json.loads(out.stdout or '{}')
        dur = float(info.get('format', {}).get('duration', 0) or 0)
        has_audio = any(s.get('codec_type') == 'audio'
                        for s in info.get('streams', []))
        return dur, has_audio
    except Exception:
        return 0.0, False


def build_ffmpeg(clips, audio, w, h, fit, transition, out, opts=None):
    """Construit la commande FFmpeg pour un montage mixte photos + vidéos + audio.

    Renvoie (args, duree_totale_en_secondes).

    clips : liste de dicts dans l'ordre de la timeline. Clés :
        type      'photo' | 'video'
        path      chemin du média
        dur       durée affichée sur la timeline (s)
        trimStart point d'entrée dans la source (vidéo, s)
        srcDur    durée totale de la source (vidéo, s)
        hasAudio  bool (vidéo)
        muted     bool — couper l'audio de ce clip vidéo
        vol       volume de l'audio du clip vidéo (0..1)
        kb        effet Ken Burns (photo)
        textfile  (chemin, position) ou None
        textSize / textStyle / textColor

    opts : ttype (nom xfade), fps, quality, musicVol, fadeIn, fadeOut,
           loop (boucler la musique), blackIn / blackOut (fondu au noir, s).

    Pour préserver au mieux la durée totale, chaque clip i>=1 est rallongé de D
    (durée du fondu) ; pour une vidéo on borne ce rab à ce qu'il reste de
    source après le trim, et les offsets xfade sont calculés sur les longueurs
    réelles afin de ne jamais produire d'offset invalide.
    """
    o = opts or {}
    fps = int(o.get('fps') or FPS)
    ttype = o.get('ttype') if o.get('ttype') in TRANSITIONS else 'fade'
    preset, crf = QUALITY.get(o.get('quality'), QUALITY['standard'])

    n = len(clips)
    durs = [c['dur'] for c in clips]
    use_xfade = transition > 0 and n >= 2
    D = min(transition, min(durs) * 0.9) if use_xfade else 0.0

    # rab (extension) appliqué à la queue de chaque clip i>=1
    def tail_room(c):
        if c['type'] != 'video':
            return D
        room = max(0.0, c.get('srcDur', 0) - (c.get('trimStart', 0) + c['dur']))
        return min(D, room)
    exts = [0.0] + [tail_room(clips[i]) if use_xfade else 0.0 for i in range(1, n)]

    if fit == 'cover':
        sc = f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}'
    else:
        sc = (f'scale={w}:{h}:force_original_aspect_ratio=decrease,'
              f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black')

    # ---- inputs ----
    args = ['ffmpeg', '-y', '-nostats', '-progress', 'pipe:1']
    for i, c in enumerate(clips):
        L = c['dur'] + exts[i]
        if c['type'] == 'video':
            args += ['-ss', f"{c.get('trimStart', 0):.3f}", '-t', f'{L:.3f}',
                     '-i', c['path']]
        else:
            args += ['-loop', '1', '-framerate', str(fps), '-t', f'{L:.3f}',
                     '-i', c['path']]
    audio_idx = None
    if audio:
        if o.get('loop'):
            args += ['-stream_loop', '-1']
        args += ['-i', audio]
        audio_idx = n

    # ---- chaîne vidéo par clip ----
    # On termine TOUTES les chaînes par fps+setpts+setsar identiques : xfade
    # exige des flux strictement homogènes (zoompan laisse sinon un framerate
    # mal défini qui casse le fondu quand on mélange photos et vidéos).
    filters = []
    for i, c in enumerate(clips):
        L = c['dur'] + exts[i]
        chain = f'[{i}:v]{sc}'
        if c['type'] == 'photo':
            frames = max(1, round(L * fps))
            kb = _kb_filter(c.get('kb', 'none'), w, h, frames, fps)
            if kb:
                chain += ',' + kb
        # fps en DERNIER : xfade exige un framerate constant en entrée, or
        # setpts/zoompan le laissent indéfini (1/0). On le fige à la toute fin.
        chain += f',setpts=PTS-STARTPTS,setsar=1,format=yuv420p,fps={fps}'
        filters.append(chain + f'[v{i}]')

    # ---- concat / xfade ----
    if use_xfade:
        prev = 'v0'
        total = durs[0] + exts[0]      # longueur réelle cumulée du flux courant
        for i in range(1, n):
            off = max(0.0, total - D)
            lbl = f'x{i}'
            filters.append(
                f'[{prev}][v{i}]xfade=transition={ttype}:'
                f'duration={D:.3f}:offset={off:.3f}[{lbl}]')
            prev = lbl
            total = total + (durs[i] + exts[i]) - D
        vout = prev
    else:
        concat_in = ''.join(f'[v{i}]' for i in range(n))
        filters.append(f'{concat_in}concat=n={n}:v=1:a=0[vout]')
        vout = 'vout'
        total = sum(durs)

    # ---- texte par clip (sur la timeline finale) ----
    starts = []
    acc = 0.0
    for c in clips:
        starts.append(acc)
        acc += c['dur']
    if any(c.get('textfile') for c in clips):
        prev = vout
        k = 0
        for i, c in enumerate(clips):
            tf = c.get('textfile')
            if not tf:
                continue
            path, pos = tf
            lbl = f'txt{k}'
            k += 1
            dt = _drawtext(path, pos, h, c.get('textSize', 'm'),
                           c.get('textStyle', 'outline'), c.get('textColor'))
            s, e = starts[i], starts[i] + c['dur']
            filters.append(
                f"[{prev}]{dt}:enable='between(t,{s:.3f},{e:.3f})'[{lbl}]")
            prev = lbl
        vout = prev

    # ---- ouverture / fermeture au noir ----
    bi = min(float(o.get('blackIn') or 0), total / 3)
    bo = min(float(o.get('blackOut') or 0), total / 3)
    if bi > 0 or bo > 0:
        fx = []
        if bi > 0:
            fx.append(f'fade=t=in:st=0:d={bi:.3f}')
        if bo > 0:
            fx.append(f'fade=t=out:st={max(0.0, total - bo):.3f}:d={bo:.3f}')
        filters.append(f'[{vout}]{",".join(fx)}[vfx]')
        vout = 'vfx'

    # ---- audio : musique de fond + audio des clips vidéo (mixés) ----
    a_labels = []
    if audio_idx is not None:
        mv = max(0.0, min(2.0, float(o.get('musicVol', 1.0))))
        filters.append(f'[{audio_idx}:a]aresample=44100,volume={mv:.3f}[abg]')
        a_labels.append('abg')
    for i, c in enumerate(clips):
        if c['type'] == 'video' and c.get('hasAudio') and not c.get('muted'):
            ms = int(round(starts[i] * 1000))
            cv = max(0.0, min(2.0, float(c.get('vol', 1.0))))
            lbl = f'a{i}'
            filters.append(
                f'[{i}:a]aresample=44100,asetpts=PTS-STARTPTS,volume={cv:.3f},'
                f'adelay={ms}|{ms}[{lbl}]')
            a_labels.append(lbl)

    maps = ['-map', f'[{vout}]']
    if a_labels:
        if len(a_labels) == 1:
            mixed = f'[{a_labels[0]}]'
        else:
            mix = ''.join(f'[{l}]' for l in a_labels)
            filters.append(
                f'{mix}amix=inputs={len(a_labels)}:normalize=0:'
                f'dropout_transition=0[amx]')
            mixed = '[amx]'
        # coupe à la durée vidéo, fondus audio, puis silence si l'audio est court
        afx = [f'atrim=0:{total:.3f}']
        fi = min(float(o.get('fadeIn') or 0), total / 2)
        fo = min(float(o.get('fadeOut') or 0), total / 2)
        if fi > 0:
            afx.append(f'afade=t=in:st=0:d={fi:.3f}')
        if fo > 0:
            afx.append(f'afade=t=out:st={max(0.0, total - fo):.3f}:d={fo:.3f}')
        afx.append('apad')
        filters.append(f'{mixed}{",".join(afx)}[aout]')
        maps += ['-map', '[aout]', '-c:a', 'aac', '-b:a', '192k', '-shortest']

    args += ['-filter_complex', ';'.join(filters)]
    args += maps
    args += ['-c:v', 'libx264', '-preset', preset, '-crf', str(crf),
             '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-t', f'{total:.3f}', out]
    return args, total


# ---------------------------------------------------------------------------
# Rendu asynchrone : POST /render crée un job, le navigateur suit la
# progression (parsée depuis `-progress pipe:1`), peut annuler, puis télécharge.
# ---------------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 3600  # un job non téléchargé est nettoyé au bout d'une heure


def _purge_jobs():
    now = time.time()
    with JOBS_LOCK:
        for jid, j in list(JOBS.items()):
            if now - j['created'] > JOB_TTL and j['status'] != 'running':
                shutil.rmtree(j['workdir'], ignore_errors=True)
                JOBS.pop(jid, None)


def _run_job(job, cmd):
    errlog = open(os.path.join(job['workdir'], 'ffmpeg.log'), 'w')
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errlog,
                                text=True, bufsize=1)
        job['proc'] = proc
        for line in proc.stdout:
            k, _, v = line.strip().partition('=')
            if k in ('out_time_us', 'out_time_ms') and v.lstrip('-').isdigit():
                t = int(v) / 1e6
                if job['total'] > 0:
                    job['progress'] = max(job['progress'], min(0.99, t / job['total']))
            elif k == 'speed' and v.endswith('x'):
                try:
                    job['speed'] = float(v[:-1])
                except ValueError:
                    pass
        proc.wait()
        errlog.close()
        if job['status'] == 'cancelled':
            return
        if proc.returncode != 0 or not os.path.exists(job['out']):
            with open(errlog.name, encoding='utf-8', errors='replace') as fh:
                tail = fh.read()[-1500:]
            print(tail)
            job['error'] = 'Échec FFmpeg'
            job['detail'] = tail
            job['status'] = 'error'
        else:
            job['progress'] = 1.0
            job['status'] = 'done'
    except Exception as e:  # noqa: BLE001
        job['error'] = str(e)
        job['status'] = 'error'
    finally:
        if not errlog.closed:
            errlog.close()


@app.route('/render', methods=['POST'])
def render():
    _purge_jobs()
    workdir = tempfile.mkdtemp(prefix='minicut_')
    try:
        meta = json.loads(request.form.get('clips', '[]'))
        w = int(request.form.get('width', 1280))
        h = int(request.form.get('height', 720))
        fit = request.form.get('fit', 'cover')
        transition = float(request.form.get('transition', 0))
        opts = json.loads(request.form.get('opts', '{}') or '{}')
        if int(opts.get('fps') or FPS) not in (24, 25, 30, 60):
            opts['fps'] = FPS

        # médias dans l'ordre de la timeline (1 fichier par clip)
        media_files = request.files.getlist('media')
        if not media_files or not meta:
            shutil.rmtree(workdir, ignore_errors=True)
            return jsonify(error="Aucun média reçu"), 400
        if len(media_files) != len(meta):
            shutil.rmtree(workdir, ignore_errors=True)
            return jsonify(error="Incohérence clips/médias"), 400

        clips = []
        for i, (f, m) in enumerate(zip(media_files, meta)):
            typ = m.get('type', 'photo')
            ext = os.path.splitext(f.filename)[1] or ('.mp4' if typ == 'video' else '.jpg')
            p = os.path.join(workdir, f'clip_{i:03d}{ext}')
            f.save(p)

            dur = max(0.2, float(m.get('duration', 3.0)))
            c = {'type': typ, 'path': p, 'dur': dur,
                 'trimStart': max(0.0, float(m.get('trimStart', 0))),
                 'muted': bool(m.get('muted', False)),
                 'vol': float(m.get('vol', 1.0)),
                 'kb': m.get('kb', 'none'), 'textfile': None,
                 'textPos': m.get('textPos', 'bottom'),
                 'textSize': m.get('textSize', 'm'),
                 'textStyle': m.get('textStyle', 'outline'),
                 'textColor': m.get('textColor', '#ffffff')}
            if typ == 'video':
                src_dur, has_audio = probe_media(p)
                c['srcDur'] = src_dur or (c['trimStart'] + dur)
                c['hasAudio'] = has_audio
            # texte -> fichier (gère accents / guillemets / sauts de ligne)
            txt = (m.get('text') or '').strip()
            if txt:
                tf = os.path.join(workdir, f'txt_{i:03d}.txt')
                with open(tf, 'w', encoding='utf-8') as fh:
                    fh.write(txt)
                c['textfile'] = (tf, c['textPos'])
            clips.append(c)

        audio_path = None
        if 'audio' in request.files and request.files['audio'].filename:
            af = request.files['audio']
            audio_path = os.path.join(workdir, 'audio' + (os.path.splitext(af.filename)[1] or '.mp3'))
            af.save(audio_path)

        out = os.path.join(workdir, 'montage-minicut.mp4')
        cmd, total = build_ffmpeg(clips, audio_path, w, h, fit, transition, out, opts)
        print('FFmpeg:', ' '.join(cmd))
        jid = uuid.uuid4().hex[:12]
        job = {'id': jid, 'workdir': workdir, 'out': out, 'total': total,
               'progress': 0.0, 'speed': 0.0, 'status': 'running',
               'error': None, 'detail': None, 'proc': None,
               'created': time.time()}
        with JOBS_LOCK:
            JOBS[jid] = job
        threading.Thread(target=_run_job, args=(job, cmd), daemon=True).start()
        return jsonify(job=jid, total=total)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify(error=str(e)), 500


def _job_or_404(jid):
    with JOBS_LOCK:
        return JOBS.get(jid)


@app.route('/render/<jid>/status')
def render_status(jid):
    j = _job_or_404(jid)
    if not j:
        return jsonify(error='job inconnu'), 404
    return jsonify(status=j['status'], progress=round(j['progress'], 4),
                   speed=j['speed'], total=j['total'],
                   error=j['error'], detail=j['detail'])


@app.route('/render/<jid>/cancel', methods=['POST'])
def render_cancel(jid):
    j = _job_or_404(jid)
    if not j:
        return jsonify(error='job inconnu'), 404
    if j['status'] == 'running':
        j['status'] = 'cancelled'
        if j['proc'] and j['proc'].poll() is None:
            j['proc'].terminate()
        threading.Timer(5, lambda: shutil.rmtree(j['workdir'], ignore_errors=True)).start()
    return jsonify(status=j['status'])


@app.route('/render/<jid>/file')
def render_file(jid):
    j = _job_or_404(jid)
    if not j or j['status'] != 'done':
        return jsonify(error='vidéo pas prête'), 404

    @after_this_request
    def cleanup(resp):
        def rm():
            shutil.rmtree(j['workdir'], ignore_errors=True)
            with JOBS_LOCK:
                JOBS.pop(jid, None)
        threading.Timer(60, rm).start()
        return resp

    return send_file(j['out'], as_attachment=True,
                     download_name='montage-minicut.mp4', mimetype='video/mp4')


if __name__ == '__main__':
    url = f'http://127.0.0.1:{PORT}/'
    print('=' * 48)
    print('  MiniCut Studio')
    print(f'  Ouvre :  {url}')
    print('  (Ctrl+C pour arrêter)')
    print('=' * 48)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host='127.0.0.1', port=PORT, threaded=True)
