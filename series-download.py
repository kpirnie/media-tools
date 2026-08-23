#!/usr/bin/env python3
"""
KPTV Series Downloader

Downloads a full Xtreme Codes series from the sources configured in the
kptv-proxy database, laid out as Name -> Season -> Episode with sidecar
NFO metadata and artwork.

@package KPTV Proxy Tools
@author Kevin Pirnie <me@kpirnie.com>
@copyright Copyright (c) 2026
"""

# setup the imports
import argparse, json, logging, os, re, shutil, sqlite3, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# default location of the proxy's sqlite database inside the container/host mount
DEFAULT_DB = "/settings/kptv.db"

# characters that have no business being in a path segment
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def setup_logging(debug: bool) -> None:
    """
    Configure application logging

    Mirrors the proxy's own logging split: verbose with call sites when
    debugging, clean and quiet otherwise.

    @param debug: bool Whether to enable debug level output
    @return None
    """

    # pick the format based on the debug flag
    if debug:
        fmt = "%(asctime)s [%(levelname)s] %(funcName)s:%(lineno)d - %(message)s"
        level = logging.DEBUG
    else:
        fmt = "%(asctime)s [%(levelname)s] %(message)s"
        level = logging.INFO

    class ProgressHandler(logging.StreamHandler):
        """
        Stream handler that clears the progress block before each record
        """

        def emit(self, record: logging.LogRecord) -> None:

            with PROGRESS:
                super().emit(record)

    handler = ProgressHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=level, handlers=[handler])

    # keep the third party loggers quiet unless we're debugging
    if not debug:
        for name in ("urllib3", "requests"):
            logging.getLogger(name).setLevel(logging.WARNING)


def sanitize(name: str) -> str:
    """
    Clean a string for safe use as a single path segment

    @param name: str Raw name from the provider
    @return str: Filesystem safe segment, never empty
    """

    # strip the illegal characters and collapse the whitespace
    cleaned = INVALID_PATH_CHARS.sub("", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)

    # trailing dots and spaces break on some filesystems
    cleaned = cleaned.rstrip(". ")

    # never hand back nothing
    return cleaned or "Unknown"


def normalize_extension(ext: str) -> str:
    """
    Normalize a container extension the way the proxy does

    @param ext: str Raw container_extension value from the provider
    @return str: Bare extension with no leading dot, defaulting to mp4
    """

    # strip it down and drop any leading dot
    cleaned = (ext or "").strip().lstrip(".").lower()
    return cleaned or "mp4"


class RateLimiter:
    """
    Simple per-source request pacer

    Matches the proxy's limiter: max_cnx requests per second, shared by every
    thread hitting that source.
    """

    def __init__(self, per_second: int):

        self._interval = 1.0 / float(per_second) if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def take(self) -> None:
        """
        Block until the next request slot is available

        @return None
        """

        # no pacing configured, let it fly
        if self._interval <= 0:
            return

        # hand out slots one at a time
        with self._lock:
            now = time.monotonic()
            if self._next < now:
                self._next = now
            wait = self._next - now
            self._next += self._interval

        if wait > 0:
            time.sleep(wait)

