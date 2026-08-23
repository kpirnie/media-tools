#!/usr/bin/env python3
"""
KPTV Series Transcoder

Walks a downloaded series tree and re-encodes the media in place using VAAPI
hardware encoding, replacing each file only when the result is actually
smaller.

@package KPTV Proxy Tools
@author Kevin Pirnie <me@kpirnie.com>
@copyright Copyright (c) 2026
"""

# setup the imports
import argparse, json, logging, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# the render node the encoder binds to
DEFAULT_DEVICE = "/dev/dri/renderD128"

# encoder and device resolved per detected backend
BACKENDS = {
    "nvenc": {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "device": None},
    "vaapi": {"h264": "h264_vaapi", "hevc": "hevc_vaapi", "device": "/dev/dri/renderD128"},
}

# what we consider media worth looking at
MEDIA_EXTENSIONS = (".mp4", ".mkv", ".ts", ".avi", ".mov", ".m4v", ".mpg", ".mpeg", ".wmv")


def setup_logging(debug: bool) -> None:
    """
    Configure application logging

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

    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)


def cleanup(path: str) -> None:
    """
    Remove a file, ignoring the case where it is already gone

    @param path: str Path to remove
    @return None
    """

    try:
        os.remove(path)
    except OSError:
        pass


def detect_backend() -> Optional[str]:
    """
    Work out which hardware encoder this machine can actually use

    The encoder list is not enough on its own — VAAPI encoders are compiled
    in on boxes with no VAAPI driver behind them, so the render node and the
    NVIDIA device are checked directly.

    @return Optional[str]: Backend name, or None when there is no hardware path
    """

    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                capture_output=True, text=True, timeout=30)
        encoders = result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return None

    # nvidia first, a dedicated card beats an igpu every time
    if "hevc_nvenc" in encoders and os.path.exists("/dev/nvidiactl"):
        return "nvenc"

    if "hevc_vaapi" in encoders and os.path.exists(BACKENDS["vaapi"]["device"]):
        try:
            probe_va = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=30)
            if "VAEntrypointEncSlice" in probe_va.stdout:
                return "vaapi"
        except (subprocess.TimeoutExpired, OSError):
            pass

    return None


def is_hdr(stream: Dict[str, Any]) -> bool:
    """
    Decide whether a video stream carries a high dynamic range transfer

    @param stream: Dict[str, Any] Video stream from a probe result
    @return bool: True when the stream needs tonemapping
    """

    transfer = str(stream.get("color_transfer") or "").lower()
    return transfer in ("smpte2084", "arib-std-b67")


def has_libplacebo() -> bool:
    """
    Check whether the Vulkan tonemapping filter is available

    @return bool: True when libplacebo can be used
    """

    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return False

    return "libplacebo" in result.stdout and os.path.isdir("/usr/share/vulkan/icd.d")


def probe(path: str) -> Optional[Dict[str, Any]]:
    """
    Read the stream and format details for a media file

    @param path: str Path to the media file
    @return Optional[Dict[str, Any]]: Decoded ffprobe output, or None on failure
    """

    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("probe failed for %s: %s", os.path.basename(path), e)
        return None

    if result.returncode != 0:
        return None

    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def video_stream(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Pull the first real video stream out of a probe result

    Cover art and thumbnails show up as video streams too, so attached
    pictures are skipped.

    @param info: Dict[str, Any] Decoded ffprobe output
    @return Optional[Dict[str, Any]]: The video stream, or None when there isn't one
    """

    for stream in info.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        return stream

    return None


def collect_files(root: str, extensions: Tuple[str, ...]) -> List[str]:
    """
    Walk a directory tree for media files

    @param root: str Directory to walk, or a single file
    @param extensions: Tuple[str, ...] Extensions to consider media
    @return List[str]: Sorted list of candidate paths
    """

    # a single file is a perfectly valid target
    if os.path.isfile(root):
        return [root]

    found = []

    # walk it, skipping our own leftovers
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if ".transcode." in name or name.endswith(".part"):
                continue
            if name.lower().endswith(extensions):
                found.append(os.path.join(dirpath, name))

    return sorted(found)


def should_skip(path: str, args: argparse.Namespace) -> Optional[str]:
    """
    Decide whether a file is worth spending encoder time on

    @param path: str Path to the media file
    @param args: argparse.Namespace Parsed command line arguments
    @return Optional[str]: Reason to skip, or None when it should be encoded
    """

    size = os.path.getsize(path)

    # under the floor is not worth the round trip
    if size < args.min_size * 1048576:
        return "below size floor"

    info = probe(path)
    if not info:
        return "unreadable"

    stream = video_stream(info)
    if not stream:
        return "no video stream"

    codec = str(stream.get("codec_name") or "").lower()

    # already in the target codec, re-encoding only loses quality
    if not args.force and codec in ("hevc", "h265", "av1"):
        return f"already {codec}"

    # tonemapping needs a filter chain the hardware decode path cannot carry
    if is_hdr(stream) and args.hw_decode:
        return "hdr with --hw-decode"

    # tiny bitrates have nothing left to give
    try:
        bitrate = int((info.get("format") or {}).get("bit_rate") or 0)
    except (TypeError, ValueError):
        bitrate = 0

    if bitrate and args.min_bitrate and bitrate < args.min_bitrate * 1000:
        return f"bitrate {bitrate // 1000}k below floor"

    return None

