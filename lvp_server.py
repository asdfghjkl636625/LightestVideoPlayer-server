#!/usr/bin/env python3
"""LightestVideoPlayer 用 ローカル HLS サーバ (yt-dlp / streamlink)。

動画を開く時だけ streamlink をサブプロセスで起動し(待機中は約10MB)、
YouTube の HLS マスター m3u8 URL を返す。映像/音声が分離していても
マスターの EXT-X-MEDIA を hls.js がそのまま処理する。

起動:   python lvp_server.py
        python lvp_server.py --extra "--http-cookie=KEY=VALUE"
更新:   pip install -U streamlink
API:    GET /m?v=<ID>            -> yt-dlp で合成したマスター m3u8
        GET /player?v=<動画ID>   -> 空のページ (ユーザースクリプトがここでプレイヤーを起動)
        GET /resolve?v=<動画ID>  -> {title, live, start, duration, url}  url は /p 経由の中継URL
        GET /p?u=<URL>           -> googlevideo への中継 (m3u8 は URL を /p 向けに書換え、セグメントは素通し)
        GET /ping
"""
import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import shlex
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urljoin

CACHE_TTL = 600
_cache = {}
_locks = {}
_glock = threading.Lock()
_sem = threading.Semaphore(2)  # 同時に走らせる streamlink は最大2つ
ARGS = None


def base_cmd():
    exe = shutil.which('streamlink')
    return [exe] if exe else [sys.executable, '-m', 'streamlink_cli']


def run_streamlink(vid):
    c = base_cmd() + ['--json', '--loglevel', 'none']
    if ARGS.extra:
        c += shlex.split(ARGS.extra)
    c.append('https://www.youtube.com/watch?v=' + vid)
    with _sem:
        p = subprocess.run(c, capture_output=True, timeout=60)
    out = p.stdout.decode('utf-8', 'replace').strip()
    try:
        j = json.loads(out)
    except ValueError:
        err = p.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError(err[-1] if err else 'streamlink 失敗')
    if j.get('error'):
        raise RuntimeError(str(j['error']))
    return j


def pick_url(streams):
    """HLS のマスターURLを探す (無ければ HLS バリアントURL)"""
    fallback = None
    for k, s in streams.items():
        if not isinstance(s, dict) or k in ('best', 'worst', 'best-unfiltered', 'worst-unfiltered'):
            continue
        if s.get('master'):
            return s['master']
        if str(s.get('type', '')).startswith('hls') and s.get('url') and not fallback:
            fallback = s['url']
    return fallback


# ---- yt-dlp エンジン (ライブも通常動画もHLSで取得) ----
def is_none(c):
    return c in (None, 'none')


