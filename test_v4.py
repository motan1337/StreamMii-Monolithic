#!/usr/bin/env python3
import os, sys, json, tempfile, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))

SCRIPT = os.environ.get("STREAMMII_SCRIPT") or os.path.join(
    HERE, "streammii-monolithic-v4.py")
if not os.path.isfile(SCRIPT):
    cands = sorted(f for f in os.listdir(HERE)
                   if f.startswith("streammii-monolithic-v") and f.endswith(".py"))
    if not cands:
        sys.exit(f"could not find streammii-monolithic-v4.py in {HERE}\n"
                 f"(or set STREAMMII_SCRIPT=/path/to/it)")
    SCRIPT = os.path.join(HERE, cands[-1])
print(f"testing: {SCRIPT}\n")

FAKE_HOME = tempfile.mkdtemp(prefix="streammii-test-home-")
os.environ["HOME"] = FAKE_HOME
os.environ["USERPROFILE"] = FAKE_HOME
os.makedirs(os.path.join(FAKE_HOME, "streammii"), exist_ok=True)
with open(os.path.join(FAKE_HOME, "streammii", "config.json"), "w") as fh:
    json.dump({"gpu_choice": "3", "delete_originals": False,
               "omdb_api_key": "", "skip_compliant": True}, fh)

spec = importlib.util.spec_from_file_location("sm", SCRIPT)
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)

FAILS = []
def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got : {got!r}")
        print(f"        want: {want!r}")
        FAILS.append(label)

print(" filename classifier ")
cases = [
    ("Some.Movie.1920x1080.x264.mkv",      ("unknown", "Some Movie", None)),
    ("Interstellar.2014.1080p.BluRay.mkv", ("movie",  "Interstellar", 2014)),
    ("Dune.Part.Two.2024.2160p.WEB-DL.mkv",("movie",  "Dune Part Two", 2024)),
    ("Movie.2031.mkv",                     ("movie",  "Movie", 2031)),
    ("Breaking.Bad.S01E02.1080p.mkv",      ("tv",     "Breaking Bad", None)),
    ("Some Show 1x02 HDTV.avi",            ("tv",     "Some Show", None)),
    ("Anime - Episode 12 [1080p].mkv",     ("tv",     "Anime", None)),
    ("Cam.2018.1080p.WEB-DL.mkv",          ("movie",  "Cam", 2018)),
    ("Spider-Man.2002.720p.mkv",           ("movie",  "Spider-Man", 2002)),
]
for fn, (kind, title, year) in cases:
    r = sm.parse_filename(fn)
    check(f"  {fn}", (r["kind"], r["title"], r["year"]), (kind, title, year))

print("\n season/episode extraction ")
r = sm.parse_filename("Breaking.Bad.S01E02.1080p.mkv")
check("  S01E02 -> season 1 ep 2", (r["season"], r["episode"]), (1, 2))
r = sm.parse_filename("Some Show 1x02 HDTV.avi")
check("  1x02   -> season 1 ep 2", (r["season"], r["episode"]), (1, 2))

print("\n safe_name hardening ")
check("  path traversal", sm.safe_name("../../etc"), "etc")
check("  bare dots",      sm.safe_name(".."), "unknown")
check("  win reserved",   sm.safe_name("CON"), "_CON")
check("  win reserved+ext", sm.safe_name("con.mkv"), "_con.mkv")
check("  control chars",  sm.safe_name("ev\x00il\nname"), "evil name")
check("  length cap",     len(sm.safe_name("A" * 500)), 120)
check("  empty -> fallback", sm.safe_name("///"), "unknown")

print("\n initial() bucket ")
check("  letter", sm.initial("Dune"), "D")
check("  digit",  sm.initial("300"), "3")
check("  symbol", sm.initial("!!!"), "#")
check("  empty",  sm.initial(""), "#")

print("\n signature is always one line ")
d = tempfile.mkdtemp()
weird = os.path.join(d, "evil\nname.mkv")
try:
    open(weird, "w").write("x")
    sig = sm.get_file_signature(weird)
    check("  newline escaped", "\n" in sig, False)
    check("  round-trips", json.loads(sig)[0], "evil\nname.mkv")
except OSError:
    print("SKIP  newline filename not supported on this fs")

