#!/bin/bash
#==============================================================
#   Script: run.sh
#  Purpose: One-command launch for the USV_Deploy_UI console.
#           Handles the Python environment so you don't have to
#           remember to `conda activate` first, sets the mission
#           path for shoreside, opens the browser, and starts the
#           Flask app.
#
#  USAGE
#    ./run.sh                 Start the console at timewarp 1 (real time).
#    ./run.sh N               Start at simulation timewarp N (e.g. ./run.sh 4).
#                             Default is 1, best for human-in-the-loop manual
#                             control; use 4 for fast unattended runs.
#    ./run.sh --no-browser    Start without opening a browser.
#    ./run.sh N --no-browser  Combine: set warp and skip the browser.
#    ./run.sh -h | --help     Show this message.
#
#  ENVIRONMENT
#    The web app (app.py) needs only Flask, but the boats it
#    launches need `pyquaticus`. Because boat subprocesses inherit
#    this app's interpreter (sys.executable), the app must run in a
#    Python where pyquaticus is importable. This script:
#      1. Checks if pyquaticus is already importable in `python3`.
#         If so, it uses that and skips conda entirely.
#      2. Otherwise it activates the project's conda env (by
#         absolute path) and re-checks.
#    So it runs in whatever environment already has pyquaticus and
#    only reaches for conda as a fallback.
#==============================================================

set -u  # error on unset vars (but not -e: we handle failures explicitly)

#--------------------------------------------------------------
# Configuration -- edit these paths if your layout differs.
#--------------------------------------------------------------
# Directory containing app.py (this script lives there too).
APP_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# The mission tree, exported so app.py can launch shoreside.
export MCTF_MISSION_PATH="/home/tyler/moos-ivp-mctf/missions/tyler_thesis"

# Simulation time warp. Default 1 = real time (use this when issuing manual
# commands -- 4x changes the game faster than a human can react). Override on the
# command line: `./run.sh 4` for fast unattended runs. app.py reads MCTF_TIME_WARP
# and applies it to BOTH shoreside and the boats so they stay in sync. Sim only;
# real hardware always runs 1x. (Exported below, after arg parsing.)
TIME_WARP=1

# Manual-control fallback file. The control bridge writes here and every boat's
# policy reads here, so they MUST agree on this path. One shared file holds all
# boats' commands (each boat reads only its own keys), and it lives OUTSIDE the
# staged entry dirs (blue_entry/red_entry get wiped on each launch) so commands
# survive re-staging. app.py passes this through to the bridge it launches.
export MCTF_CONTROL_FILE="/home/tyler/USV_Deploy_UI/mctf_control.json"

# Fresh-session reset: clear any leftover manual commands from a previous run.
# WHY: the control file persists across launches (so commands survive the
# entry-dir wipe mid-session), but that means a stale command -- e.g. a boat
# left on "attack" last session -- would silently carry into the new session and
# look like the boat ignoring auto mode. Deleting the file at console startup
# guarantees every boat begins on auto; both the policy and the bridge treat a
# missing file as "no overrides". Commands set during THIS session still persist
# normally (the file is recreated on the first command).
rm -f "$MCTF_CONTROL_FILE"

# Conda env (path-based, not a named env). Used only if pyquaticus
# isn't already importable.
CONDA_ENV_PATH="/home/tyler/pyquaticus/env-full"
# conda.sh provides `conda activate` in non-interactive scripts.
# Common locations; the script tries each until one exists.
CONDA_SH_CANDIDATES=(
    "/home/tyler/anaconda3/etc/profile.d/conda.sh"
    "/home/tyler/miniconda3/etc/profile.d/conda.sh"
    "$HOME/anaconda3/etc/profile.d/conda.sh"
    "$HOME/miniconda3/etc/profile.d/conda.sh"
)

URL="http://127.0.0.1:5000"

#--------------------------------------------------------------
# Args
#--------------------------------------------------------------
OPEN_BROWSER="yes"
for ARGI in "$@"; do
    case "$ARGI" in
        -h|--help)
            # Print only the top header block (stop at the first blank line
            # after the header, so section-divider comments aren't included).
            sed -n '2,/^$/p' "$0" | sed 's/^#//;s/^ //'
            exit 0 ;;
        --no-browser)
            OPEN_BROWSER="no" ;;
        ''|*[!0-9.]*)
            # Not purely digits/decimal -> not a warp value, and not a known flag.
            echo "run.sh: bad arg [$ARGI] (try --help)"
            exit 1 ;;
        *)
            # A bare number -> simulation time warp (e.g. ./run.sh 4).
            TIME_WARP="$ARGI" ;;
    esac
