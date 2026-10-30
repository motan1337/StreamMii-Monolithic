# StreamMii-Monolithic

**A simple tool that converts media into Wii compatible formats for smooth playback.**

Current version: **v4** · **MIT licensed** · Requires **ffmpeg with libx264**

**Python:** the script itself runs on **3.8+**. The pinned `requirements.txt`
needs **3.10+**, because `requests` and `urllib3` dropped everything older. On
Python 3.8/3.9 (Debian 11, Raspberry Pi OS Bullseye, Ubuntu 20.04) skip the
pinned file all three dependencies are optional and StreamMii converts and
organises perfectly well without them.

---

## Things StreamMii can delete

StreamMii moves and deletes files. These are the only destructive things it
does, all of them are listed here, and **all of them are off by default in
v4**:

| Behaviour | Default | Control |
| :--- | :--- | :--- |
| Delete the original after a successful convert | **OFF** | `--delete`, or `delete_originals: true` |
| Delete `.srt` files stranded in folders with no video | **OFF** | `--clean-orphan-subs` |
| Quarantine JAV files under 50 MB | ON, but it **moves** them to `_streammii_rejected/`, never deletes | — |
| Remove folders it emptied while organising | ON | `--no-cleanup` turns it off |
| Delete its **own** abandoned `.streammii_tmp_*` files older than 1 h under the scan root | ON | — |


> **Upgrading from v3?** v3 had three bugs that destroyed data:
> it overwrote every `.mkv` input in place even with `delete_originals: false`,
> it deleted any folder whose only contents were dotfiles (`.git`, `.env`,
> `.stfolder`), and it swept empty directories across the *entire* scan tree.
> All three are fixed in v4. **Do not keep using v3.**

---

### How to use

```
cd /your/media
streammii --dry-run        # see the plan
streammii                  # do it

Or point it anywhere from anywhere:
streammii --path /your/media --dry-run
```

When started through the installed launcher, it works on the
**current directory** by default, like any other CLI. When you run the `.py`
directly it defaults to the folder the script sits in the "drop it next to
your media" workflow. `--path` always wins over both.

It asks before doing anything, and **keeps your originals by default**.

If a conversion would land on top of one of your files, StreamMii writes
`Movie.wii.mkv` instead of overwriting `Movie.mkv`. Pass `--delete` if you
actually want originals replaced.

**Flags:**

```
--path DIR           scan DIR (default: current dir via the launcher,
                     otherwise the folder the script lives in)
--mode 1..7          pick the mode non interactively (see menu below)
--dry-run            show exactly what it would do, change nothing
-y / --yes           skip the confirmation prompt (for scripts/cron)
--delete             delete originals after a successful convert
--no-delete          never delete originals (overrides config)
--keep-originals     alias for --no-delete
--force-reencode     re-encode even files that are already Wii ready
--clean-orphan-subs  delete .srt files left in folders with no video
--profile P          H.264 profile: baseline (default) / main / high
--reset-config       wipe the saved config and rerun first launch setup
--no-cleanup         never remove folders left empty by organising
--rescan-organized   also scan movies/, anime/, audio/ ... (off by default)
--sport NAME         sport for --mode 7, skipping the submenu
--adult-mode M       sub-mode for --mode 3: solo, group, hentai, jav,
                     lgbt solo, lgbt-group
--creator NAME       creator for --adult-mode solo/group/lgbt-* (repeatable)
--stall-timeout SEC  kill ffmpeg after SEC seconds with no output
                     (default 600, 0 disables)
--version            print version and exit
-h / --help          print help and exit
```

**Running it unattended.** `-y` means *never prompt* not "prompt anyway and
hope". If a run still needs an answer, StreamMii says which flag is missing and
exits **2**, so cron and systemd see a real failure instead of a green tick:

```
streammii --path /media --mode 1 -y                                  # movies/TV
streammii --path /media --mode 7 -y --sport "formula 1"              # sports
streammii --path /media --mode 3 -y --adult-mode group \
          --creator "A" --creator "B"                                # adult
```

