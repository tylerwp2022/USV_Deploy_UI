# =============================================================================
# pyquaticus_moos_launcher.py  --  Drive ONE boat with a policy via MOOS-IvP
# -----------------------------------------------------------------------------
# PURPOSE
#   The per-boat driver. For a single USV it:
#     1. Launches the MOOS-IvP "surveyor" community for this vehicle (via the
#        mission's launch_surveyor.sh), giving it a role, start pose, timewarp,
#        and shore/boat IPs.
#     2. Opens a pyquaticus MOOS bridge to that community.
#     3. Runs the control loop: read observation -> ask the entry's policy for
#        an action -> step the bridge (which publishes the action to MOOS).
#
# WHERE THE POLICY COMES FROM
#   submission_runner.py has already unzipped the entry into ./blue_entry/ or
#   ./red_entry/. We import solution() from the matching package below.
#
# AGENT-ID NAMESPACES  (important, easy to break)
#   Two naming schemes coexist:
#     - boat_id   : 'blue_one', 'red_two', ...  (MOOS/UI side)
#     - agent_id  : 'agent_0' ... 'agent_5'     (policy side)
#   agent_id_mapping translates boat_id -> agent_id. The policy (solution.py)
#   expects EVERYTHING keyed by agent_id: the per-agent obs dicts AND the
#   tuple keys inside global_state, e.g. (('agent_0','pos')). Below we remap
#   both before calling compute_action. If you change one mapping you must
#   change all of them or the policy will KeyError or read the wrong boat.
#
# ACTION SPACE  (the bug that bit us)
#   The bundled heuristic policy returns CONTINUOUS actions: [speed, heading]
#   pairs such as [3.0, -45] (see heuristic_policy.bearing_to_action). The
#   bridge must therefore be created with action_space='continuous'. It was
#   previously 'discrete', which expects a single integer menu index (0..16
#   from gen_config.ACTION_MAP); feeding it a [speed, heading] list silently
#   misbehaves. If you swap in a policy that emits discrete ints, change this
#   back to 'discrete' to match. >>> This setting MUST match what the policy
#   emits AND what your WestPoint2026 bridge supports. <<<
#
# PATHS
#   mission_path is read from the MCTF_MISSION_PATH env var, defaulting to the
#   thesis mission. The surveyor log path is derived from the same base so the
#   two can't drift (the old hardcoded /home/moos/logs disagreed with a
#   /home/tyler mission path and would fail to write).
# =============================================================================

import argparse
import os
import time
import subprocess

from pyquaticus.moos_bridge.pyquaticus_moos_bridge_ext import PyQuaticusMoosBridgeFullObs
from pyquaticus.moos_bridge.pyquaticus_moos_bridge import PyQuaticusMoosBridge
from pyquaticus.moos_bridge.config import FieldReaderConfig, pyquaticus_config_std, WestPoint2026

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# Mission directory containing the surveyor/ launch scripts. Override per machine
# with:  export MCTF_MISSION_PATH=/path/to/moos-ivp-mctf/missions/<mission>
DEFAULT_MISSION_PATH = "/home/tyler/moos-ivp-mctf/missions/tyler_thesis"
mission_path = os.environ.get("MCTF_MISSION_PATH", DEFAULT_MISSION_PATH)

# Surveyor logs live under the mission so the path always matches the mission
# owner (fixes the old /home/moos vs /home/tyler mismatch). Override with
# MCTF_LOG_PATH if you keep logs elsewhere.
log_path = os.environ.get("MCTF_LOG_PATH", os.path.join(mission_path, "logs"))

# boat_id (MOOS/UI side) -> agent_id (policy side). See AGENT-ID NAMESPACES.
agent_id_mapping = {
    'blue_one': 'agent_0', 'blue_two': 'agent_1', 'blue_three': 'agent_2',
    'red_one':  'agent_3', 'red_two':  'agent_4', 'red_three':  'agent_5',
}

# Boat ordinal word -> number, used to build the surveyor "-b1 / -r2" flag.
text_to_num = {'one': 1, 'two': 2, 'three': 3}

# Default MOOSDB ports per boat (used to enumerate the roster; the actual port
# for THIS boat comes from --boat_port).
boat_ports = {
    'blue_one': 9015, 'blue_two': 9016, 'blue_three': 9017,
    'red_one':  9011, 'red_two':  9012, 'red_three':  9013,
}