def tonemap_chain(args: argparse.Namespace) -> List[str]:
    """
    Build the HDR to SDR filter steps

    libplacebo runs the conversion on the GPU over Vulkan; the zscale chain is
    the software fallback for boxes without a Vulkan ICD.

    @param args: argparse.Namespace Parsed command line arguments
    @return List[str]: Filter steps to insert ahead of the encoder
    """

    if args.libplacebo:
        return ["libplacebo=colorspace=bt709:color_primaries=bt709:color_trc=bt709:format=yuv420p"]

    return [
        "zscale=t=linear:npl=100",
        "format=gbrpf32le",
        "zscale=p=bt709",
        f"tonemap=tonemap={args.tonemap}:desat=0",
        "zscale=t=bt709:m=bt709:r=tv",
        "format=yuv420p",
    ]


def build_command(path: str, temp: str, args: argparse.Namespace, hdr: bool) -> List[str]:
    """
    Assemble the ffmpeg command line for the detected backend

    Decoding stays in software by default, which handles far more input formats
    than the full hardware path; only the encode is offloaded. HDR sources pick
    up a tonemapping chain, which rules out hardware decode since the filters
    need frames in system memory.

    @param path: str Source media path
    @param temp: str Destination temp path
    @param args: argparse.Namespace Parsed command line arguments
    @param hdr: bool Whether the source needs tonemapping
    @return List[str]: Argument vector for subprocess
    """

    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]

    # libplacebo needs a vulkan device initialised up front
    if hdr and args.libplacebo:
        cmd += ["-init_hw_device", "vulkan"]

    chain = []

    # full hardware path, decode included
    if args.hw_decode and not hdr:
        if args.backend == "nvenc":
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", path]
            if args.max_height:
                chain.append(f"scale_cuda=w=-2:h={args.max_height}")
        else:
            cmd += [
                "-hwaccel", "vaapi",
                "-hwaccel_device", args.device,
                "-hwaccel_output_format", "vaapi",
                "-i", path,
            ]
            if args.max_height:
                chain.append(f"scale_vaapi=w=-2:h={args.max_height}")

    # software decode, hardware encode — the compatible default
    else:
        if args.backend == "vaapi":
            cmd += ["-vaapi_device", args.device]
        cmd += ["-i", path]

        if args.max_height:
            chain.append(f"scale=-2:'min({args.max_height},ih)'")

        if hdr:
            chain += tonemap_chain(args)

        # vaapi wants frames on the card, nvenc takes them from system memory
        if args.backend == "vaapi":
            chain.append("format=nv12")
            chain.append("hwupload")

    if chain:
        cmd += ["-vf", ",".join(chain)]

    # keep every stream, then say how each type is handled
    cmd += [
        "-map", "0",
        "-c:v", args.vcodec,
        "-c:a", args.acodec,
        "-c:s", "copy",
        "-map_metadata", "0",
    ]

    # the two encoders spell constant quality differently
    if args.backend == "nvenc":
        cmd += ["-rc", "constqp", "-qp", str(args.qp)]
    else:
        cmd += ["-qp", str(args.qp)]

    # mp4 cannot carry the subtitle formats mkv can, so drop them there
    if temp.lower().endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]

    cmd.append(temp)
    return cmd

