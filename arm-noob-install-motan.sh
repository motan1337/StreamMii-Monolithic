#!/usr/bin/env bash
# StreamMii installer (arm) -- v4
set -euo pipefail

VENV_DIR="/opt/streammii-venv"
LAUNCHER="/usr/local/bin/streammii"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

say()  { printf '[!] %s\n' "$*"; }
warn() { printf '[~] %s\n' "$*" >&2; }
die()  { printf '[X] %s\n' "$*" >&2; exit 1; }

# consent because we dont want to accidently rape the host
ASSUME_YES="${STREAMMII_ASSUME_YES:-0}"

# install
ask() {
    local prompt="$1" reply=""
    if [[ "$ASSUME_YES" == "1" ]]; then
        say "$prompt -> yes (STREAMMII_ASSUME_YES=1)"
        return 0
    fi
    if [[ ! -t 0 ]]; then
        warn "$prompt -> no (no terminal to ask on; set STREAMMII_ASSUME_YES=1 to accept)"
        return 1
    fi
    read -rp "[?] $prompt [y/N] " reply || reply=""
    [[ "$reply" == [Yy]* ]]
}

pause_at_end() {
    [[ -t 0 ]] || return 0
    read -rp "Press Enter to exit..." _ || true
}

have_encoder() {
    local enc_list
    enc_list=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)
    case "$enc_list" in *"$1"*) return 0 ;; *) return 1 ;; esac
}

ffmpeg_first_line() {
    local v
    v=$(ffmpeg -version 2>&1 || true)
    printf '%s\n' "${v%%$'\n'*}"
}

if [[ $EUID -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        say "Requesting root privileges..."
        exec sudo -- bash "$0" "$@"
    elif command -v doas >/dev/null 2>&1; then
        say "Requesting root privileges (doas)..."
        exec doas bash "$0" "$@"
    else
        die "not root, and neither sudo nor doas is installed.
    Rerun this script as root:  su -c './$(basename "$0")'
    (Alpine and other minimal systems ship no sudo by default.)"
    fi
fi

# architecture to know with what motherfucker we deal with
ARCH=$(uname -m)
say "Detected architecture: $ARCH"
case "$ARCH" in
    x86_64)          ARCH_LABEL="x86_64 (64-bit PC)" ;;
    aarch64|arm64)   ARCH_LABEL="ARM64 (aarch64)" ;;
    armv7l|armv7)    ARCH_LABEL="ARM 32-bit (armv7)" ;;
    armv6l|armv6)    ARCH_LABEL="ARM 32-bit (armv6 limited support)" ;;
    *)               ARCH_LABEL="Unknown ($ARCH)" ;;
esac
say "Architecture label: $ARCH_LABEL"

if [[ "$ARCH" == armv6* ]]; then
    echo
    warn "armv6 detected.  Some packages may not exist in official repos."
    warn "Raspberry Pi OS is by far the best supported distro here."
    warn "Encoding 640x480 H.264 on a Pi Zero is extremely slow expect"
    warn "many times realtime.  Converting on a PC and copying over is saner."
    echo
fi

# package manager
detect_pkg_manager() {
    if   command -v apt-get &>/dev/null; then echo "apt"
    elif command -v dnf     &>/dev/null; then echo "dnf"
    elif command -v yum     &>/dev/null; then echo "yum"
    elif command -v pacman  &>/dev/null; then echo "pacman"
    elif command -v zypper  &>/dev/null; then echo "zypper"
    elif command -v apk     &>/dev/null; then echo "apk"
    else die "No supported package manager found."
    fi
}
PKG_MGR=$(detect_pkg_manager)
say "Detected package manager: $PKG_MGR"

pkg_update() {
    case "$PKG_MGR" in
        apt)     apt-get update -qq ;;
        dnf|yum) "$PKG_MGR" check-update -q || true ;;
        pacman)
            # -Sy alone leaves a partial upgrade and can break the system, so
            # -Syu is the only safe form but it upgrades everything, so ask. I HAVE LIFETIME TRAUMA WITH ARCH OK?
            if ask "Arch needs a full system upgrade (pacman -Syu) before installing. Run it?"; then
                pacman -Syu --noconfirm
            else
                warn "Skipping the upgrade; a package may 404 as a result."
            fi ;;
        zypper)  zypper --non-interactive refresh ;;
        apk)     apk update ;;
    esac
}

