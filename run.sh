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
#    ./run.sh                 Start the console (auto-opens browser).
#    ./run.sh --no-browser    Start without opening a browser.
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
        *)
            echo "run.sh: bad arg [$ARGI] (try --help)"
            exit 1 ;;
    esac
done

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
exec "$PYTHON" app.py
