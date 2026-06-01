#!/usr/bin/env python3
"""
Param Studio — a modern, offline rqt_reconfigure replacement.

Reads any running node's ROS 2 parameters (descriptors → sliders/toggles),
lets you search and tune them, and — the point of the whole thing — gives a
clear per-set verdict by *reading the value back* after every write. Built for
tuning the MPPI controller over a flaky network at ICRA, where a silently
dropped `set` is dangerous.

Mirrors the team's raceline_studio pattern: Flask + an rclpy node spun in a
daemon thread, all browser assets vendored locally (no internet), launched in a
dedicated Firefox window.

Usage:
    ros2 run param_studio studio
    → opens a Firefox window at http://localhost:5060
"""
import os
import sys
import time
import shutil
import datetime
import threading
import subprocess
import webbrowser
from pathlib import Path

import yaml
from flask import Flask, render_template, jsonify, request

import rclpy
from rclpy.node import Node
from rcl_interfaces.srv import (
    ListParameters, DescribeParameters, GetParameters, SetParameters,
)
from rcl_interfaces.msg import Parameter, ParameterValue, ParameterType

# ── Config ──────────────────────────────────────────────────────────────────
# Default 5077: avoids 5050 (raceline_studio) and 5060 (registered SIP port,
# which some desktops grab). Override with PARAM_STUDIO_PORT.
PORT = int(os.environ.get("PARAM_STUDIO_PORT", "5077"))
# Where snapshot/profile YAMLs live. Defaults to the MPPI config folder so
# snapshots sit next to — and can be restored from / diffed against — the real
# params_*.yaml race configs. Override with PARAM_STUDIO_SNAPSHOT_DIR.
DEFAULT_SNAPSHOT_DIR = os.path.expanduser(
    "~/ros2_ws/roboracer_ws/src/mppi/mppi_bringup/config")
SNAPSHOT_DIR = Path(os.environ.get("PARAM_STUDIO_SNAPSHOT_DIR", DEFAULT_SNAPSHOT_DIR))

# Per-call service timeouts (seconds). Tuned so a dead/laggy node yields a clean
# verdict instead of hanging the UI — important on a bad ICRA link.
T_SET = 2.0
T_GET = 2.0
T_LIST = 3.0
T_PING = 1.5

app = Flask(__name__)

# ParameterValue.type codes → short names used by the UI, and back.
PT = ParameterType
TYPE_NAME = {
    PT.PARAMETER_NOT_SET: "not_set",
    PT.PARAMETER_BOOL: "bool",
    PT.PARAMETER_INTEGER: "integer",
    PT.PARAMETER_DOUBLE: "double",
    PT.PARAMETER_STRING: "string",
    PT.PARAMETER_BYTE_ARRAY: "byte_array",
    PT.PARAMETER_BOOL_ARRAY: "bool_array",
    PT.PARAMETER_INTEGER_ARRAY: "integer_array",
    PT.PARAMETER_DOUBLE_ARRAY: "double_array",
    PT.PARAMETER_STRING_ARRAY: "string_array",
}
NAME_TYPE = {v: k for k, v in TYPE_NAME.items()}


def _pv_to_py(pv):
    """rcl_interfaces ParameterValue → native Python value."""
    t = pv.type
    if t == PT.PARAMETER_BOOL:
        return bool(pv.bool_value)
    if t == PT.PARAMETER_INTEGER:
        return int(pv.integer_value)
    if t == PT.PARAMETER_DOUBLE:
        return float(pv.double_value)
    if t == PT.PARAMETER_STRING:
        return pv.string_value
    if t == PT.PARAMETER_BOOL_ARRAY:
        return list(pv.bool_array_value)
    if t == PT.PARAMETER_INTEGER_ARRAY:
        return [int(x) for x in pv.integer_array_value]
    if t == PT.PARAMETER_DOUBLE_ARRAY:
        return [float(x) for x in pv.double_array_value]
    if t == PT.PARAMETER_STRING_ARRAY:
        return list(pv.string_array_value)
    return None