class Progress:
    """
    Live multi-transfer progress display

    Keeps one line per in-flight download and repaints the block in place.
    Silently does nothing when stdout is not a terminal, so piped and cron
    output stays clean.
    """

    def __init__(self, enabled: bool):

        self.enabled = enabled and sys.stdout.isatty()
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._drawn = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """
        Begin repainting the progress block on its own thread

        @return None
        """

        if not self.enabled or self._thread:
            return

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """
        Stop repainting and clear the block

        @return None
        """

        if not self.enabled:
            return

        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self._clear()

    def add(self, key: str, total: int) -> None:
        """
        Register a transfer as started

        @param key: str Short label for this transfer
        @param total: int Expected byte count, zero when unknown
        @return None
        """

        with self._lock:
            self._active[key] = {"total": total, "written": 0, "start": time.monotonic()}

    def update(self, key: str, written: int) -> None:
        """
        Record progress for a transfer

        @param key: str Label the transfer was registered under
        @param written: int Bytes written so far
        @return None
        """

        with self._lock:
            if key in self._active:
                self._active[key]["written"] = written

    def remove(self, key: str) -> None:
        """
        Drop a finished or failed transfer from the display

        @param key: str Label the transfer was registered under
        @return None
        """

        with self._lock:
            self._active.pop(key, None)
            self._clear()

    def pause(self):
        """
        Context manager that clears the block so a log line can be written

        @return Progress: Self, for use as a context manager
        """

        return self

    def __enter__(self):

        if self.enabled:
            self._lock.acquire()
            self._clear()
        return self

    def __exit__(self, *exc: Any) -> None:

        if self.enabled:
            self._lock.release()

    def _loop(self) -> None:
        """
        Repaint the block a couple of times a second until stopped

        @return None
        """

        while not self._stop.wait(0.4):
            with self._lock:
                self._draw()

    def _clear(self) -> None:
        """
        Erase the currently painted block

        @return None
        """

        if not self.enabled or self._drawn == 0:
            return

        sys.stdout.write("\033[%dA\033[J" % self._drawn)
        sys.stdout.flush()
        self._drawn = 0

    def _draw(self) -> None:
        """
        Paint one line per active transfer

        @return None
        """

        if not self.enabled:
            return

        self._clear()

        width = shutil.get_terminal_size((100, 24)).columns
        lines = []

        # snapshot each transfer into a rendered line
        for key, item in list(self._active.items()):
            written = item["written"]
            total = item["total"]
            elapsed = max(0.001, time.monotonic() - item["start"])
            rate = written / elapsed / 1048576.0

            if total > 0:
                pct = min(100, int(written * 100 / total))
                filled = int(pct / 5)
                bar = "#" * filled + "-" * (20 - filled)
                line = "  %-12s [%s] %3d%%  %6.1f/%.1f MB  %5.1f MB/s" % (
                    key, bar, pct, written / 1048576.0, total / 1048576.0, rate
                )
            else:
                line = "  %-12s %6.1f MB  %5.1f MB/s" % (key, written / 1048576.0, rate)

            lines.append(line[: width - 1])

        if not lines:
            return

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        self._drawn = len(lines)


# shared across the download workers, replaced once the run starts
PROGRESS = Progress(False)

