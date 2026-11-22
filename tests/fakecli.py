"""Fake command-line tools for the script and Make-target tests.

Each test gets a folder whose bin/ comes first on PATH and holds one fake program per tool. The fake logs
every call as one JSON line (tool, args, kubectl's namespace, KUBECONFIG, AWS_PROFILE, whether Terraform got
the SSH public key, working directory, stdin) and answers from what the test set up through `Fakes`. No
cluster, container or AWS account is touched.

The fake knows just enough of each real tool for the scripts here:
- kubectl: namespaces and Secrets that exist; `create secret` keeps the values it was sent; `wait --for=create`
  waits for one resource only (like kubectl 1.37); `apply` changes the rollout revisions it reports;
  `port-forward` prints the local port it picked.
- curl: answers by URL (or by path, any host); an HTTP error status is an error exit only with -f.
- terraform: `output -raw NAME` and `state pull`. aws: `ssm get-parameter` by name, other calls by
  `service.operation` (identity and region list by default, empty otherwise).
- docker: a kustomize render prints MANIFEST, a Helm render ESO_MANIFEST. helm, k3d, ansible-playbook: log only.
Any call can be made to fail (`fail`), and a real command can be logged on its way through (`spy`).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
TOOLS = ("kubectl", "curl", "docker", "k3d", "terraform", "aws", "helm", "ansible-playbook")

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test@example"
INSTANCE_ID = "i-0123456789abcdef0"
NODE_IP = "203.0.113.20"  # what `terraform output -raw public_ip` prints
OWNER_IP = "203.0.113.10"  # what checkip answers
CHECKIP = "https://checkip.amazonaws.com"
FORWARD_PORT = "41234"  # the local port `kubectl port-forward` says it picked
MANIFEST = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: policypal\n"
ESO_MANIFEST = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: external-secrets\n"

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

tool, args = Path(sys.argv[0]).name, sys.argv[1:]
d = Path(os.environ["FAKE_DIR"])
# kubectl: the namespace is logged on its own; args keep everything else, in order.
ns = ""
if tool == "kubectl":
    rest, it = [], iter(args)
    for x in it:
        if x in ("-n", "--namespace"):
            ns = next(it)
        elif x.startswith("--namespace="):
            ns = x.split("=", 1)[1]
        else:
            rest.append(x)
    args = rest
stdin = sys.stdin.read() if "-" in args or any(a.endswith("/dev/stdin") for a in args) or tool == "docker" and "-i" in args else ""
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"tool": tool, "args": args, "ns": ns, "kubeconfig": os.environ.get("KUBECONFIG", ""),
                          "profile": os.environ.get("AWS_PROFILE", ""), "cwd": os.getcwd(), "stdin": stdin,
                          "ssh_key": bool(os.environ.get("TF_VAR_ssh_public_key"))}) + "\n")

def load(name, default):
    f = d / name
    return json.loads(f.read_text()) if f.exists() else default

spy = d / f"spy-{tool}"
if spy.exists():
    real = spy.read_text()
    os.execv(real, [real] + args)

for failing in load("fail.json", []):
    if failing[0] == tool and args[:len(failing) - 1] == failing[1:]:
        print(f"fake {tool}: {' '.join(args)} failed", file=sys.stderr)
        sys.exit(1)

def answer(name, default_rc=0):
    f = d / name
    if f.exists():
        sys.stdout.write(f.read_text())
        sys.exit(0)
    sys.exit(default_rc)

if tool == "kubectl":
    a = args
    if a[:2] == ["get", "namespace"]:
        sys.exit(0 if (d / "namespace-exists").exists() else 1)
    if a[:2] == ["get", "secret"]:
        sys.exit(0 if all((d / f"secret-{n}").exists() for n in a[2:] if not n.startswith("-")) else 1)
    if a[:3] == ["create", "secret", "generic"]:
        (d / f"secret-{a[3]}").write_text(stdin)
        sys.exit(0)
    if a[:2] == ["wait", "--for=create"]:
        # Like kubectl 1.37: --for=create waits for one resource; given several, it fails at once on the
        # first missing one (seen on k3d).
        names = [x.split("/", 1)[1] for x in a[2:] if x.startswith("secret/")]
        missing = [n for n in names if not (d / f"secret-{n}").exists()]
        if missing:
            print(f'Error from server (NotFound): secrets "{missing[0]}" not found' if len(names) > 1
                  else "error: timed out waiting for the condition", file=sys.stderr)
        sys.exit(1 if missing or not names else 0)
    if a[:1] == ["apply"]:
        (d / "applied").touch()
        sys.exit(0)
    if a[:2] == ["get", "deployment"] and any("revision" in x for x in a):
        when = "after" if (d / "applied").exists() else "before"
        answer(f"rev-{a[2]}-{when}", 1)
    if a[:1] == ["port-forward"]:
        port = (d / "forward-port").read_text()
        remote = next((x.rsplit(":", 1)[1] for x in a if x.startswith(":")), "")
        print(f"Forwarding from 127.0.0.1:{port} -> {remote}\nForwarding from [::1]:{port} -> {remote}")
    sys.exit(0)
if tool == "curl":
    url = next(x for x in args if x.startswith("http"))
    path = "/" + url.split("//", 1)[1].partition("/")[2]
    sent = " ".join(args)
    rules = [r for r in load("http.json", []) if r["url"] in (url, path) and (not r["when"] or r["when"] in sent)]
    if not rules:
        sys.exit(7)  # could not connect
    rule = max(rules, key=lambda r: bool(r["when"]))
    sys.stdout.write(rule["body"])
    # Like curl: an HTTP error status is an error exit only with -f (--fail).
    sys.exit(rule["rc"] if rule["rc"] and any(x.startswith("-f") for x in args) else 0)
if tool == "docker":
    if any("kustomize" in x for x in args):
        answer("manifest")
    if any(x.startswith("alpine/helm:") for x in args):
        answer("eso-manifest")
    sys.exit(0)
if tool == "terraform" and args[:2] == ["output", "-raw"]:
    answer(f"tf-output-{args[2]}", 1)
if tool == "terraform" and args[:2] == ["state", "pull"]:
    answer("tf-state", 1)
if tool == "aws" and args[:2] == ["ssm", "get-parameter"]:
    answer("ssm" + args[args.index("--name") + 1].replace("/", "_"), 254)
if tool == "aws" and len(args) > 1:
    answer(f"aws-{args[0]}.{args[1]}")
sys.exit(0)
'''


def flag_value(args: list[str], flag: str) -> str:
    """The word after `flag` in a logged call's args."""
    return args[args.index(flag) + 1]