def _py_to_pv(value, ptype):
    """Native Python value + type code → ParameterValue for a set request."""
    pv = ParameterValue()
    pv.type = ptype
    if ptype == PT.PARAMETER_BOOL:
        pv.bool_value = bool(value)
    elif ptype == PT.PARAMETER_INTEGER:
        pv.integer_value = int(value)
    elif ptype == PT.PARAMETER_DOUBLE:
        pv.double_value = float(value)
    elif ptype == PT.PARAMETER_STRING:
        pv.string_value = str(value)
    else:
        raise ValueError(f"unsupported type for set: {TYPE_NAME.get(ptype, ptype)}")
    return pv


def _values_equal(a, b, ptype):
    """Read-back comparison. Doubles use a tolerance; everything else is exact."""
    if ptype == PT.PARAMETER_DOUBLE:
        try:
            return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(b))) + 1e-9
        except (TypeError, ValueError):
            return False
    if ptype == PT.PARAMETER_INTEGER:
        try:
            return int(a) == int(b)
        except (TypeError, ValueError):
            return False
    if ptype == PT.PARAMETER_BOOL:
        return bool(a) == bool(b)
    return a == b


MISC = "misc"

# Curated grouping for the MPPI controller. Ordered to mirror the layout of the
# params_*.yaml configs (mppi_bringup/config). Each param is placed by what it
# actually does. Anything NOT listed here falls through to "misc" — that's the
# bucket for params we don't actively tune: disabled-feature families
# (slip / lat-acc / steer-sat costs, speed-profile drive feedforward) and knobs
# that never appear in the yaml configs. To start tuning one of those, just move
# its name up into the relevant group below.
MPPI_GROUPS = [
    ("Startup (restart to apply)", [
        "is_sim", "wpt_path_absolute", "wpt_path", "map_dir", "map_ind",
        "state_predictor", "n_samples", "n_steps", "sim_time_step",
        "random_seed", "render"]),
    ("MPPI solver", [
        "temperature", "init_vel", "startup_speed", "friction", "n_iterations"]),
    ("Exploration", [
        "control_sample_std_steer", "control_sample_std_accel",
        "steer_vel_scale", "accel_scale"]),
    ("Reward weights", [
        "xy_reward_weight", "velocity_reward_weight", "yaw_reward_weight"]),
    ("Speed profile", [
        "use_waypoint_speed_profile", "speed_profile_scale",
        "speed_profile_min_speed", "speed_profile_max_speed",
        "speed_profile_lookahead_steps", "speed_profile_iterations"]),
    ("Wall cost", [
        "wall_cost_enabled", "wall_cost_weight", "wall_cost_margin",
        "wall_cost_power", "wall_cost_map_yaml"]),
    ("Opponent", [
        "opponent_path_topic", "opponent_cost_enabled", "opponent_cost_weight",
        "opponent_cost_radius", "opponent_cost_power", "opponent_cost_discount",
        "opponent_path_timeout", "opponent_behavior_mode",
        "opponent_follow_weight", "opponent_follow_distance",
        "opponent_same_lane_width",
        "opponent_auto_exit_closing_speed", "opponent_auto_exit_wall_clearance",
        "opponent_auto_min_commit_sec", "opponent_auto_pass_cooldown_sec"]),
    ("Opponent passing", [
        "opponent_pass_weight", "opponent_pass_lateral_offset",
        "opponent_pass_longitudinal_window", "opponent_auto_wall_check_enabled",
        "opponent_auto_min_wall_clearance", "opponent_auto_check_steps",
        "opponent_auto_min_closing_speed", "opponent_auto_max_ahead_distance",
        "opponent_auto_side_switch_margin"]),
    ("State estimator", [
        "use_pose_delta_state_estimate", "state_est_vy_prior",
        "state_est_wz_prior", "state_est_hiccup_dt",
        "state_est_hiccup_prior_scale"]),
    ("MPPI guard", [
        "mppi_guard_on_timing_jump", "mppi_guard_wall_gap",
        "mppi_guard_stamp_gap", "mppi_guard_aopt_threshold",
        "mppi_guard_saturation_callbacks",
        "mppi_guard_bad_callbacks_to_clear_control"]),
    ("Output limits", [
        "min_speed", "max_speed", "max_steering_angle"]),
    ("Visualization", [
        "publish_markers", "marker_frame_id", "reference_line_width",
        "optimal_line_width", "sampled_line_width", "sampled_trajectory_count",
        "sampled_trajectory_alpha", "viz_publish_rate_hz"]),
    ("Control & runtime", [
        "control_loop_hz", "control_trigger_mode", "control_watchdog_hz",
        "control_watchdog_max_silence_sec", "control_pose_stale_sec",
        "live_tuning_enabled"]),
]
_NAME_TO_GROUP = {n: label for label, names in MPPI_GROUPS for n in names}
_MPPI_GROUP_ORDER = [label for label, _ in MPPI_GROUPS]


