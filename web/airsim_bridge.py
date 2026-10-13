"""AirSim positions and chase-camera previews for the local web viewer."""

from __future__ import annotations

import base64
from collections import deque
import io
import json
import math
from pathlib import Path
import threading
import sys
import time

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from UAV.utils.airsim_client import AirSimClient as Rpc, calibrate, vector


OUTPUT_LOCK = threading.Lock()
CAMERA = "back_center"


def emit(message):
    with OUTPUT_LOCK:
        print(json.dumps(message, ensure_ascii=True, allow_nan=False), flush=True)


def quat(value):
    return [float(value[k]) for k in ("w_val", "x_val", "y_val", "z_val")]


def multiply(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return [w*v-x*i-y*j-z*k, w*i+x*v+y*k-z*j, w*j-x*k+y*v+z*i, w*k+x*j-y*i+z*v]


def inverse(q):
    norm = sum(v*v for v in q)
    return [q[0]/norm, -q[1]/norm, -q[2]/norm, -q[3]/norm]


def rotate(q, v):
    return multiply(multiply(q, [0, *v]), inverse(q))[1:]


def pose(position, orientation):
    return {"position": dict(zip(("x_val", "y_val", "z_val"), position)),
            "orientation": dict(zip(("w_val", "x_val", "y_val", "z_val"), orientation))}


def configure_camera(rpc, name):
    # Account for the built-in camera's child-component offset and rotation.
    rpc.call("simSetCameraPose", CAMERA, pose([0, 0, 0], [1, 0, 0, 0]), name, False)
    vehicle = rpc.call("simGetVehiclePose", name)
    camera = rpc.call("simGetCameraInfo", CAMERA, name, False)["pose"]
    vehicle_inverse = inverse(quat(vehicle["orientation"]))
    parent_rotation = multiply(vehicle_inverse, quat(camera["orientation"]))
    parent_offset = rotate(vehicle_inverse, [a-b for a, b in zip(vector(camera["position"]), vector(vehicle["position"]))])
    relative_position = rotate(inverse(parent_rotation), [a-b for a, b in zip([-6, 0, -4], parent_offset)])
    pitch = -math.atan2(4, 6)
    looking_at_vehicle = [math.cos(pitch/2), 0, math.sin(pitch/2), 0]
    relative_rotation = multiply(inverse(parent_rotation), looking_at_vehicle)
    rpc.call("simSetCameraPose", CAMERA, pose(relative_position, relative_rotation), name, False)
    rpc.call("simSetCameraFov", CAMERA, 80.0, name, False)


class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.records = {}
        self.epoch = threading.Event()
        self.capture = threading.Event()
        self.capture.set()
        self.camera_name = "UAV_01"

    def commands(self):
        for line in sys.stdin:
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    continue
                if isinstance(message.get('camera'), str):
                    with self.lock:
                        self.camera_name = message['camera']
                if message.get('capture') is True:
                    self.capture.set()
                elif message.get('capture') is False:
                    self.capture.clear()
            except (ValueError, TypeError):
                continue

    def camera_loop(self, epoch):
        rpc = Rpc(timeout=8)
        configured = set()
        while not epoch.is_set():
            if not self.capture.is_set():
                epoch.wait(.5)
                continue
            with self.lock:
                name = self.camera_name
                available = name in self.records
            if not available:
                epoch.wait(.25)
                continue
            started = time.monotonic()
            try:
                if name not in configured:
                    configure_camera(rpc, name)
                    configured.add(name)
                raw = rpc.call("simGetImage", CAMERA, 0, name, False)
                if isinstance(raw, str):
                    raw = raw.encode("utf-8", "surrogateescape")
                if not raw:
                    raise RuntimeError("Camera returned an empty image")
                with Image.open(io.BytesIO(bytes(raw))) as image:
                    image = image.convert("RGB")
                    image.thumbnail((960, 540))
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=80)
                    width, height = image.size
                if epoch.is_set():
                    break
                stamp = int(time.time() * 1000)
                with self.lock:
                    if name != self.camera_name or not self.capture.is_set():
                        continue
                    self.records[name].update(camera_updated_at=stamp, camera_error=None)
                emit({"type": "frame", "name": name, "updated_at": stamp, "width": width, "height": height,
                      "data": base64.b64encode(output.getvalue()).decode("ascii")})
            except Exception as error:
                configured.discard(name)
                with self.lock:
                    if name in self.records:
                        self.records[name]["camera_error"] = str(error)
                if epoch.wait(1):
                    break
            epoch.wait(max(0, .35 - (time.monotonic() - started)))
        rpc.close()

    def run(self):
        threading.Thread(target=self.commands, daemon=True).start()
        rpc = Rpc()
        while True:
            anchor = None
            coordinate_error = None
            try:
                if not rpc.call("ping"):
                    raise ConnectionError("AirSim did not answer ping")
                settings = json.loads(rpc.call("getSettingsString"))
                vehicles = rpc.call("listVehicles")
                configured = settings.get("Vehicles", {})
                names = [name for name in vehicles if configured.get(name, {}).get("VehicleType", "SimpleFlight") in ("SimpleFlight", "PX4Multirotor")][:5]
                if not names:
                    raise RuntimeError("No multirotor vehicles are available")
                self.epoch.set()
                self.epoch = threading.Event()
                session_id = str(time.time_ns())
                trails = {name: deque(maxlen=2000) for name in names}
                with self.lock:
                    self.records = {name: {"name": name, "position_m": None, "updated_at": 0,
                        "camera_updated_at": 0, "camera_error": None} for name in names}
                    if self.camera_name not in self.records:
                        self.camera_name = names[0]
                emit({"type": "reset"})
                threading.Thread(target=self.camera_loop, args=(self.epoch,), daemon=True).start()
                next_calibration = 0
                while True:
                    started = time.monotonic()
                    if anchor is None and started >= next_calibration:
                        try:
                            anchor = calibrate(rpc)
                            coordinate_error = None
                        except Exception as error:
                            coordinate_error = str(error)
                            next_calibration = time.monotonic() + 5
                    if anchor is not None:
                        reference = vector(rpc.call("simGetObjectPose", anchor["actor"])["position"])
                        scale = anchor["scale"]
                        world_offset = [a-b*scale for a, b in zip(anchor["position_m"], [reference[0], reference[1], -reference[2]])]
                    for name in names:
                        current = rpc.call("simGetObjectPose", name)
                        ned = vector(current["position"])
                        q = quat(current["orientation"])
                        position = [a+b*scale for a, b in zip(world_offset, [ned[0], ned[1], -ned[2]])] if anchor else None
                        stamp = int(time.time()*1000)
                        trail = trails[name]
                        while trail and stamp - trail[0][0] > 300000:
                            trail.popleft()
                        if position is not None and (not trail or math.hypot(position[0]-trail[-1][1], position[1]-trail[-1][2]) >= .25):
                            trail.append([stamp, position[0], position[1]])
                        with self.lock:
                            self.records[name].update(position_m=position, updated_at=stamp, trail=list(trail),
                                yaw_rad=math.atan2(2*(q[0]*q[3]+q[1]*q[2]), 1-2*(q[2]**2+q[3]**2)))
                    with self.lock:
                        records = [dict(record) for record in self.records.values()]
                    emit({"type": "state", "connected": True, "coordinate_error": coordinate_error, "session_id": session_id,
                          "updated_at": int(time.time()*1000), "drones": records})
                    time.sleep(max(0, .25-(time.monotonic()-started)))
            except Exception as error:
                self.epoch.set()
                rpc.close()
                emit({"type": "reset"})
                emit({"type": "state", "connected": False, "error": str(error),
                      "updated_at": int(time.time()*1000), "drones": []})
                time.sleep(1)


if __name__ == "__main__":
    Monitor().run()
