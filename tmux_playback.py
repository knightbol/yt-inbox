"""Temporarily hand a local tmux client's real terminal to mpv, then reattach."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import termios
import time

import core


def is_vt(value):
    return bool(re.fullmatch(r"/dev/tty[1-9][0-9]*", value))


def query(program, *args):
    result = subprocess.run([program, *args], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise core.InboxError("Could not inspect tmux: " + result.stderr.strip())
    return result.stdout.strip()


def local_client():
    """Do not select an arbitrary client in shared, detached or remote sessions."""
    pane = os.environ.get("TMUX_PANE", "")
    program = shutil.which("tmux")
    if not os.environ.get("TMUX") or not re.fullmatch(r"%[0-9]+", pane) or not program:
        return None
    try:
        info = query(program, "display-message", "-p", "-t", pane,
                     "#{session_id}\n#{pane_tty}\n#{socket_path}").splitlines()
        if len(info) != 3 or not re.fullmatch(r"\$[0-9]+", info[0]) or info[1] != os.ttyname(sys.stdin.fileno()):
            raise core.InboxError("The tmux pane does not match this terminal.")
        session, _, socket = info
        clients = [line.split("\t") for line in query(program, "-S", socket, "list-clients", "-t", session,
                   "-F", "#{client_tty}\t#{client_pid}\t#{client_readonly}\t#{client_control_mode}").splitlines()]
        if not any(len(c) == 4 and is_vt(c[0]) for c in clients):
            return None
        if len(clients) != 1:
            raise core.InboxError("This tmux session has multiple clients. Keep only the local TTY client attached before playing DRM video.")
        tty, pid, readonly, control = clients[0]
        if readonly != "0" or control != "0" or not pid.isdigit() or int(pid) <= 1:
            raise core.InboxError("DRM playback needs a normal, writable tmux client on the local TTY.")
        destroy = query(program, "-S", socket, "show-options", "-A", "-v", "-t", session, "destroy-unattached")
        exit_unattached = query(program, "-S", socket, "show-options", "-s", "-v", "exit-unattached")
        if destroy != "off" or exit_unattached != "off":
            raise core.InboxError("tmux is configured to exit or destroy detached sessions. Run playback directly on the TTY; the inbox will not detach this session.")
        return {"program": program, "socket": socket, "session": session, "tty": tty, "pid": int(pid)}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise core.InboxError("Could not identify the local tmux client. Run the inbox directly on the TTY.") from exc


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def play(paths, command, client):
    # Recheck before detaching, in case clients were switched during preparation.
    if local_client() != client:
        raise core.InboxError("The tmux client changed. Select the video again.")
    with tempfile.TemporaryDirectory(prefix="tmux-player-", dir=paths.state) as folder:
        job = Path(folder)
        # Carry only player-related overrides, not the pane's TERM/TMUX values.
        keys = ("PATH", "MPV_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PULSE_SERVER", "PIPEWIRE_REMOTE", "LANG")
        env = {key: value for key, value in os.environ.items() if key in keys or key.startswith("LC_")}
        request = job / "request.json"
        core.atomic_write(request, json.dumps({"command": command, "client": client, "env": env}))
        helper = "exec " + shlex.join([sys.executable, str(Path(__file__).resolve()), str(request)])
        try:
            query(client["program"], "-S", client["socket"], "detach-client", "-t", client["tty"], "-E", helper)
        except subprocess.TimeoutExpired as exc:
            raise core.InboxError("tmux did not acknowledge the player handoff.") from exc
        started, result = job / "started", job / "result.json"
        deadline = time.monotonic() + 10
        try:
            while not result.exists():
                if not alive(client["pid"]):
                    raise core.InboxError("The tmux player helper exited before returning a result. Reattach with tmux attach.")
                if not started.exists() and time.monotonic() > deadline:
                    raise core.InboxError("The tmux player helper did not start. Reattach with tmux attach.")
                time.sleep(0.1)
        except KeyboardInterrupt:
            (job / "cancel").touch()
            deadline = time.monotonic() + 5
            while not result.exists() and alive(client["pid"]) and time.monotonic() < deadline:
                time.sleep(0.1)
            raise
        response = json.loads(result.read_text(encoding="utf-8"))
        if response["code"] == 130:
            raise KeyboardInterrupt
        if response["code"]:
            raise core.InboxError(f"mpv exited with code {response['code']}. " + response.get("error", ""))
        return 0


def player_job(request):
    """Runs as the former tmux client, with its controlling TTY and keyboard."""
    request = Path(request)
    job = request.parent
    data = json.loads(request.read_text(encoding="utf-8"))
    client = data["client"]
    code, error, proc, modes = 1, "", None, None
    try:
        core.atomic_write(job / "started", str(os.getpid()))
        if os.ttyname(sys.stdin.fileno()) != client["tty"]:
            raise core.InboxError("tmux handed the player a different terminal.")
        modes = termios.tcgetattr(sys.stdin.fileno())
        print("YouTube Inbox - mpv: Space pauses; arrows seek; q returns to tmux.", flush=True)
        with tempfile.TemporaryFile() as log:
            try:
                proc = subprocess.Popen(data["command"], stdout=log, stderr=subprocess.STDOUT,
                                        env=dict(os.environ, **data["env"]))
                while True:
                    if (job / "cancel").exists():
                        raise KeyboardInterrupt
                    try:
                        code = proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
            except KeyboardInterrupt:
                code = 130
            finally:
                if proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            log.seek(max(0, log.seek(0, os.SEEK_END) - 8000))
            error = log.read().decode("utf-8", errors="replace")
    except (core.InboxError, OSError, ValueError) as exc:
        error = str(exc)
    finally:
        # Notify the waiting inbox before replacing this process with its client.
        # Reattach even if reporting failed, so a closed inbox cannot strand it.
        try:
            if modes is not None:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, modes)
                except termios.error as exc:
                    code = code or 1
                    error += " Could not restore terminal modes: " + str(exc)
            core.atomic_write(job / "result.json", json.dumps({"code": code, "error": error}))
        finally:
            env = dict(os.environ)
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            os.execve(client["program"], [client["program"], "-S", client["socket"],
                      "attach-session", "-E", "-t", client["session"]], env)


if __name__ == "__main__":
    player_job(sys.argv[1])