def _generic_group(name, description):
    """Fallback for non-MPPI nodes: a leading [tag] in the description if
    present, else the name's first underscore-delimited token."""
    desc = (description or "").strip()
    if desc.startswith("["):
        end = desc.find("]")
        if end > 1:
            return desc[1:end].strip()
    head = name.split("_", 1)[0]
    return head if head else "general"


def _assign_groups(items):
    """Assign each param a display group and return (groups_in_display_order).

    For an MPPI node (recognized by curated names being present) unknown params
    go to 'misc'. For any other node we group generically and then fold
    single-param groups into 'misc' so the UI never shows lonely one-row groups.
    'misc' always sorts last."""
    is_mppi = any(it["name"] in _NAME_TO_GROUP for it in items)
    if is_mppi:
        for it in items:
            it["group"] = _NAME_TO_GROUP.get(it["name"], MISC)
        present = [g for g in _MPPI_GROUP_ORDER if any(it["group"] == g for it in items)]
    else:
        for it in items:
            it["group"] = _generic_group(it["name"], it["description"])
        counts = {}
        for it in items:
            counts[it["group"]] = counts.get(it["group"], 0) + 1
        for it in items:
            if counts[it["group"]] < 2:
                it["group"] = MISC
        present = sorted({it["group"] for it in items if it["group"] != MISC})
    if any(it["group"] == MISC for it in items):
        present.append(MISC)
    return present