Modes 3 and 7 have a second menu, so they need `--adult-mode` / `--sport` to run
unattended. Earlier builds prompted regardless of `-y`, hit `EOFError` with no
terminal attached, printed "aborted." and **exited 0** a scheduled job
reported success having converted nothing.

Exit codes: `0` success · `1` setup problem (no ffmpeg, bad `--path`) ·
`2` needs input it cannot ask for · `130` interrupted.

By default it **skips** files that are already Wii ready, **remuxes** when only
the container is wrong, and re-encodes **audio only** when the video is already
fine, so reruns are fast. Set `"skip_compliant": false` in the config or pass
`--force-reencode` to force a full re-encode pass.

### What gets scanned and what doesn't

The organiser owns these folders under your scan root:

```
movies/  tv shows/  audio/  adult/  documentaries/  k-drama/  anime/  sports/
```

**Anything inside them is StreamMii's own output and is not scanned as input,
and files already in them are never reorganised.** Both halves of that matter.
The first v4 build walked straight into its own output, which meant a second
pass converted everything a second time and stacked up `Movie.wii (2).mkv`,
`Movie.wii (3).mkv`...The second pass also re-derived each show's name from its
*current* parent folder, so `anime/S/Some Anime/season 01/` became
`anime/S/season 01/season 01/`, and again on every run after that.

`--rescan-organized` opts back in if you genuinely want those folders
re-processed. If your media already lives inside a folder with one of those
names, point `--path` at it and StreamMii will warn you that it is about to
nest a folder of the same name inside.

---

## Features

* **GPU acceleration**, picked per platform (this is new in v4, v3 assumed
  Windows and silently dropped Linux AMD users to a slow CPU path):
  * **AMD** - `h264_amf` on Windows, `h264_vaapi` on Linux, `videotoolbox` on macOS
  * **NVIDIA** - NVENC
  * **Intel** - QSV
  * **CPU** - **libx264** (v3 used libxvid here, see the v4 notes for why that was broken)
* If a GPU encoder fails, it automatically falls back to the next one and tells you.
* **Automatic media metadata handling** via guessit + optional OMDB lookup.

---

## Output settings

| Parameter | Value |
| :--- | :--- |
| **Resolution** | 640x480 (anamorphic; display aspect ratio is preserved) or 640x*N* for ultra wide. Sources **already smaller** than 640x480 keep their size they are no longer upscaled. |
| **Scaling** | Lanczos |
| **Video** | H.264, 1000 kbps, `-maxrate 1000k -bufsize 2000k`, **Baseline profile @ level 3.0** |
| **FPS** | **Capped** at 30 (25 for ultra-wide). Sources at or below the cap keep their original framerate. |
| **Audio** | AAC 128 kbps, downmixed to stereo |
| **Container** | MKV |

> **Framerate note:** v3's README said FPS "usually matches source media" it
> did not. v3 always forced 25 or 30 fps, so a 23.976 fps film came out at 30
> with duplicated frames and visible judder. v4 only applies `-r` when the
> source is *above* the cap. The "already compliant" check uses the *same* cap
> the encoder would apply to that aspect ratio an earlier v4 build accepted
> anything up to 31 fps flat, so a 30 fps ultrawide file read as compliant
> while a re-encode of it would have produced 25.

> **Profile note:** Baseline (no CABAC, no B-frames) is the safe default for the
> Wii's 729 MHz Broadway CPU. If your Wii handles Main profile fine, use
> `--profile main` for better quality at the same bitrate. Set
> `"wii_max_profile"` in the config to control which profiles count as
> "already compliant".

---

## Supported media types

Video: `.mkv .mp4 .avi .mov .flv .wmv .webm` 

Audio: `.mp3 .flac .wav .aac .m4a .ogg`

* Movies
* TV Shows
* <details><summary>Adult Content</summary>

  Solo, Group, Hentai, JAV, LGBT (solo/group).

  JAV mode strips leading `site@` prefixes from filenames and quarantines files
  under 50 MB into `_streammii_rejected/` **before** wasting time encoding them.
  v3 encoded them first and then deleted them outright with no recovery.

  </details>
