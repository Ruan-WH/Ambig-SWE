"""Restrict container egress after setup, before starting the benchmark agent."""

import json
from pathlib import Path

from openhands.events.action import CmdRunAction
from openhands.events.observation import CmdOutputObservation


def isolated_container_kwargs(kwargs: dict | None) -> dict:
    result = dict(kwargs or {})
    if result.get("privileged") or result.get("cap_add"):
        raise ValueError(
            "Agent network isolation requires an unprivileged container without cap_add"
        )
    if result.get("network_mode") or result.get("pid_mode") == "host":
        raise ValueError(
            "Agent network isolation requires a private Docker network namespace"
        )
    drops = list(result.get("cap_drop") or [])
    for cap in ["NET_ADMIN", "NET_RAW"]:
        if cap not in drops:
            drops.append(cap)
    result["cap_drop"] = drops
    return result


# Applied by a trusted privileged Docker exec, not by an agent command. The
# long-lived runtime/agent processes retain their original capability bounds.
# REPLY direction keeps host-initiated API requests working but blocks even
# already-established outbound connections left over from initialization.
FIREWALL_SCRIPT = r"""
set -eu
for tool in iptables ip6tables; do
    "$tool" -w 10 -N OH_AGENT_EGRESS
    if [ "$tool" = iptables ]; then
        "$tool" -w 10 -A OH_AGENT_EGRESS -o lo -d 127.0.0.1/32 -j ACCEPT
    else
        "$tool" -w 10 -A OH_AGENT_EGRESS -o lo -d ::1/128 -j ACCEPT
    fi
    "$tool" -w 10 -A OH_AGENT_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED --ctdir REPLY -j ACCEPT
    "$tool" -w 10 -A OH_AGENT_EGRESS -j REJECT
    "$tool" -w 10 -I OUTPUT 1 -j OH_AGENT_EGRESS
    "$tool" -w 10 -C OUTPUT -j OH_AGENT_EGRESS
    "$tool" -w 10 -C OH_AGENT_EGRESS -j REJECT
done
"""

PROBE_SCRIPT = r"""
import json, socket, os, urllib.request
from urllib.parse import urlsplit
result = {}
def blocked(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            return False
    except OSError:
        return True
result['public_ipv4_blocked'] = blocked('1.1.1.1', 443)
result['public_ipv6_blocked'] = blocked('2606:4700:4700::1111', 443)
with socket.socket() as server:
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    with socket.create_connection(server.getsockname(), timeout=3):
        connection, _ = server.accept()
        connection.close()
result['loopback_ok'] = True
# Block Docker's embedded DNS as well: otherwise it can forward requests via
# the host, outside the container's public-IP firewall.
try:
    socket.getaddrinfo('pypi.org', 443)
    result['external_dns_blocked'] = False
except OSError:
    result['external_dns_blocked'] = True
# Literal IP probes cannot be bypassed by DNS/proxy variables. Also exercise
# the package-download route with environment proxies explicitly disabled.
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('https://pypi.org/simple/astropy/', timeout=5):
        result['pypi_blocked'] = False
except Exception:
    result['pypi_blocked'] = True
for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    if os.environ.get(name):
        proxy = urlsplit(os.environ[name])
        if proxy.hostname and proxy.port:
            result['proxy_' + name + '_blocked'] = blocked(proxy.hostname, proxy.port)
result['ok'] = all(result.values())
print('AGENT_NETWORK_EVIDENCE ' + json.dumps(result))
"""


def restrict_agent_network(runtime, metadata, instance_id: str) -> dict:
    evidence = {"instance_id": instance_id, "phase": "before_agent", "ok": False}
    path = Path(metadata.eval_output_dir) / "network_evidence" / f"{instance_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        container = getattr(runtime, "container", None)
        if container is None:
            raise RuntimeError(
                "Agent network isolation currently requires a local Docker runtime"
            )
        container.reload()
        host = container.attrs["HostConfig"]
        if (
            host.get("Privileged")
            or host.get("CapAdd")
            or host.get("NetworkMode") in ("host", "none")
            or str(host.get("NetworkMode", "")).startswith("container:")
        ):
            raise RuntimeError(
                "Unsafe container privileges/network mode for agent isolation"
            )
        if not {"NET_ADMIN", "NET_RAW"}.issubset(set(host.get("CapDrop") or [])):
            raise RuntimeError(
                "Agent must drop NET_ADMIN and NET_RAW before container startup"
            )
        # Older cached runtime images do not have these tools. Install during
        # the setup phase only; this does not require rebuilding Python layers.
        install = container.exec_run(
            [
                "bash",
                "-ceu",
                "if ! command -v iptables >/dev/null || ! command -v ip6tables >/dev/null; then apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=60 -o Acquire::https::Timeout=60 update && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=60 -o Acquire::https::Timeout=60 install -y --no-install-recommends iptables; fi",
            ],
            user="0",
        )
        if install.exit_code:
            raise RuntimeError(
                "Cannot install network firewall tools: "
                + install.output.decode(errors="replace")[-2000:]
            )
        applied = container.exec_run(
            ["bash", "-ceu", FIREWALL_SCRIPT], user="0", privileged=True
        )
        if applied.exit_code:
            raise RuntimeError(
                "Cannot apply agent firewall: "
                + applied.output.decode(errors="replace")[-2000:]
            )
        evidence["policy"] = "loopback_and_host_initiated_replies_only"
        action = CmdRunAction(command="python - <<'PY'\n" + PROBE_SCRIPT + "\nPY")
        action.timeout = 60
        # This uses the actual host->runtime HTTP channel AFTER applying rules,
        # proving control traffic still works. Executed with agent privileges.
        obs = runtime.run_action(action)
        if not isinstance(obs, CmdOutputObservation) or obs.exit_code:
            raise RuntimeError(
                "Agent network probe failed or runtime API became unreachable"
            )
        line = next(
            (
                line
                for line in (obs.content or "").splitlines()
                if line.startswith("AGENT_NETWORK_EVIDENCE ")
            ),
            None,
        )
        if line is None:
            raise RuntimeError("Network probe evidence is missing")
        evidence["probe"] = json.loads(line.split(" ", 1)[1])
        required = {
            "ok",
            "public_ipv4_blocked",
            "public_ipv6_blocked",
            "loopback_ok",
            "external_dns_blocked",
            "pypi_blocked",
        }
        if not required.issubset(evidence["probe"]) or not all(
            value is True for value in evidence["probe"].values()
        ):
            raise RuntimeError("Agent egress isolation probes did not all pass")
        evidence["ok"] = True
        return evidence
    except Exception as error:
        evidence["error"] = str(error)
        raise
    finally:
        path.write_text(json.dumps(evidence, indent=2))
