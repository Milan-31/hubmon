"""Lyric source and timing table for the hubmon panel's spectrum overlay.

The overlay is a background, not a player: it must never raise, never stall the
render loop and never make a network call on the drawing thread. So this module
is split into pure text work (parsing an LRC blob into a timing table, and
looking up which line is live at a moment) and, added later, the watcher that
feeds it. Only the pure part lives here for now -- it needs no player to test.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from bisect import bisect_right

# WinRT is only needed for the SMTC watcher at the bottom of this file; the pure
# text work above must stay importable on a machine without it, so the library
# and its success flag live behind a guard instead of a top-level import.
try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager)
    _HAS_WINRT = True
except Exception:
    _HAS_WINRT = False

# The artwork comes back as a WinRT random-access stream, so reading it needs
# the Storage.Streams projection as well. Kept separate from the watcher guard:
# a machine with Media.Control but no Streams still reports the track, it just
# has no cover to show.
try:
    from winrt.windows.storage.streams import DataReader
    _HAS_STREAMS = True
except Exception:
    _HAS_STREAMS = False

# [mm:ss.xx] or [mm:ss] timestamps. The fractional part is optional because some
# sources drop it; the meta tags ([ti:], [by:], [offset:]) deliberately do not
# match, so they fall out of the parse without a tag-name whitelist to maintain.
_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text):
    """Return [(seconds, text)] from an LRC blob, sorted by time.

    Meta tags ([ti:]...), blank lines and duplicate timing rows are dropped; a
    line carrying several [mm:ss.xx] stamps expands into one entry each.
    """
    rows = []
    for line in text.splitlines():
        stamps = _TS.findall(line)
        if not stamps:
            continue                  # no stamp: a meta tag or a stray blank line
        words = _TS.sub("", line).strip()
        if not words:
            continue                  # a bare stamp marks a gap, not a lyric line
        for mm, ss in stamps:
            rows.append((int(mm) * 60 + float(ss), words))
    rows.sort(key=lambda r: r[0])     # stamps need not arrive in order
    return rows


def line_at(lines, t):
    """Index of the line active at ``t`` seconds, or -1 before the first one.

    ``lines`` is the [(seconds, text)] list from parse_lrc, sorted by time.
    """
    # bisect over the timestamps and step back one: the row at or before t wins,
    # and -1 falls out when t precedes every stamp (including an empty table)
    return bisect_right(lines, t, key=lambda r: r[0]) - 1


# ---------------------------------------------------------------- fetching
_HDRS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://y.qq.com/",
}
_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_TIMEOUT = 6.0
# Lookups live next to the config, under %LOCALAPPDATA%\hubmon, so a lyric found
# once survives a restart without re-hitting the network every launch.
LYRIC_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                         "hubmon", "lyrics")
# Bumped whenever a cached table would have been produced differently, so stale
# entries from an older matcher stop being served (see lines_for).
_CACHE_VER = "2"


def _norm(s):
    """Letters and digits only, lower-cased, for comparing names loosely."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _agrees(a, b):
    """Whether two names mean the same thing, allowing either to carry extras.

    Loose on purpose: a session's title is often '夜曲 (Live)' where the search
    hit is plain '夜曲', or the reverse. Each side needs two characters of
    substance first, so a stray single letter cannot match everything.
    """
    a, b = _norm(a), _norm(b)
    return len(a) >= 2 and len(b) >= 2 and (a in b or b in a)


# QQ's lyric blobs open with a credit row -- '[00:00.00]夜曲 - 周杰伦 (Jay Chou)'
# -- which repeats the title the session already gave us and would otherwise be
# shown as the first "lyric". Only the *opening* rows are dropped, and only when
# they repeat the track's own name, so a real line that merely starts with the
# title, or any line further into the song, is never touched.
_HDR_SEP = re.compile(r"\s*[-–—|·:：]\s*")