# ── ROS 2 node: raw parameter service clients, cached per target node ─────────
class StudioNode(Node):
    def __init__(self):
        super().__init__("param_studio")
        self._lock = threading.Lock()
        # NB: do not name this `_clients` — rclpy.node.Node already owns that as
        # its internal list and create_client() appends to it.
        self._client_cache = {}  # fqn -> {'list','describe','get','set': client}

    # -- low-level plumbing ---------------------------------------------------
    def clients_for(self, fqn):
        with self._lock:
            c = self._client_cache.get(fqn)
            if c is None:
                c = {
                    "list": self.create_client(ListParameters, f"{fqn}/list_parameters"),
                    "describe": self.create_client(DescribeParameters, f"{fqn}/describe_parameters"),
                    "get": self.create_client(GetParameters, f"{fqn}/get_parameters"),
                    "set": self.create_client(SetParameters, f"{fqn}/set_parameters"),
                }
                self._client_cache[fqn] = c
            return c

    def _call(self, client, req, timeout):
        """call_async + poll the future with a timeout (rclpy spins in another
        thread). Returns the response, or None on timeout/unavailable."""
        if not client.service_is_ready():
            if not client.wait_for_service(timeout_sec=min(timeout, 1.0)):
                return None
        fut = client.call_async(req)
        start = time.time()
        while not fut.done() and time.time() - start < timeout:
            time.sleep(0.01)
        if not fut.done():
            return None
        return fut.result()

    # -- node discovery -------------------------------------------------------
    def param_nodes(self):
        """FQNs of nodes that advertise the parameter services (excluding self)."""
        services = dict(self.get_service_names_and_types())
        out = []
        for name, ns in self.get_node_names_and_namespaces():
            fqn = ("" if ns == "/" else ns.rstrip("/")) + "/" + name
            if name == "param_studio":
                continue
            if f"{fqn}/list_parameters" in services:
                out.append(fqn)
        return sorted(out)

    # -- parameter reads ------------------------------------------------------
    def list_names(self, fqn, timeout=T_LIST):
        c = self.clients_for(fqn)
        req = ListParameters.Request()
        req.depth = 0  # recursive: every parameter
        resp = self._call(c["list"], req, timeout)
        if resp is None:
            return None
        return sorted(resp.result.names)

    def describe(self, fqn, names, timeout=T_LIST):
        c = self.clients_for(fqn)
        req = DescribeParameters.Request()
        req.names = names
        resp = self._call(c["describe"], req, timeout)
        return None if resp is None else list(resp.descriptors)

    def get_values(self, fqn, names, timeout=T_GET):
        c = self.clients_for(fqn)
        req = GetParameters.Request()
        req.names = names
        resp = self._call(c["get"], req, timeout)
        if resp is None:
            return None
        return [_pv_to_py(v) for v in resp.values]

    def snapshot_params(self, fqn):
        """{name: value} for every parameter on the node (for snapshot/diff)."""
        names = self.list_names(fqn)
        if names is None:
            return None
        vals = self.get_values(fqn, names)
        if vals is None:
            return None
        return dict(zip(names, vals))

    def full_params(self, fqn):
        """(params, groups_order): rich param dicts plus the display order of
        their groups (curated for MPPI, generic otherwise, 'misc' last)."""
        names = self.list_names(fqn)
        if names is None:
            return None, None
        descs = self.describe(fqn, names)
        vals = self.get_values(fqn, names)
        if descs is None or vals is None:
            return None, None
        out = []
        for name, d, val in zip(names, descs, vals):
            tname = TYPE_NAME.get(d.type, "not_set")
            rng = None
            if d.floating_point_range:
                r = d.floating_point_range[0]
                rng = {"lo": r.from_value, "hi": r.to_value, "step": r.step}
            elif d.integer_range:
                r = d.integer_range[0]
                rng = {"lo": r.from_value, "hi": r.to_value, "step": r.step}
            out.append({
                "name": name,
                "type": tname,
                "value": val,
                "read_only": bool(d.read_only),
                "description": d.description,
                "range": rng,
            })
        groups_order = _assign_groups(out)
        return out, groups_order

    # -- the core: set then read back ----------------------------------------
    def set_and_readback(self, fqn, name, ptype, value, timeout=T_SET):
        c = self.clients_for(fqn)
        t0 = time.time()
        req = SetParameters.Request()
        p = Parameter()
        p.name = name
        p.value = _py_to_pv(value, ptype)
        req.parameters = [p]
        sresp = self._call(c["set"], req, timeout)
        if sresp is None:
            return {"set_ok": False, "timed_out": True, "reason": "no response from node",
                    "readback": None, "applied": False, "rtt_ms": round((time.time() - t0) * 1000)}
        res = sresp.results[0] if sresp.results else None
        set_ok = bool(res.successful) if res else False
        reason = (res.reason if res else "") or ""

        gresp = self.get_values(fqn, [name], timeout=timeout)
        rtt = round((time.time() - t0) * 1000)
        if gresp is None or not gresp:
            return {"set_ok": set_ok, "timed_out": True, "reason": reason or "no read-back",
                    "readback": None, "applied": False, "rtt_ms": rtt}
        rb = gresp[0]
        return {"set_ok": set_ok, "timed_out": False, "reason": reason,
                "readback": rb, "applied": _values_equal(rb, value, ptype), "rtt_ms": rtt}


NODE = None  # type: StudioNode | None


def _err(msg, code=400):
    return jsonify({"error": msg}), code


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/nodes")
def api_nodes():
    nodes = NODE.param_nodes()
    # Float a node named '/mppi' (or any *mppi*) to the top — it's the target.
    nodes.sort(key=lambda n: (0 if "mppi" in n.lower() else 1, n))
    return jsonify({"nodes": nodes})