class Source:
    """
    A single kp_sources row plus the plumbing needed to talk to it

    Carries the provider credentials, the connection ceiling and the rate
    limiter so every request out of this script obeys the same limits the
    proxy itself does.
    """

    def __init__(self, row: sqlite3.Row):

        self.id = row["id"]
        self.name = row["name"]
        self.url = str(row["uri"]).rstrip("/")
        self.username = row["uname"]
        self.password = row["pword"]
        self.max_cnx = int(row["max_cnx"] or 1)
        self.max_retries = int(row["max_retries"] or 3)
        self.user_agent = row["user_agent"] or "VLC/3.0.20 LibVLC/3.0.20"
        self.req_origin = row["req_origin"] or ""
        self.req_referer = row["req_referer"] or ""

        # the connection ceiling doubles as the request pacing, same as the proxy
        self.limiter = RateLimiter(self.max_cnx)
        self.slots = threading.BoundedSemaphore(max(1, self.max_cnx))

        # pooled session sized to the allowed connections
        self.session = requests.Session()
        retry = Retry(
            total=self.max_retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
        )
        adapter = HTTPAdapter(
            pool_connections=max(1, self.max_cnx),
            pool_maxsize=max(1, self.max_cnx),
            max_retries=retry,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def headers(self) -> Dict[str, str]:
        """
        Build the request headers this source expects

        @return Dict[str, str]: Header map including the configured agent/origin/referer
        """

        # start with what every request gets
        hdrs = {"User-Agent": self.user_agent, "Connection": "keep-alive"}

        # only send the optional ones when the source actually sets them
        if self.req_origin:
            hdrs["Origin"] = self.req_origin
        if self.req_referer:
            hdrs["Referer"] = self.req_referer

        return hdrs

    def api(self, action: str, **params: str) -> Optional[Any]:
        """
        Call a player_api.php action on this source

        @param action: str The Xtreme Codes action name
        @param params: str Additional query parameters for the action
        @return Optional[Any]: Decoded JSON response, or None on any failure
        """

        # build the url with the credentials baked in
        query = {
            "username": self.username,
            "password": self.password,
            "action": action,
        }
        query.update(params)
        url = f"{self.url}/player_api.php"

        # respect the connection ceiling and the pacing before going out
        with self.slots:
            self.limiter.take()
            try:
                resp = self.session.get(
                    url, params=query, headers=self.headers(), timeout=60
                )
            except requests.RequestException as e:
                logger.warning("%s: %s request failed: %s", self.name, action, e)
                return None

            if resp.status_code != 200:
                logger.warning(
                    "%s: %s returned HTTP %d", self.name, action, resp.status_code
                )
                return None

            try:
                return resp.json()
            except ValueError as e:
                logger.warning("%s: %s returned invalid JSON: %s", self.name, action, e)
                return None

    def episode_url(self, upstream_id: str, extension: str) -> str:
        """
        Build the direct episode file url for this source

        @param upstream_id: str The provider's own episode id
        @param extension: str Container extension without the dot
        @return str: Fully qualified download url
        """

        return f"{self.url}/series/{self.username}/{self.password}/{upstream_id}.{extension}"

    def fetch_bytes(self, url: str, timeout: int = 60) -> Optional[bytes]:
        """
        Pull a small file (artwork) from an arbitrary url

        @param url: str Absolute url to fetch
        @param timeout: int Request timeout in seconds
        @return Optional[bytes]: The response body, or None on failure
        """

        # artwork is usually off-panel, but still counts against the source
        with self.slots:
            self.limiter.take()
            try:
                resp = self.session.get(url, headers=self.headers(), timeout=timeout)
                if resp.status_code == 200 and resp.content:
                    return resp.content
            except requests.RequestException as e:
                logger.debug("artwork fetch failed for %s: %s", url, e)

        return None


def load_sources(db_path: str, only: Optional[str]) -> List[Source]:
    """
    Read the Xtreme Codes sources out of the proxy database

    Only sources carrying credentials are usable here — plain M3U sources have
    no series API to query.

    @param db_path: str Path to the proxy's kptv.db
    @param only: Optional[str] Restrict to a single source by name or id
    @return List[Source]: Sources in the proxy's own sort order
    @throws SystemExit: When the database cannot be opened
    """

    conn = open_db(db_path)

    rows = conn.execute(
        """SELECT id, name, uri, uname, pword, sort_order, max_cnx, max_retries,
                  user_agent, req_origin, req_referer
           FROM kp_sources
           WHERE uname <> '' AND pword <> ''
           ORDER BY sort_order ASC"""
    ).fetchall()
    conn.close()

    sources = []

    # build each one, honoring the optional single source restriction
    for row in rows:
        if only and str(only) != str(row["id"]) and only.lower() != str(row["name"]).lower():
            continue
        sources.append(Source(row))

    return sources


def open_db(db_path: str) -> sqlite3.Connection:
    """
    Open the proxy database read only

    Falls back to a normal open when the read only handle cannot be created,
    which happens when the WAL sidecar files are not reachable.

    @param db_path: str Path to the proxy's kptv.db
    @return sqlite3.Connection: Open connection with row access by name
    @throws SystemExit: When the database cannot be opened at all
    """

    # make sure it actually exists before we try
    if not os.path.isfile(db_path):
        logger.error("database not found: %s", db_path)
        sys.exit(1)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(db_path, timeout=15)
        except sqlite3.Error as e:
            logger.error("could not open %s: %s", db_path, e)
            sys.exit(1)

    conn.row_factory = sqlite3.Row
    return conn


def cached_candidates(db_path: str, series_name: str, exact: bool) -> List[Tuple[str, str]]:
    """
    Find series already cached in kp_series_info by name

    The proxy stores the rendered get_series_info payload per source and series,
    so a series a client has already drilled into can be matched without hitting
    the provider at all.

    @param db_path: str Path to the proxy's kptv.db
    @param series_name: str Name being searched for
    @param exact: bool Require an exact case-insensitive name match
    @return List[Tuple[str, str]]: (source_url, series_id) pairs that matched
    """

    conn = open_db(db_path)
    rows = conn.execute(
        "SELECT source_url, series_id, payload FROM kp_series_info"
    ).fetchall()
    conn.close()

    wanted = series_name.strip().lower()
    hits = []

    # dig the provider's name back out of each cached payload
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            continue

        info = payload.get("info") or {}
        if not isinstance(info, dict):
            continue

        name = str(info.get("name") or info.get("title") or "").strip().lower()
        if not name:
            continue

        if (exact and name == wanted) or (not exact and wanted in name):
            hits.append((row["source_url"], row["series_id"]))

    return hits


def cached_series_info(db_path: str, source_url: str, series_id: str) -> Optional[Dict[str, Any]]:
    """
    Load a cached series payload and translate it back to upstream ids

    The stored payload carries the proxy's own episode ids, so kp_series_episodes
    is joined back in to recover the provider's ids and container extensions.

    @param db_path: str Path to the proxy's kptv.db
    @param source_url: str Source url the series belongs to
    @param series_id: str Provider's series id
    @return Optional[Dict[str, Any]]: Series info with upstream ids restored, or None
    """

    conn = open_db(db_path)

    row = conn.execute(
        "SELECT payload FROM kp_series_info WHERE source_url = ? AND series_id = ?",
        (source_url, series_id),
    ).fetchone()

    if not row or not row["payload"]:
        conn.close()
        return None

    # pull the id mappings for this series so we can undo the proxy's rewrite
    maps = conn.execute(
        """SELECT episode_id, upstream_id, extension
           FROM kp_series_episodes WHERE source_url = ? AND series_id = ?""",
        (source_url, series_id),
    ).fetchall()
    conn.close()

    mapping = {str(m["episode_id"]): (m["upstream_id"], m["extension"]) for m in maps}

    # no mappings means the payload is useless to us, the ids point at the proxy
    if not mapping:
        return None

    try:
        payload = json.loads(row["payload"])
    except ValueError:
        return None

    episodes = payload.get("episodes") or {}
    restored: Dict[str, List[Dict[str, Any]]] = {}

    # swap every proxy id back for the provider's own
    for season, eps in episodes.items():
        out = []
        for ep in eps:
            found = mapping.get(str(ep.get("id")))
            if not found:
                continue
            ep = dict(ep)
            ep["id"] = found[0]
            ep["container_extension"] = normalize_extension(found[1])
            ep.pop("direct_source", None)
            out.append(ep)
        if out:
            restored[season] = out

    # a partial mapping is worse than none, fall through to a live fetch
    if not restored:
        return None

    payload["episodes"] = restored
    return payload


def find_series(sources: List[Source], series_name: str, exact: bool) -> List[Tuple[Source, str, str]]:
    """
    Locate a series by name across every configured source

    @param sources: List[Source] Sources to search, in proxy sort order
    @param series_name: str Name being searched for
    @param exact: bool Require an exact case-insensitive name match
    @return List[Tuple[Source, str, str]]: (source, series_id, provider name) matches
    """

    wanted = series_name.strip().lower()
    matches = []

    # walk every source so we end up with fallbacks, not just the first hit
    for src in sources:
        listing = src.api("get_series")
        if not isinstance(listing, list):
            logger.warning("%s: no series listing returned", src.name)
            continue

        for entry in listing:
            name = str(entry.get("name") or "").strip()
            if not name:
                continue

            low = name.lower()
            if (exact and low == wanted) or (not exact and wanted in low):
                matches.append((src, str(entry.get("series_id")), name))

    return matches


def fetch_series_info(source: Source, series_id: str) -> Optional[Dict[str, Any]]:
    """
    Pull the season and episode tree for a series straight from the provider

    Handles both the season keyed object most panels emit and the flat array a
    few of them return instead.

    @param source: Source The source to query
    @param series_id: str Provider's series id
    @return Optional[Dict[str, Any]]: Normalized series info, or None on failure
    """

    data = source.api("get_series_info", series_id=series_id)
    if not isinstance(data, dict):
        return None

    episodes = data.get("episodes")
    normalized: Dict[str, List[Dict[str, Any]]] = {}

    # season keyed object, the common shape
    if isinstance(episodes, dict):
        for season, eps in episodes.items():
            if isinstance(eps, list) and eps:
                normalized[str(season)] = eps

    # flat array, bucket it by the episode's own season field
    elif isinstance(episodes, list):
        for ep in episodes:
            season = str(ep.get("season") or "1")
            normalized.setdefault(season, []).append(ep)

    if not normalized:
        return None

    data["episodes"] = normalized
    return data


def episode_key(ep: Dict[str, Any], season: str) -> Tuple[int, int]:
    """
    Build the season/episode pair used to match episodes across sources

    @param ep: Dict[str, Any] Episode entry from a series info payload
    @param season: str Season key the episode was filed under
    @return Tuple[int, int]: Season number and episode number
    """

    def as_int(value: Any, fallback: int) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return fallback

    return (as_int(ep.get("season") or season, 0), as_int(ep.get("episode_num"), 0))


def write_text(path: str, content: str) -> None:
    """
    Write a text sidecar, creating the parent directory as needed

    @param path: str Destination file path
    @param content: str Text to write
    @return None
    """

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_binary(path: str, content: bytes) -> None:
    """
    Write a binary sidecar (artwork), creating the parent directory as needed

    @param path: str Destination file path
    @param content: bytes Data to write
    @return None
    """

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def nfo_string(root: ET.Element) -> str:
    """
    Render an NFO element tree to a writable string

    @param root: ET.Element Root element of the NFO document
    @return str: XML declaration plus the serialized tree
    """

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n' + body + "\n"


def add_child(parent: ET.Element, tag: str, value: Any) -> None:
    """
    Append a child element when the value is actually present

    @param parent: ET.Element Element to append to
    @param tag: str Child tag name
    @param value: Any Value to write as text
    @return None
    """

    # skip the empties, no point writing dead tags
    if value in (None, "", [], {}):
        return

    child = ET.SubElement(parent, tag)
    child.text = str(value)


def write_series_meta(series_dir: str, info: Dict[str, Any], source: Source, overwrite: bool) -> None:
    """
    Write the series level metadata and artwork

    Produces tvshow.nfo plus poster/fanart in the layout Emby, Jellyfin and Plex
    all read from.

    @param series_dir: str Root directory for this series
    @param info: Dict[str, Any] The series info payload's info block
    @param source: Source Source used to pull the artwork
    @param overwrite: bool Replace existing sidecars
    @return None
    """

    root = ET.Element("tvshow")
    add_child(root, "title", info.get("name") or info.get("title"))
    add_child(root, "originaltitle", info.get("o_name"))
    add_child(root, "plot", info.get("plot"))
    add_child(root, "premiered", info.get("releaseDate") or info.get("releasedate"))
    add_child(root, "year", str(info.get("releaseDate") or info.get("releasedate") or "")[:4])
    add_child(root, "rating", info.get("rating"))
    add_child(root, "mpaa", info.get("rating_5based") and info.get("age"))
    add_child(root, "studio", info.get("director"))
    add_child(root, "runtime", info.get("episode_run_time"))
    add_child(root, "tmdbid", info.get("tmdb") or info.get("tmdb_id"))

    # genres come back as a comma delimited string
    for genre in str(info.get("genre") or "").split(","):
        add_child(root, "genre", genre.strip())

    # cast is a plain string too, not structured
    for actor in str(info.get("cast") or "").split(","):
        name = actor.strip()
        if name:
            add_child(ET.SubElement(root, "actor"), "name", name)

    nfo_path = os.path.join(series_dir, "tvshow.nfo")
    if overwrite or not os.path.exists(nfo_path):
        write_text(nfo_path, nfo_string(root))

    # keep the raw payload around, it holds more than the nfo has tags for
    raw_path = os.path.join(series_dir, "series.json")
    if overwrite or not os.path.exists(raw_path):
        write_text(raw_path, json.dumps(info, indent=2))

    # now the artwork
    for key, filename in (("cover", "poster.jpg"), ("cover_big", "poster.jpg"),
                          ("backdrop_path", "fanart.jpg")):
        url = info.get(key)

        # backdrops come back as a list on most panels
        if isinstance(url, list):
            url = url[0] if url else None
        if not url:
            continue

        dest = os.path.join(series_dir, filename)
        if os.path.exists(dest) and not overwrite:
            continue

        data = source.fetch_bytes(str(url))
        if data:
            write_binary(dest, data)
            logger.debug("wrote %s", dest)


def write_season_meta(series_dir: str, season_num: int, seasons: Any, source: Source, overwrite: bool) -> None:
    """
    Write the season poster when the provider supplies one

    @param series_dir: str Root directory for this series
    @param season_num: int Season number being written
    @param seasons: Any The seasons block from the series info payload
    @param source: Source Source used to pull the artwork
    @param overwrite: bool Replace existing artwork
    @return None
    """

    # the seasons block is optional and shaped differently per panel
    if not isinstance(seasons, list):
        return

    for season in seasons:
        if not isinstance(season, dict):
            continue

        try:
            num = int(str(season.get("season_number")).strip())
        except (TypeError, ValueError):
            continue

        if num != season_num:
            continue

        url = season.get("cover_big") or season.get("cover")
        if not url:
            return

        dest = os.path.join(series_dir, f"season{season_num:02d}-poster.jpg")
        if os.path.exists(dest) and not overwrite:
            return

        data = source.fetch_bytes(str(url))
        if data:
            write_binary(dest, data)
            logger.debug("wrote %s", dest)
        return


def write_episode_meta(base_path: str, ep: Dict[str, Any], series_title: str,
                       season_num: int, episode_num: int, source: Source,
                       overwrite: bool) -> None:
    """
    Write the episode NFO and thumbnail beside the media file

    @param base_path: str Episode file path without its extension
    @param ep: Dict[str, Any] Episode entry from the series info payload
    @param series_title: str Series display name
    @param season_num: int Season number
    @param episode_num: int Episode number
    @param source: Source Source used to pull the thumbnail
    @param overwrite: bool Replace existing sidecars
    @return None
    """

    info = ep.get("info") or {}
    if not isinstance(info, dict):
        info = {}

    root = ET.Element("episodedetails")
    add_child(root, "title", ep.get("title") or info.get("name"))
    add_child(root, "showtitle", series_title)
    add_child(root, "season", season_num)
    add_child(root, "episode", episode_num)
    add_child(root, "plot", info.get("plot") or info.get("description"))
    add_child(root, "aired", info.get("releasedate") or info.get("air_date") or ep.get("added"))
    add_child(root, "rating", info.get("rating"))
    add_child(root, "runtime", info.get("duration_secs") and int(info["duration_secs"]) // 60)
    add_child(root, "tmdbid", info.get("tmdb_id"))

    nfo_path = f"{base_path}.nfo"
    if overwrite or not os.path.exists(nfo_path):
        write_text(nfo_path, nfo_string(root))

    # thumbnail, named the way the media servers expect to find it
    thumb = info.get("movie_image") or info.get("cover_big") or info.get("cover")
    if not thumb:
        return

    dest = f"{base_path}-thumb.jpg"
    if os.path.exists(dest) and not overwrite:
        return

    data = source.fetch_bytes(str(thumb))
    if data:
        write_binary(dest, data)


def download_file(source: Source, url: str, dest: str, overwrite: bool) -> bool:
    """
    Stream an episode file to disk

    Downloads to a .part file and renames on completion so an interrupted run
    never leaves a truncated file looking finished.

    @param source: Source Source the file belongs to, for headers and limits
    @param url: str Absolute download url
    @param dest: str Final destination path
    @param overwrite: bool Re-download even when the file already exists
    @return bool: True when the file is on disk and complete
    """

    # already have it, nothing to do
    if os.path.exists(dest) and not overwrite:
        logger.info("exists, skipping: %s", os.path.basename(dest))
        return True

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    partial = f"{dest}.part"

    # take a connection slot for the whole transfer, not just the request
    with source.slots:
        source.limiter.take()
        try:
            with source.session.get(
                url, headers=source.headers(), stream=True, timeout=(30, 120)
            ) as resp:

                if resp.status_code != 200:
                    logger.warning(
                        "%s: HTTP %d for %s", source.name, resp.status_code, os.path.basename(dest)
                    )
                    return False

                expected = int(resp.headers.get("Content-Length") or 0)
                written = 0

                # the season/episode tag is enough to identify the line
                label = re.search(r"S\d{2}E\d{2}", os.path.basename(dest))
                key = label.group(0) if label else os.path.basename(dest)[:12]
                PROGRESS.add(key, expected)

                try:
                    with open(partial, "wb") as handle:
                        for chunk in resp.iter_content(chunk_size=1024 * 512):
                            if chunk:
                                handle.write(chunk)
                                written += len(chunk)
                                PROGRESS.update(key, written)
                finally:
                    PROGRESS.remove(key)

        except requests.RequestException as e:
            logger.warning("%s: download failed for %s: %s", source.name, os.path.basename(dest), e)
            if os.path.exists(partial):
                os.remove(partial)
            return False

    # short read means the provider cut us off, treat it as a failure
    if expected and written < expected:
        logger.warning(
            "%s: truncated %s (%d of %d bytes)", source.name, os.path.basename(dest), written, expected
        )
        os.remove(partial)
        return False

    # nothing at all came back
    if written == 0:
        logger.warning("%s: empty response for %s", source.name, os.path.basename(dest))
        os.remove(partial)
        return False

    os.replace(partial, dest)
    logger.info("downloaded: %s (%.1f MB)", os.path.basename(dest), written / 1048576.0)
    return True


def resolve_series_info(db_path: str, source: Source, series_id: str,
                        use_cache: bool) -> Optional[Dict[str, Any]]:
    """
    Get a series tree from the proxy cache when possible, the provider otherwise

    @param db_path: str Path to the proxy's kptv.db
    @param source: Source Source the series belongs to
    @param series_id: str Provider's series id
    @param use_cache: bool Allow the cached payload to satisfy the request
    @return Optional[Dict[str, Any]]: Series info payload, or None
    """

    # try the proxy's own cache first, it saves a round trip entirely
    if use_cache:
        cached = cached_series_info(db_path, source.url, series_id)
        if cached:
            logger.info("%s: using cached series info for %s", source.name, series_id)
            return cached

    return fetch_series_info(source, series_id)

def process_episode(job: Dict[str, Any]) -> bool:
    """
    Download a single episode and write its sidecars

    Runs on the download pool, so everything it touches is either per-episode
    or already guarded.

    @param job: Dict[str, Any] Everything needed to fetch and file one episode
    @return bool: True when the episode landed on disk
    """

    src = job["source"]
    ep = job["episode"]
    args = job["args"]

    # the provider's own url when we have it, ours when we don't
    url = str(ep.get("direct_source") or "").strip() or src.episode_url(str(ep.get("id")), job["extension"])
    ok = download_file(src, url, job["dest"], args.overwrite)

    # primary failed, walk the other sources carrying this series
    if not ok:
        ok = try_fallbacks(job["fallbacks"], args, job["season"], job["episode_num"],
                           job["base_path"], job["extension"])

    if ok:
        write_episode_meta(job["base_path"], ep, job["series_title"], job["season"],
                           job["episode_num"], src, args.overwrite)
    else:
        logger.error("failed on every source: S%02dE%02d", job["season"], job["episode_num"])

    return ok

def run(args: argparse.Namespace) -> int:
    """
    Locate the series and pull every episode down

    @param args: argparse.Namespace Parsed command line arguments
    @return int: Process exit code
    """

    sources = load_sources(args.db, args.source)
    if not sources:
        logger.error("no Xtreme Codes sources found in %s", args.db)
        return 1

    logger.info("loaded %d source(s) from %s", len(sources), args.db)

    candidates: List[Tuple[Source, str, str]] = []

    # several sources can sit on the same panel url with different credentials,
    # and source_url is all the proxy stores, so a cached row maps to every one
    by_url: Dict[str, List[Source]] = {}
    for src in sources:
        by_url.setdefault(src.url, []).append(src)

    seen = set()

    # fast path, the proxy may already have this series cached
    if not args.no_cache:
        for source_url, series_id in cached_candidates(args.db, args.series, args.exact):
            for src in by_url.get(source_url.rstrip("/"), []):
                if (src.url, series_id) in seen:
                    continue
                candidates.append((src, series_id, args.series))
                seen.add((src.url, series_id))
                logger.info("%s: matched series %s from cache (%s)", src.name, series_id, source_url)

    # the cache only covers series a client already drilled into, so always ask
    # the providers too — that is where the cross-source fallbacks come from
    for fsrc, fsid, fname in find_series(sources, args.series, args.exact):
        if (fsrc.url, fsid) in seen:
            continue
        seen.add((fsrc.url, fsid))
        candidates.append((fsrc, fsid, fname))

    if not candidates:
        logger.error("series not found on any source: %s", args.series)
        return 1

    for src, sid, name in candidates:
        logger.info("candidate: %s on %s (series_id %s)", name, src.name, sid)

    # the first candidate drives the tree, the rest are fallbacks
    info_payload = None
    primary: Optional[Tuple[Source, str, str]] = None

    for candidate in candidates:
        src, sid, name = candidate
        payload = resolve_series_info(args.db, src, sid, not args.no_cache)
        if payload:
            info_payload = payload
            primary = candidate
            break
        logger.warning("%s: no series info for %s, trying next source", src.name, sid)

    if not info_payload or not primary:
        logger.error("could not retrieve series info from any source")
        return 1

    src, series_id, provider_name = primary
    info_block = info_payload.get("info") or {}
    if not isinstance(info_block, dict):
        info_block = {}

    series_title = str(info_block.get("name") or provider_name or args.series).strip()
    series_dir = os.path.join(args.output, sanitize(series_title))

    logger.info("writing to %s", series_dir)

    if not args.dry_run:
        os.makedirs(series_dir, exist_ok=True)
        write_series_meta(series_dir, info_block, src, args.overwrite)

    # pre-load the fallback trees lazily, keyed by season/episode
    fallbacks: List[Tuple[Source, str, Optional[Dict[Tuple[int, int], Dict[str, Any]]]]] = [
        (fsrc, fsid, None) for fsrc, fsid, _ in candidates if (fsrc, fsid) != (src, series_id)
    ]

    episodes = info_payload.get("episodes") or {}
    jobs: List[Dict[str, Any]] = []
    total = 0

    # walk the seasons in numeric order so the output reads sanely
    for season_key in sorted(episodes.keys(), key=lambda s: int(re.sub(r"\D", "", s) or 0)):
        season_eps = episodes[season_key]

        try:
            season_num = int(re.sub(r"\D", "", str(season_key)) or 0)
        except ValueError:
            season_num = 0

        # optional single season restriction
        if args.season is not None and season_num != args.season:
            continue

        season_dir = os.path.join(series_dir, f"Season {season_num:02d}")

        if not args.dry_run:
            os.makedirs(season_dir, exist_ok=True)
            write_season_meta(series_dir, season_num, info_payload.get("seasons"), src, args.overwrite)

        # then every episode within it, in order
        for ep in sorted(season_eps, key=lambda e: episode_key(e, season_key)[1]):
            _, ep_num = episode_key(ep, season_key)
            ep_title = str(ep.get("title") or "").strip()
            extension = normalize_extension(ep.get("container_extension"))

            # strip a leading series name off the episode title, it's redundant here
            if ep_title.lower().startswith(series_title.lower()):
                ep_title = ep_title[len(series_title):].lstrip(" -_")

            tag = f"S{season_num:02d}E{ep_num:02d}"
            stem = f"{sanitize(series_title)} - {tag}"
            if ep_title:
                stem = f"{stem} - {sanitize(ep_title)}"

            base_path = os.path.join(season_dir, stem)
            dest = f"{base_path}.{extension}"
            total += 1

            if args.dry_run:
                logger.info("would download: %s", dest)
                continue

            jobs.append({
                "source": src, "episode": ep, "args": args, "fallbacks": fallbacks,
                "series_title": series_title, "season": season_num, "episode_num": ep_num,
                "base_path": base_path, "dest": dest, "extension": extension,
            })

    if args.dry_run:
        logger.info("done: %d episode(s)", total)
        return 0

    # leave one connection free for the api and artwork calls
    workers = args.jobs if args.jobs else max(1, src.max_cnx - 1)
    logger.info("downloading %d episode(s) with %d worker(s)", len(jobs), workers)

    global PROGRESS
    PROGRESS = Progress(not args.no_progress)
    PROGRESS.start()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(process_episode, jobs))
    finally:
        PROGRESS.stop()

    failed = results.count(False)
    logger.info("done: %d episode(s), %d failed", total, failed)
    return 0 if failed == 0 else 2

# fallback trees are built lazily and shared across the download workers
FALLBACK_LOCK = threading.Lock()

def try_fallbacks(fallbacks: List[Tuple[Source, str, Optional[Dict[Tuple[int, int], Dict[str, Any]]]]],
                  args: argparse.Namespace, season_num: int, ep_num: int,
                  base_path: str, extension: str) -> bool:
    """
    Retry a failed episode against the other sources carrying the same series

    Episode ids are per provider, so the fallback trees are matched on season
    and episode number rather than id.

    @param fallbacks: List Fallback sources, with their lazily built episode index
    @param args: argparse.Namespace Parsed command line arguments
    @param season_num: int Season number of the failed episode
    @param ep_num: int Episode number of the failed episode
    @param base_path: str Episode file path without its extension
    @param extension: str Container extension from the primary source
    @return bool: True when a fallback delivered the file
    """

    for idx, (fsrc, fsid, index) in enumerate(fallbacks):

        # build this source's index the first time we need it, once across all workers
        if index is None:
            with FALLBACK_LOCK:
                fsrc, fsid, index = fallbacks[idx]
                if index is None:
                    payload = resolve_series_info(args.db, fsrc, fsid, not args.no_cache)
                    index = {}
                    if payload:
                        for skey, eps in (payload.get("episodes") or {}).items():
                            for fep in eps:
                                index[episode_key(fep, skey)] = fep
                    fallbacks[idx] = (fsrc, fsid, index)

        fep = index.get((season_num, ep_num))
        if not fep:
            continue

        fext = normalize_extension(fep.get("container_extension")) or extension
        logger.info("%s: falling back for S%02dE%02d", fsrc.name, season_num, ep_num)

        url = str(fep.get("direct_source") or "").strip() or fsrc.episode_url(str(fep.get("id")), fext)
        if download_file(fsrc, url, f"{base_path}.{fext}", args.overwrite):
            return True

    return False


def main() -> int:
    """
    Parse arguments and hand off to the runner

    @return int: Process exit code
    """

    parser = argparse.ArgumentParser(
        description="Download an Xtreme Codes series from the kptv-proxy sources, "
                    "laid out as Name / Season / Episode with metadata and artwork."
    )
    parser.add_argument("-s", "--series", required=True, help="series name to download")
    parser.add_argument("-o", "--output", required=True, help="output directory")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"path to kptv.db (default: {DEFAULT_DB})")
    parser.add_argument("--source", help="restrict to a single source by name or id")
    parser.add_argument("--season", type=int, help="download only this season number")
    parser.add_argument("--exact", action="store_true", help="require an exact name match")
    parser.add_argument("--overwrite", action="store_true", help="re-download existing files")
    parser.add_argument("--no-cache", action="store_true", help="ignore the proxy's cached series info")
    parser.add_argument("--jobs", type=int, help="parallel downloads (default: source max_cnx - 1)")
    parser.add_argument("--no-progress", action="store_true", help="disable the live progress display")
    parser.add_argument("--dry-run", action="store_true", help="list what would be downloaded")
    parser.add_argument("--debug", action="store_true", help="verbose logging")

    args = parser.parse_args()
    setup_logging(args.debug)

    try:
        return run(args)
    except KeyboardInterrupt:
        logger.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())