def ytdlp_info(vid):
    ea = 'youtube:skip=dash,translated_subs' + (';player_client=' + ARGS.clients if ARGS.clients else '')
    c = [sys.executable, '-m', 'yt_dlp', '-J', '--no-playlist', '--ignore-config',
         '-f', 'all', '--ignore-no-formats-error', '--socket-timeout', '15', '--extractor-args', ea]
    if ARGS.cookies_from_browser:
        c += ['--cookies-from-browser', ARGS.cookies_from_browser]
    if ARGS.cookies:
        c += ['--cookies', ARGS.cookies]
    c.append('https://www.youtube.com/watch?v=' + vid)
    with _sem:
        p = subprocess.run(c, capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout.strip():
        err = p.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError(err[-1] if err else 'yt-dlp 失敗')
    info = json.loads(p.stdout)
    info['_warn'] = [l for l in p.stderr.decode('utf-8', 'replace').splitlines() if 'WARNING' in l or 'ERROR' in l][-3:]
    return info


def build_master(info):
    """映像バリアント + 音声グループのマスター m3u8 を合成"""
    fmts = [f for f in (info.get('formats') or [])
            if str(f.get('protocol', '')).startswith('m3u8') and f.get('url')]
    video = [f for f in fmts if not is_none(f.get('vcodec')) and f.get('height')]
    audio = [f for f in fmts if is_none(f.get('vcodec')) and not f.get('height')]  # acodec が不明(None)でも音声専用として扱う
    if not video and not audio:
        return None
    avc = [f for f in video if str(f.get('vcodec', '')).startswith('avc1')]
    if avc:
        video = avc
    best = {}
    for f in video:
        k = (f['height'], round(f.get('fps') or 0))
        if k not in best or (f.get('tbr') or 0) > (best[k].get('tbr') or 0):
            best[k] = f
    video = sorted(best.values(), key=lambda f: (f['height'], f.get('fps') or 0))
    a = None
    if audio:
        a = max(audio, key=lambda f: (f.get('language_preference') or 0,
                                      not str(f.get('acodec') or 'mp4a').startswith('opus'),
                                      'high' in str(f.get('format_note') or ''),
                                      f.get('abr') or f.get('tbr') or 0,
                                      f.get('quality') or 0))
        if is_none(a.get('acodec')) or not a.get('acodec'):
            a = dict(a, acodec='mp4a.40.2')
    L = ['#EXTM3U', '#EXT-X-INDEPENDENT-SEGMENTS']
    if a:
        L.append('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="%s"' % a['url'])
    for f in video:
        muxed = not is_none(f.get('acodec'))
        codecs, bw, aud = f['vcodec'], (f.get('tbr') or 1000), ''
        if not muxed and a:
            codecs += ',' + a['acodec']
            bw += (a.get('abr') or a.get('tbr') or 128)
            aud = ',AUDIO="aud"'
        elif muxed:
            codecs += ',' + f['acodec']
        w = f.get('width') or int(f['height'] * 16 / 9)
        line = '#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d,CODECS="%s"' % (int(bw * 1000), w, f['height'], codecs)
        if f.get('fps'):
            line += ',FRAME-RATE=%.3f' % f['fps']
        L += [line + aud, f['url']]
    if a:
        L += ['#EXT-X-STREAM-INF:BANDWIDTH=%d,CODECS="%s"' % (int((a.get('abr') or a.get('tbr') or 128) * 1000), a['acodec']), a['url']]
    return '\n'.join(L) + '\n'


def via_ytdlp(vid):
    info = ytdlp_info(vid)
    if info.get('live_status') == 'is_upcoming':
        raise RuntimeError('配信開始前です')
    master = build_master(info)
    if not master:
        # HLS が無い → 映像+音声入りの https 形式 (itag 18 など) を直接再生
        mux = [f for f in (info.get('formats') or [])
               if f.get('protocol') == 'https' and f.get('url') and f.get('ext') == 'mp4'
               and not is_none(f.get('vcodec')) and not is_none(f.get('acodec'))]
        if mux:
            f = max(mux, key=lambda f: (f.get('height') or 0, f.get('tbr') or 0))
            ua = (f.get('http_headers') or {}).get('User-Agent')
            return {'id': vid, 'title': info.get('title') or vid, 'live': False, 'start': 0,
                    'duration': info.get('duration') or 0, 'direct': True, 'height': f.get('height') or 0,
                    'url': prox(f['url'], ua)}
        fs = info.get('formats') or []
        kinds = {}
        for f in fs:
            k = '%s/%s' % (f.get('protocol'), 'v' if not is_none(f.get('vcodec')) else 'a')
            kinds[k] = kinds.get(k, 0) + 1
        raise RuntimeError('m3u8 なし (formats=%d %s)' % (len(fs), kinds))
    live = bool(info.get('is_live'))
    return {'id': vid, 'title': info.get('title') or vid, 'live': live,
            'start': (info.get('release_timestamp') or info.get('timestamp') or 0) if live else 0,
            'duration': info.get('duration') or 0,
            'url': '/m?v=' + vid, '_master': master}


def via_streamlink(vid):
    j = run_streamlink(vid)
    url = pick_url(j.get('streams') or {})
    if not url:
        raise RuntimeError('HLS(m3u8) が見つかりません')
    md = j.get('metadata') or {}
    return {'id': vid, 'title': md.get('title') or vid, 'live': True, 'start': 0, 'duration': 0,
            'url': '/p?u=' + quote(url, safe='')}


def resolve(vid):
    now = time.time()
    with _glock:
        c = _cache.get(vid)
        if c and now - c[0] < CACHE_TTL:
            return c[1]
        lock = _locks.setdefault(vid, threading.Lock())
    with lock:  # 同一IDの同時抽出を1回にまとめる
        c = _cache.get(vid)
        if c and time.time() - c[0] < CACHE_TTL:
            return c[1]
        engines = {'ytdlp': [via_ytdlp], 'streamlink': [via_streamlink],
                   'auto': [via_ytdlp, via_streamlink]}[ARGS.engine]
        errs, res = [], None
        for fn in engines:
            try:
                res = fn(vid)
                break
            except Exception as e:  # noqa: BLE001
                errs.append('%s: %s' % (fn.__name__[4:], e))
                print('[resolve] 失敗', errs[-1], file=sys.stderr, flush=True)
        if not res:
            raise RuntimeError(' / '.join(errs))
        with _glock:
            t = time.time()
            for k in [k for k, v in _cache.items() if t - v[0] >= CACHE_TTL]:
                _cache.pop(k, None)
                _locks.pop(k, None)
            _cache[vid] = (t, res)
        return res


PAGE = ('<!doctype html><meta charset="utf-8"><title>LightestVideoPlayer</title>'
        '<body style="background:#0e0e10;margin:0">').encode()
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
URI_RE = re.compile(r'URI="([^"]+)"')


def allowed(url):
    h = (urlparse(url).hostname or '').lower()
    return urlparse(url).scheme == 'https' and (h.endswith('.googlevideo.com') or h.endswith('.youtube.com'))


def prox(u, ua=None):
    return '/p?u=' + quote(u, safe='') + ('&ua=' + quote(ua, safe='') if ua else '')


def rewrite_m3u8(text, base):
    out = []
    for l in text.splitlines():
        if not l.strip():
            out.append(l)
        elif l.startswith('#'):
            out.append(URI_RE.sub(lambda m: 'URI="%s"' % prox(urljoin(base, m.group(1))), l))
        else:
            out.append(prox(urljoin(base, l.strip())))
    return ('\n'.join(out) + '\n').encode()


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, url, ua=None):
        if not allowed(url):
            print('[proxy] blocked host %s' % url[:120], file=sys.stderr, flush=True)
            return self._send(403, {'error': 'host not allowed'})
        req = urllib.request.Request(url, headers={'User-Agent': ua or UA})
        if self.headers.get('Range'):
            req.add_header('Range', self.headers['Range'])
        try:
            r = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as e:
            print('[proxy] upstream HTTP %d %s' % (e.code, url[:120]), file=sys.stderr, flush=True)
            return self._send(e.code, {'error': 'upstream HTTP %d' % e.code})
        except Exception as e:  # noqa: BLE001
            print('[proxy] error %s %s' % (e, url[:120]), file=sys.stderr, flush=True)
            return self._send(502, {'error': str(e)[:200]})
        with r:
            first = r.read(8)
            if first.startswith(b'#EXTM3U'):  # プレイリスト: 小さいので丸ごと読んで書換
                body = rewrite_m3u8((first + r.read()).decode('utf-8', 'replace'), r.geturl())
                return self._send(200, body, 'application/vnd.apple.mpegurl')
            self.send_response(r.status)
            for k in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                if r.headers.get(k):
                    self.send_header(k, r.headers[k])
            self.end_headers()
            self.wfile.write(first)
            while True:
                b = r.read(65536)
                if not b:
                    break
                self.wfile.write(b)

    def do_GET(self):
        # DNS rebinding 対策: 想定外の Host は拒否
        if (self.headers.get('Host') or '') not in ('127.0.0.1:%d' % ARGS.port, 'localhost:%d' % ARGS.port):
            return self._send(403, {'error': 'bad host'})
        u = urlparse(self.path)
        try:
            if u.path == '/player':
                return self._send(200, PAGE, 'text/html; charset=utf-8')
            if u.path == '/ping':
                return self._send(200, {'ok': True})
            if u.path == '/m':  # yt-dlp で合成したマスター (URL は /p 向けに書換)
                v = (parse_qs(u.query).get('v') or [''])[0]
                m = resolve(v).get('_master') if re.fullmatch(r'[\w-]{5,20}', v) else None
                if not m:
                    return self._send(404, {'error': 'master なし'})
                return self._send(200, rewrite_m3u8(m, ''), 'application/vnd.apple.mpegurl')
            if u.path == '/p':
                q = parse_qs(u.query)
                return self._proxy((q.get('u') or [''])[0], (q.get('ua') or [None])[0])
            if u.path == '/resolve':
                v = (parse_qs(u.query).get('v') or [''])[0]
                if not re.fullmatch(r'[\w-]{5,20}', v):
                    return self._send(400, {'error': 'v パラメータが不正'})
                try:
                    return self._send(200, {k: x for k, x in resolve(v).items() if not k.startswith('_')})
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {'error': str(e)[:300]})
            self._send(404, {'error': 'not found'})
        except (BrokenPipeError, ConnectionResetError):
            pass  # クライアントが中断

    def log_message(self, fmt, *a):
        pass


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--engine', default='auto', choices=['auto', 'ytdlp', 'streamlink'],
                    help='auto: yt-dlp → 失敗時 streamlink (既定)')
    ap.add_argument('--clients', default='', help='yt-dlp player_client (カンマ区切り)。既定は yt-dlp の標準 (visionos 等がHLSを返す)')
    ap.add_argument('--cookies-from-browser', default=None, help='yt-dlp: 例 chrome / firefox')
    ap.add_argument('--cookies', default=None, help='yt-dlp: cookies.txt のパス')
    ap.add_argument('--extra', default='', help='streamlink に渡す追加引数 (引用符で囲む)')
    ARGS = ap.parse_args()
    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), H)
    print('http://%s:%d  (Ctrl+C で終了)' % (ARGS.host, ARGS.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