@app.route("/api/params")
def api_params():
    fqn = request.args.get("node", "")
    if not fqn:
        return _err("node required")
    params, groups_order = NODE.full_params(fqn)
    if params is None:
        return _err(f"{fqn} did not respond (parameter services unavailable)", 503)
    return jsonify({"node": fqn, "params": params, "groups_order": groups_order})


@app.route("/api/ping")
def api_ping():
    """Cheap round-trip for the health badge: an empty get_parameters call."""
    fqn = request.args.get("node", "")
    if not fqn:
        return _err("node required")
    c = NODE.clients_for(fqn)
    t0 = time.time()
    req = GetParameters.Request()
    req.names = []
    resp = NODE._call(c["get"], req, T_PING)
    rtt = round((time.time() - t0) * 1000)
    return jsonify({"reachable": resp is not None, "rtt_ms": rtt})


@app.route("/api/set", methods=["POST"])
def api_set():
    body = request.json or {}
    fqn = body.get("node", "")
    name = body.get("name", "")
    tname = body.get("type", "")
    value = body.get("value")
    if not fqn or not name:
        return _err("node and name required")
    ptype = NAME_TYPE.get(tname)
    if ptype is None or tname not in ("bool", "integer", "double", "string"):
        return _err(f"unsupported type: {tname!r}")
    try:
        verdict = NODE.set_and_readback(fqn, name, ptype, value)
    except Exception as e:  # bad cast etc.
        return _err(str(e))
    return jsonify(verdict)


@app.route("/api/snapshots")
def api_snapshots():
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted((f.name for f in SNAPSHOT_DIR.glob("*.yaml")), reverse=True)
    return jsonify({"dir": str(SNAPSHOT_DIR), "files": files})


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    body = request.json or {}
    fqn = body.get("node", "")
    if not fqn:
        return _err("node required")
    params = NODE.snapshot_params(fqn)
    if params is None:
        return _err(f"{fqn} did not respond", 503)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # 'snapshot_' prefix keeps generated files visually distinct from the
    # curated params_*.yaml race configs in the same folder.
    fname = f"snapshot_{fqn.strip('/').replace('/', '_')}_{stamp}.yaml"
    doc = {fqn: {"ros__parameters": params}}
    (SNAPSHOT_DIR / fname).write_text(yaml.safe_dump(doc, default_flow_style=False, sort_keys=True))
    return jsonify({"ok": True, "file": fname, "path": str(SNAPSHOT_DIR / fname), "count": len(params)})


def _parse_profile(text):
    """YAML text → {name: value}. Tolerates any top-level node key (e.g.
    'lmppi_node:' / 'mppi_node:') and also a flat {name: value} map."""
    doc = yaml.safe_load(text) or {}
    for top in doc.values():
        if isinstance(top, dict) and "ros__parameters" in top:
            return dict(top["ros__parameters"])
    return {k: v for k, v in doc.items() if not isinstance(v, dict)}


def _load_profile(fname):
    """Read a snapshot/config YAML from SNAPSHOT_DIR → {name: value}."""
    path = SNAPSHOT_DIR / fname
    if not path.is_file():
        raise FileNotFoundError(fname)
    return _parse_profile(path.read_text())


def _describe_map(fqn):
    """{name: ParameterDescriptor} for the node's OWN declared params.

    We must describe the node's own names, not arbitrary profile names: rclpy's
    DescribeParameters returns an EMPTY list if any requested name is undeclared
    (and a snapshot/config YAML routinely contains params a given node doesn't
    have). Looking profile names up in this map cleanly identifies which ones the
    node actually declares."""
    names = NODE.list_names(fqn)
    if not names:
        return {}
    descs = NODE.describe(fqn, names) or []
    return {n: d for n, d in zip(names, descs)}


def _profile_from_body(body):
    """Resolve a profile from a request body, which carries EITHER a `file`
    (a name in SNAPSHOT_DIR, from the list) OR raw `content` (a YAML string the
    user browsed to via the OS file dialog). Returns (profile_dict, label)."""
    if body.get("content") is not None:
        return _parse_profile(body["content"]), (body.get("filename") or "(browsed file)")
    fname = body.get("file", "")
    if not fname:
        raise ValueError("file or content required")
    return _load_profile(fname), fname


