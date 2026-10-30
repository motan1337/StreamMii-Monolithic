#!/usr/bin/env bash
# StreamMii installer (x86_64 / generic Linux) v4
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

# privilege
if [[ $EUID -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        say "Requesting root privileges..."
        exec sudo -- bash "$0" "$@"
    elif command -v doas >/dev/null 2>&1; then
        say "Requesting root privileges (doas)..."
        exec doas bash "$0" "$@"
    else
        die "not root, and neither sudo nor doas is installed.
    Re run this script as root:  su -c './$(basename "$0")'
    (Alpine and other minimal systems ship no sudo by default.)"
    fi
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
            # -Sy alone leaves a partial upgrade and can break the system, so
            # -Syu is the only safe form but it upgrades everything, so ask. I HAVE LIFETIME TRAUMA WITH ARCH OK?
        pacman)
            if ask "Arch needs a full system upgrade (pacman -Syu) before installing. Run it?"; then
                pacman -Syu --noconfirm
            else
                warn "Skipping the upgrade.  Installing without refreshing the"
                warn "package database if a package 404s, that is why."
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

# python
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
    # only ever remove something that is unmistakably a venv ive made
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
        say "Using hash verified requirements: $REQ_FILE"
        if "$VPIP" install --require-hashes --only-binary=:all: -r "$REQ_FILE"; then
            return 0
        fi
        warn "No wheel available for one or more packages on this platform."
        warn "Retrying with source builds allowed the packages are still"
        warn "hash verified; only the build backend is not."
        "$VPIP" install --require-hashes -r "$REQ_FILE" \
            || die "hash verified install failed refusing to fall back silently."
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
    no OMDB lookups and the built-in filename parser instead of guessit."
}

install_deps

# ffmPEG_ME_PLEASE_MASTER_BOOT_RECORD
echo; say "Installing FFmpeg..."
case "$PKG_MGR" in
    apt)    pkg_install ffmpeg ;;
    dnf)
        if ! rpm -q rpmfusion-free-release &>/dev/null; then
            say "Enabling RPM Fusion (Fedora ships a codec stripped ffmpeg free)..."
            dnf install -y \
              "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm" \
              || warn "RPM Fusion setup failed; ffmpeg may lack encoders."
        fi
        pkg_install ffmpeg || pkg_install ffmpeg-free ;;
    yum)
        if ! rpm -q epel-release &>/dev/null; then
            say "Enabling EPEL (RHEL/CentOS have no ffmpeg in the base repos)..."
            yum install -y epel-release || warn "EPEL setup failed."
        fi
        if ! rpm -q rpmfusion-free-release &>/dev/null; then
            el=$(rpm -E %rhel 2>/dev/null || echo "")
            if [[ -n "$el" ]]; then
                say "Enabling RPM Fusion (free) for EL${el}..."
                yum install -y \
                  "https://download1.rpmfusion.org/free/el/rpmfusion-free-release-${el}.noarch.rpm" \
                  || warn "RPM Fusion setup failed; ffmpeg may lack encoders."
            fi
        fi
        pkg_install ffmpeg || warn "ffmpeg install failed; see https://ffmpeg.org/download.html" ;;
    pacman) pkg_install ffmpeg ;;
    zypper)
        # openSUSE OSS ffmpeg has no x264
        pkg_install ffmpeg || warn "ffmpeg install failed; you may need the Packman repo."
        ;;
    apk)    pkg_install ffmpeg ;;
esac

command -v ffmpeg &>/dev/null || die "FFmpeg not found after install: https://ffmpeg.org/download.html"

if ! have_encoder libx264; then
    warn "======================================================================"
    warn "This ffmpeg has NO libx264 encoder."
    warn "StreamMii will fall back to libxvid, which produces MPEG-4 files that"
    warn "are NOT Wii compliant and will be re encoded on every run."
    warn "Fix:"
    case "$PKG_MGR" in
        dnf|yum) warn "  enable RPM Fusion, then: dnf install ffmpeg" ;;
        zypper)  warn "  add the Packman repo, then: zypper install from packman ffmpeg" ;;
        apk)     warn "  enable the 'community' repository, then: apk add ffmpeg" ;;
        *)       warn "  install a full ffmpeg build: https://ffmpeg.org/download.html" ;;
    esac
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
