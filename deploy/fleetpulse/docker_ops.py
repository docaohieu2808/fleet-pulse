"""Docker and Traefik steps of a canary: build, start, split traffic, promote, restore, clean up."""
import json
import os
import pathlib
import subprocess
import time
import urllib.request


def sh(*args: str, cwd: str | None = None, capture: bool = False) -> str:
    out = subprocess.run(args, cwd=cwd, check=True, text=True,
                         stdout=subprocess.PIPE if capture else None)
    return (out.stdout or "").strip()


def build(src: str, image: str, tag: str) -> None:
    sh("docker", "build", "-q", "-t", f"{image}:{tag}", src)


def image_id(ref: str) -> str | None:
    try:
        return sh("docker", "image", "inspect", "-f", "{{.Id}}", ref, capture=True)
    except subprocess.CalledProcessError:
        return None


def migrations(image_ref: str, migrations_dir: str) -> dict[str, str]:
    """name -> sha256 of every migration file in the image, so an edited migration counts as a change too.
    Raises when the directory is missing: the guard must not pass by accident."""
    out = sh("docker", "run", "--rm", "--entrypoint", "sh", image_ref, "-c",
             f'cd "{migrations_dir}" && sha256sum *', capture=True)
    files = {}
    for line in out.splitlines():
        digest, name = line.split(None, 1)
        files[name.strip()] = digest
    return files


def start_canary(cfg: dict, image_ref: str, app_dir: str) -> None:
    c = cfg["canary"]
    r = cfg["route"]
    svc = c["service"]
    labels = {
        "traefik.enable": "true",
        "traefik.docker.network": r["traefik_network"],
        # Real traffic reaches the canary only through the weighted split. This router exists for the readiness
        # check: a name under the wildcard certificate (strict SNI rejects anything else), with no DNS record.
        f"traefik.http.routers.{svc}.rule": f"Host(`{r['canary_host']}`)",
        f"traefik.http.routers.{svc}.entrypoints": "websecure",
        f"traefik.http.routers.{svc}.tls": "true",
        # Same allow-list as the app, so the canary is never reachable from outside the tailnet.
        f"traefik.http.routers.{svc}.middlewares": ",".join(r["middlewares"]),
        f"traefik.http.services.{svc}.loadbalancer.server.port": str(c["port"]),
        "fleet-pulse.role": "canary",
        "fleet-pulse.app": cfg["app"],
    }
    # Created on the network Traefik routes through. Attached to it later, Traefik can keep the address it saw
    # first (on a network it cannot reach) and every canary request times out (seen 24 Oct: all 499).
    first = r["traefik_network"]
    # unless-stopped: during a promote the canary briefly serves all traffic and must survive a crash.
    args = ["docker", "run", "-d", "--name", c["name"], "--restart", "unless-stopped",
            "--memory", c["memory"], "--cpus", c["cpus"], "--network", first]
    if c.get("env_file"):
        args += ["--env-file", os.path.join(app_dir, c["env_file"])]
    for k, v in c.get("env", {}).items():
        args += ["-e", f"{k}={v}"]
    for k, v in labels.items():
        args += ["-l", f"{k}={v}"]
    sh(*args, image_ref, *c["command"], capture=True)
    for net in c["networks"]:
        if net != first:
            sh("docker", "network", "connect", net, c["name"])


def container_ip(name: str, network: str) -> str:
    out = sh("docker", "inspect", name, "--format", "{{json .NetworkSettings.Networks}}", capture=True)
    return json.loads(out)[network]["IPAddress"]


def wait_healthy(name: str, network: str, port: int, path: str, timeout_s: int = 90) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            url = f"http://{container_ip(name, network)}:{port}{path}"
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    return True
        except (OSError, KeyError, subprocess.CalledProcessError):
            pass
        time.sleep(2)
    return False


def _via_traefik(cfg: dict, host: str, path: str) -> str:
    out = subprocess.run(
        ["curl", "-sk", "-o", "/dev/null", "-m", "5", "-w", "%{http_code}",
         "--resolve", f"{host}:443:{cfg['route']['traefik_address']}", f"https://{host}{path}"],
        text=True, stdout=subprocess.PIPE)
    return out.stdout


def wait_routable(cfg: dict, host: str | None = None, timeout_s: int = 60) -> bool:
    """200 through Traefik on `host` (default: the canary's own name): the exact path real traffic takes."""
    host = host or cfg["route"]["canary_host"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _via_traefik(cfg, host, cfg["canary"]["health_path"]) == "200":
            return True
        time.sleep(2)
    return False


def route_file(cfg: dict) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(cfg["route"]["dynamic_dir"])) / f"{cfg['app']}-canary.yml"


def write_split(cfg: dict, canary_weight: int) -> None:
    """Route the app's host through a weighted service: stable gets 100 - canary_weight."""
    r = cfg["route"]
    name = f"{cfg['app']}-canary-split"
    services = []
    if canary_weight < 100:
        services.append(f"          - name: {cfg['stable_service']}\n            weight: {100 - canary_weight}\n")
    services.append(f"          - name: {cfg['canary']['service']}@docker\n            weight: {canary_weight}\n")
    body = (
        "# Written by fleet-pulse-deploy during a canary; removed on promote or rollback.\n"
        "http:\n  routers:\n"
        f"    {name}:\n"
        f"      rule: Host(`{r['host']}`)\n"
        "      priority: 10000\n"
        "      entryPoints: [websecure]\n"
        "      tls: {}\n"
        f"      middlewares: [{', '.join(r['middlewares'])}]\n"
        f"      service: {name}\n"
        "  services:\n"
        f"    {name}:\n"
        "      weighted:\n        services:\n" + "".join(services)
    )
    path = route_file(cfg)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body)
    tmp.replace(path)  # atomic, so Traefik never reads half a file
    time.sleep(3)  # file provider watch picks it up


def remove_split(cfg: dict) -> None:
    route_file(cfg).unlink(missing_ok=True)
    time.sleep(3)


def remove_canary(cfg: dict) -> bool:
    """True when no canary container is left."""
    subprocess.run(["docker", "rm", "-f", cfg["canary"]["name"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return not canary_container_exists(cfg)


def canary_container_exists(cfg: dict) -> bool:
    return bool(sh("docker", "ps", "-a", "--filter", f"name=^{cfg['canary']['name']}$", "--format", "{{.Names}}",
                   capture=True))


def canary_exists(cfg: dict) -> bool:
    return canary_container_exists(cfg) or route_file(cfg).exists()


def run_stable(cfg: dict, image_ref: str, app_dir: str) -> bool:
    """Point the stable tag at image_ref, recreate the stable services, wait until the container answers."""
    c = cfg["canary"]
    sh("docker", "tag", image_ref, f"{cfg['image']}:{cfg['stable_tag']}")
    sh("docker", "compose", "up", "-d", "--no-deps", *cfg["promote_services"], cwd=app_dir)
    return wait_healthy(cfg["stable_container"], c["networks"][0], c["port"], c["health_path"], 120)


def prune_canary_images(cfg: dict, keep: int = 5) -> list[str]:
    """Remove all but the newest `keep` canary-built tags (c<timestamp>); the stable and :previous tags stay."""
    tags = sh("docker", "image", "ls", cfg["image"], "--format", "{{.Tag}}", capture=True).split()
    built = sorted((t for t in tags if t.startswith("c") and t[1:].isdigit()), reverse=True)
    removed = []
    for t in built[keep:]:
        if subprocess.run(["docker", "image", "rm", f"{cfg['image']}:{t}"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            removed.append(t)
    return removed