def _strip_header(lines, title, artist):
    """Drop the lyric table's leading 'song - singer' credit rows."""
    if not title:
        return lines
    t, a = _norm(title), _norm(artist or "")
    i = 0
    while i < len(lines):
        text = lines[i][1]
        n = _norm(text)
        if not (n == t or n == t + a
                or (_HDR_SEP.search(text) and n.startswith(t))):
            break
        i += 1
    return lines[i:]


def _pick(cands, title, song_id, artist, album, duration):
    """The search hit the track's own metadata corroborates, or None.

    An injected numeric id (QQMusic-ID-Injector writes it into the SMTC genres)
    is authoritative and short-circuits everything. Otherwise a hit is only
    *eligible* when the metadata backs it -- its name agrees with the session's
    title, or its singer and its length both agree -- and the eligible hits are
    then ranked by artist / album / duration. Nothing eligible means no lyric.

    Eligible is the whole point. Searching blind and taking the first hit pastes
    some arbitrary song onto the panel: a session whose "title" is not a song at
    all (a browser tab, a video page) resolved to whatever the search happened to
    return first, and '抖音-记录美好生活' came back as 夜曲 -- a track that was
    not playing. Better to show nothing than to show the wrong song.
    """
    if not cands:
        return None
    sid = str(song_id or "")
    if sid:
        for c in cands:
            if str(c.get("id")) == sid:
                return c
    best, best_score = None, -1.0
    for c in cands:
        singers = c.get("singer") or []
        artist_ok = bool(artist) and any(
            _agrees(s.get("name") or "", artist) for s in singers)
        iv = c.get("interval")
        gap = abs(iv - duration) if (duration and isinstance(iv, (int, float))) else None
        dur_ok = gap is not None and gap <= 3
        if not (_agrees(c.get("name") or "", title) or (artist_ok and dur_ok)):
            continue                     # the metadata does not back this hit
        score = 3.0 if artist_ok else 0.0
        if album and album == ((c.get("album") or {}).get("name") or ""):
            score += 2.0
        if dur_ok:
            score += 3.0 + max(0.0, 3.0 - gap)   # closer wins inside the window
        if score > best_score:                 # strict > keeps the first on ties
            best, best_score = c, score
    return best


