# =============================================================================
# submission_runner.py  --  Unpack one entry and start its boat driver
# -----------------------------------------------------------------------------
# PURPOSE
#   The middle layer between the Flask UI (app.py) and the per-boat driver
#   (pyquaticus_moos_launcher.py). For a single boat it:
#     1. Unzips the selected entry into ./blue_entry/ or ./red_entry/ depending
#        on team color (this is where solution.py + heuristic_policy.py land).
#     2. Invokes pyquaticus_moos_launcher.py with the boat's parameters, which
#        launches the MOOS surveyor and runs the policy control loop.
#
# WHY A SEPARATE PROCESS
#   Each boat gets its own runner invocation (one gnome-terminal per boat from
#   app.py). Keeping unzip + launch together here means the launcher can always
#   assume the entry is already extracted next to it.
#
# HISTORY
#   The commented-out `docker` blocks are from an earlier design where each
#   entry ran in an isolated container (image jkliem/wp25:v2). That has been
#   replaced by local unzip + direct execution. The dead code is intentionally
#   retained for reference until the container path is formally retired.
# =============================================================================

import subprocess
import argparse
import sys


def run_game(entry_path, sim, color, boat_id, boat_name,
             timewarp, shore_ip, boat_ip, boat_port):
    """Unzip `entry_path` for the given team color, then run the launcher.

    The unzip target is chosen by color: blue -> ./blue_entry/, red ->
    ./red_entry/. `unzip -o` overwrites any prior extraction so a re-run always
    reflects the latest zip.

    NOTE: we deliberately do NOT import solution here. An earlier version did
    `from blue_entry.solution import solution` at this point, which (a) was
    never used in this function and (b) could crash if the unzip had not yet
    produced solution.py. The launcher imports the solution itself, after this
    extraction has completed.
    """
    # Compare exactly rather than `"b" in color` so a future color name that
    # merely contains the letter 'b' can't be misrouted to the blue branch.
    if color == 'blue':
        subprocess.run(['unzip', '-o', entry_path, '-d', './blue_entry/'])
    else:
        subprocess.run(['unzip', '-o', entry_path, '-d', './red_entry/'])

    # Build the launcher command as an explicit argv list (no shell), so values
    # are passed as discrete arguments and never re-parsed by a shell.
    #
    # Use sys.executable (the ABSOLUTE path to the interpreter running THIS
    # process) rather than the bare name 'python3'. 'python3' would be re-resolved
    # from PATH in the child, and a pyenv shim ahead of the active conda env on
    # PATH can intercept it and select the wrong interpreter -- one without
    # pyquaticus installed -> ModuleNotFoundError. sys.executable guarantees the
    # child runs in the same environment (e.g. conda env-full) as the parent.
    cmd = [sys.executable, '-u', 'pyquaticus_moos_launcher.py']
    if sim:
        cmd.append('--sim')
    cmd += [
        f'--color={color}',
        f'--boat_id={boat_id}',
        f'--boat_name={boat_name}',
        f'--timewarp={timewarp}',
        f'--shore_ip={shore_ip}',
        f'--boat_ip={boat_ip}',
        f'--boat_port={boat_port}',
    ]
    print("Launching launcher with:", ' '.join(cmd))
    # check=True surfaces a non-zero launcher exit as a CalledProcessError in
    # the watching terminal, rather than failing silently.
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deploy the MCTF2026 policies on USVs via MOOS-IvP")
    parser.add_argument('--entry_name', required=True, type=str,
                        help="Path to the entry zip to load (no leading /).")
    parser.add_argument('--sim', action='store_true',
                        help="Run in simulation rather than on hardware.")
    parser.add_argument('--color', required=True, choices=['red', 'blue'],
                        help="Which team is the trained/controlled agent.")
    parser.add_argument('--boat_id', required=True,
                        choices=["blue_one", "blue_two", "blue_three",
                                 "red_one", "red_two", "red_three"],
                        help="Logical boat id within the team.")
    parser.add_argument('--boat_name', required=False,
                        choices=['s', 't', 'u', 'v', 'w', 'x', 'y', 'z'],
                        help="Physical boat letter (surveyor vehicle name).")
    parser.add_argument('--timewarp', required=True, type=int, default=4,
                        help="MOOS time warp (sim speed multiplier).")
    parser.add_argument('--shore_ip', required=True, type=str, default='localhost',
                        help="Shoreside MOOSDB IP ('localhost' in sim).")
    parser.add_argument('--boat_ip', required=True, type=str, default='localhost',
                        help="This boat's MOOSDB IP.")
    parser.add_argument('--boat_port', required=True, type=int, default=9012,
                        help="This boat's MOOSDB port.")
    args = parser.parse_args()

    run_game(args.entry_name, args.sim, args.color, args.boat_id,
             args.boat_name, args.timewarp, args.shore_ip,
             args.boat_ip, args.boat_port)
