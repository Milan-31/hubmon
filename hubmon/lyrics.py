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


def _pick(cands, song_id, artist, album, duration):
    """Best-matching search hit for the playing track, or None if there are none.

    An injected numeric id (QQMusic-ID-Injector writes it into the SMTC genres)
    is authoritative and short-circuits the fuzzy score. Otherwise score by
    artist / album / duration; the first candidate wins ties, so a weak match
    still yields *a* lyric instead of nothing.
    """
    if not cands:
        return None
    sid = str(song_id or "")
    if sid:
        for c in cands:
            if str(c.get("id")) == sid:
                return c
    if not (artist or album or duration):
        return cands[0]
    best, best_score = cands[0], -1.0
    for c in cands:
        score = 0.0
        singers = c.get("singer") or []
        if artist and any(artist in (s.get("name") or "") for s in singers):
            score += 3
        if album and album == ((c.get("album") or {}).get("name") or ""):
            score += 2
        iv = c.get("interval")
        if duration and isinstance(iv, (int, float)):
            gap = abs(iv - duration)
            if gap <= 3:
                score += 3
                score += max(0.0, 3 - gap)     # closer wins inside the window
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

    def _fetch_fallback(self, title, artist):
        """NetEase public API: search the song, then read its lrc blob."""
        kw = urllib.parse.quote(" ".join(p for p in (title, artist) if p))
        hits = (self._get("https://music.163.com/api/search/get/web"
                          "?s=%s&type=1&limit=5&offset=0" % kw)
                .get("result") or {}).get("songs") or []
        if not hits:
            return []
        blob = (self._get("https://music.163.com/api/song/lyric"
                          "?id=%s&lv=1&kv=1&tv=-1" % hits[0].get("id"))
                .get("lrc") or {}).get("lyric") or ""
        return parse_lrc(blob)

    def lines_for(self, title, artist, album="", duration=0.0, song_id=""):
        """Return the [(seconds, text)] table for a track, or [] on any failure."""
        if not title:
            return []
        key = hashlib.sha1(("%s|%s" % (title, artist)).encode("utf-8")).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        lines = []
        try:
            pick = _pick(self._search("%s %s" % (title, artist)),
                         song_id, artist, album, duration)
            if pick:
                lines = self._fetch_lyric(pick.get("mid"))
        except Exception:
            lines = []
        if not lines:                       # QQ had nothing (or failed): try NetEase
            try:
                lines = self._fetch_fallback(title, artist)
            except Exception:
                lines = []
        if lines:
            self._cache_put(key, lines)
        return lines


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
          "position_s": 0.0, "duration_s": 0.0, "song_id": ""}


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
                session = mgr.get_current_session()
                if session is None:
                    self._write(dict(_EMPTY))
                else:
                    props = loop.run_until_complete(
                        session.try_get_media_properties_async())
                    ps = int(session.get_playback_info().playback_status)
                    tl = session.get_timeline_properties()
                    self._write({
                        "title": str(props.title or ""),
                        "artist": str(props.artist or ""),
                        "album": str(props.album_title or ""),
                        "status": ("playing" if ps == 4 else
                                   "paused" if ps == 5 else "none"),
                        "position_s": float(tl.position.total_seconds()),
                        "duration_s": float(tl.end_time.total_seconds()),
                        "song_id": _parse_song_id(props.genres),
                    })
            except Exception:
                self._write(dict(_EMPTY))   # a bad read must not kill the poller
            self._stop.wait(self.poll)


# --------------------------------------------------------------- overlay state
# Per-character reveal: the line's transition length now scales with how many
# glyphs it has -- each glyph fades in over LYRIC_CHAR, and each one starts
# LYRIC_STAGGER later than the previous, so the sentence types itself in.
LYRIC_STAGGER = 0.045
LYRIC_CHAR = 0.22


def word_u(t, dur, k):
    """Reveal progress (0..1) of glyph ``k`` at frame progress ``t`` of ``dur``.

    ``t`` is the 0..1 transition progress, ``dur`` the line's total transition
    length in seconds and ``k`` the glyph's index in the line. Glyph ``k``
    starts LYRIC_STAGGER*k seconds in and takes LYRIC_CHAR to finish, so the
    line reveals left to right.
    """
    u = (t * dur - k * LYRIC_STAGGER) / LYRIC_CHAR
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
            self._clock = 0.0

        raw = getattr(cfg, "lyrics_effect", "rotate")
        eff = self._pick(raw)

        if not lines or snap.get("status") != "playing":
            return {"visible": False, "text": "", "prev": "", "t": 1.0,
                    "effect": eff, "position_s": 0.0, "dur": LYRIC_CHAR}

        if snap.get("duration_s", 0.0) > 0:
            now = float(snap.get("position_s", 0.0))   # SMTC keeps the timeline
        else:
            self._clock += dt                          # no timeline: count frames
            now = self._clock

        idx = line_at(lines, now)
        text = lines[idx][1] if idx >= 0 else ""
        # The line's transition length is its glyph count: the last glyph starts
        # LYRIC_STAGGER*(n-1) in and takes LYRIC_CHAR, so dur covers the whole
        # left-to-right reveal. Same line -> same dur on every frame.
        dur = LYRIC_CHAR + LYRIC_STAGGER * max(0, len(text) - 1)
        if idx != self._idx:                           # a new line came up
            self._prev = lines[self._idx][1] if 0 <= self._idx < len(lines) else ""
            self._idx = idx
            self._t = 0.0
            if raw == "rotate":
                self._rotate += 1
                eff = self._pick(raw)
        else:
            self._t = min(1.0, self._t + dt / dur)

        prev = self._prev if self._t < 1.0 else ""
        return {"visible": bool(text), "text": text, "prev": prev,
                "t": self._t, "effect": eff, "position_s": now, "dur": dur}