* Audio
* Documentaries
* K-Drama
* Anime
* <details><summary>Sports</summary>

  Team Sports > Football, Basketball, Volleyball, Rugby, Baseball, Softball, Handball.

  Combat Sports > Boxing, MMA, Wrestling, Karate.

  Winter Sports > Skiing, Snowboarding, Ice Skating, Bobsledding.

  Water Sports > Surfing, Rowing, Kayaking, Synchronized Swimming.

  Motor Sports > F1, MotoGP.

  Individual Sports > Athletics (Track and Field), Tennis, Golf, Pool, Swimming, Badminton, Table Tennis, Cycling, Gymnastics.

  Equestrian > Show Jumping, Dressage.

  Other > Cricket, Hockey.

  </details>

---

## Where StreamMii keeps its stuff

```
~/streammii/
├── config.json              your settings (chmod 600 it can hold an OMDB key)
└── logs/
    ├── processed.jsonl      files already handled, one JSON record per line
    └── errors/              ffmpeg output for anything that failed
```

* Reset everything: `streammii --reset-config` (or just delete `~/streammii/`)
* Force a full recheck: `--force-reencode`
* On Windows this lives under `%USERPROFILE%\streammii\`
* `STREAMMII_LAUNCHER=1` in the environment makes StreamMii default to the
  current directory instead of its own folder. The installers set this in the
  launcher; you never need to set it by hand.

Each record is `[name, size, mtime, fingerprint]`, where the fingerprint is a
SHA-256 over the file's first and last 256 KB. Earlier v3 records were
`[name, size, mtime]` with no content and no path so two different
`S01E01.mkv` files of the same size and mtime (routine after a `cp -p`, an
rsync, or a batch rip) produced the *same* signature and the second one was
silently never converted. Old 3 field records are still honoured, so upgrading
does not reconvert anything you have already done.

The format changed from `name|size_kb` (v3 and earlier) to JSON lines because a
filename containing a newline could inject fake entries into the old format.
Old `processed.txt` files are ignored.

`processed.jsonl` rotates at 5 MB (one generation is kept as
`processed.jsonl.1`, and both are read), and `logs/errors/` is pruned of
anything older than 30 days.

> **Deleting `processed.jsonl` is no longer a documented step, and used to be a
> trap.** This README previously listed it as routine maintenance, but combined
> with the output rescanning bug above it duplicated your entire library. Use
> `--force-reencode` instead. Even if you do delete the log, StreamMii now
> recognises sources whose converted counterpart is already filed away and
> leaves them alone.

---

##  Install:

###  Linux

```bash
chmod +x noob-install-by-motanu.sh
./noob-install-by-motanu.sh
```

Then run it as `streammii` from anywhere.

> **This is the big fix in v4.** The v3 installers built a venv at
> `/opt/myapp-venv`, never mentioned it, and the README told you to run
> `python streammii-monolithic-v3.py` which used the *system* Python and died
> with `ModuleNotFoundError: guessit`. The documented install simply did not
> work. v4 installs to `/opt/streammii-venv` and drops a
> `/usr/local/bin/streammii` launcher that runs the script inside its own venv.

| Distro | Status |
| :--- | :--- |
| Debian / Ubuntu / Mint | tested |
| Arch / Manjaro | tested, see the upgrade note below |
| Fedora | tested (RPM Fusion is enabled for you) |
| openSUSE | installs; ffmpeg may need the Packman repo |
| RHEL / CentOS | EPEL + RPM Fusion are now enabled for you, but lightly tested |
| Alpine | **needs "apk add bash" first**, see below |

**Alpine and other minimal systems.** The installer is a bash script and Alpine
ships busybox `ash`, so run `apk add bash` before `./noob-install-by-motanu.sh`.
There is also no `sudo` the script uses `doas` if present, otherwise run it as
root (`su -c ./noob-install-by-motanu.sh`).

**Two things the installers change on your system.** Neither was mentioned
before, and both now ask first:

* **Arch:** installing needs a package database refresh, and the only safe form
  of that is `pacman -Syu`, which upgrades *every package on the machine*. You
  are asked before it runs. Decline and the install continues without a
  refresh (a package may 404 as a result).
* **Raspberry Pi OS / Raspbian (ARM installer):** ffmpeg needs the
  `contrib`/`non-free` components, so `/etc/apt/sources.list*` is edited. You
  are asked first, and `*.streammii.bak` backups are written either way.

Set `STREAMMII_ASSUME_YES=1` to accept both without a prompt (for unattended
installs). With no terminal attached, both default to **no** rather than
silently changing your system.

The installer now **verifies ffmpeg actually has libx264** rather than just
checking that an `ffmpeg` binary exists Fedora's `ffmpeg-free` and openSUSE's
OSS build both pass a `command -v ffmpeg` check while being useless here.
If libx264 is missing it tells you exactly what to do for your distro.

Not compatible: Gentoo, NixOS, Void, Slackware, Android (Termux).

No support provided for aarch64 / armv6 / armv7 issues.

<details><summary>ARM (aarch64 / armv7 / armv6)</summary>

```bash
chmod +x arm-noob-install-motan.sh
./arm-noob-install-motan.sh
```

**aarch64** should work on: Ubuntu 22.04/24.04, Linux Mint 21/22, Debian 11/12,
Raspberry Pi OS (Bullseye/Bookworm), Arch, Manjaro, Fedora 38–40,
openSUSE Leap/Tumbleweed, Alpine 3.18+.

**armv6 / armv7** should work on Raspberry Pi OS (Bullseye/Bookworm) and Debian 11/12.

> **Bullseye and Debian 11 ship Python 3.9**, so the hash pinned
> `requirements.txt` will not install there `requests` and `urllib3` require
> 3.10+. The installer falls back to unpinned versions automatically and prints
> a warning. StreamMii itself is unaffected; those packages are optional.

Partially working / limited:
- Fedora on armv7 (Fedora dropped 32-bit ARM after F36)
- Alpine on armv6 (build ffmpeg from source)
- RHEL & CentOS (needs EPEL + RPM Fusion added manually)
- Ubuntu 20.04 (`python3-venv` may need installing by hand)

**Be realistic about armv6.** Encoding 640x480 H.264 on a Pi Zero runs at a
small fraction of realtime. Convert on a PC and copy the files over.

The Raspbian contrib/non-free repo edit was rewritten again in v4. The v3
version was anchored to `^deb .*raspbian` in `/etc/apt/sources.list`, which is
correct but matches nothing on Bookworm that uses deb822 `.sources` files in
`sources.list.d/`, and `non-free` split into `non-free-firmware`. v4 handles
both formats, leaves unrelated repos alone, and writes `*.streammii.bak`
backups.

</details>

### Windows

```
Run NOOB_SETUP_WINGET.bat
```

winget verifies every package against a SHA256 in Microsoft's signed manifest
repo before running it. No unverified downloads, no piped remote scripts.
winget ships by default on Win10 1809+ and Win11. If it's missing, the
installer prints manual steps and exits rather than running anything unverified.

Three things changed in v4:

* **It no longer elevates itself.** v3 relaunched as admin, which repointed
  `%LocalAppData%` at the *administrator's* profile so it couldn't find a
  per user Python install and put pip packages in the wrong profile entirely.
  winget installs per user fine and raises its own UAC prompt when needed.
* **Exit codes are handled correctly.** v3 used `if errorlevel 1`, but winget
  returns *negative* HRESULTs and cmd's `if errorlevel N` is a signed `>=`
  comparison so genuine winget failures went completely unnoticed. v4
  compares `%errorlevel%` as a string, whitelists the benign "already
  installed" codes, and ultimately decides pass/fail by **verifying python and
  ffmpeg actually run**.
* **Everything is quoted.** v3 did `%PYTHON_EXE% -m pip ...`, which becomes
  `C:\Program Files\Python312\python.exe -m pip` unquoted "C:\Program is not
  recognized" and every single pip call failed.

Dependencies go into a venv at `%LOCALAPPDATA%\StreamMii\venv`, and a launcher
is written to `%LOCALAPPDATA%\StreamMii\streammii.cmd`. Make a desktop shortcut
to it.

### Installing packages manually

```bash
# pinned + hash verified (recommended) needs Python 3.10+
pip install --require-hashes --only-binary=:all: -r requirements.txt