class Lyrics:
    """Fetch a track's LRC from QQ Music, falling back to NetEase.

    Network work only: the caller hands it SMTC metadata and gets back a
    parse_lrc table (possibly empty). Nothing raises -- the overlay is cosmetic,
    so a lookup failure must degrade to "no lyric", never to a crash.
    """

    def __init__(self, cache_dir=None):
        self._dir = cache_dir or LYRIC_DIR

    # ---------------------------------------------------------------- cache
    def _cache_path(self, key):
        return os.path.join(self._dir, key + ".json")

    def _cache_get(self, key):
        try:
            with open(self._cache_path(key), "r", encoding="utf-8") as f:
                rows = json.load(f)
            return [(float(t), s) for t, s in rows]   # tuples, matching parse_lrc
        except Exception:
            return None

    def _cache_put(self, key, lines):
        try:
            os.makedirs(self._dir, exist_ok=True)
            with open(self._cache_path(key), "w", encoding="utf-8") as f:
                json.dump([[t, s] for t, s in lines], f)
        except Exception:
            pass                                      # a cache miss is not fatal

    # ---------------------------------------------------------------- net
    def _post(self, payload):
        req = urllib.request.Request(
            _API, data=json.dumps(payload).encode(),
            headers=dict(_HDRS, **{"Content-Type": "application/json"}))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())

    def _get(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": _HDRS["User-Agent"], "Referer": "https://music.163.com/"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())

    def _search(self, kw):
        payload = {
            "comm": {"ct": 19, "cv": "1859", "uin": "0"},
            "req": {"module": "music.search.SearchCgiService",
                    "method": "DoSearchForQQMusicDesktop",
                    "param": {"query": kw, "num_per_page": 5, "page_num": 1}},
        }
        d = self._post(payload)
        return (d.get("req", {}).get("data", {}).get("body", {})
                 .get("song", {}).get("list") or [])

    def _fetch_lyric(self, mid):
        payload = {
            "comm": {"ct": 24, "cv": 0},
            "req": {"module": "music.musichallSong.PlayLyricInfo",
                    "method": "GetPlayLyricInfo",
                    "param": {"songMID": mid, "songID": 0}},
        }
        blob = (self._post(payload).get("req", {}).get("data", {})
                .get("lyric") or "")
        if not blob:
            return []
        return parse_lrc(base64.b64decode(blob).decode("utf-8", "replace"))

    def _fetch_fallback(self, title, artist, album, duration, song_id):
        """NetEase public API: search the song, then read the chosen hit's blob.

        Its search is as blind as QQ's, so the same corroboration gate runs here
        (the hits are reshaped into QQ's field names first). Without it the root
        cause would merely move to this path the moment QQ came back empty.
        """
        kw = urllib.parse.quote(" ".join(p for p in (title, artist) if p))
        raw = (self._get("https://music.163.com/api/search/get/web"
                         "?s=%s&type=1&limit=5&offset=0" % kw)
               .get("result") or {}).get("songs") or []
        hits = [{"id": h.get("id"), "name": h.get("name"),
                 "singer": [{"name": a.get("name")}
                            for a in (h.get("artists") or [])],
                 "album": {"name": (h.get("album") or {}).get("name")},
                 "interval": (h.get("duration") or 0) / 1000.0}
                for h in raw]
        pick = _pick(hits, title, song_id, artist, album, duration)
        if not pick:
            return []
        blob = (self._get("https://music.163.com/api/song/lyric"
                          "?id=%s&lv=1&kv=1&tv=-1" % pick.get("id"))
                .get("lrc") or {}).get("lyric") or ""
        return parse_lrc(blob)

    def lines_for(self, title, artist, album="", duration=0.0, song_id=""):
        """Return the [(seconds, text)] table for a track, or [] on any failure."""
        if not title:
            return []
        # The version tag retires tables cached before the corroboration gate:
        # those were matched blind, so a wrong one (a browser page's title
        # resolving to some random song) would otherwise keep coming back.
        key = hashlib.sha1(("%s|%s|%s" % (_CACHE_VER, title, artist))
                           .encode("utf-8")).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        lines = []
        try:
            pick = _pick(self._search("%s %s" % (title, artist)),
                         title, song_id, artist, album, duration)
            if pick:
                lines = self._fetch_lyric(pick.get("mid"))
        except Exception:
            lines = []
        if not lines:                       # QQ had nothing (or failed): try NetEase
            try:
                lines = self._fetch_fallback(title, artist, album, duration, song_id)
            except Exception:
                lines = []
        lines = _strip_header(lines, title, artist)
        if lines:
            self._cache_put(key, lines)
        return lines


# ------------------------------------------------------------ player presence
# The band is only meaningful while a music player is actually open. SMTC also
# carries browsers and video players, whose titles are music-video names often
# enough that a lyric would otherwise appear with no player running.
PLAYERS = (
    # "music" alone already covers QQ音乐 (QQMusic.exe), 网易云 (cloudmusic.exe),
    # 酷狗 (KGMusic.exe), 酷我 (kwmusic.exe), 咪咕 (MiguMusic.exe), Apple Music,
    # Amazon Music and Windows' own Media Player (Music.UI.exe); the rest of the
    # common desktop players carry no such word in their image name.
    "music", "spotify", "foobar", "aimp", "winamp", "itunes", "tidal",
    "qobuz", "deezer", "mediamonkey",
)
_PRESENCE_TTL = 2.0
_presence = [0.0, False]


def _scan_players():
    """One pass over the process table for a music app; True/False, never raises."""
    try:
        import psutil
    except Exception:
        return True                   # cannot tell: keep the overlay's old behaviour
    try:
        for p in psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            if any(k in name for k in PLAYERS):
                return True
    except Exception:
        return True
    return False


def music_app_running():
    """Whether a music player in PLAYERS is open, memoised for 2 seconds.

    The render thread asks this every frame, and walking the process table there
    costs milliseconds, so the answer is cached rather than recomputed.
    """
    now = time.monotonic()
    if now - _presence[0] > _PRESENCE_TTL:
        _presence[0] = now
        _presence[1] = _scan_players()
    return _presence[1]


# ---------------------------------------------------------------- SMTC watch
def _com_apartment():
    """Bring a COM apartment up on the calling thread; returns the HRESULT, or None.

    WinRT sits on COM, and only the thread that imported the library has an
    apartment opened for it. The watcher thread below did not, so on a frozen
    build its first activation would fail with CO_E_NOTINITIALIZED (0x800401f0)
    unless it opens its own -- hence this, mirroring ``audio._com_apartment``.
    S_OK (0) and S_FALSE (1) both mean the apartment is ours now; anything else
    leaves COM as it was found.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        return int(ctypes.windll.ole32.CoInitializeEx(None, 0))  # MULTITHREADED
    except Exception:
        return None


# QQMusic-ID-Injector drops its numeric id into the track's SMTC genres list (e.g.
# ["QQ-12345"]); that number is what _pick uses to short-circuit the fuzzy search.
_SONG_ID = re.compile(r"QQ-(\d+)", re.I)


def _parse_song_id(genres):
    """The injected QQ song id found in ``genres``, or "" when none is there."""
    for g in genres or ():
        m = _SONG_ID.search(str(g))
        if m:
            return m.group(1)
    return ""


_EMPTY = {"title": "", "artist": "", "album": "", "status": "none",
          "position_s": 0.0, "duration_s": 0.0, "song_id": "", "cover": b""}


def _select(infos):
    """The one live session the panel should report, out of everything SMTC has.

    Several players can be going at once -- a QQ Music track underneath a browser
    video, say -- and Windows' *current* session then follows whatever the system
    last deemed active, which is frequently the browser. That session's "title"
    is a web page and its artist is empty, and feeding it through the match gate
    blanked the overlay although a real song was plainly playing. So every
    playing session is ranked instead, and the injected QQ id or a real artist
    outranks a bare page title; only when nothing is playing does the current
    session stand in (paused, so the overlay hides as before).

    ``infos`` are the dicts built by MediaWatcher._read, plus ``playing`` and
    ``current``. Returns a snapshot shaped like ``_EMPTY`` (the ranking flags
    dropped), or the empty snapshot when there is nothing to report.
    """
    pool = [i for i in infos if i["playing"]] or [i for i in infos if i["current"]]
    if not pool:
        return dict(_EMPTY)
    best = max(pool, key=lambda i: (bool(i["song_id"]), bool(i["artist"]),
                                    bool(i["title"]), i["current"]))
    return {k: best.get(k, v) for k, v in _EMPTY.items()}


class MediaWatcher:
    """Poll the Windows media session for the playing track, off the UI thread.

    Cosmetic data only: ``snapshot`` always returns the last reading and never
    raises, a missing session or a missing winrt both degrade to the empty
    snapshot, and the poller never touches the render thread's state. The thread
    owns its own COM apartment and asyncio loop, so the caller never talks to
    WinRT directly.
    """

    def __init__(self, poll=0.7):
        self.poll = float(poll)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._snap = dict(_EMPTY)
        self._thread = None
        # artwork cache: a thumbnail is read once per track, never on the
        # render thread and never once per poll
        self._cover_key = None
        self._cover = b""

    # ------------------------------------------------------------ life cycle
    def start(self):
        """Idempotent; a no-op when winrt is unavailable, so nothing can raise."""
        if not _HAS_WINRT:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hubmon-media",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        """Idempotent and bounded: the poller returns within the join timeout."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def snapshot(self):
        """A copy of the last reading, safe to take from the render thread."""
        with self._lock:
            return dict(self._snap)

    # ------------------------------------------------------------- polling
    def _write(self, snap):
        with self._lock:
            self._snap = snap

    def _read(self, loop, session):
        """Extract one session's fields, with the two ranking flags attached."""
        try:
            props = loop.run_until_complete(
                session.try_get_media_properties_async())
            ps = int(session.get_playback_info().playback_status)
            tl = session.get_timeline_properties()
            title = str(props.title or "")
            artist = str(props.artist or "")
            album = str(props.album_title or "")
            song_id = _parse_song_id(props.genres)
            return {
                "title": title,
                "artist": artist,
                "album": album,
                "status": ("playing" if ps == 4 else
                           "paused" if ps == 5 else "none"),
                "position_s": float(tl.position.total_seconds()),
                "duration_s": float(tl.end_time.total_seconds()),
                "song_id": song_id,
                "cover": (self._read_cover(loop, props,
                                           "%s|%s|%s|%s" % (title, artist,
                                                            album, song_id))
                          if ps == 4 else b""),
                "playing": ps == 4,
                "current": False,
            }
        except Exception:
            return None                  # a dying session is simply skipped

    def _read_cover(self, loop, props, key):
        """The track's artwork as encoded bytes, read once per track.

        A thumbnail is a random-access stream in WinRT, so it is drained here,
        on the poller's own loop, and cached against the track key. The render
        thread then only ever receives a bytes object it can decode at leisure.
        Nothing about a missing thumbnail may raise: artwork is decoration.
        """
        if key == self._cover_key:
            return self._cover
        self._cover_key = key
        self._cover = b""
        thumb = getattr(props, "thumbnail", None)
        if thumb is None or not _HAS_STREAMS:
            return self._cover
        stream = reader = None
        try:
            stream = loop.run_until_complete(thumb.open_read_async())
            size = int(stream.size)
            if 0 < size <= 4 * 1024 * 1024:
                reader = DataReader(stream)
                loop.run_until_complete(reader.load_async(size))
                buf = bytearray(size)
                reader.read_bytes(buf)
                self._cover = bytes(buf)
        except Exception:
            self._cover = b""
        finally:
            for obj in (reader, stream):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
        return self._cover

    def _run(self):
        _com_apartment()               # the apartment belongs to this thread
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            mgr = loop.run_until_complete(SessionManager.request_async())
        except Exception:
            self._write(dict(_EMPTY))
            return
        while not self._stop.is_set():
            try:
                cur = mgr.get_current_session()
                infos = []
                for s in mgr.get_sessions():
                    info = self._read(loop, s)
                    if info is not None:
                        info["current"] = s == cur
                        infos.append(info)
                self._write(_select(infos))
            except Exception:
                self._write(dict(_EMPTY))   # a bad read must not kill the poller
            self._stop.wait(self.poll)


# --------------------------------------------------------------- overlay state
# Per-character reveal: the line's transition length now scales with how many
# glyphs it has -- each glyph fades in over LYRIC_CHAR, and each one starts
# LYRIC_STAGGER later than the previous, so the sentence types itself in.
LYRIC_STAGGER = 0.045
LYRIC_CHAR = 0.22
# The geom flight gets a longer beat than the plain reveal: its shards cross the
# whole panel, and on the reveal's own pace they land before the eye has followed
# them. Only the *length* is stretched here -- the shape of the motion is the
# renderer's easing, which drifts the fragments out slowly and settles them home.
LYRIC_GEOM_SLOW = 1.5
# The three-row scene slides its stack up by one row when the line changes. That
# travel is its own fixed-length beat -- the glyph reveal's length depends on the
# sentence, which would make short lines snap and long ones crawl.
LYRIC_SCROLL = 0.5


def word_u(t, dur, k, slow=1.0):
    """Reveal progress (0..1) of glyph ``k`` at frame progress ``t`` of ``dur``.

    ``t`` is the 0..1 transition progress, ``dur`` the line's total transition
    length in seconds and ``k`` the glyph's index in the line. Glyph ``k``
    starts LYRIC_STAGGER*k seconds in and takes LYRIC_CHAR to finish, so the
    line reveals left to right.

    ``slow`` stretches both of those paces together, and has to be paired with a
    ``dur`` stretched by the same factor -- note that ``t * dur`` is the elapsed
    time either way, so stretching ``dur`` alone would only append dead time
    after the reveal had already finished rather than pace it out.
    """
    if t >= 1.0:
        return 1.0          # settled exactly; the division lands 1 ULP short
    u = (t * dur - k * LYRIC_STAGGER * slow) / (LYRIC_CHAR * slow)
    return max(0.0, min(1.0, u))


class LyricState:
    """Per-frame overlay state for the lyric background.

    Handed the SMTC snapshot and the parse_lrc table, it answers what to draw:
    the live line, the one it replaced, how far the transition has run (0..1) and
    which effect is in force. The render thread owns one instance; nothing here
    touches the network or the watcher, so it cannot stall the loop.
    """

    def __init__(self):
        self._song = ""              # title|artist of the track the state is on
        self._idx = -1               # live line index, -1 before the first line
        self._t = 1.0                # transition progress; 1 means settled
        self._prev = ""              # line being replaced, "" once settled
        self._clock = 0.0            # fallback clock when SMTC has no timeline
        self._rotate = 0             # switch count, drives the rotate effect
        self._scroll = 1.0           # three-row stack travel, 0..1

    def _pick(self, raw):
        """Resolve the configured effect; "rotate" alternates on each switch."""
        if raw == "rotate":
            return "fade" if self._rotate % 2 == 1 else "geom"
        return raw

    def update(self, cfg, snap, lines, dt):
        """Advance one frame and return what the overlay should draw."""
        song = "%s|%s" % (snap.get("title", ""), snap.get("artist", ""))
        if song != self._song:       # a new track: drop the old transition
            self._song = song
            self._idx = -1
            self._t = 1.0
            self._prev = ""
            self._scroll = 1.0
            self._clock = 0.0

        raw = getattr(cfg, "lyrics_effect", "rotate")
        eff = self._pick(raw)

        if not lines or snap.get("status") != "playing":
            return {"visible": False, "text": "", "prev": "", "next": "",
                    "t": 1.0, "scroll": 1.0, "effect": eff,
                    "position_s": 0.0, "dur": LYRIC_CHAR}

        if snap.get("duration_s", 0.0) > 0:
            now = float(snap.get("position_s", 0.0))   # SMTC keeps the timeline
        else:
            self._clock += dt                          # no timeline: count frames
            now = self._clock

        idx = line_at(lines, now)
        text = lines[idx][1] if idx >= 0 else ""
        changed = idx != self._idx                   # a new line came up
        if changed:
            self._prev = lines[self._idx][1] if 0 <= self._idx < len(lines) else ""
            self._idx = idx
            self._t = 0.0
            self._scroll = 0.0
            if raw == "rotate":
                self._rotate += 1
                eff = self._pick(raw)
        else:
            self._scroll = min(1.0, self._scroll + dt / LYRIC_SCROLL)
        # The line's transition length is its glyph count: the last glyph starts
        # LYRIC_STAGGER*(n-1) in and takes LYRIC_CHAR, so dur covers the whole
        # left-to-right reveal -- stretched for geom, whose flight is longer.
        # The effect only settles on a new line, so a line keeps one dur.
        dur = LYRIC_CHAR + LYRIC_STAGGER * max(0, len(text) - 1)
        if eff == "geom":
            dur *= LYRIC_GEOM_SLOW
        if not changed:
            self._t = min(1.0, self._t + dt / dur)

        prev = self._prev if self._t < 1.0 else ""
        nxt = lines[idx + 1][1] if 0 <= idx + 1 < len(lines) else ""
        return {"visible": bool(text), "text": text, "prev": prev, "next": nxt,
                "t": self._t, "scroll": self._scroll, "effect": eff,
                "position_s": now, "dur": dur}
