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


def _kb_filter(kb, w, h, frames):
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
            f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={FPS}")


def _drawtext(textfile, pos, h):
    """Segment drawtext centré horizontalement, contour noir, lecture du texte
    depuis un fichier (gère accents / guillemets / sauts de ligne sans échappement)."""
    fs = max(12, int(h * 0.06))
    bw = max(1, int(fs * 0.12))
    if pos == 'top':
        y = f'{int(h * 0.10)}'
    elif pos == 'center':
        y = '(h-text_h)/2'
    else:  # bottom
        y = f'h-text_h-{int(h * 0.10)}'
    return (f"drawtext=fontfile={FONT}:textfile={textfile}:"
            f"fontcolor=white:fontsize={fs}:borderw={bw}:bordercolor=black@0.85:"
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


def build_ffmpeg(clips, audio, w, h, fit, transition, out):
    """Construit la commande FFmpeg pour un montage mixte photos + vidéos + audio.

    clips : liste de dicts dans l'ordre de la timeline. Clés :
        type      'photo' | 'video'
        path      chemin du média
        dur       durée affichée sur la timeline (s)
        trimStart point d'entrée dans la source (vidéo, s)
        srcDur    durée totale de la source (vidéo, s)
        hasAudio  bool (vidéo)
        muted     bool — couper l'audio de ce clip vidéo
        kb        effet Ken Burns (photo)
        textfile  (chemin, position) ou None
        textPos   position du texte

    Pour préserver au mieux la durée totale, chaque clip i>=1 est rallongé de D
    (durée du fondu) ; pour une vidéo on borne ce rab à ce qu'il reste de
    source après le trim, et les offsets xfade sont calculés sur les longueurs
    réelles afin de ne jamais produire d'offset invalide.
    """
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
    args = ['ffmpeg', '-y']
    for i, c in enumerate(clips):
        L = c['dur'] + exts[i]
        if c['type'] == 'video':
            args += ['-ss', f"{c.get('trimStart', 0):.3f}", '-t', f'{L:.3f}',
                     '-i', c['path']]
        else:
            args += ['-loop', '1', '-framerate', str(FPS), '-t', f'{L:.3f}',
                     '-i', c['path']]
    audio_idx = None
    if audio:
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
            frames = max(1, round(L * FPS))
            kb = _kb_filter(c.get('kb', 'none'), w, h, frames)
            if kb:
                chain += ',' + kb
        # fps en DERNIER : xfade exige un framerate constant en entrée, or
        # setpts/zoompan le laissent indéfini (1/0). On le fige à la toute fin.
        chain += f',setpts=PTS-STARTPTS,setsar=1,format=yuv420p,fps={FPS}'
        filters.append(chain + f'[v{i}]')

    # ---- concat / xfade ----
    if use_xfade:
        prev = 'v0'
        total = durs[0] + exts[0]      # longueur réelle cumulée du flux courant
        for i in range(1, n):
            off = max(0.0, total - D)
            lbl = f'x{i}'
            filters.append(
                f'[{prev}][v{i}]xfade=transition=fade:'
                f'duration={D:.3f}:offset={off:.3f}[{lbl}]')
            prev = lbl
            total = total + (durs[i] + exts[i]) - D
        vout = prev
    else:
        concat_in = ''.join(f'[v{i}]' for i in range(n))
        filters.append(f'{concat_in}concat=n={n}:v=1:a=0[vout]')
        vout = 'vout'

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
            dt = _drawtext(path, pos, h)
            s, e = starts[i], starts[i] + c['dur']
            filters.append(
                f"[{prev}]{dt}:enable='between(t,{s:.3f},{e:.3f})'[{lbl}]")
            prev = lbl
        vout = prev

    # ---- audio : musique de fond + audio des clips vidéo (mixés) ----
    a_labels = []
    if audio_idx is not None:
        filters.append(f'[{audio_idx}:a]aresample=44100[abg]')
        a_labels.append('abg')
    for i, c in enumerate(clips):
        if c['type'] == 'video' and c.get('hasAudio') and not c.get('muted'):
            ms = int(round(starts[i] * 1000))
            lbl = f'a{i}'
            filters.append(
                f'[{i}:a]aresample=44100,asetpts=PTS-STARTPTS,'
                f'adelay={ms}|{ms}[{lbl}]')
            a_labels.append(lbl)

    maps = ['-map', f'[{vout}]']
    if a_labels:
        if len(a_labels) == 1:
            filters.append(f'[{a_labels[0]}]apad[aout]')
        else:
            mix = ''.join(f'[{l}]' for l in a_labels)
            filters.append(
                f'{mix}amix=inputs={len(a_labels)}:normalize=0:'
                f'dropout_transition=0,apad[aout]')
        maps += ['-map', '[aout]', '-c:a', 'aac', '-b:a', '192k', '-shortest']

    args += ['-filter_complex', ';'.join(filters)]
    args += maps
    args += ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
             '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out]
    return args


@app.route('/render', methods=['POST'])
def render():
    workdir = tempfile.mkdtemp(prefix='minicut_')
    try:
        meta = json.loads(request.form.get('clips', '[]'))
        w = int(request.form.get('width', 1280))
        h = int(request.form.get('height', 720))
        fit = request.form.get('fit', 'cover')
        transition = float(request.form.get('transition', 0))

        # médias dans l'ordre de la timeline (1 fichier par clip)
        media_files = request.files.getlist('media')
        if not media_files or not meta:
            return jsonify(error="Aucun média reçu"), 400
        if len(media_files) != len(meta):
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
                 'kb': m.get('kb', 'none'), 'textfile': None,
                 'textPos': m.get('textPos', 'bottom')}
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
        cmd = build_ffmpeg(clips, audio_path, w, h, fit, transition, out)
        print('FFmpeg:', ' '.join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(out):
            print(proc.stderr[-3000:])
            return jsonify(error="Échec FFmpeg", detail=proc.stderr[-1500:]), 500

        @after_this_request
        def cleanup(resp):
            threading.Timer(30, lambda: shutil.rmtree(workdir, ignore_errors=True)).start()
            return resp

        return send_file(out, as_attachment=True,
                         download_name='montage-minicut.mp4', mimetype='video/mp4')
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify(error=str(e)), 500


if __name__ == '__main__':
    url = f'http://127.0.0.1:{PORT}/'
    print('=' * 48)
    print('  MiniCut Studio')
    print(f'  Ouvre :  {url}')
    print('  (Ctrl+C pour arrêter)')
    print('=' * 48)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host='127.0.0.1', port=PORT, threaded=True)
