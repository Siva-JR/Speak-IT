"""OS flags, state/config paths and the control channel, shared by dictate.py, dashboard.py and platform_win.py.
Standard library only. On Linux/macOS the daemon listens on a 0600 unix socket; on Windows (no AF_UNIX) it listens on
127.0.0.1 and every request must carry the secret in STATE/ctl.token, so other local programs cannot drive it."""
import hmac, os, secrets, socket, sys, tempfile
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
OS_NAME = "windows" if IS_WIN else "mac" if IS_MAC else "linux" if IS_LINUX else sys.platform

if IS_WIN:
    _local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    _roam = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    CONF_DIR = _roam / "dictate"
    STATE = Path(os.environ.get("DICTATE_STATE", _local / "dictate" / "state"))
    RUNTIME = Path(tempfile.gettempdir())
else:
    CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dictate"
    STATE = Path(os.environ.get("DICTATE_STATE", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "dictate"))
    RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))

SOCK = os.environ.get("DICTATE_SOCK", str(RUNTIME / "dictate.sock"))
PORT = int(os.environ.get("DICTATE_PORT", "48621"))
TOKEN_FILE = STATE / "ctl.token"
START_HINT = ("run: dictate serve   (it also starts at login after install.ps1)" if IS_WIN
              else "systemctl --user start dictate")


def ctl_listen():
    """Bound, listening server socket for the daemon."""
    if IS_WIN:
        STATE.mkdir(parents=True, exist_ok=True)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)     # no other process may share the port
        s.bind(("127.0.0.1", PORT)); s.listen(4)
        TOKEN_FILE.write_text(secrets.token_hex(16))
        return s
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    s = socket.socket(socket.AF_UNIX); s.bind(SOCK); s.listen(4); os.chmod(SOCK, 0o600)
    return s


def ctl_unpack(data):
    """Command text from a raw request, or None if it is not authorised."""
    text = data.decode(errors="replace")
    if not IS_WIN:
        return text.strip()
    try:
        token, _, cmd = text.partition("\n")
        if hmac.compare_digest(token.strip(), TOKEN_FILE.read_text().strip()):
            return cmd.strip()
    except OSError:
        pass
    return None


def ctl_request(cmd, timeout=60):
    """Send one command to the running daemon and return its reply. Raises ConnectionError/FileNotFoundError if down."""
    if IS_WIN:
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM); c.settimeout(timeout)
        try:
            token = TOKEN_FILE.read_text().strip()
        except OSError as e:
            raise FileNotFoundError(f"no daemon token ({TOKEN_FILE})") from e
        c.connect(("127.0.0.1", PORT)); c.sendall(f"{token}\n{cmd}".encode())
    else:
        c = socket.socket(socket.AF_UNIX); c.settimeout(timeout)
        c.connect(SOCK); c.sendall(cmd.encode())
    try:
        return c.recv(65536).decode()
    finally:
        c.close()