# Python 3.8 / 3.9: the pinned set will NOT install (see below)
pip install "requests<2.32" "guessit<4" colorama

# or install nothing at all every one of them is optional
```

### What these packages do

All three are **optional**. StreamMii checks for each at import and carries on
without it:

| Package | If missing |
| :--- | :--- |
| `requests` | no OMDB title lookups; the built in filename parser is used |
| `guessit` | falls back to the built in filename parser |
| `colorama` | no ANSI colour on legacy Windows consoles |

Conversion, organising, and every safety check work identically either way.
**ffmpeg and ffprobe are the only hard requirements**

> **Python 3.10 floor.** `requests` and `urllib3` both declare
> `Requires-Python >=3.10`, so `pip install -r requirements.txt` fails outright
> on Python 3.8/3.9 which includes Debian 11, Raspberry Pi OS Bullseye and
> Ubuntu 20.04, all listed as supported below. This is not new: the same two
> pins were in the previous `requirements.txt`. It was simply never written
> down, and the README claimed "Python 3.8+" without qualification. Use the
> unpinned line above on those systems, or run with no dependencies at all.

---

## Unverified platforms 

No support is provided for issues on any of these.

<details><summary>Windows on ARM</summary>

- ffmpeg: https://github.com/tordona/ffmpeg-win-arm64
- python: https://www.python.org/downloads/release/python-31313/

</details>

<details><summary>macOS (Apple Silicon)</summary>

Several ffmpeg sources exist, each with tradeoffs pick what suits you:
1. https://github.com/Vargol/ffmpeg-apple-arm64-build
2. https://gitlab.com/martinr92/ffmpeg
3. https://osxexperts.net/

Python: https://www.python.org/downloads/release/python-3144/

</details>

<details><summary>macOS (Intel)</summary>

- ffmpeg: https://evermeet.cx/ffmpeg/ 
- python: https://www.python.org/downloads/release/python-3144/

Hackintosh untested.

</details>

Whichever you use, sanity check it before trusting it:

```bash
ffmpeg -encoders | grep libx264     # must print something
```

---

##  Why StreamMii-Monolithic?

Same StreamMii, but one file instead of six. Download the monolithic script
plus your OS's installer, run the installer, done.

Changes vs monolithic v1 / the cucu-labs version:
> 3x faster probing
> Fails with a clean error instead of crashing
> H.264 encoder for non standard pixel formats that won't ruin playback
> Won't delete itself or files around it
> Secure OMDB/IMDB lookups
> Assorted bug fixes

---

## V4 Release , bug fixes

### Data loss 

- **Folders containing only dotfiles were deleted.** `cleanup_single_folder`
  built its file list as `[f for f in os.listdir(path) if not f.startswith(".")]`
  and then `rmtree`'d the folder when that list came back empty. A directory
  holding `.git/` and `.env` therefore read as "empty" and was destroyed.
  Emptiness is now decided from the **unfiltered** listing; only a small
  allow list of OS junk (`.DS_Store`, `Thumbs.db`, `desktop.ini`) is ignored.
- **Every `.mkv` input was overwritten in place.** The output path was
  `basename + ".mkv"`, which for an `.mkv` source *is the source path* 
  `os.replace()` then destroyed the original even with
  `delete_originals: false`. It also silently clobbered an unrelated `foo.mkv`
  sitting next to `foo.avi`. Output paths are now checked against the input and
  against existing files; collisions become `.wii.mkv`.
- **Empty folder cleanup swept the entire scan tree.** With `--path` pointed at
  a home directory that would be a very bad day. Cleanup is now scoped to
  directories StreamMii itself created or emptied during that run.
- **JAV files under 50 MB were deleted with an unguarded `os.remove`**, after
  already spending CPU encoding them. Now the size is checked *first* and the
  file is moved to `_streammii_rejected/`.

### Correctness

- **The CPU encoder produced files StreamMii could never accept.** The fallback
  was `libxvid`, which outputs `mpeg4`, while the compliance check requires
  `h264` so v3 re-encoded its own output on every single run, losing quality
  each time. The CPU path is now `libx264`. Verified: a v4 output fed back in
  is reported as `already Wii-ready (skip)`.
- **`--help` launched the interactive setup wizard.** The config was loaded at
  import time, before argparse ran. `--help`, `--version` and `--dry-run` now
  work on a clean machine.
- **`1920x1080` in a filename was parsed as a TV episode.** `\d{1,2}x\d{1,2}`
  matched "20x10" inside the resolution, and `19\d{2}` matched "1920" as a
  year. The title also included the file extension (`Movie 2019 mkv`) and show
  names were truncated at the first dot. Rewritten: resolutions are stripped
  first, the year is anchored with lookarounds and runs to 2059, and release
  tags (`1080p`, `x264`, `WEB-DL`, `BluRay`…) are trimmed. Short ambiguous tags
  are deliberately excluded so real titles like *Cam* and *Web of Lies* survive.
- **"Remux (stream copy)" re-encoded the audio anyway.** `-c:a aac` was emitted
  unconditionally, so compliant AAC was re-encoded AAC→AAC for nothing.
  Verified: remuxed audio is now byte-identical to the source.
- **Framerate was forced instead of capped** (see the output settings table).
- **Audio mode had no stream mapping**, so embedded cover art was at the mercy
  of ffmpeg's default stream selection. Now `-vn -map 0:a:0 -c:a libmp3lame -ac 2`.
- Temp files use a unique `.streammii_tmp_*` name instead of `_tmp.mkv`, so they
  can't clobber a real file or be picked up as input by the next run. Abandoned
  temps from a crashed run are swept on startup (with an age guard so a parallel
  run isn't disturbed).
- H.264 profile and framerate are now part of the compliance check, so a
  High@L4.1 file isn't wrongly marked "already Wii-ready".

### Robustness and security

- `-nostdin` plus `stdin=DEVNULL` on every ffmpeg call ffmpeg reads stdin by
  default and was eating the menu input during multi file runs.
- `stdout` is no longer an undrained `PIPE` (deadlock risk), and the progress
  loop no longer busy spins at EOF.
- ffprobe calls have a 60s timeout; a corrupt file or dead network mount can't
  hang the run forever.
- `config.json` is created 0600 **atomically** via `os.open`, instead of being
  written with the default umask and chmod'd afterwards the OMDB key is no
  longer briefly world readable.
- OMDB responses get `raise_for_status()` and a 256 KB size cap.
- `safe_name()` handles Windows reserved names (`CON`, `COM1`...), control
  characters, and length limits.
- The processed log is JSON lines, so a filename containing a newline can't
  inject entries.
- Ctrl+C cleans up in flight temp files and exits 130.
- Scans skip `node_modules`, `$RECYCLE.BIN`, `@eaDir`, `lost+found` and dot directories.

### V4 hardening 

**Data / library integrity**

- **StreamMii reingested its own output.** `movies/`, `anime/`, `audio/` and
  the rest were walked as if they were input, so a second pass converted
  everything again and stacked up `Movie.wii (2).mkv`. The README made this
  routine by telling people to delete `processed.jsonl`. Those folders are now
  excluded (`--rescan-organized` opts back in), and a source whose converted
  counterpart is already filed away is recognised even with no log at all.
- **Reorganising corrupted the folder tree.** Modes 2, 5 and 6 took the
  series/artist name from the file's immediate parent folder, which after the
  first pass is `season 01`. `anime/S/Some Anime/season 01/` became
  `anime/S/season 01/season 01/`, and stayed wrong. Files sitting directly in
  the scan root also took the *scan root's own name* as the show name. The
  parent folder is now used only when it actually identifies the work.
- **The processed log signature could collide.** `[name, size, mtime]` with no
  path and no content meant two different `S01E01.mkv` of equal size and mtime
  were the same file as far as StreamMii was concerned, and the second was
  never converted. A head+tail SHA-256 fingerprint is now part of the record.

**Correctness**

- **A corrupt external `.srt` killed the whole encode.** `-map 1:0?` makes the
  *stream* optional, not the *input file* ffmpeg aborts before stream
  selection. The encoder fallback loop then burned a second full attempt on
  libxvid for a fault that had nothing to do with the encoder, and told you
  libx264 had failed. Sidecar subtitles are probed first and dropped with a
  warning if unreadable; input errors no longer trigger an encoder fallback.
- **`-c:a mp3` was the audio fallback.** `mp3` is a *decoder* name; there is no
  such encoder. On any ffmpeg without libmp3lame every audio conversion failed.
  The chain is now libmp3lame → libshine -> a clear message.
- **Compliance used a flat 31 fps cap** while the encoder caps ultra wide at 25.
- **Sources below 640x480 were upscaled**, spending the whole bitrate budget on
  invented pixels.
- **No usable H.264 encoder** now fails once, up front, instead of once per file
  with a misleading "falling back" message.

**Robustness / security**

- **No timeout on the encode.** ffprobe had 60 s; the encode itself had none, so
  a dead NFS mount hung the run forever. There is now a stall watchdog
  (`--stall-timeout`, default 600 s, 0 disables) and a timeout on audio too.
- **SIGTERM orphaned ffmpeg.** There was no `p.kill()` anywhere and no signal
  handler, so `finally:` never ran: temp files were left behind and ffmpeg kept
  encoding after the parent was gone. Ctrl+C only appeared to work because the
  *terminal* signals the whole process group. SIGTERM/SIGHUP are now handled and
  the child is killed on every exit path.
- **`-y` did not mean non interactive.** Modes 3 and 7 prompted anyway, hit
  `EOFError` with no terminal, and exited **0**. Added `--sport`,
  `--adult-mode` and `--creator`; missing input is now a named error and exit 2.
- **The config temp file followed symlinks.** A fixed, predictable path opened
  without `O_EXCL`; now `mkstemp` + `fsync` + atomic replace.
- **The OMDB size cap did nothing.** `len(r.content)` had already downloaded the
  whole body. Now read through a capped stream, with redirects refused (the API
  key travels in the query string) and results cached one lookup per title
  instead of one HTTP request *per file*.
- `--no-cleanup` to disable empty folder removal, which the README listed as
  having no off switch.
- Log rotation and error log pruning; buffered log writes.
- `_is_inside_root` uses `normcase`, so containment works on Windows and macOS.
- Config values are validated, so a typo says so instead of silently encoding
  the wrong thing.

---

## FAQ

**Why are there multiple versions of monolithic StreamMii in the repo?**
> Fallback if the current one misbehaves, a visible diff between versions, and a
> backup of everything.
>
> **Exception: do not use v3 for anything.** It destroys data. Use v4 or, if
> you must go back, v2.

**Why separate the classic and monolithic versions?**
> Better version control, less clutter, more control over the work, and
> different feedback on each. New monolithic versions still go up in the
> cucu-labs repo for convenience. Update: 30.10.2026 repos had compelity seperated.

**Can I run it without the installer?**
> Yes, but use a venv Debian 12+, Ubuntu 23.04+, Fedora 38+ and Arch all
> refuse `pip install` into the system Python with
> `error: externally-managed-environment` (PEP 668):
>
> ```bash
> python3 -m venv ~/.venvs/streammii
> ~/.venvs/streammii/bin/pip install --require-hashes -r requirements.txt
> ~/.venvs/streammii/bin/python streammii-monolithic-v4.py --path /your/media --dry-run
> ```
>
> You still need ffmpeg with libx264 on PATH. The bare `pip install` this FAQ
> used to suggest fails on every current mainstream distro.

---

## Original repo
> https://github.com/cucu-labs/StreamMii

## Fork  // unused
> https://github.com/motan1337/StreamMii

## Other intresting projects you may be intrested in: 
> https://github.com/motan1337/clipctl.py
> 
> https://github.com/motan1337/Modern-IoT-Scanner
>
> https://github.com/motan1337/cool-webports-games
>
> https://github.com/motan1337/Steam-Token-Loader 
>
> https://dev.motan-femboy.cc/ (personal website)
>
> https://github.com/motan1337/Shitty-games
>
> https://github.com/motan1337/4chan-shitty-clone-motan
>
> https://github.com/motan1337/Templates-for-web-devs