# Physical boat letter -> IP, for reference when running on hardware.
boat_ips = {
    's': "192.168.1.12", 't': "192.168.1.22", 'u': "192.168.1.32",
    'v': "192.168.1.42", 'w': "192.168.1.52", 'x': "192.168.1.62",
    'y': "192.168.1.72", 'z': "192.168.1.82", 'p': "192.168.1.92",
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deploy the MCTF2026 policies on USVs via MOOS-IvP")
    parser.add_argument('--sim', action='store_true',
                        help="Run in simulation rather than on hardware.")
    parser.add_argument('--color', required=True, choices=['red', 'blue'],
                        help="Which team is the trained/controlled agent.")
    parser.add_argument('--boat_id', required=True,
                        choices=["blue_one", "blue_two", "blue_three",
                                 "red_one", "red_two", "red_three"],
                        help="Logical boat id within the team.")
    parser.add_argument('--boat_name', required=False,
                        choices=['s', 't', 'u', 'v', 'w', 'x', 'y', 'z', 'p', 'q', 'r'],
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

    # Import the team's policy. submission_runner.py already extracted it into
    # ./<color>_entry/, so the package import resolves at this point.
    if args.color == "blue":
        from blue_entry.solution import solution
    else:
        from red_entry.solution import solution

    # In sim everything is local; on hardware we talk to the real shoreside IP.
    if args.sim:
        print("Simulation mode")
        server = "localhost"
    else:
        server = args.shore_ip

    # ---- Build the roster the bridge needs -------------------------------
    # teammates: same-color boats other than this one (cap at num_players-1).
    # opponents: the other color's boats (cap at num_players).
    boat_ids = list(boat_ports.keys())
    num_players = 3
    teammates = [b for b in boat_ids
                 if b.startswith(args.color) and b != args.boat_id][:num_players - 1]
    opponents = [b for b in boat_ids
                 if not b.startswith(args.color)][:num_players]
    all_agent_names = list(boat_ids)

    print(f"All Players: {all_agent_names}")
    print(f"Teammates: {teammates}")
    print(f"Opponents: {opponents}")
    print(f"Connecting to {server}:{boat_ports[args.boat_id]}")
    print(f"Boat id: {args.boat_id}")
    print(f"Boat name: {args.boat_name}")
    print(f"Num players: {num_players}")

    # ---- Launch the MOOS surveyor community for this boat ----------------
    surveyor_path = mission_path + '/surveyor'
    print("Surveyor PATH:", surveyor_path)

    # Assemble the launch_surveyor.sh flags. The vehicle is identified two ways:
    #   -v<letter>        the physical/sim vehicle name (e.g. -vu)
    #   -<b|r><ordinal>   team color + boat number   (e.g. -b1 for blue_one)
    boat_color = 'b' if args.color == 'blue' else 'r'
    boat_ordinal = text_to_num[args.boat_id.split("_")[1]]  # 'blue_one' -> 1
    cmd = ['./launch_surveyor.sh', f'-v{args.boat_name}', f'-{boat_color}{boat_ordinal}']
    if args.sim:
        cmd.append('--sim')
    cmd.append(f'{args.timewarp}')
    cmd.append(f'--logpath={log_path}')
    cmd.append('--start=100,81.49,21.3')   # x,y,heading spawn pose (mission frame)
    cmd.append('--role=CONTROL')           # this process drives the boat externally
    cmd.append('--auto')                   # non-interactive launch
    cmd.append(f'--shore={args.shore_ip}')
    cmd.append(f'--ip={args.boat_ip}')
    print("Final Launch Command:", cmd)

    # Output is suppressed so the surveyor's own logging doesn't flood this
    # terminal; surveyor writes its logs under --logpath.
    res = subprocess.run(cmd, cwd=surveyor_path,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Return From Launch Surveyor Command:", res)

    time.sleep(3)  # Give the surveyor a moment to come up before bridging.

    # ---- Open the pyquaticus MOOS bridge ---------------------------------
    print("Launching Bridge")
    # action_space='continuous' MUST match the policy's output. See the
    # ACTION SPACE note in the file header before changing this.
    env = PyQuaticusMoosBridgeFullObs(
        args.boat_ip, args.boat_id, args.boat_port,
        teammates, opponents, all_agent_names,
        WestPoint2026(mission_path),
        timewarp=args.timewarp,
        action_space='continuous',
        quiet=False,
    )
    
    os.environ["MCTF_BOAT_ID"] = args.boat_id   # e.g. "blue_one" — tells the entry its own name
    os.environ["MCTF_MOOS_PORT"] = str(args.boat_port)      # ADD THIS

    # ---- Control loop ----------------------------------------------------
    # Instantiate the entry's policy. compute_action() takes:
    #   agent_id (policy-side), normalized obs, unnormalized obs, global_state
    # and returns a [speed, heading] action (continuous).
    sol = solution()
    try:
        agent_id = args.boat_id
        env.normalize = True
        obs_norm, info = env.reset()

        while True:
            # Remap every key from boat_id-space into agent_id-space, because
            # the policy is written entirely in terms of agent_0..agent_5.
            final_obs_norm = {agent_id_mapping[k]: obs_norm[k] for k in obs_norm}
            final_obs_unnorm = {agent_id_mapping[k]: info['unnorm_obs'][k]
                                for k in info['unnorm_obs']}

            # global_state has a mix of key shapes: per-agent entries use a
            # (boat_id, field) tuple and must be remapped to (agent_id, field);
            # scalar/global entries are plain keys and pass through unchanged.
            global_state = info['global_state']
            new_glob_state = {}
            for k in global_state:
                if isinstance(k, tuple):
                    new_glob_state[(agent_id_mapping[k[0]], k[1])] = global_state[k]
                else:
                    new_glob_state[k] = global_state[k]

            # Ask the policy what THIS boat should do, then push it to MOOS.
            action = sol.compute_action(
                agent_id_mapping[agent_id],
                final_obs_norm, final_obs_unnorm, new_glob_state)
            obs_norm, _, _, _, info = env.step(action)

    finally:
        # Always close the bridge cleanly (e.g. on Ctrl-C) so MOOS connections
        # are released rather than left dangling.
        print("Interrupted / loop ended -- closing bridge")
        env.close()
