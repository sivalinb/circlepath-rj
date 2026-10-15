"""Download official binaries into ignored local data; run a loopback-only Grafana stack.

No Homebrew installation, background service registration, or Docker daemon is needed.
Artifacts are verified against checksums from the same official release source.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import signal
import subprocess
import tarfile
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "observability"
VERSIONS = {"grafana": "13.2.4", "prometheus": "3.15.0", "tempo": "3.1.0"}


def fetch(url: str) -> requests.Response:
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    return response


def artifact(name: str, operating_system: str, arch: str):
    version = VERSIONS[name]
    if name == "grafana":
        page = fetch(f"https://grafana.com/grafana/download/{version}?edition=oss").text
        urls = sorted(set(re.findall(r"https://dl\.grafana\.com/[^\s<>\"\\]+tar\.gz", page)))
        url = next(x for x in urls if f"_{operating_system}_{arch}.tar.gz" in x)
        checksum = fetch(url + ".sha256").text.split()[0]
    else:
        repo = "prometheus/prometheus" if name == "prometheus" else "grafana/tempo"
        release = fetch(f"https://api.github.com/repos/{repo}/releases/tags/v{version}").json()
        target = f".{operating_system}-{arch}.tar.gz" if name == "prometheus" else f"_{operating_system}_{arch}.tar.gz"
        asset = next(a for a in release["assets"] if a["name"].endswith(target))
        url = asset["browser_download_url"]
        if (asset.get("digest") or "").startswith("sha256:"):
            checksum = asset["digest"].split(":", 1)[1]
        else:
            checksums_url = next(a["browser_download_url"] for a in release["assets"] if "sha256sums" in a["name"] or "checksums" in a["name"])
            checksum = next(line.split()[0] for line in fetch(checksums_url).text.splitlines() if line.rstrip().endswith(url.rsplit("/", 1)[-1]))
    return url, checksum


def install():
    operating_system = "darwin" if platform.system() == "Darwin" else "linux"
    arch = "arm64" if platform.machine() in {"arm64", "aarch64"} else "amd64"
    (DATA / "binaries").mkdir(parents=True, exist_ok=True, mode=0o700)
    manifests = {}
    for name, version in VERSIONS.items():
        destination = DATA / "binaries" / f"{name}-{version}"
        if destination.exists():
            print(name, "already downloaded")
            continue
        url, checksum = artifact(name, operating_system, arch)
        print("Downloading official", name, version, flush=True)
        content = fetch(url).content
        actual = hashlib.sha256(content).hexdigest()
        if actual != checksum:
            raise RuntimeError(f"Checksum mismatch for {name}; no binary was installed.")
        archive = DATA / f"{name}.tar.gz"
        archive.write_bytes(content)
        destination.mkdir(mode=0o700)
        with tarfile.open(archive) as bundle:
            bundle.extractall(destination, filter="data")
        archive.unlink()
        manifests[name] = {"version": version, "url": url, "sha256": actual}
    (DATA / "downloads.json").write_text(json.dumps(manifests, indent=2))


def executable(name: str) -> Path:
    folder = DATA / "binaries" / f"{name}-{VERSIONS[name]}"
    matches = [p for p in folder.rglob(name) if p.is_file() and os.access(p, os.X_OK)]
    if not matches:
        raise RuntimeError(f"Run install before starting {name}.")
    return matches[0]


def configure(app_port: int):
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    for folder in ["logs", "grafana", "tempo", "prometheus", "plugins", "provisioning/alerting",
                   "provisioning/datasources", "provisioning/dashboards"]:
        (DATA / folder).mkdir(parents=True, exist_ok=True)
    datasources = {"apiVersion": 1, "datasources": [
        {"name": "CirclePath metrics", "uid": "circlepath-prometheus", "type": "prometheus",
         "access": "proxy", "url": "http://127.0.0.1:9091", "isDefault": True},
        {"name": "CirclePath traces", "uid": "circlepath-tempo", "type": "tempo",
         "access": "proxy", "url": "http://127.0.0.1:3201"},
    ]}
    (DATA / "provisioning/datasources/circlepath.yaml").write_text(yaml.safe_dump(datasources))
    dashboard_provider = {"apiVersion": 1, "providers": [{"name": "CirclePath", "folder": "CirclePath",
        "type": "file", "options": {"path": str(ROOT / "observability/grafana/dashboards")}}]}
    (DATA / "provisioning/dashboards/circlepath.yaml").write_text(yaml.safe_dump(dashboard_provider))
    prometheus = {"global": {"scrape_interval": "5s"}, "scrape_configs": [
        {"job_name": "circlepath", "metrics_path": "/metrics/", "static_configs": [{"targets": [f"127.0.0.1:{app_port}"]}]}
    ]}
    (DATA / "prometheus.yaml").write_text(yaml.safe_dump(prometheus))
    tempo = {
        "stream_over_http_enabled": True,
        "server": {"http_listen_address": "127.0.0.1", "http_listen_port": 3201,
                   "grpc_listen_address": "127.0.0.1", "grpc_listen_port": 9096},
        "distributor": {"receivers": {"otlp": {"protocols": {
            "http": {"endpoint": "127.0.0.1:4320"}, "grpc": {"endpoint": "127.0.0.1:4321"}}}}},
        "usage_report": {"reporting_enabled": False},
        "storage": {"trace": {"backend": "local", "wal": {"path": str(DATA / "tempo/wal")},
                              "local": {"path": str(DATA / "tempo/blocks")}}},
    }
    (DATA / "tempo.yaml").write_text(yaml.safe_dump(tempo))
    login = DATA / "grafana-login.txt"
    if not login.exists():
        login.write_text("admin\n" + secrets.token_urlsafe(24) + "\n")
        login.chmod(0o600)


def start(app_port: int):
    configure(app_port)
    commands = {
        "prometheus": [str(executable("prometheus")), f"--config.file={DATA / 'prometheus.yaml'}",
                       f"--storage.tsdb.path={DATA / 'prometheus'}", "--web.listen-address=127.0.0.1:9091"],
        "tempo": [str(executable("tempo")), f"-config.file={DATA / 'tempo.yaml'}",
                  f"-live-store.shutdown_marker_dir={DATA / 'tempo/shutdown-marker'}",
                  f"-live-store.wal.path={DATA / 'tempo/live-store'}",
                  # This spelling is the actual flag exposed by Tempo 3.1.0 -help.
                  f"-backend-schedulerbackend-scheduler.local-work-path={DATA / 'tempo/work-cache'}"],
    }
    grafana = executable("grafana")
    home = grafana.parent.parent
    commands["grafana"] = [str(grafana), "server", f"--homepath={home}"]
    manifest = {}
    for name, command in commands.items():
        pid_file = DATA / f"{name}.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 0)
                print(name, "already running")
                continue
            except ProcessLookupError:
                pass
        env = os.environ.copy()
        if name == "grafana":
            env.update({
                "GF_PATHS_DATA": str(DATA / "grafana"), "GF_PATHS_LOGS": str(DATA / "logs"),
                "GF_PATHS_PLUGINS": str(DATA / "plugins"), "GF_PATHS_PROVISIONING": str(DATA / "provisioning"),
                "GF_SERVER_HTTP_ADDR": "127.0.0.1", "GF_SERVER_HTTP_PORT": "3300",
                "GF_AUTH_ANONYMOUS_ENABLED": "true", "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
                "GF_SECURITY_ADMIN_PASSWORD": (DATA / "grafana-login.txt").read_text().splitlines()[1],
                "GF_ANALYTICS_REPORTING_ENABLED": "false", "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
                "GF_USERS_ALLOW_SIGN_UP": "false", "GF_LOG_LEVEL": "warn",
                "GF_PLUGINS_PREINSTALL_DISABLED": "true",
            })
        with (DATA / "logs" / f"{name}.log").open("a") as log:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=log, start_new_session=True)  # noqa: S603
        pid_file.write_text(str(process.pid))
        manifest[name] = process.pid
        print("Started", name, "PID", process.pid)
    print("Grafana: http://127.0.0.1:3300/d/circlepath/circlepath")
    print("Set OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4320/v1/traces for the app.")


def stop():
    for name in VERSIONS:
        pid_file = DATA / f"{name}.pid"
        if not pid_file.exists():
            continue
        pid = int(pid_file.read_text())
        try:
            command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()  # noqa: S603, S607
            if str(DATA / "binaries") in command:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)  # noqa: S603, S607
                    if result.returncode or result.stdout.strip().startswith("Z"):
                        break
                    time.sleep(0.1)
        except (ProcessLookupError, subprocess.CalledProcessError):
            pass
        pid_file.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["install", "start", "stop"])
    parser.add_argument("--app-port", type=int, default=7860)
    args = parser.parse_args()
    if args.command == "install":
        install()
    elif args.command == "start":
        start(args.app_port)
    else:
        stop()