class Fakes:
    def __init__(self, root: Path) -> None:
        self.dir = root
        self.bin = root / "bin"
        self.log = root / "calls.log"
        self.home = root / "home"
        # Make targets write the generated inventory here, never into the repo (a live one may be in use).
        self.inventory = root / "inventory" / "aws.yml"
        self.bin.mkdir()
        self.log.touch()
        # ephemeral-tf.sh (cloud-up, cloud-down, every `terraform output`) needs the public key.
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_ed25519.pub").write_text(PUBLIC_KEY + "\n")
        for tool in TOOLS:
            self._install(tool)
        self._write("manifest", MANIFEST)
        self._write("eso-manifest", ESO_MANIFEST)
        self.forward_port(FORWARD_PORT)
        self.tf_output("public_ip", NODE_IP)
        self.tf_output("instance_id", INSTANCE_ID)
        self.http(CHECKIP, OWNER_IP + "\n")
        self.aws("sts.get-caller-identity", "arn:aws:iam::111122223333:user/tester")
        self.aws("ec2.describe-regions", "ap-southeast-1")

    def _install(self, tool: str) -> None:
        path = self.bin / tool
        path.write_text(FAKE)
        path.chmod(0o755)

    def _write(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text)

    def _update(self, name: str, change: Any) -> None:
        path = self.dir / name
        data = json.loads(path.read_text()) if path.exists() else []
        path.write_text(json.dumps(change(data)))

    # --- what the scripts find ------------------------------------------------------------------------
    def fail(self, tool: str, *args: str) -> None:
        """Every call of `tool` whose args start with `args` fails (exit 1)."""
        self._update("fail.json", lambda rules: rules + [[tool, *args]])

    def spy(self, command: str) -> None:
        """Log every call of the real `command`, then run it."""
        real = shutil.which(command)
        assert real, f"{command} not installed"
        self._install(command)
        self._write(f"spy-{command}", real)

    def http(self, url: str, body: str | None, rc: int | None = None, when: str | None = None) -> None:
        """curl of `url` (a full URL, or a path on any host) prints `body`, exiting `rc` when called with -f.
        `when`: only for requests whose arguments contain this text. None as body: nothing listens (exit 7)."""
        def change(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rules = [r for r in rules if (r["url"], r["when"]) != (url, when)]
            return rules + ([{"url": url, "when": when, "body": body, "rc": rc}] if body is not None else [])
        self._update("http.json", change)

    def tf_output(self, name: str, value: str | None) -> None:
        """What `terraform output -raw name` prints; None: no such output (exit 1)."""
        path = self.dir / f"tf-output-{name}"
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(value)

    def tf_state(self, text: str) -> None:
        self._write("tf-state", text)

    def aws(self, call: str, output: str) -> None:
        """What `aws service operation ...` prints, e.g. call="sts.get-caller-identity"."""
        self._write(f"aws-{call}", output + "\n")

    def parameter(self, name: str, value: str) -> None:
        """An SSM parameter `aws ssm get-parameter --name name` returns."""
        self._write("ssm" + name.replace("/", "_"), value)

    def namespace_exists(self) -> None:
        self._write("namespace-exists", "")

    def secret_exists(self, name: str) -> None:
        self._write(f"secret-{name}", "existing")

    def eso_synced(self) -> None:
        """What External Secrets Operator does on AWS: both Secrets exist once the ExternalSecrets are applied."""
        self.secret_exists("policypal-db")
        self.secret_exists("policypal-admin")

    def revisions(self, name: str, before: str | None, after: str | None) -> None:
        """A Deployment's rollout revision before and after `kubectl apply` (None: it doesn't exist)."""
        for when, value in (("before", before), ("after", after)):
            if value is not None:
                self._write(f"rev-{name}-{when}", value)

    def forward_port(self, port: str) -> None:
        self._write("forward-port", port)

    # --- what the scripts did -------------------------------------------------------------------------
    def calls(self, tool: str | None = None, *prefix: str) -> list[dict[str, Any]]:
        """Every logged call, in order; or those of `tool` whose args start with `prefix`."""
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [r for r in rows if tool is None or r["tool"] == tool and r["args"][:len(prefix)] == list(prefix)]

    def kubectl(self, *prefix: str) -> list[list[str]]:
        """The args of kubectl calls that start with these words (the namespace is in each call's "ns")."""
        return [c["args"] for c in self.calls("kubectl") if c["args"][:len(prefix)] == list(prefix)]

    def created_secret(self, name: str) -> dict[str, str]:
        text = (self.dir / f"secret-{name}").read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if line)

    def created_secrets(self) -> set[tuple[str, str]]:
        """(Secret, key) for every Secret the scripts created."""
        return {(p.name.removeprefix("secret-"), key)
                for p in self.dir.glob("secret-*") for key in self.created_secret(p.name.removeprefix("secret-"))}

    # --- running --------------------------------------------------------------------------------------
    def env(self) -> dict[str, str]:
        return {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.home),
                "FAKE_LOG": str(self.log), "FAKE_DIR": str(self.dir)}

    def script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPTS / name), *args], cwd=REPO_ROOT, env=self.env(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def make(self, target: str, *overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["make", "--no-print-directory", target, f"AWS_INVENTORY={self.inventory}",
                               *overrides], cwd=REPO_ROOT, env=self.env(), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)


def output(result: subprocess.CompletedProcess[str]) -> str:
    """Everything a script or target printed."""
    return result.stdout + result.stderr