print("\n dotfile only dir is not deleted ")
root = tempfile.mkdtemp()
sm.WORK_ROOT = root
repo = os.path.join(root, "myrepo")
os.makedirs(os.path.join(repo, ".git"))
open(os.path.join(repo, ".env"), "w").write("SECRET=1")
sm.cleanup_single_folder(repo)
check("  .git/.env dir survives", os.path.isdir(repo), True)

junk = os.path.join(root, "junkonly")
os.makedirs(junk)
open(os.path.join(junk, ".DS_Store"), "w").write("x")
open(os.path.join(junk, "Thumbs.db"), "w").write("x")
sm.cleanup_single_folder(junk)
check("  junk-only dir removed", os.path.isdir(junk), False)

truly = os.path.join(root, "empty")
os.makedirs(truly)
sm.cleanup_single_folder(truly)
check("  truly empty dir removed", os.path.isdir(truly), False)

print("\n cleanup never escapes WORK_ROOT ")
outside = tempfile.mkdtemp()
sm.cleanup_single_folder(outside)
check("  outside dir untouched", os.path.isdir(outside), True)
sm.cleanup_single_folder(root)
check("  WORK_ROOT itself untouched", os.path.isdir(root), True)

print("\n output_path_for never clobbers an input ")
od = tempfile.mkdtemp()
sm.DELETE_ORIGINALS = False
src_mkv = os.path.join(od, "movie.mkv"); open(src_mkv, "w").write("x")
out = sm.output_path_for(src_mkv, ".mkv")
check("  .mkv in -> different path out", out != src_mkv, True)
check("  .mkv in -> .wii.mkv", os.path.basename(out), "movie.wii.mkv")

src_avi = os.path.join(od, "film.avi"); open(src_avi, "w").write("x")
check("  .avi in -> film.mkv", os.path.basename(sm.output_path_for(src_avi, ".mkv")),
      "film.mkv")

open(os.path.join(od, "film.mkv"), "w").write("PRECIOUS")
check("  existing film.mkv not clobbered",
      os.path.basename(sm.output_path_for(src_avi, ".mkv")), "film.wii.mkv")

sm.DELETE_ORIGINALS = True
check("  --delete allows in-place replace",
      sm.output_path_for(src_mkv, ".mkv"), src_mkv)
sm.DELETE_ORIGINALS = False

print("\n compliance assessment ")
h264_ok = {"video": {"codec": "h264", "width": 640, "height": 480,
                     "pix_fmt": "yuv420p", "profile": "constrained baseline",
                     "fps": 25.0},
           "audio": [{"codec": "aac", "channels": 2}]}
check("  our own baseline output is compliant", sm.assess_media(h264_ok), (False, False))

xvid = dict(h264_ok); xvid["video"] = dict(h264_ok["video"], codec="mpeg4")
check("  xvid/mpeg4 is NOT compliant", sm.assess_media(xvid)[0], True)

hi = dict(h264_ok); hi["video"] = dict(h264_ok["video"], profile="high")
check("  High profile flagged", sm.assess_media(hi)[0], True)

fast = dict(h264_ok); fast["video"] = dict(h264_ok["video"], fps=60.0)
check("  60fps flagged", sm.assess_media(fast)[0], True)

surround = dict(h264_ok); surround["audio"] = [{"codec": "ac3", "channels": 6}]
check("  5.1 ac3 -> audio-only path", sm.assess_media(surround), (False, True))
check("  action for 5.1 in mkv", sm.decide_video_action(surround, ".mkv"), "audio")
check("  action for compliant mp4", sm.decide_video_action(h264_ok, ".mp4"), "remux")
check("  action for compliant mkv", sm.decide_video_action(h264_ok, ".mkv"), "skip")

print("\n framerate is a cap, not a force ")
check("  23.976 src -> no -r", sm.pick_scaling(1.78, 23.976)[1], None)
check("  25 src     -> no -r", sm.pick_scaling(1.78, 25.0)[1], None)
check("  60 src     -> -r 30", sm.pick_scaling(1.78, 60.0)[1], 30)
check("  unknown    -> -r 30", sm.pick_scaling(1.78, 0.0)[1], 30)

print("\n encoder plans ")
plans = sm.build_encoder_plans("3")
check("  cpu default is libx264", plans[0]["enc"], "libx264")
check("  libxvid is not first", "libxvid" != plans[0]["enc"], True)