done

# Export the resolved warp so app.py (and the processes it launches) pick it up.
export MCTF_TIME_WARP="$TIME_WARP"

#--------------------------------------------------------------
# Helper: is pyquaticus importable by the given python?
#--------------------------------------------------------------
has_pyquaticus() {
    "$1" -c "import pyquaticus" >/dev/null 2>&1
}

#--------------------------------------------------------------
# Pick a Python that can import pyquaticus.
#   1. Try the current python3 as-is.
#   2. If that fails, source conda + activate the env, try again.
#--------------------------------------------------------------
PYTHON="python3"

if has_pyquaticus "$PYTHON"; then
    echo "run.sh: pyquaticus already available in $(command -v $PYTHON) -- skipping conda."
else
    echo "run.sh: pyquaticus not found in base python; activating conda env..."

    # Find and source conda.sh so `conda activate` works here.
    CONDA_SH=""
    for cand in "${CONDA_SH_CANDIDATES[@]}"; do
        if [ -f "$cand" ]; then CONDA_SH="$cand"; break; fi
    done
    if [ -z "$CONDA_SH" ]; then
        echo "run.sh: ERROR -- could not find conda.sh. Edit CONDA_SH_CANDIDATES."
        echo "        (Look for it under your anaconda/miniconda install:"
        echo "         find ~ -name conda.sh -path '*profile.d*' 2>/dev/null )"
        exit 1
    fi
    # shellcheck disable=SC1090
    source "$CONDA_SH"

    if [ ! -d "$CONDA_ENV_PATH" ]; then
        echo "run.sh: ERROR -- conda env path not found: $CONDA_ENV_PATH"
        exit 1
    fi
    conda activate "$CONDA_ENV_PATH" || {
        echo "run.sh: ERROR -- failed to activate $CONDA_ENV_PATH"
        exit 1
    }

    # Use the env's python explicitly.
    PYTHON="python3"
    if ! has_pyquaticus "$PYTHON"; then
        echo "run.sh: ERROR -- pyquaticus still not importable after activating env."
        echo "        Active python: $(command -v $PYTHON)"
        echo "        Check that pyquaticus is installed in $CONDA_ENV_PATH."
        exit 1
    fi
    echo "run.sh: env active -- using $(command -v $PYTHON)"
fi

#--------------------------------------------------------------
# Open the browser shortly after the server starts.
# Backgrounded with a small delay so the page loads after Flask
# is listening. Tries common openers; silently skips if none.
#--------------------------------------------------------------
if [ "$OPEN_BROWSER" = "yes" ]; then
    (
        sleep 2
        if   command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
        elif command -v gio      >/dev/null 2>&1; then gio open "$URL"
        elif command -v firefox  >/dev/null 2>&1; then firefox "$URL"
        elif command -v google-chrome >/dev/null 2>&1; then google-chrome "$URL"
        fi
    ) >/dev/null 2>&1 &
fi

#--------------------------------------------------------------
# Launch the app.
#   - cd into the app dir so relative paths (templates, entries,
#     submission_runner.py) resolve.
#   - exec replaces this script with the Flask process, so Ctrl-C
#     goes straight to Flask and there's no leftover wrapper shell.
#--------------------------------------------------------------
cd "$APP_DIR" || { echo "run.sh: ERROR -- cannot cd to $APP_DIR"; exit 1; }

echo "run.sh: starting USV_Deploy_UI at $URL  (Ctrl-C to stop)"
echo "run.sh: MCTF_MISSION_PATH=$MCTF_MISSION_PATH"
echo "run.sh: MCTF_CONTROL_FILE=$MCTF_CONTROL_FILE"
echo "run.sh: MCTF_TIME_WARP=$MCTF_TIME_WARP  (sim time warp; 1=real time)"

# The manual-control bridge is NOT started here -- it's launched on demand from
# the console's "Launch MCTF Control" button, which spawns it as a tracked
# process (so Stop All also stops it). It inherits MCTF_CONTROL_FILE from the
# export above, so the bridge and the boats share the same fallback control file.
exec "$PYTHON" app.py
