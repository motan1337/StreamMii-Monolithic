#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamMii-Monolithic v4
Convert media into Wii / WiiMC friendly formats.

v4 is a safety + correctness release over v3.
Nothing here is a rewrite for the sake of it  every change closes a bug that
was reproduced on v3.
"""

import os
import re
import sys
import json
import time
import uuid
import shutil
import signal
import hashlib
import argparse
import tempfile
import threading
import subprocess
from collections import deque

try:
    import requests
except ImportError:          # OMDB enrichment is optional
    requests = None

try:
    import colorama
    colorama.just_fix_windows_console()
except Exception:
    pass

try:
    from guessit import guessit as guessit_parse
    GUESSIT_AVAILABLE = True
except ImportError:
    GUESSIT_AVAILABLE = False
    guessit_parse = None


SCRIPT_VERSION = "4"

# paths

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), "streammii")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "processed.jsonl")
ERROR_LOG_DIR = os.path.join(LOG_DIR, "errors")

# runtime state all of this is populated by main(), never at import time

WORK_ROOT = SCRIPT_DIR
DRY_RUN = False
FORCE_REENCODE = False
DELETE_ORIGINALS = False
SKIP_COMPLIANT = True
CLEAN_ORPHAN_SUBS = False
OMDB_API_KEY = ""
H264_PROFILE = "baseline"
WII_MAX_PROFILE = "main"
VAAPI_DEVICE = "/dev/dri/renderD128"
ENC_PLANS = []
NON_INTERACTIVE = False
RESCAN_ORGANIZED = False
CLEANUP_EMPTY_DIRS = True
STALL_TIMEOUT = 600
OUTPUT_INDEX = set()
TOUCHED_DIRS = set()
TEMP_FILES = set()


def _stdin_is_tty():
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def safe_input(prompt=""):
    if NON_INTERACTIVE or not _stdin_is_tty():
        question = prompt.strip().rstrip(":>").strip() or "a prompt"
        print(f"\nerror: this run needs an answer to: {question}")
        if NON_INTERACTIVE:
            print("       -y was given, so StreamMii will not prompt.")
        else:
            print("       there is no terminal to ask on.")
        print("       supply it on the command line instead see --help")
        print("       (--mode, --sport, --adult-mode, --creator).")
        _shutdown()
        sys.exit(2)
    try:
        return input(prompt)
    except (KeyboardInterrupt, EOFError):
        print("\naborted.")
        _shutdown()
        sys.exit(130)          # 0 used to mean "all good" to scripts...


def cleanup_temp_files():
    for p in list(TEMP_FILES):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
        TEMP_FILES.discard(p)


def _shutdown():
    flush_processed_log()
    cleanup_temp_files()


def _install_signal_handlers():
    def _handler(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            pass               # not the main thread, or unsupported here


def ensure_dirs():
    try:
        for d in (CONFIG_DIR, LOG_DIR, ERROR_LOG_DIR):
            os.makedirs(d, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create {CONFIG_DIR} ({e})")
        sys.exit(1)


# config

DEFAULT_CONFIG = {
    "gpu_choice": "3",          # 1 amd  2 nvidia  3 cpu  4 intel
    "delete_originals": False,
    "omdb_api_key": "",
    "skip_compliant": True,
    "remove_orphan_subs": False,
    "h264_profile": "baseline",
    "wii_max_profile": "main",
    "vaapi_device": "/dev/dri/renderD128",
}


def first_launch_setup(non_interactive=False):
    if non_interactive:
        cfg = dict(DEFAULT_CONFIG)
        print("no config found; -y given, writing safe defaults "
              "(cpu encode, keep originals).")
        _write_config(cfg)
        return cfg

    print("welcome to streammii setup!")
    gpu_choice = ""
    while gpu_choice not in ("1", "2", "3", "4"):
        print("select gpu type or cpu fallback:")
        print("1 -> amd")
        print("2 -> nvidia")
        print("3 -> cpu (libx264, good quality, slower)")
        print("4 -> intel")
        gpu_choice = safe_input("> ").strip()

    delete_choice = ""
    while delete_choice.lower() not in ("y", "n"):
        delete_choice = safe_input(
            "delete originals after re-encode? (y/n): ").strip()
    delete_originals = delete_choice.lower() == "y"

    print("optional: enter omdb api key (or just press enter to skip)")
    omdb_api_key = safe_input("> ").strip()

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "gpu_choice": gpu_choice,
        "delete_originals": delete_originals,
        "omdb_api_key": omdb_api_key,
    })
    _write_config(cfg)
    return cfg


def _write_config(cfg):
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-",
                                   suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)          # no-op/absent on Windows
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
        tmp = None
    except OSError as e:
        print(f"warning: could not write config ({e})")
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


VALID_PROFILES = {"baseline", "main", "high"}


def validate_config(cfg):
    out = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        out.update(cfg)

    if str(out.get("gpu_choice")) not in ("1", "2", "3", "4"):
        print(f"config: gpu_choice {out.get('gpu_choice')!r} is not 1-4 "
              f" using cpu (3)")
        out["gpu_choice"] = "3"
    else:
        out["gpu_choice"] = str(out["gpu_choice"])

    if str(out.get("h264_profile", "")).lower() not in VALID_PROFILES:
        print(f"config: h264_profile {out.get('h264_profile')!r} is not "
              f"baseline/main/high using baseline")
        out["h264_profile"] = "baseline"

    if str(out.get("wii_max_profile", "")).lower() not in PROFILE_RANK:
        print(f"config: wii_max_profile {out.get('wii_max_profile')!r} is not "
              f"a known H.264 profile using main")
        out["wii_max_profile"] = "main"

    dev = str(out.get("vaapi_device", "") or "")
    if not dev or any(c in dev for c in "\n\r\0"):
        out["vaapi_device"] = "/dev/dri/renderD128"

    for flag in ("delete_originals", "skip_compliant", "remove_orphan_subs"):
        out[flag] = bool(out.get(flag))

    out["omdb_api_key"] = str(out.get("omdb_api_key", "") or "")
    return out


def load_config(non_interactive=False):
    if not os.path.exists(CONFIG_FILE):
        return validate_config(first_launch_setup(non_interactive))
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config root is not an object")
    except (json.JSONDecodeError, ValueError, OSError) as e:
        print(f"config file is unreadable ({e}). running setup again.")
        return validate_config(first_launch_setup(non_interactive))
    return validate_config(data)


# processed log json lines so weird filenames can't corrupt it

FINGERPRINT_CHUNK = 256 * 1024
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_FLUSH_EVERY = 25
ERROR_LOG_MAX_AGE = 30 * 86400

_LOG_BUFFER = []


def _content_fingerprint(path, size):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            h.update(f.read(FINGERPRINT_CHUNK))
            if size > FINGERPRINT_CHUNK * 2:
                f.seek(-FINGERPRINT_CHUNK, os.SEEK_END)
                h.update(f.read(FINGERPRINT_CHUNK))
    except OSError:
        return ""
    return h.hexdigest()[:16]


def _sig_parts(path):
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    head = [os.path.basename(path), st.st_size, int(st.st_mtime)]
    legacy = json.dumps(head, ensure_ascii=False)
    full = json.dumps(head + [_content_fingerprint(path, st.st_size)],
                      ensure_ascii=False)
    return legacy, full


def get_file_signature(path):
    return _sig_parts(path)[1]


def already_processed(path, sig_set):
    if not sig_set:
        return False
    legacy, full = _sig_parts(path)
    if full is None:
        return False
    return full in sig_set or legacy in sig_set


def read_processed_log():
    sigs = set()
    for src in (LOG_FILE + ".1", LOG_FILE):
        if not os.path.exists(src):
            continue
        try:
            with open(src, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, list) or len(rec) not in (3, 4):
                        continue
                    sigs.add(line)
                    if len(rec) == 4:
                        # also index the legacy form so both match
                        sigs.add(json.dumps(rec[:3], ensure_ascii=False))
        except OSError:
            pass
    return sigs


def write_processed_log(path, sig_set=None):
    if DRY_RUN:
        return
    legacy, sig = _sig_parts(path)
    if sig is None:
        return
    if sig_set is not None and (sig in sig_set or legacy in sig_set):
        return
    _LOG_BUFFER.append(sig)
    if sig_set is not None:
        sig_set.add(sig)
        sig_set.add(legacy)
    if len(_LOG_BUFFER) >= LOG_FLUSH_EVERY:
        flush_processed_log()


def flush_processed_log():
    if not _LOG_BUFFER:
        return
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("".join(s + "\n" for s in _LOG_BUFFER))
    except OSError:
        return
    del _LOG_BUFFER[:]


def rotate_processed_log():
    try:
        if os.path.getsize(LOG_FILE) <= LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(LOG_FILE, LOG_FILE + ".1")
        print("processed log rotated (previous generation kept as "
              "processed.jsonl.1)")
    except OSError:
        pass


def prune_error_logs():
    cutoff = time.time() - ERROR_LOG_MAX_AGE
    try:
        names = os.listdir(ERROR_LOG_DIR)
    except OSError:
        return
    for n in names:
        p = os.path.join(ERROR_LOG_DIR, n)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


# name sanitising

WIN_RESERVED = {"con", "prn", "aux", "nul"}
WIN_RESERVED |= {f"com{i}" for i in range(1, 10)}
WIN_RESERVED |= {f"lpt{i}" for i in range(1, 10)}


def safe_name(name, fallback="unknown", maxlen=120):
    if not isinstance(name, str):
        return fallback
    # newline/tab are separators become spaces; NUL and friends just vanish
    # doing it in this order stops "evil\nname" collapsing into "evilname"
    cleaned = re.sub(r"[\t\n\r\v\f]", " ", name)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(". ")
    if not cleaned or cleaned in (".", ".."):
        return fallback
    # windows fucking refuses CON / PRN / COM1 ... even with an extension
    if cleaned.split(".")[0].lower() in WIN_RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > maxlen:
        cleaned = cleaned[:maxlen].rstrip(". ")
    return cleaned or fallback


def initial(name):
    if not name:
        return "#"
    c = name[0].upper()
    return c if c.isalnum() else "#"


# filename classification , the old regexes matched inside 1920x1080

RESOLUTION_RE = re.compile(r"\b\d{3,4}\s?[xX]\s?\d{3,4}\b")
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20[0-5]\d)(?!\d)")
SXXEYY_RE = re.compile(r"\bs(\d{1,2})[\s._-]?e(\d{1,3})\b", re.I)
NxNN_RE = re.compile(r"(?<!\d)(\d{1,2})x(\d{2})(?!\d)")
EP_RE = re.compile(r"\b(?:ep|episode)[\s._-]?(\d{1,3})\b", re.I)

# deliberately conservative, ultra short tags (ts, tc, dd, dv, nf, cam, r5,
# evo, web on its own) are NOT in here they are real words and real film
# titles ("Cam", "Web of Lies") and would eat the title instead of the junk
RELEASE_TAGS = re.compile(
    r"\b(?:"
    r"1080[pi]|720[pi]|2160[pi]|480[pi]|576[pi]|4k|8k|uhd|"
    r"hdr10|hdr|hlg|dolby ?vision|"
    r"x ?264|x ?265|h ?264|h ?265|hevc|xvid|divx|av1|vp9|"
    r"bluray|blu ?ray|bdrip|brrip|bdremux|dvdrip|dvdscr|webrip|web ?dl|"
    r"hdtv|pdtv|hdrip|remux|"
    r"aac|ac3|eac3|dts ?hd|dts|truehd|flac|opus|atmos|ddp|"
    r"proper|repack|extended|unrated|uncut|remastered|limited|internal|"
    r"multi|dual ?audio|subbed|dubbed|hardsub|softsub|"
    r"10 ?bit|8 ?bit|hi10p|hi10|"
    r"amzn|dsnp|hmax|atvp|"
    r"yify|yts|rarbg"
    r")\b",
    re.I,
)


def _strip_release_junk(stem):
    s = RESOLUTION_RE.sub(lambda m: " " * len(m.group(0)), stem)
    s = re.sub(r"\[[^\]]*\]", lambda m: " " * len(m.group(0)), s)
    # one char in, one char out -> offsets are still 1:1 with "s"
    norm = re.sub(r"[._\-]", " ", s)
    m = RELEASE_TAGS.search(norm)
    if m and m.start() > 0:      # a tag at index 0 would leave us with nothing
        s = s[:m.start()]
    return s


def _tidy(s):
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -")


def parse_filename(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    # blank out resolutions first (length preserving, so offsets stay valid
    # against stem).  v3 matched \d{1,2}x\d{1,2} inside "1920x1080" and
    # filed every 1080p movie as a TV show
    probe = RESOLUTION_RE.sub(lambda m: " " * len(m.group(0)), stem)

    m = SXXEYY_RE.search(probe) or NxNN_RE.search(probe)
    if m:
        title = _tidy(_strip_release_junk(stem[:m.start()])) or _tidy(
            _strip_release_junk(stem))
        return {"kind": "tv", "title": title,
                "season": int(m.group(1)), "episode": int(m.group(2)),
                "year": None}

    m = EP_RE.search(probe)
    if m:
        title = _tidy(_strip_release_junk(stem[:m.start()])) or _tidy(
            _strip_release_junk(stem))
        return {"kind": "tv", "title": title,
                "season": 1, "episode": int(m.group(1)), "year": None}

    cut = _strip_release_junk(stem)
    ym = None
    for ym in YEAR_RE.finditer(cut):
        pass                       # keep the LAST year titles can start with one
    if ym:
        title = _tidy(cut[:ym.start()]) or _tidy(cut)
        return {"kind": "movie", "title": title, "season": None,
                "episode": None, "year": int(ym.group(0))}

    return {"kind": "unknown", "title": _tidy(cut) or stem,
            "season": None, "episode": None, "year": None}


OMDB_MAX_BYTES = 256 * 1024
OMDB_CACHE_MAX = 4096
_OMDB_CACHE = {}


def fetch_movie_metadata(title, year=None):
    if requests is None or not OMDB_API_KEY or not title:
        return None

    key = (title, year)
    if key in _OMDB_CACHE:
        return _OMDB_CACHE[key]

    result = None
    try:
        params = {"t": title, "apikey": OMDB_API_KEY}
        if year:
            params["y"] = str(year)
        with requests.get("https://www.omdbapi.com/", params=params,
                          timeout=6, allow_redirects=False,
                          stream=True) as r:
            r.raise_for_status()
            body = r.raw.read(OMDB_MAX_BYTES + 1, decode_content=True)
        if len(body) <= OMDB_MAX_BYTES:
            d = json.loads(body.decode("utf-8", "replace"))
            if isinstance(d, dict) and d.get("Response") == "True":
                t = d.get("Title") or title
                y = d.get("Year")
                result = f"{t} ({y})" if y else t
    except Exception:
        result = None               # enrichment is optional, never fatal

    if len(_OMDB_CACHE) >= OMDB_CACHE_MAX:
        _OMDB_CACHE.clear()
    _OMDB_CACHE[key] = result
    return result


def guessit_info(filepath):
    if not GUESSIT_AVAILABLE or guessit_parse is None:
        return None
    try:
        return guessit_parse(os.path.basename(filepath))
    except Exception:
        return None

# ffmpeg and ffprobe

FFMPEG_BASE = ["ffmpeg", "-nostdin", "-hide_banner", "-y"]

TEXT_SUB_CODECS = {
    "subrip", "ass", "ssa", "mov_text", "text", "webvtt",
    "microdvd", "subviewer", "subviewer1", "subviewer2",
    "mpl2", "pjs", "realtext", "sami", "stl", "vplayer",
}

WII_MAX_W = 640
WII_MAX_H = 480
WII_VIDEO_CODEC = "h264"
WII_VIDEO_PIXFMTS = {"yuv420p", "yuvj420p"}
WII_AUDIO_CODEC = "aac"
WII_AUDIO_MAX_CH = 2
WII_MAX_FPS = 31.0

PROFILE_RANK = {"constrained baseline": 0, "baseline": 0, "main": 1,
                "high": 2, "high 10": 3, "high 4:2:2": 3, "high 4:4:4": 3}

_ENCODERS_CACHE = None


def available_encoders():
    global _ENCODERS_CACHE
    if _ENCODERS_CACHE is None:
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="ignore", timeout=30)
            _ENCODERS_CACHE = set(
                re.findall(r"^\s*[A-Z.]{6}\s+(\S+)", r.stdout or "", re.M))
        except Exception:
            _ENCODERS_CACHE = set()
    return _ENCODERS_CACHE


def _plan(enc, hwaccel=None, init=(), vf_extra="", profiles=None, extra=()):
    return {"enc": enc, "hwaccel": hwaccel, "init": list(init),
            "vf_extra": vf_extra, "profiles": profiles or {}, "extra": list(extra)}


def build_encoder_plans(gpu_choice):
    win = sys.platform.startswith("win")
    mac = sys.platform == "darwin"
    plans = []

    if gpu_choice == "1":                       # AMD
        if win:
            plans.append(_plan("h264_amf", hwaccel="d3d11va",
                               profiles={"baseline": "constrained_baseline",
                                         "main": "main", "high": "high"}))
        elif mac:
            plans.append(_plan("h264_videotoolbox",
                               profiles={"baseline": "baseline",
                                         "main": "main", "high": "high"}))
        else:
            plans.append(_plan(
                "h264_vaapi",
                init=["-init_hw_device", f"vaapi=va:{VAAPI_DEVICE}",
                      "-filter_hw_device", "va"],
                vf_extra=",format=nv12,hwupload",
                # most AMD VAAPI drivers do not expose baseline at all
                profiles={"baseline": "constrained_baseline",
                          "main": "main", "high": "high"}))
    elif gpu_choice == "2":                     # NVIDIA
        plans.append(_plan("h264_nvenc", hwaccel="cuda",
                           profiles={"baseline": "baseline",
                                     "main": "main", "high": "high"}))
    elif gpu_choice == "4":                     # Intel
        plans.append(_plan("h264_qsv", hwaccel="qsv",
                           profiles={"baseline": "baseline",
                                     "main": "main", "high": "high"}))

    # always present safety net software H.264
    plans.append(_plan("libx264",
                       profiles={"baseline": "baseline", "main": "main",
                                 "high": "high"},
                       extra=["-level", "3.0"]))
    # absolute last resort if this ffmpeg has no H.264 encoder at all
    plans.append(_plan("libxvid"))

    have = available_encoders()
    if have:
        plans = [p for p in plans if p["enc"] in have]
    return plans


MP3_ENCODERS = ("libmp3lame", "libshine")


def pick_mp3_encoder():
    have = available_encoders()
    if not have:
        return "libmp3lame"          # could not enumerate; try the usual one
    for enc in MP3_ENCODERS:
        if enc in have:
            return enc
    return None


def fmt_time(sec):
    if sec <= 0:
        return "00:00"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_fps(value):
    if not value or value in ("0/0", "N/A"):
        return 0.0
    try:
        if "/" in str(value):
            num, den = str(value).split("/", 1)
            num, den = float(num), float(den)
            return num / den if den else 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def probe_video(infile):
    duration = 0.0
    aspect = 16 / 9
    subs = []
    meta = {"video": None, "audio": []}
    try:
        cmd = [
            "ffprobe", "-hide_banner", "-v", "error",
            "-show_entries",
            "format=duration:stream=index,codec_name,codec_type,width,height,"
            "pix_fmt,channels,profile,r_frame_rate,avg_frame_rate",
            "-of", "json", infile,
        ]
        res = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="ignore", timeout=60)          # never hang forever
        data = json.loads(res.stdout or "{}")

        try:
            duration = float(data.get("format", {}).get("duration", 0) or 0)
        except (TypeError, ValueError):
            duration = 0.0

        for s in data.get("streams", []):
            ctype = (s.get("codec_type") or "").lower()
            if ctype == "video" and s.get("width") and s.get("height"):
                w, h = int(s["width"]), int(s["height"])
                if h > 0:
                    aspect = round(w / h, 3)
                if meta["video"] is None:
                    fps = _parse_fps(s.get("avg_frame_rate")) or \
                        _parse_fps(s.get("r_frame_rate"))
                    meta["video"] = {
                        "codec": (s.get("codec_name") or "").lower(),
                        "width": w, "height": h,
                        "pix_fmt": (s.get("pix_fmt") or "").lower(),
                        "profile": str(s.get("profile") or "").lower(),
                        "fps": fps,
                    }
            elif ctype == "audio":
                meta["audio"].append({
                    "codec": (s.get("codec_name") or "").lower(),
                    "channels": int(s.get("channels") or 0),
                })
            elif ctype == "subtitle":
                subs.append({"index": s.get("index"),
                             "codec_name": (s.get("codec_name") or "").lower(),
                             "codec_type": ctype})
    except subprocess.TimeoutExpired:
        print("  ffprobe timed out, treating as unknown")
    except Exception:
        pass
    return duration, aspect, subs, meta


def assess_media(meta):
    v = meta.get("video")
    if not v:
        return True, True

    prof_ok = True
    prof = (v.get("profile") or "").strip()
    if prof:
        rank = PROFILE_RANK.get(prof)
        maxr = PROFILE_RANK.get(WII_MAX_PROFILE, 1)
        prof_ok = rank is not None and rank <= maxr
    fps = v.get("fps") or 0.0
    cap = fps_cap_for(v.get("width") or 0, v.get("height") or 0)
    fps_ok = fps <= cap + 1.0 if fps > 0 else True

    vid_ok = (
        v.get("codec") == WII_VIDEO_CODEC
        and 0 < (v.get("width") or 0) <= WII_MAX_W
        and 0 < (v.get("height") or 0) <= WII_MAX_H
        and v.get("pix_fmt") in WII_VIDEO_PIXFMTS
        and prof_ok and fps_ok
    )

    audios = meta.get("audio") or []
    aud_ok = True if not audios else all(
        a.get("codec") == WII_AUDIO_CODEC
        and 0 < (a.get("channels") or 0) <= WII_AUDIO_MAX_CH
        for a in audios)
    return (not vid_ok), (not aud_ok)


def decide_video_action(meta, ext):
    if FORCE_REENCODE or not SKIP_COMPLIANT:
        return "encode"
    needs_v, needs_a = assess_media(meta)
    if not needs_v and not needs_a:
        return "skip" if ext.lower() == ".mkv" else "remux"
    if not needs_v and needs_a:
        return "audio"
    return "encode"


ULTRAWIDE_ASPECT = 1.85


def fps_cap_for(width, height):
    if width and height and height > 0:
        return 25.0 if (width / height) > ULTRAWIDE_ASPECT else 30.0
    return 30.0


def pick_scaling(aspect, src_fps, src_w=0, src_h=0):
    if aspect <= ULTRAWIDE_ASPECT:
        vf, cap = "scale=640:480:flags=lanczos", 30
    else:
        vf, cap = "scale=640:-2:flags=lanczos", 25

    if 0 < src_w <= WII_MAX_W and 0 < src_h <= WII_MAX_H:
        vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2:flags=lanczos"

    r = cap if (src_fps <= 0 or src_fps > cap + 0.01) else None
    return vf, r


INPUT_ERROR_RE = re.compile(
    r"Error opening input|Invalid data found when processing input|"
    r"No such file or directory|Permission denied|Invalid argument",
    re.I)


def looks_like_input_error(logs):
    return any(INPUT_ERROR_RE.search(line) for line in logs)


def subtitle_is_readable(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-hide_banner", "-v", "error",
             "-show_entries", "stream=codec_type", "-of", "json", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="ignore", timeout=15)
        data = json.loads(r.stdout or "{}")
        return any((s.get("codec_type") or "").lower() == "subtitle"
                   for s in data.get("streams", []))
    except Exception:
        return False


def _err_log_path(src_path):
    h = uuid.uuid5(uuid.NAMESPACE_URL,
                   os.path.abspath(src_path)).hex[:8]
    base = safe_name(os.path.basename(src_path), "file", maxlen=80)
    return os.path.join(ERROR_LOG_DIR, f"{base}.{h}.log")

# output path selection 

def _same_path(a, b):
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return (os.path.normcase(os.path.abspath(a)) ==
            os.path.normcase(os.path.abspath(b)))


def _unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while i < 1000:
        cand = f"{base} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1
    return f"{base} ({uuid.uuid4().hex[:8]}){ext}"


def output_path_for(src_path, target_ext):
    base_no_ext, _ = os.path.splitext(src_path)
    candidate = base_no_ext + target_ext

    if _same_path(candidate, src_path):
        if DELETE_ORIGINALS:
            return candidate            # replacing in place is what was asked for
        return _unique_path(base_no_ext + ".wii" + target_ext)

    if os.path.exists(candidate):
        # a diiffrent existing file would be overwritten so i never do that
        return _unique_path(base_no_ext + ".wii" + target_ext)
    return candidate


TEMP_PREFIX = ".streammii_tmp_"
STALE_TEMP_AGE = 3600            # 1h long enough not to touch a parallel run


def _tmp_path(src_path, ext):
    d = os.path.dirname(os.path.abspath(src_path))
    for _ in range(20):
        cand = os.path.join(d, f"{TEMP_PREFIX}{uuid.uuid4().hex[:12]}{ext}")
        if not os.path.exists(cand):
            return cand
    raise RuntimeError("could not allocate a temp filename")

# video processing

def _kill_child(proc):
    try:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    except Exception:
        pass


class _StallWatchdog:
    def __init__(self, proc, timeout):
        self.proc = proc
        self.timeout = timeout
        self.timer = None
        self.fired = False

    def _fire(self):
        self.fired = True
        _kill_child(self.proc)

    def kick(self):
        if self.timeout <= 0:
            return
        if self.timer is not None:
            self.timer.cancel()
        self.timer = threading.Timer(self.timeout, self._fire)
        self.timer.daemon = True
        self.timer.start()

    def stop(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


def _run_encode(plan, src_path, output, copy_v, vf, rate, dur,
                has_external_srt, srt, output_subs_is_text, needs_audio):
    cmd = list(FFMPEG_BASE)
    if not copy_v:
        cmd += plan["init"]
        if plan["hwaccel"]:
            cmd += ["-hwaccel", plan["hwaccel"]]

    cmd += ["-i", src_path]
    if has_external_srt:
        cmd += ["-i", srt]

    cmd += ["-map", "0:v:0?", "-map", "0:a?", "-map", "0:s?"]
    if has_external_srt:
        cmd += ["-map", "1:0?"]

    if copy_v:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-vf", vf + plan["vf_extra"],
                "-c:v", plan["enc"], "-b:v", "1000k"]
        if rate is not None:
            cmd += ["-r", str(rate)]
        if plan["enc"] != "libxvid":
            # vbv cap the wii's decode buffer is small, bitrate spikes stutter
            cmd += ["-maxrate", "1000k", "-bufsize", "2000k"]
            if not plan["vf_extra"]:
                cmd += ["-pix_fmt", "yuv420p"]
        prof = plan["profiles"].get(H264_PROFILE)
        if prof:
            cmd += ["-profile:v", prof]
        cmd += plan["extra"]

    # remux means the audio was already compliant aac stereo re encoding it
    # would be pure generational loss for zero benefit v3 emitted
    # "-c:a aac" unconditionally and still called it a stream copy.
    if copy_v and not needs_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]

    for idx, is_text in enumerate(output_subs_is_text):
        cmd += [f"-c:s:{idx}", "srt" if is_text else "copy"]

    cmd.append(output)

    p = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,      # ffmpeg must not eat our prompts
        stdout=subprocess.DEVNULL,     # an undrained PIPE can deadlock
        stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="ignore")

    logs = deque(maxlen=500)          # bounded a failing ffmpeg can spew a lot
    start = time.time()
    watchdog = _StallWatchdog(p, STALL_TIMEOUT)
    watchdog.kick()

    try:
        # iter(readline, "") stops cleanly at EOF instead of spinning 
        for line in iter(p.stderr.readline, ""):
            watchdog.kick()               # any output means alive
            m = re.search(r"time=\s*(-?)(\d+):(\d{2}):(\d{2})\.(\d+)", line)
            if m and dur > 0:
                if m.group(1) == "-":
                    continue                  # ffmpeg emits time=-00:00:00 early
                frac = m.group(5)
                now = (int(m.group(2)) * 3600 + int(m.group(3)) * 60 +
                       int(m.group(4)) + int(frac) / (10 ** len(frac)))
                pct = min(100.0, max(0.0, (now / dur) * 100))
                filled = int(pct / 2.5)
                bar = "\u2588" * filled + "-" * (40 - filled)
                elapsed = time.time() - start
                eta = ""
                if pct > 0.5:
                    eta_s = max(0, int(elapsed / (pct / 100.0) - elapsed))
                    eta = f" ETA {fmt_time(eta_s)}"
                print(f"Encoding [{bar}] {pct:.1f}% "
                      f"({fmt_time(now)} / {fmt_time(dur)}){eta}".ljust(80),
                      end="\r", flush=True)
                continue
            logs.append(line)                 # keep only nonprogress lines
        p.wait()
    except BaseException:
        _kill_child(p)
        raise
    finally:
        watchdog.stop()

    if dur > 0:
        print(" " * 80, end="\r")
        print()

    if watchdog.fired:
        logs.append(f"[streammii] no output from ffmpeg for {STALL_TIMEOUT}s "
                    f"-- killed as stalled\n")
        print(f"  ffmpeg stalled (no output for {STALL_TIMEOUT}s) -- killed")

    return p.returncode, logs, looks_like_input_error(logs)


def process_video(src_path):
    _, ext = os.path.splitext(src_path)

    dur, aspect, internal_subs, meta = probe_video(src_path)

    if meta.get("video") is None:
        print(f"no video stream, skipping: {os.path.basename(src_path)}")
        return ("skip", src_path)

    src_fps = (meta["video"].get("fps") or 0.0)
    vf, rate = pick_scaling(aspect, src_fps,
                            meta["video"].get("width") or 0,
                            meta["video"].get("height") or 0)
    action = decide_video_action(meta, ext)
    _, needs_a = assess_media(meta)

    labels = {"skip": "already Wii-ready (skip)",
              "remux": "remux to mkv (stream copy)",
              "audio": "re-encode audio only",
              "encode": "full re-encode"}

    if action == "skip":
        if DRY_RUN:
            print(f"  [skip] {os.path.basename(src_path)} -> {labels[action]}")
        else:
            print(f"already wii-ready, skipping encode: "
                  f"{os.path.basename(src_path)}")
        return ("skip", src_path)

    final_out = output_path_for(src_path, ".mkv")

    if DRY_RUN:
        print(f"  [{action}] {os.path.basename(src_path)} -> {labels[action]} "
              f"=> {os.path.basename(final_out)}")
        return ("ok", final_out)

    copy_video = action in ("remux", "audio")

    base_no_ext, _ = os.path.splitext(src_path)
    srt = base_no_ext + ".srt"
    has_external_srt = os.path.exists(srt)
    if has_external_srt and not subtitle_is_readable(srt):
        # encode the video anyway rather than failing the whole file
        print(f"  external subtitle unreadable, encoding without it: "
              f"{os.path.basename(srt)}")
        has_external_srt = False

    output_subs_is_text = [s.get("codec_name", "") in TEXT_SUB_CODECS
                           for s in internal_subs]
    if has_external_srt:
        output_subs_is_text.append(True)

    tmp_out = _tmp_path(src_path, ".mkv")
    TEMP_FILES.add(tmp_out)

    if copy_video:
        # a stream copy uses no encoder at all, so an empty ENC_PLANS (an
        # ffmpeg with no H.264 encoder) must not block a plain remux
        plans = ENC_PLANS[:1] or [_plan("copy")]
    else:
        plans = ENC_PLANS
    if not plans:
        print("  no usable video encoder in this ffmpeg -- skipping")
        return ("fail", None)

    rcode, logs, input_err = 1, [], False
    try:
        for i, plan in enumerate(plans):
            rcode, logs, input_err = _run_encode(
                plan, src_path, tmp_out, copy_video, vf, rate, dur,
                has_external_srt, srt, output_subs_is_text, needs_a)
            if rcode == 0:
                break
            if copy_video:
                break                      # a stream copy has nothing to fall back to
            if input_err:
                print("  ffmpeg could not read an input file -- that is not an "
                      "encoder fault, so there is nothing to fall back to")
                break
            if i + 1 < len(plans):
                print(f"  {plan['enc']} failed, falling back to "
                      f"{plans[i + 1]['enc']} ...")

        if rcode == 0 and os.path.exists(tmp_out):
            try:
                os.replace(tmp_out, final_out)
            except OSError as e:
                print(f"could not finalise output: {e}")
                return ("fail", None)
            TEMP_FILES.discard(tmp_out)
            return ("ok", final_out)

        err_path = _err_log_path(src_path)
        try:
            with open(err_path, "w", encoding="utf-8") as f:
                f.write("".join(logs))
        except OSError:
            pass
        print(f"failed: check {err_path}")
        return ("fail", None)
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        TEMP_FILES.discard(tmp_out)


def process_audio(src_path):
    _, ext = os.path.splitext(src_path)

    if SKIP_COMPLIANT and not FORCE_REENCODE and ext.lower() == ".mp3":
        print(f"already mp3, skipping: {os.path.basename(src_path)}")
        return ("skip", src_path)

    final_out = output_path_for(src_path, ".mp3")

    if DRY_RUN:
        print(f"  [encode] {os.path.basename(src_path)} -> mp3 192k "
              f"=> {os.path.basename(final_out)}")
        return ("ok", final_out)

    enc = pick_mp3_encoder()
    if enc is None:
        print("  this ffmpeg has no mp3 encoder (needs libmp3lame or "
              "libshine) -- skipping")
        return ("fail", None)

    tmp_out = _tmp_path(src_path, ".mp3")
    TEMP_FILES.add(tmp_out)
    cmd = list(FFMPEG_BASE) + [
        "-i", src_path, "-vn", "-map", "0:a:0",
        "-c:a", enc, "-b:a", "192k", "-ac", "2", tmp_out,
    ]

    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="ignore",
                           timeout=STALL_TIMEOUT if STALL_TIMEOUT > 0 else None)
        if r.returncode == 0 and os.path.exists(tmp_out):
            try:
                os.replace(tmp_out, final_out)
            except OSError as e:
                print(f"could not finalise output: {e}")
                return ("fail", None)
            TEMP_FILES.discard(tmp_out)
            return ("ok", final_out)

        err = _err_log_path(src_path)
        try:
            with open(err, "w", encoding="utf-8") as f:
                f.write(r.stderr or "")
        except OSError:
            pass
        print(f"failed: check {err}")
        return ("fail", None)
    except subprocess.TimeoutExpired:
        print(f"  ffmpeg timed out after {STALL_TIMEOUT}s: "
              f"{os.path.basename(src_path)}")
        return ("fail", None)
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        TEMP_FILES.discard(tmp_out)

# organiser

sports_categories = {
    "team sports": ["football", "basketball", "volleyball", "rugby",
                    "baseball", "softball", "handball"],
    "combat sports": ["boxing", "mixed martial arts", "wrestling", "karate"],
    "winter sports": ["skiing", "snowboarding", "ice skating", "bobsledding"],
    "water sports": ["surfing", "rowing", "kayaking", "synchronized swimming"],
    "motor sports": ["formula 1", "motogp"],
    "individual sports": ["athletics (track and field)", "tennis", "golf",
                          "pool", "swimming", "badminton", "table tennis",
                          "cycling", "gymnastics"],
    "equestrian": ["show jumping", "dressage"],
    "other": ["cricket", "hockey"],
}

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".flv", ".wmv", ".webm"}
AUDIO_EXTS = {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg"}
SUB_EXTS = {".srt"}
MIN_JAV_SIZE = 50 * 1024 * 1024
REJECT_DIRNAME = "_streammii_rejected"

JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini", ".directory"}

SKIP_DIRS = {"node_modules", "$recycle.bin", "system volume information",
             "@eadir", "lost+found", "__pycache__", REJECT_DIRNAME.lower()}

MANAGED_ROOTS = {"movies", "tv shows", "audio", "adult", "documentaries",
                 "k-drama", "anime", "sports"}

SEASON_DIR_RE = re.compile(r"^(?:season|series|s)[\s._-]*\d{1,3}$", re.I)


def _in_managed_root(path):
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(WORK_ROOT))
    except ValueError:                     # different drive on Windows
        return False
    if rel.startswith(os.pardir):
        return False
    return rel.split(os.sep)[0].lower() in MANAGED_ROOTS


def usable_parent_name(src_dir):
    name = os.path.basename(os.path.abspath(src_dir))
    if not name:
        return None
    if os.path.normcase(os.path.abspath(src_dir)) == \
            os.path.normcase(os.path.abspath(WORK_ROOT)):
        return None
    if name.lower() in MANAGED_ROOTS:
        return None
    if SEASON_DIR_RE.match(name):
        return None
    if len(name) == 1:                     # an initial() bucket
        return None
    return name


def build_output_index(root):
    idx = set()
    for cat in MANAGED_ROOTS:
        base = os.path.join(root, cat)
        if not os.path.isdir(base):
            continue
        for dp, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                idx.add(f.lower())
    return idx


def converted_counterpart_exists(src_path):
    if not OUTPUT_INDEX:
        return False
    stem = os.path.splitext(os.path.basename(src_path))[0].lower()
    for ext in (".mkv", ".mp3"):
        if stem + ".wii" + ext in OUTPUT_INDEX:
            return True
        if stem + ext in OUTPUT_INDEX:
            return True
    return False


def _is_inside_root(path):
    ap = os.path.normcase(os.path.realpath(path))
    ar = os.path.normcase(os.path.realpath(WORK_ROOT))
    return ap != ar and (ap.startswith(ar + os.sep))


def _dir_is_effectively_empty(path):
    try:
        entries = os.listdir(path)
    except OSError:
        return False
    if not entries:
        return True
    return all(e.lower() in JUNK_FILES for e in entries)


def cleanup_single_folder(path):
    if not os.path.isdir(path) or not _is_inside_root(path):
        return
    if CLEANUP_EMPTY_DIRS and _dir_is_effectively_empty(path):
        try:
            shutil.rmtree(path)
        except OSError:
            pass
        return

    if not CLEAN_ORPHAN_SUBS:
        return

    try:
        files = [f for f in os.listdir(path)
                 if os.path.isfile(os.path.join(path, f))]
    except OSError:
        return
    if not files:
        return
    has_vid = any(os.path.splitext(f)[1].lower() in VIDEO_EXTS for f in files)
    has_srt = any(os.path.splitext(f)[1].lower() in SUB_EXTS for f in files)
    if has_vid or not has_srt:
        return
    for f in files:
        if os.path.splitext(f)[1].lower() in SUB_EXTS:
            try:
                os.remove(os.path.join(path, f))
            except OSError:
                pass
    if CLEANUP_EMPTY_DIRS and _dir_is_effectively_empty(path):
        try:
            shutil.rmtree(path)
        except OSError:
            pass


def finalize():
    if not CLEANUP_EMPTY_DIRS and not CLEAN_ORPHAN_SUBS:
        return
    seen = set()
    for d in sorted(TOUCHED_DIRS, key=lambda p: p.count(os.sep), reverse=True):
        cur = d
        while _is_inside_root(cur) and cur not in seen:
            seen.add(cur)
            cleanup_single_folder(cur)
            cur = os.path.dirname(cur)


def _register_touched(path):
    if path:
        TOUCHED_DIRS.add(os.path.abspath(path))


def move_to_rejected(fp, reason):
    dest_dir = os.path.join(WORK_ROOT, REJECT_DIRNAME)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        target = _unique_path(os.path.join(dest_dir, os.path.basename(fp)))
        _register_touched(os.path.dirname(os.path.abspath(fp)))
        shutil.move(fp, target)
        print(f"  quarantined ({reason}): {os.path.basename(fp)} "
              f"-> {REJECT_DIRNAME}/")
    except (OSError, shutil.Error) as e:
        print(f"  could not quarantine {os.path.basename(fp)}: {e}")


def organize_media_by_type(fp, mode, sport_name=None, adult_mode=None,
                           adult_data=None):
    if not os.path.isfile(fp):
        return

    if _in_managed_root(fp) and not RESCAN_ORGANIZED:
        return

    base_name = os.path.basename(fp)
    src_dir = os.path.dirname(os.path.abspath(fp))

    def move_and_cleanup(dest):
        nonlocal fp
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            print(f"  could not create {dest}: {e}")
            return
        _register_touched(dest)
        target = os.path.join(dest, base_name)
        if not _same_path(target, fp):
            target = _unique_path(target)
            try:
                shutil.move(fp, target)
                fp = target
            except (OSError, shutil.Error) as e:
                print(f"  could not move {base_name}: {e}")
                return
        _register_touched(src_dir)
        cleanup_single_folder(src_dir)

    if mode == "1":
        g = guessit_info(fp)
        if g and g.get("type") == "episode" and (g.get("series") or g.get("title")):
            series = safe_name(g.get("series") or g.get("title"), "unknown_show")
            season = g.get("season")
            if isinstance(season, int):
                season = f"{season:02d}"
            elif isinstance(season, str) and season.isdigit():
                season = f"{int(season):02d}"
            else:
                season = "01"
            move_and_cleanup(os.path.join(WORK_ROOT, "tv shows",
                                          initial(series), series,
                                          f"season {season}"))
            return
        if g and g.get("type") == "movie" and g.get("title"):
            title = safe_name(g["title"], "unknown_movie")
            year = g.get("year")
            movie_dir = safe_name(f"{title} ({year})" if year else title,
                                  "unknown_movie")
            move_and_cleanup(os.path.join(WORK_ROOT, "movies",
                                          initial(title), movie_dir))
            return

        info = parse_filename(base_name)
        if info["kind"] == "tv":
            show = safe_name(fetch_movie_metadata(info["title"]) or
                             info["title"], "unknown_show")
            season = f"{info['season'] or 1:02d}"
            move_and_cleanup(os.path.join(WORK_ROOT, "tv shows", initial(show),
                                          show, f"season {season}"))
        else:
            movie = safe_name(
                fetch_movie_metadata(info["title"], info["year"]) or
                (f"{info['title']} ({info['year']})" if info["year"]
                 else info["title"]),
                "unknown_movie")
            move_and_cleanup(os.path.join(WORK_ROOT, "movies", initial(movie),
                                          movie))

    elif mode == "2":
        parent_folder = usable_parent_name(src_dir)      
        album = safe_name(parent_folder or "", "unknown_album")
        stem = os.path.splitext(base_name)[0]
        parts = stem.split(" - ")
        artist = safe_name(parts[0] if len(parts) >= 2 else (parent_folder or ""),
                           "unknown_artist")
        move_and_cleanup(os.path.join(WORK_ROOT, "audio", initial(artist),
                                      artist, album))

    elif mode == "3" and adult_mode:
        if adult_mode == "1":
            creator = safe_name(adult_data or "", "unknown_creator")
            dest = os.path.join(WORK_ROOT, "adult", "solo",
                                initial(creator), creator)
        elif adult_mode == "2":
            names = [safe_name(n, "unknown") for n in (adult_data or [])]
            group = safe_name(" + ".join(names) if names else "",
                              "unknown_group")
            dest = os.path.join(WORK_ROOT, "adult", "group", group)
        elif adult_mode == "3":
            dest = os.path.join(WORK_ROOT, "adult", "hentai",
                                initial(base_name))
        elif adult_mode == "4":
            if "@" in base_name:
                clean_name = base_name.split("@")[-1]
                if clean_name and clean_name not in (".", ".."):
                    new_fp = _unique_path(os.path.join(src_dir, clean_name))
                    try:
                        os.rename(fp, new_fp)
                        fp = new_fp
                        base_name = os.path.basename(new_fp)
                    except OSError:
                        pass
            code = safe_name(os.path.splitext(base_name)[0], "uncategorized")
            dest = os.path.join(WORK_ROOT, "adult", "jav", code)
        elif adult_mode == "5_solo":
            creator = safe_name(adult_data or "", "unknown_creator")
            dest = os.path.join(WORK_ROOT, "adult", "lgbt", "solo",
                                initial(creator), creator)
        elif adult_mode == "5_group":
            names = [safe_name(n, "unknown") for n in (adult_data or [])]
            group = safe_name(" + ".join(names) if names else "",
                              "unknown_group")
            dest = os.path.join(WORK_ROOT, "adult", "lgbt", "group", group)
        else:
            return
        move_and_cleanup(dest)

    elif mode == "4":
        move_and_cleanup(os.path.join(WORK_ROOT, "documentaries",
                                      initial(base_name)))

    elif mode in ("5", "6"):
        cats = {"5": "k-drama", "6": "anime"}
        info = parse_filename(base_name)
        parent_title = usable_parent_name(src_dir)       
        title = safe_name(parent_title or info["title"] or base_name,
                          "unknown_series")
        dest = os.path.join(WORK_ROOT, cats[mode], initial(title), title)
        if info["kind"] == "tv":
            dest = os.path.join(dest, f"season {info['season'] or 1:02d}")
        move_and_cleanup(dest)

    elif mode == "7" and sport_name:
        sport = safe_name(sport_name, "unknown_sport")
        move_and_cleanup(os.path.join(WORK_ROOT, "sports", sport))


def _require_interactive(what, flag):
    if NON_INTERACTIVE or not _stdin_is_tty():
        reason = ("-y was given" if NON_INTERACTIVE
                  else "there is no terminal to prompt on")
        print(f"error: {what} needs more information and {reason}.")
        print(f"       pass {flag}")
        _shutdown()
        sys.exit(2)


def resolve_sport(cli_sport):
    if cli_sport:
        return cli_sport.strip()
    _require_interactive("--mode 7", "--sport NAME")
    return sports_submenu()


def resolve_adult(cli_mode, cli_creators):
    if cli_mode:
        mode = ADULT_MODE_MAP[cli_mode]
        creators = [c.strip() for c in (cli_creators or []) if c.strip()]
        if mode in ("1", "5_solo"):
            if not creators:
                print(f"error: --adult-mode {cli_mode} needs --creator NAME")
                sys.exit(2)
            return mode, creators[0]
        if mode in ("2", "5_group"):
            if not creators:
                print(f"error: --adult-mode {cli_mode} needs at least one "
                      f"--creator NAME")
                sys.exit(2)
            return mode, creators
        return mode, None

    _require_interactive("--mode 3", "--adult-mode "
                         + "/".join(sorted(ADULT_MODE_MAP)) + " [--creator NAME]")
    sub = safe_input("adult: 1=solo 2=group 3=hentai 4=jav 5=lgbt: ").strip()
    if sub == "1":
        return "1", safe_input("creator: ").strip()
    if sub == "2":
        return "2", collect_creators("creators")
    if sub in ("3", "4"):
        return sub, None
    if sub == "5":
        lgbt = safe_input("lgbt: 1=solo 2=group: ").strip()
        if lgbt == "1":
            return "5_solo", safe_input("creator: ").strip()
        if lgbt == "2":
            return "5_group", collect_creators("creators")
    return None, None

def sports_submenu():
    print("pick a sport category:")
    cats = list(sports_categories.keys())
    for i, cat in enumerate(cats, 1):
        print(f"{i} -> {cat}")
    choice = ""
    while not choice.isdigit() or not (1 <= int(choice) <= len(cats)):
        choice = safe_input("> ").strip()
    cat = cats[int(choice) - 1]
    sports = sports_categories[cat]
    print(f"pick a sport from {cat}:")
    for i, s in enumerate(sports, 1):
        print(f"{i} -> {s}")
    choice = ""
    while not choice.isdigit() or not (1 <= int(choice) <= len(sports)):
        choice = safe_input("> ").strip()
    return sports[int(choice) - 1]

ascii_art = r"""
   _____ __                            __  ____ _
  / ___// /_________  ____ _____ ___  /  |/  (_|_)
  \__ \/ __/ ___/ _ \/ __ `/ __ `__ \/ /|_/ / / /
 ___/ / /_/ /  /  __/ /_/ / / / / / / /  / / / /