pkg_install() {
    case "$PKG_MGR" in
        apt)     DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
        dnf|yum) "$PKG_MGR" install -y "$@" ;;
        pacman)  pacman -S --needed --noconfirm "$@" ;;
        zypper)  zypper --non-interactive install -y "$@" ;;
        apk)     apk add --no-cache "$@" ;;
    esac
}

# python env venv pip boratrat etc
echo; say "Updating package lists..."
pkg_update

say "Installing Python 3 + pip + venv..."
case "$PKG_MGR" in
    apt)     pkg_install python3 python3-pip python3-venv ;;
    dnf|yum) pkg_install python3 python3-pip ;;
    pacman)  pkg_install python python-pip ;;
    zypper)  pkg_install python3 python3-pip ;;
    apk)     pkg_install python3 py3-pip ;;
esac

if   command -v python3 &>/dev/null; then PYTHON_EXE=python3
elif command -v python  &>/dev/null; then PYTHON_EXE=python
else die "Python not found after install."
fi
say "Found Python: $PYTHON_EXE ($($PYTHON_EXE --version 2>&1))"

echo; say "Creating virtual environment at $VENV_DIR..."
if [[ -e "$VENV_DIR" ]]; then
    [[ -f "$VENV_DIR/pyvenv.cfg" ]] || die "$VENV_DIR exists and is not a venv refusing to delete it."
    rm -rf "$VENV_DIR"
fi
"$PYTHON_EXE" -m venv "$VENV_DIR" || die "venv creation failed (install python3-venv?)"

VPY="$VENV_DIR/bin/python"
VPIP="$VENV_DIR/bin/pip"

say "Installing Python packages..."

