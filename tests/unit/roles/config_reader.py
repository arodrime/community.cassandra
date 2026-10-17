from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

# Runs cassandra_config's node-side reader (files/config_read.py) as the role
# does: python -c, a JSON request on stdin, a JSON answer on stdout.

import json
import os
import subprocess
import sys

import yaml

ROLE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "roles", "cassandra_config")
with open(os.path.join(ROLE, "files", "config_read.py"), encoding="utf-8") as f:
    SCRIPT = f.read()
with open(os.path.join(ROLE, "vars", "main.yml"), encoding="utf-8") as f:
    SNITCHES = yaml.safe_load(f)["_cassandra_config_snitches_without_rackdc"]


def config_read(request, env=None):
    """The answer of a step; request as the role sends it (Ansible's to_json: ASCII)."""
    out = subprocess.run([sys.executable, "-c", SCRIPT], input=json.dumps(request).encode("ascii"),
                         stdout=subprocess.PIPE, check=True, env=env)
    return json.loads(out.stdout.decode("ascii"))


def compare(conf_dir, files, compare_live=False, identity=None, env=None):
    """The compare step for files {name: new text} against the live ones in conf_dir."""
    return config_read({"step": "compare", "conf_dir": os.fsdecode(conf_dir), "names": list(files), "files": files,
                        "compare": compare_live, "identity": identity}, env)


def preview(tmp_path, name, live, new, env=None):
    """The masked diff of a live file (None: none yet, bytes: as is) against the role's (text); "" when the same."""
    (tmp_path / "live").mkdir()
    if live is not None:
        (tmp_path / "live" / name).write_bytes(live if isinstance(live, bytes) else live.encode())
    result = compare(tmp_path / "live", {name: new}, env=env)["preview"][0]
    assert result["rc"] == (1 if result["stdout"] else 0)  # 1: the files differ
    return result["stdout"]


def identity_changes(tmp_path, live, new, live_rackdc="dc=d\n", new_rackdc="dc=d\n", env=None):
    """The settings a joined node must keep that change: live files written (text or bytes), the new ones as
    rendered (text), or as kept on the node (bytes: written there)."""
    request = {"snitches": SNITCHES}
    for key, name, text in (("live_yaml", "live.yaml", live), ("new_yaml", "new.yaml", new),
                            ("live_rackdc", "live.p", live_rackdc), ("new_rackdc", "new.p", new_rackdc)):
        if key.startswith("new") and not isinstance(text, bytes):
            request[key] = {"text": text}
            continue
        (tmp_path / name).write_bytes(text if isinstance(text, bytes) else text.encode())
        request[key] = str(tmp_path / name) if key.startswith("live") else {"path": str(tmp_path / name)}
    answer = compare(tmp_path, {}, identity=request, env=env)["identity"]
    assert answer["rc"] == 0, answer
    return answer["changes"]