print("\n output is not reingested as input ")
mr = tempfile.mkdtemp()
sm.WORK_ROOT = mr
sm.RESCAN_ORGANIZED = False
os.makedirs(os.path.join(mr, "movies", "I", "Interstellar (2014)"))
os.makedirs(os.path.join(mr, "Downloads"))
done = os.path.join(mr, "movies", "I", "Interstellar (2014)", "Interstellar.wii.mkv")
fresh = os.path.join(mr, "Downloads", "Some.Movie.2020.mkv")
open(done, "w").write("x")
open(fresh, "w").write("x")
found = [os.path.basename(p) for p in sm.collect_targets(mr, "1")]
check("  managed roots skipped", "Interstellar.wii.mkv" in found, False)
check("  real input still found", "Some.Movie.2020.mkv" in found, True)
sm.RESCAN_ORGANIZED = True
found = [os.path.basename(p) for p in sm.collect_targets(mr, "1")]
check("  --rescan-organized re-includes", "Interstellar.wii.mkv" in found, True)
sm.RESCAN_ORGANIZED = False

check("  _in_managed_root(output)", sm._in_managed_root(done), True)
check("  _in_managed_root(input)", sm._in_managed_root(fresh), False)

print("\n organiser is idempotent ")
od2 = tempfile.mkdtemp()
sm.WORK_ROOT = od2
os.makedirs(os.path.join(od2, "Some Anime"))
ep = os.path.join(od2, "Some Anime", "Some.Anime.S01E03.mkv")
open(ep, "w").write("x")
sm.organize_media_by_type(ep, "6")
first = [os.path.join(dp, f) for dp, d, fl in os.walk(od2) for f in fl][0]
rel1 = os.path.relpath(first, od2)
sm.organize_media_by_type(first, "6")
second = [os.path.join(dp, f) for dp, d, fl in os.walk(od2) for f in fl][0]
check("  run 1 lands correctly", rel1,
      os.path.join("anime", "S", "Some Anime", "season 01",
                   "Some.Anime.S01E03.mkv"))
check("  run 2 does not move it again", os.path.relpath(second, od2), rel1)

print("\n parent folder heuristic ")
ph = tempfile.mkdtemp()
sm.WORK_ROOT = ph
check("  scan root rejected", sm.usable_parent_name(ph), None)
check("  'season 01' rejected",
      sm.usable_parent_name(os.path.join(ph, "season 01")), None)
check("  'Season 1' rejected",
      sm.usable_parent_name(os.path.join(ph, "Season 1")), None)
check("  'S02' rejected", sm.usable_parent_name(os.path.join(ph, "S02")), None)
check("  managed root rejected",
      sm.usable_parent_name(os.path.join(ph, "anime")), None)
check("  initial bucket rejected",
      sm.usable_parent_name(os.path.join(ph, "A")), None)
check("  real show name kept",
      sm.usable_parent_name(os.path.join(ph, "Cowboy Bebop")), "Cowboy Bebop")

print("\n files in the scan root do not take the root name ")
rn = tempfile.mkdtemp(prefix="my-media-")
sm.WORK_ROOT = rn
flat = os.path.join(rn, "Some.Anime.S01E03.1080p.mkv")
open(flat, "w").write("x")
sm.organize_media_by_type(flat, "6")
landed = os.path.relpath(
    [os.path.join(dp, f) for dp, d, fl in os.walk(rn) for f in fl][0], rn)
check("  uses the parsed title, not the folder",
      landed.split(os.sep)[2], "Some Anime")

print("\n signature is content aware ")
cd = tempfile.mkdtemp()
os.makedirs(os.path.join(cd, "A"))
os.makedirs(os.path.join(cd, "B"))
fa = os.path.join(cd, "A", "S01E01.mkv")
fb = os.path.join(cd, "B", "S01E01.mkv")
open(fa, "w").write("SAME LENGTH DATA")
open(fb, "w").write("DIFFERENT DATA!!")
os.utime(fa, (1700000000, 1700000000))
os.utime(fb, (1700000000, 1700000000))
check("  same name/size/mtime, different bytes -> different sig",
      sm.get_file_signature(fa) == sm.get_file_signature(fb), False)
check("  identical file -> same sig",
      sm.get_file_signature(fa) == sm.get_file_signature(fa), True)

print("\n legacy 3 field log entries still count ")
legacy_key, full_key = sm._sig_parts(fa)
check("  legacy record recognised", sm.already_processed(fa, {legacy_key}), True)
check("  new record recognised", sm.already_processed(fa, {full_key}), True)
check("  unrelated record ignored", sm.already_processed(fa, {"[]"}), False)