# requirements.txt pins requests and urllib3, both of which declare
# Requires-Python >=3.10.  On Debian 11, Raspberry Pi OS Bullseye and Ubuntu
# 20.04 (Python 3.9 and 3.8) pip refuses the whole file, so check first and say
# so plainly rather than dying on a wall of resolver output.
PYVER=$("$VPY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_OK_FOR_PINS=$("$VPY" -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')

install_deps() {
    if [[ -f "$REQ_FILE" && "$PY_OK_FOR_PINS" == "1" ]]; then
        say "Using hash-verified requirements: $REQ_FILE"
        if "$VPIP" install --require-hashes --only-binary=:all: -r "$REQ_FILE"; then
            return 0
        fi
        warn "No wheel available for one or more packages on this platform."
        warn "Retrying with source builds allowed, the packages are still"
        warn "hash verified; only the build backend is not."
        "$VPIP" install --require-hashes -r "$REQ_FILE" \
            || die "hash verified install failed, refusing to fall back silently."
        return 0
    fi

    if [[ -f "$REQ_FILE" ]]; then
        warn "======================================================================"
        warn "This venv is Python $PYVER.  requirements.txt needs 3.10 or newer,"
        warn "because requests and urllib3 dropped support for anything older."
        warn "Falling back to versions that DO run here."
        warn "These are version pinned but NOT hash verified."
        warn "StreamMii works fine either way all three packages are optional."
        warn "======================================================================"
    else
        warn "requirements.txt not found next to installer; falling back to pinned versions."
        warn "These are version pinned but NOT hash verified."
    fi
    "$VPIP" install "requests<2.32" "guessit<4" "colorama==0.4.6" \
        || warn "Optional packages failed to install.  StreamMii still works:
    no OMDB lookups and the built in filename parser instead of guessit."
}

install_deps

# ffmPEG_ME_PLEASE_MASTER_BOOT_RECORD
echo; say "Installing FFmpeg..."

enable_raspbian_nonfree() {
    local changed=0

    if ! ask "FFmpeg on Raspberry Pi OS needs the contrib/non free components. Add them to your APT sources? (backups are written as *.streammii.bak)"; then
        warn "Leaving APT sources alone.  If ffmpeg will not install, add"
        warn "contrib/non-free by hand and re run."
        return 0
    fi

    local f
    for f in /etc/apt/sources.list.d/*.sources; do
        [[ -f "$f" ]] || continue
        grep -qi 'raspbian\|raspberrypi' "$f" || continue
        if grep -q '^Components:' "$f" && ! grep -qi '^Components:.*contrib' "$f"; then
            cp -a "$f" "$f.streammii.bak"
            sed -i 's/^\(Components:.*\)$/\1 contrib non free non free firmware/' "$f"
            changed=1
        fi
    done

    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [[ -f "$f" ]] || continue
        grep -qi 'raspbian\|raspberrypi' "$f" || continue
        if grep -qE '^deb .*(raspbian|raspberrypi)' "$f" && ! grep -qE '^deb .*contrib' "$f"; then
            cp -a "$f" "$f.streammii.bak"
            sed -i -E '/^deb .*(raspbian|raspberrypi)/ { /contrib/! s/$/ contrib non-free/ }' "$f"
            changed=1
        fi
    done

    if (( changed )); then
        say "Enabled contrib/non free (backups written as *.streammii.bak)"
        pkg_update
    else
        say "contrib/non free already enabled, or not a Raspbian syle system."
    fi
}

install_ffmpeg_apt() {
    [[ "$ARCH" == armv6* || "$ARCH" == armv7* ]] && enable_raspbian_nonfree
    pkg_install ffmpeg
}

install_ffmpeg_dnf() {
    if ! rpm -q rpmfusion-free-release &>/dev/null; then
        say "Enabling RPM Fusion (free)..."
        local ver; ver=$(rpm -E %fedora)
        [[ "$ARCH" == armv7* ]] && warn "Fedora dropped 32-bit ARM after F36; this will probably fail."
        dnf install -y \
            "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-${ver}.noarch.rpm" \
            || warn "RPM Fusion setup failed; ffmpeg may lack encoders."
    fi
    pkg_install ffmpeg || pkg_install ffmpeg-free
}

case "$PKG_MGR" in
    apt)    install_ffmpeg_apt ;;
    dnf)    install_ffmpeg_dnf ;;
    yum)    pkg_install ffmpeg ;;
    pacman) pkg_install ffmpeg ;;
    zypper) pkg_install ffmpeg || warn "ffmpeg install failed; you may need the Packman repo." ;;
    apk)    pkg_install ffmpeg ;;
esac

if ! command -v ffmpeg &>/dev/null; then
    echo
    warn "FFmpeg not found after install."
    if [[ "$ARCH" == armv6* ]]; then
        warn "armv6 usually requires building FFmpeg from source."
        warn "See: https://trac.ffmpeg.org/wiki/CompilationGuide"
    fi
    die "cannot continue without ffmpeg."
fi

if ! have_encoder libx264; then
    warn "======================================================================"
    warn "This ffmpeg has NO libx264 encoder."
    warn "StreamMii will fall back to libxvid, which produces MPEG-4 files that"
    warn "are NOT Wii compliant and get re encoded on every single run."
    warn "Install a full ffmpeg build before using StreamMii seriously."
    warn "======================================================================"
fi

# launcher
STREAMMII_PY="$(ls -1 "$SCRIPT_DIR"/streammii-monolithic-v*.py 2>/dev/null | sort -V | tail -1 || true)"

if [[ -n "$STREAMMII_PY" ]]; then
    install -d /opt/streammii
    install -m 0755 "$STREAMMII_PY" /opt/streammii/streammii.py
    cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# StreamMii launcher runs the script inside its own venv so the
# dependencies installed by the installer are importable.
# The marker tells StreamMii it was started from an installed location, so it
# defaults to the CURRENT directory rather than /opt/streammii.
export STREAMMII_LAUNCHER=1
exec "$VPY" /opt/streammii/streammii.py "\$@"
EOF
    chmod 0755 "$LAUNCHER"
    say "Installed launcher: $LAUNCHER"
else
    warn "No streammii-monolithic-v*.py found next to this installer."
    warn "Run it manually with: $VPY /path/to/streammii-monolithic-v4.py"
fi

echo
echo "================ VERIFICATION ================"
echo; echo "> Architecture : $ARCH_LABEL"
echo; echo "> Python (venv):"; "$VPY" --version
echo; echo "> Venv location: $VENV_DIR"
echo; echo "> Pip packages:"
for pkg in requests guessit colorama; do
    "$VPIP" show "$pkg" 2>/dev/null | grep -E "^(Name|Version)" || echo "  [X] $pkg -- not found"
done
echo; echo "> FFmpeg:"; ffmpeg_first_line
if have_encoder libx264; then echo "  libx264: yes"; else echo "  libx264: NO (see warning above)"; fi
if [[ -x "$LAUNCHER" ]]; then
    echo; echo "> Launcher:"; "$LAUNCHER" --version || true
fi
echo
echo "================ ALL DONE! ================"
echo
echo "Run it with:  cd /your/media && streammii"
echo "         or:  streammii --path /your/media"
echo "Dry run first: streammii --dry-run"
echo
pause_at_end
