"""Private atomic storage and a campaign-wide reservation ledger."""
import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

LIMIT_NANODOLLARS = 5_000_000_000
DEFAULT_ROOT = Path.home() / ".local/share/job-scraper/benchmarks"


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("private_directory_is_symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path, value):
    write_private(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_private(path, text):
    path = Path(path)
    private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("private_file_is_symlink")
    fd, tmp = tempfile.mkstemp(prefix=".atomic-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_keys(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("credentials_must_be_regular_file")
    if path.stat().st_mode & 0o077:
        raise ValueError("credentials_require_mode_600")
    names = {"OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"}
    keys = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name not in names or not sep or name in keys:
            raise ValueError("invalid_credential_assignment")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if (len(value) < 16 or any(x.isspace() for x in value)
                or any(x in value for x in ("$", "`", "your_", "<", ">"))):
            raise ValueError("missing_or_placeholder_credential")
        keys[name] = value
    if set(keys) != names:
        raise ValueError("three_credentials_required")
    return keys


@contextmanager
def campaign_lock(root):
    root = private_dir(root)
    path = root / "campaign.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("benchmark_already_running") from None
        yield
    finally:
        os.close(fd)


class Ledger:
    """Caller must hold campaign_lock. Amounts are integer billionths of a dollar.

    A durable reservation is also the crash record. Unknown outcomes retain its full
    charge, even if no response file exists. A new run ID cannot reset this ledger.
    """
    def __init__(self, root):
        self.path = Path(root) / "campaign-ledger.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            "version": 1, "limit": LIMIT_NANODOLLARS, "attempts": []}
        if self.data.get("limit") != LIMIT_NANODOLLARS or self.data.get("version") != 1:
            raise ValueError("invalid_campaign_ledger")
        for entry in self.data["attempts"]:
            if type(entry.get("charged")) is not int or entry["charged"] < 0:
                raise ValueError("invalid_campaign_charge")

    @property
    def used(self):
        return sum(e["charged"] for e in self.data["attempts"])

    def reserve(self, identity, amount, *, provider=None, job_id=None):
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid_reservation")
        if self.used + amount > LIMIT_NANODOLLARS:
            raise ValueError("budget_exhausted")
        index = len(self.data["attempts"])
        self.data["attempts"].append({"identity": identity, "reserved": amount,
                                      "charged": amount, "state": "reserved",
                                      "provider": provider, "job_id": job_id})
        write_json(self.path, self.data)
        return index

    def settle(self, index, cost, state, **metadata):
        entry = self.data["attempts"][index]
        if entry["state"] != "reserved":
            raise ValueError("attempt_already_settled")
        if cost is not None and (type(cost) is not int or cost < 0):
            raise ValueError("invalid_usage_cost")
        entry.update(state=state, **metadata)
        # Incomplete/missing usage stays fully reserved. Never hide a bound breach.
        entry["charged"] = cost if cost is not None else entry["reserved"]
        write_json(self.path, self.data)
        if entry["charged"] > entry["reserved"]:
            raise ValueError("billing_bound_exceeded_stop")