/____/\__/_/   \___/\__,_/_/ /_/ /_/_/  /_/_/_/

  StreamMii Monolithic V4
"""

menu_text = """
choose a mode:
1 > movies/tv shows (auto detect)
2 > audio only
3 > adult
4 > documentaries
5 > k-drama
6 > anime
7 > sports
enter a number from 1 to 7: """


def check_dependencies():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        print(f"error: required tool(s) not found in PATH: {', '.join(missing)}")
        print("install ffmpeg (https://ffmpeg.org/download.html) and try again.")
        sys.exit(1)


def collect_creators(prompt_label):
    try:
        n = int(safe_input(f"how many {prompt_label}? ").strip())
    except ValueError:
        n = 0
    n = max(0, min(n, 50))
    return [safe_input(f"name {i + 1}: ").strip() for i in range(n)]

ADULT_MODE_MAP = {
    "solo": "1",
    "group": "2",
    "hentai": "3",
    "jav": "4",
    "lgbt-solo": "5_solo",
    "lgbt-group": "5_group",
}


def parse_args():
    p = argparse.ArgumentParser(
        prog="streammii",
        description="StreamMii-Monolithic: convert media to Wii-friendly formats.")
    p.add_argument("--version", action="version",
                   version=f"StreamMii-Monolithic {SCRIPT_VERSION}")
    p.add_argument("--path", default=None,
                   help="folder to scan (default: the current directory when run via the installed launcher, otherwise the folder this script is in)")
    p.add_argument("--mode", default=None, choices=[str(i) for i in range(1, 8)],
                   help="pick the mode non-interactively (1-7)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would happen without touching any files")
    p.add_argument("-y", "--yes", action="store_true",
                   help="skip the confirmation prompt")
    p.add_argument("--delete", action="store_true",
                   help="delete originals after a successful convert")
    p.add_argument("--no-delete", "--keep-originals", dest="no_delete",
                   action="store_true",
                   help="never delete originals, regardless of config")
    p.add_argument("--force-reencode", action="store_true",
                   help="re-encode even files that are already Wii-ready")
    p.add_argument("--clean-orphan-subs", action="store_true",
                   help="delete .srt files left in folders with no video "
                        "(destructive, off by default)")
    p.add_argument("--profile", default=None,
                   choices=["baseline", "main", "high"],
                   help="H.264 profile to encode with (default: baseline)")
    p.add_argument("--reset-config", action="store_true",
                   help="delete the saved config and re-run first-launch setup")
    p.add_argument("--no-cleanup", action="store_true",
                   help="never remove folders left empty by organising")
    p.add_argument("--rescan-organized", action="store_true",
                   help="also scan and re-organise files already filed under "
                        "movies/, anime/, audio/ ... (off by default)")
    p.add_argument("--sport", default=None, metavar="NAME",
                   help="sport name for --mode 7, skipping the submenu")
    p.add_argument("--adult-mode", default=None,
                   choices=sorted(ADULT_MODE_MAP),
                   help="sub-mode for --mode 3, skipping the submenu")
    p.add_argument("--creator", action="append", default=None, metavar="NAME",
                   help="creator name for --adult-mode solo/group/lgbt-* "
                        "(repeat for several)")
    p.add_argument("--stall-timeout", type=int, default=STALL_TIMEOUT,
                   metavar="SEC",
                   help=f"kill ffmpeg after SEC seconds with no output "
                        f"(default: {STALL_TIMEOUT}, 0 disables)")
    return p.parse_args()


def sweep_stale_temps(root):
    removed = 0
    now = time.time()
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        for f in files:
            if not f.startswith(TEMP_PREFIX):
                continue
            p = os.path.join(dp, f)
            try:
                if now - os.path.getmtime(p) < STALE_TEMP_AGE:
                    continue
                os.remove(p)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"cleaned up {removed} abandoned temp file(s) from a previous run")


def collect_targets(root, choice):
    targets = []
    self_path = os.path.realpath(__file__)
    want_video = choice in ("1", "3", "4", "5", "6", "7")
    root_abs = os.path.normcase(os.path.abspath(root))
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        if not RESCAN_ORGANIZED and \
                os.path.normcase(os.path.abspath(dp)) == root_abs:
            dirs[:] = [d for d in dirs if d.lower() not in MANAGED_ROOTS]
        for f in files:
            if f.startswith(TEMP_PREFIX):
                continue
            low = f.lower()
            if low.endswith("_tmp.mkv") or low.endswith("_tmp.mp3"):
                continue                      # leftovers from a crashed v3 run
            src_path = os.path.normpath(os.path.join(dp, f))
            if os.path.realpath(src_path) == self_path:
                continue
            ext = os.path.splitext(low)[1]
            if want_video and ext in VIDEO_EXTS:
                targets.append(src_path)
            elif choice == "2" and ext in AUDIO_EXTS:
                targets.append(src_path)
    return targets


def main():
    global WORK_ROOT, DRY_RUN, FORCE_REENCODE, DELETE_ORIGINALS
    global SKIP_COMPLIANT, CLEAN_ORPHAN_SUBS, OMDB_API_KEY
    global H264_PROFILE, WII_MAX_PROFILE, VAAPI_DEVICE, ENC_PLANS
    global NON_INTERACTIVE, RESCAN_ORGANIZED, CLEANUP_EMPTY_DIRS
    global STALL_TIMEOUT, OUTPUT_INDEX

    args = parse_args()
    NON_INTERACTIVE = bool(args.yes)
    _install_signal_handlers()           
    ensure_dirs()
    rotate_processed_log()                 
    prune_error_logs()                    

    if args.reset_config:
        try:
            os.remove(CONFIG_FILE)
            print("config removed.")
        except OSError:
            pass

    check_dependencies()
    config = load_config(non_interactive=NON_INTERACTIVE or
                         not _stdin_is_tty())

    DRY_RUN = args.dry_run
    FORCE_REENCODE = args.force_reencode
    DELETE_ORIGINALS = bool(config.get("delete_originals", False))
    if args.delete:
        DELETE_ORIGINALS = True
    if args.no_delete:
        DELETE_ORIGINALS = False
    SKIP_COMPLIANT = bool(config.get("skip_compliant", True))
    CLEAN_ORPHAN_SUBS = (args.clean_orphan_subs or
                         bool(config.get("remove_orphan_subs", False)))
    OMDB_API_KEY = str(config.get("omdb_api_key", "") or "").strip()
    H264_PROFILE = args.profile or str(config.get("h264_profile", "baseline"))
    WII_MAX_PROFILE = str(config.get("wii_max_profile", "main")).lower()
    VAAPI_DEVICE = str(config.get("vaapi_device", "/dev/dri/renderD128"))
    ENC_PLANS = build_encoder_plans(str(config.get("gpu_choice", "3")))
    RESCAN_ORGANIZED = bool(args.rescan_organized)   
    CLEANUP_EMPTY_DIRS = not args.no_cleanup               
    STALL_TIMEOUT = max(0, int(args.stall_timeout))         

    if not ENC_PLANS:
        print("error: this ffmpeg has no usable H.264 encoder "
              "(looked for libx264 and the hardware encoders).")
        print("       install a full ffmpeg build: "
              "https://ffmpeg.org/download.html")
        sys.exit(1)

    if args.path:
        WORK_ROOT = os.path.abspath(os.path.expanduser(args.path))
        if not os.path.isdir(WORK_ROOT):
            print(f"error: --path is not a directory: {WORK_ROOT}")
            sys.exit(1)
    elif os.environ.get("STREAMMII_LAUNCHER") == "1":
        WORK_ROOT = os.getcwd()

    if os.path.basename(os.path.normpath(WORK_ROOT)).lower() in MANAGED_ROOTS:
        print(f"note: the scan root is itself named "
              f"'{os.path.basename(os.path.normpath(WORK_ROOT))}', so the "
              f"organiser will nest a folder of the same name inside it.")

    processed_sigs = read_processed_log()
    OUTPUT_INDEX = (set() if (RESCAN_ORGANIZED or FORCE_REENCODE)
                    else build_output_index(WORK_ROOT))   

    PURPLE, RESET = "\033[95m", "\033[0m"
    print(f"{PURPLE}{ascii_art}{RESET}")

    choice = args.mode or ""
    while choice not in [str(i) for i in range(1, 8)]:
        print(menu_text)
        choice = safe_input("> ").strip()

    sport = adult_mode = adult_data = None

    if choice == "3":
        adult_mode, adult_data = resolve_adult(args.adult_mode, args.creator)

    if choice == "7":
        sport = resolve_sport(args.sport)

    if not DRY_RUN:
        sweep_stale_temps(WORK_ROOT)

    targets = collect_targets(WORK_ROOT, choice)
    scanned = len(targets)
    targets = [t for t in targets if not already_processed(t, processed_sigs)]
    already_converted = 0
    if OUTPUT_INDEX:
        kept = []
        for t in targets:
            if converted_counterpart_exists(t):
                already_converted += 1
            else:
                kept.append(t)
        targets = kept

    print()
    print(f"scan root : {WORK_ROOT}")
    print(f"to process: {len(targets)} file(s)")
    print(f"encoder   : {' -> '.join(p['enc'] for p in ENC_PLANS)} "
          f"(profile {H264_PROFILE})")
    print(f"delete originals after re-encode: "
          f"{'YES' if DELETE_ORIGINALS else 'no'}")
    if already_converted:
        print(f"skipped   : {already_converted} file(s) already converted "
              f"into the organised tree")
    if CLEAN_ORPHAN_SUBS:
        print("orphan .srt cleanup            : YES (destructive)")
    if not CLEANUP_EMPTY_DIRS:
        print("empty-folder cleanup           : DISABLED (--no-cleanup)")
    if DRY_RUN:
        print("mode      : DRY RUN (nothing will be changed)")
    print()

    if not targets:
        print("nothing to do.")
        if scanned == 0 and not RESCAN_ORGANIZED:
            print("(folders the organiser manages -- movies/, anime/, audio/ "
                  "... -- are not scanned;")
            print(" pass --rescan-organized if you meant to include them.)")
        return

    if not NON_INTERACTIVE and not DRY_RUN:
        if safe_input("proceed? (y/N): ").strip().lower() != "y":
            print("aborted.")
            return

    processed = skipped = failed = rejected = 0

    for src_path in targets:
        if not os.path.exists(src_path):
            continue
        ext = os.path.splitext(src_path)[1].lower()
        is_video = ext in VIDEO_EXTS

        # jav size check happens before the encode, v3 burned cpu on a file it
        # was about to delete, and deleted it with an unguarded os.remove()
        if (choice == "3" and adult_mode == "4" and is_video and not DRY_RUN):
            try:
                too_small = os.path.getsize(src_path) < MIN_JAV_SIZE
            except OSError:
                too_small = False
            if too_small:
                move_to_rejected(src_path, "<50MB")
                rejected += 1
                continue

        status, out_path = (process_video(src_path) if is_video
                            else process_audio(src_path))

        if status == "fail" or not out_path:
            failed += 1
            continue

        same = _same_path(out_path, src_path)
        if status == "skip":
            skipped += 1
        else:
            processed += 1

        write_processed_log(src_path, processed_sigs)
        if not same:
            write_processed_log(out_path, processed_sigs)

        if DELETE_ORIGINALS and not same and not DRY_RUN:
            try:
                os.remove(src_path)
            except OSError as e:
                print(f"  could not delete original: {e}")
            if is_video:
                srt = os.path.splitext(src_path)[0] + ".srt"
                if os.path.exists(srt):
                    try:
                        os.remove(srt)
                    except OSError:
                        pass
            _register_touched(os.path.dirname(os.path.abspath(src_path)))

        if not DRY_RUN:
            if is_video:
                organize_media_by_type(out_path, choice, sport, adult_mode,
                                       adult_data)
            else:
                organize_media_by_type(out_path, choice)

    flush_processed_log()                  
    if not DRY_RUN:
        finalize()

    print()
    print(f"done. processed: {processed}  skipped (already ok): {skipped}  "
          f"failed: {failed}  quarantined: {rejected}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\naborted.")
        _shutdown()
        sys.exit(130)
    finally:
        _shutdown()