@app.route("/api/diff", methods=["POST"])
def api_diff():
    body = request.json or {}
    fqn = body.get("node", "")
    if not fqn:
        return _err("node required")
    try:
        saved, label = _profile_from_body(body)
    except FileNotFoundError as e:
        return _err(f"no such profile: {e}", 404)
    except (ValueError, yaml.YAMLError) as e:
        return _err(f"could not read profile: {e}")
    descs = _describe_map(fqn)
    current = NODE.snapshot_params(fqn)
    if current is None:
        return _err(f"{fqn} did not respond", 503)
    rows = []
    for name, sval in saved.items():
        cval = current.get(name)
        d = descs.get(name)
        ptype = d.type if d else PT.PARAMETER_NOT_SET
        differ = (name not in current) or (not _values_equal(cval, sval, ptype))
        rows.append({"name": name, "saved": sval, "current": cval, "differ": differ,
                     "type": TYPE_NAME.get(ptype, "not_set"),
                     "read_only": bool(d.read_only) if d else True,
                     "missing": name not in current})
    rows.sort(key=lambda r: (not r["differ"], r["name"]))
    return jsonify({"node": fqn, "profile": label, "rows": rows})


@app.route("/api/restore", methods=["POST"])
def api_restore():
    body = request.json or {}
    fqn = body.get("node", "")
    if not fqn:
        return _err("node required")
    try:
        saved, label = _profile_from_body(body)
    except FileNotFoundError as e:
        return _err(f"no such profile: {e}", 404)
    except (ValueError, yaml.YAMLError) as e:
        return _err(f"could not read profile: {e}")
    descs = _describe_map(fqn)
    results = []
    applied = failed = skipped = 0
    for name, val in saved.items():
        d = descs.get(name)
        if d is None:
            results.append({"name": name, "status": "skipped", "reason": "not declared on node"})
            skipped += 1
            continue
        tname = TYPE_NAME.get(d.type, "not_set")
        if d.read_only:
            results.append({"name": name, "status": "skipped", "reason": "read-only"})
            skipped += 1
            continue
        if tname not in ("bool", "integer", "double", "string"):
            results.append({"name": name, "status": "skipped", "reason": f"unsupported type {tname}"})
            skipped += 1
            continue
        try:
            v = NODE.set_and_readback(fqn, name, d.type, val)
        except Exception as e:
            results.append({"name": name, "status": "failed", "reason": str(e)})
            failed += 1
            continue
        ok = v["set_ok"] and v["applied"] and not v["timed_out"]
        results.append({"name": name, "status": "applied" if ok else "failed",
                        "reason": v["reason"], "readback": v["readback"], "rtt_ms": v["rtt_ms"]})
        applied += ok
        failed += (not ok)
    return jsonify({"node": fqn, "profile": label, "applied": applied, "failed": failed,
                    "skipped": skipped, "results": results})


# ── Browser launch ────────────────────────────────────────────────────────────
def open_browser(url):
    """Open the URL in a new Firefox tab (reusing the running browser); fall
    back to the default browser."""
    ff = shutil.which("firefox")
    if ff:
        try:
            subprocess.Popen([ff, "--new-tab", url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            pass
    try:
        webbrowser.get("firefox").open_new_tab(url)
        return
    except Exception:
        pass
    webbrowser.open(url, new=2)  # new=2 → new tab if possible


def main():
    global NODE
    rclpy.init()
    NODE = StudioNode()
    threading.Thread(target=rclpy.spin, args=(NODE,), daemon=True).start()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    url = f"http://localhost:{PORT}"
    print(f"\n  Param Studio → {url}")
    print(f"  Snapshots    → {SNAPSHOT_DIR}\n")
    try:
        if os.environ.get("PARAM_STUDIO_NO_BROWSER") != "1":
            threading.Timer(0.4, lambda: open_browser(url)).start()
        app.run(host="0.0.0.0", port=PORT, use_reloader=False)
    finally:
        NODE.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