def transcode(path: str, args: argparse.Namespace) -> Tuple[bool, int, int]:
    """
    Re-encode a single file and replace it when the result is smaller

    The encode writes to a sibling temp file and only takes the original's
    place on a clean exit, so an interrupted run never destroys anything.

    @param path: str Path to the media file
    @param args: argparse.Namespace Parsed command line arguments
    @return Tuple[bool, int, int]: Replaced flag, original size, resulting size
    """

    original = os.path.getsize(path)

    stem, ext = os.path.splitext(path)
    container = args.container or ext.lstrip(".")
    temp = f"{stem}.transcode.{container}"

    info = probe(path)
    stream = video_stream(info) if info else None
    hdr = bool(stream and is_hdr(stream))

    if hdr:
        logger.info("tonemapping hdr source: %s", os.path.basename(path))

    cmd = build_command(path, temp, args, hdr)
    logger.debug("running: %s", " ".join(cmd))

    if args.dry_run:
        logger.info("would transcode: %s (%.1f MB)", os.path.basename(path), original / 1048576.0)
        return (False, original, original)

    logger.info("transcoding: %s (%.1f MB)", os.path.basename(path), original / 1048576.0)
    started = time.monotonic()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        logger.warning("timed out: %s", os.path.basename(path))
        cleanup(temp)
        return (False, original, original)
    except OSError as e:
        logger.error("ffmpeg not runnable: %s", e)
        cleanup(temp)
        return (False, original, original)

    if result.returncode != 0:
        logger.warning("failed: %s: %s", os.path.basename(path), result.stderr.strip()[:400])
        cleanup(temp)
        return (False, original, original)

    if not os.path.isfile(temp) or os.path.getsize(temp) == 0:
        logger.warning("produced nothing: %s", os.path.basename(path))
        cleanup(temp)
        return (False, original, original)

    shrunk = os.path.getsize(temp)
    elapsed = time.monotonic() - started

    # bigger result means the original was already efficient, keep it
    if shrunk >= original and not args.keep_larger:
        logger.info("no savings, keeping original: %s", os.path.basename(path))
        cleanup(temp)
        return (False, original, original)

    # the container may have changed, so retire the old file explicitly
    final = f"{stem}.{container}"
    os.replace(temp, final)
    if final != path:
        cleanup(path)

    logger.info("shrank %s: %.1f -> %.1f MB (%d%% smaller, %.0fs)",
                os.path.basename(final), original / 1048576.0, shrunk / 1048576.0,
                int(100 - (shrunk * 100 / original)), elapsed)

    return (True, original, shrunk)


def run(args: argparse.Namespace) -> int:
    """
    Walk the target and transcode everything that qualifies

    @param args: argparse.Namespace Parsed command line arguments
    @return int: Process exit code
    """

    # work out the hardware path before anything else
    if args.backend == "auto":
        detected = detect_backend()
        if not detected:
            logger.error("no usable hardware encoder found")
            return 1
        args.backend = detected

    logger.info("using %s backend", args.backend)

    if args.device == "auto":
        args.device = BACKENDS[args.backend]["device"]

    if args.device and not os.path.exists(args.device):
        logger.error("render device not found: %s", args.device)
        return 1

    if args.vcodec == "auto":
        args.vcodec = BACKENDS[args.backend]["hevc"]

    args.libplacebo = has_libplacebo()

    if not os.path.exists(args.path):
        logger.error("path not found: %s", args.path)
        return 1

    extensions = tuple(f".{e.strip().lower().lstrip('.')}" for e in args.extensions.split(","))
    files = collect_files(args.path, extensions)

    if not files:
        logger.info("no media found under %s", args.path)
        return 0

    logger.info("found %d file(s) under %s", len(files), args.path)

    # filter down to what is actually worth encoding
    queue = []
    for path in files:
        reason = should_skip(path, args)
        if reason:
            logger.debug("skipping %s: %s", os.path.basename(path), reason)
            continue
        queue.append(path)

    logger.info("%d file(s) qualify, %d skipped", len(queue), len(files) - len(queue))

    if not queue:
        return 0

    saved = 0
    replaced = 0

    # one encoder engine means one job by default, but allow the override
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for ok, before, after in pool.map(lambda p: transcode(p, args), queue):
            if ok:
                replaced += 1
                saved += before - after

    logger.info("done: %d replaced, %.2f GB reclaimed", replaced, saved / 1073741824.0)
    return 0


def main() -> int:
    """
    Parse arguments and hand off to the runner

    @return int: Process exit code
    """

    parser = argparse.ArgumentParser(
        description="Re-encode a downloaded series tree in place using VAAPI hardware encoding."
    )
    parser.add_argument("path", help="directory tree or single file to process")
    parser.add_argument("--device", default="auto", help="render node (default: auto)")
    parser.add_argument("--vcodec", default="auto", help="hardware encoder (default: auto)")
    parser.add_argument("--backend", default="auto", help="hardware backend (default: auto)")
    parser.add_argument("--tonemap", default="hable", help="software tonemap operator (default: hable)")
    parser.add_argument("--acodec", default="copy", help="audio handling (default: copy)")
    parser.add_argument("--qp", type=int, default=26, help="quality, lower is bigger (default: 26)")
    parser.add_argument("--max-height", type=int, help="cap output height, preserving aspect")
    parser.add_argument("--container", help="output container extension (default: keep source)")
    parser.add_argument("--min-size", type=int, default=500, help="skip files under this many MB (default: 500)")
    parser.add_argument("--min-bitrate", type=int, default=0, help="skip files under this many kbps")
    parser.add_argument("--extensions", default=",".join(e.lstrip(".") for e in MEDIA_EXTENSIONS),
                        help="comma separated extensions to consider")
    parser.add_argument("--hw-decode", action="store_true", help="decode on the GPU as well as encode")
    parser.add_argument("--force", action="store_true", help="re-encode even when already HEVC or AV1")
    parser.add_argument("--keep-larger", action="store_true", help="keep the result even when it grew")
    parser.add_argument("--jobs", type=int, default=1, help="parallel encodes (default: 1)")
    parser.add_argument("--timeout", type=int, default=14400, help="per-file timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="list what would be transcoded")
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