print("\n mp3 encoder chain ")
check("  never returns the bogus 'mp3' decoder name",
      sm.pick_mp3_encoder() == "mp3", False)
check("  picks a real encoder or None",
      sm.pick_mp3_encoder() in (None,) + sm.MP3_ENCODERS, True)

print("\n no upscaling below 640x480 ")
check("  320x240 keeps its size",
      "640" not in sm.pick_scaling(1.33, 25.0, 320, 240)[0], True)
check("  1280x720 is scaled down",
      sm.pick_scaling(1.78, 25.0, 1280, 720)[0], "scale=640:480:flags=lanczos")
check("  ultra-wide keeps -2 height",
      sm.pick_scaling(2.37, 25.0, 2560, 1080)[0], "scale=640:-2:flags=lanczos")

print("\n fps cap matches what the encoder would emit ")
check("  ultra-wide cap is 25", sm.fps_cap_for(2560, 1080), 25.0)
check("  normal cap is 30", sm.fps_cap_for(1280, 720), 30.0)
uw = {"video": {"codec": "h264", "width": 640, "height": 266,
                "pix_fmt": "yuv420p", "profile": "baseline", "fps": 30.0},
      "audio": [{"codec": "aac", "channels": 2}]}
check("  30fps ultra wide is NOT called compliant", sm.assess_media(uw)[0], True)

print("\n input errors are told apart from encoder errors ")
check("  input error detected",
      sm.looks_like_input_error(["Error opening input file x.srt\n"]), True)
check("  encoder error not misread",
      sm.looks_like_input_error(["Unknown encoder 'h264_nvenc'\n"]), False)

print("\n=== config validation ===")
bad = sm.validate_config({"gpu_choice": "nonsense", "h264_profile": "man",
                          "wii_max_profile": "ultra", "vaapi_device": "a\nb",
                          "delete_originals": "yes-ish"})
check("  bad gpu_choice -> cpu", bad["gpu_choice"], "3")
check("  bad profile -> baseline", bad["h264_profile"], "baseline")
check("  bad wii_max_profile -> main", bad["wii_max_profile"], "main")
check("  newline in vaapi_device rejected", "\n" in bad["vaapi_device"], False)
check("  delete_originals coerced to bool",
      isinstance(bad["delete_originals"], bool), True)
good = sm.validate_config({"gpu_choice": 2, "h264_profile": "main"})
check("  valid values survive", (good["gpu_choice"], good["h264_profile"]),
      ("2", "main"))

print("\n --no-cleanup respects your folders ")
nc = tempfile.mkdtemp()
sm.WORK_ROOT = nc
victim = os.path.join(nc, "gone")
os.makedirs(victim)
sm.CLEANUP_EMPTY_DIRS = False
sm.cleanup_single_folder(victim)
check("  empty dir kept with --no-cleanup", os.path.isdir(victim), True)
sm.CLEANUP_EMPTY_DIRS = True
sm.cleanup_single_folder(victim)
check("  removed again when enabled", os.path.isdir(victim), False)

print("\n config file is written 0600 and atomically ")
sm.ensure_dirs()
sm._write_config({"gpu_choice": "3", "omdb_api_key": "SECRET"})
mode = oct(os.stat(sm.CONFIG_FILE).st_mode & 0o777)
if os.name == "nt":
    print("SKIP  posix permissions not meaningful on Windows")
else:
    check("  mode is 0600", mode, "0o600")
check("  no .tmp left behind",
      [f for f in os.listdir(sm.CONFIG_DIR) if f.endswith(".tmp")], [])
check("  round-trips", sm.load_config(True)["omdb_api_key"], "SECRET")

print("\n adult sub modes are reachable from the cli ")
check("  every CLI name maps to an internal mode",
      sorted(sm.ADULT_MODE_MAP.values()),
      sorted(["1", "2", "3", "4", "5_solo", "5_group"]))
check("  solo needs a creator name",
      sm.resolve_adult("solo", ["Someone"]), ("1", "Someone"))
check("  group takes a list",
      sm.resolve_adult("group", ["A", "B"]), ("2", ["A", "B"]))
check("  jav needs no extra data", sm.resolve_adult("jav", None), ("4", None))
check("  --sport bypasses the submenu", sm.resolve_sport("padel"), "padel")

print("\n" + "=" * 50)
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL UNIT TESTS PASSED")
