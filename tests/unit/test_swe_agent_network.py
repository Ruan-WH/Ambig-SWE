import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from evaluation.benchmarks.swe_bench.agent_network import (
    isolated_container_kwargs,
    restrict_agent_network,
)
from openhands.events.observation import CmdOutputObservation


def test_isolation_preserves_limits_and_does_not_mutate_caller():
    original = {"mem_limit": "6g", "cap_drop": ["SYS_PTRACE"]}
    result = isolated_container_kwargs(original)
    assert result["mem_limit"] == "6g"
    assert result["cap_drop"] == ["SYS_PTRACE", "NET_ADMIN", "NET_RAW"]
    assert original["cap_drop"] == ["SYS_PTRACE"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"privileged": True},
        {"cap_add": ["NET_ADMIN"]},
        {"network_mode": "host"},
        {"pid_mode": "host"},
    ],
)
def test_unsafe_container_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        isolated_container_kwargs(kwargs)


def runtime_fixture():
    runtime = MagicMock()
    runtime.container.attrs = {
        "HostConfig": {"CapDrop": ["NET_ADMIN", "NET_RAW"], "NetworkMode": "default"}
    }
    runtime.container.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"")
    return runtime


def test_success_evidence_is_saved_after_control_channel_probe(tmp_path):
    runtime = runtime_fixture()
    runtime.run_action.return_value = CmdOutputObservation(
        content='AGENT_NETWORK_EVIDENCE {"ok":true,"loopback_ok":true,"pypi_blocked":true,"external_dns_blocked":true,"public_ipv4_blocked":true,"public_ipv6_blocked":true}',
        command="probe",
        exit_code=0,
    )
    result = restrict_agent_network(
        runtime, SimpleNamespace(eval_output_dir=str(tmp_path)), "example"
    )
    assert result["ok"]
    saved = json.loads((tmp_path / "network_evidence/example.json").read_text())
    assert saved["ok"]
    assert runtime.container.exec_run.call_args_list[1].kwargs["privileged"] is True
    assert runtime.run_action.call_count == 1


@pytest.mark.parametrize(
    "failure", ["installation", "firewall", "control", "leak", "missing"]
)
def test_failure_is_recorded_and_raises_before_agent(tmp_path, failure):
    runtime = runtime_fixture()
    ok = SimpleNamespace(exit_code=0, output=b"")
    bad = SimpleNamespace(exit_code=1, output=b"failed")
    if failure == "installation":
        runtime.container.exec_run.return_value = bad
    elif failure == "firewall":
        runtime.container.exec_run.side_effect = [ok, bad]
    elif failure == "control":
        runtime.run_action.side_effect = RuntimeError("runtime unreachable")
    else:
        runtime.run_action.return_value = CmdOutputObservation(
            content=(
                'AGENT_NETWORK_EVIDENCE {"ok":true}'
                if failure == "missing"
                else 'AGENT_NETWORK_EVIDENCE {"ok":false}'
            ),
            command="probe",
            exit_code=0,
        )
    with pytest.raises(RuntimeError):
        restrict_agent_network(
            runtime, SimpleNamespace(eval_output_dir=str(tmp_path)), "example"
        )
    saved = json.loads((tmp_path / "network_evidence/example.json").read_text())
    assert not saved["ok"]
    assert "error" in saved


@pytest.mark.parametrize("seal,recovery", [(False, False), (True, False), (True, True)])
def test_three_arms_share_the_same_container_restrictions(
    seal, recovery, monkeypatch, tmp_path
):
    import pandas as pd
    from openhands.core.config import LLMConfig
    from evaluation.benchmarks.swe_bench import hidden_run_infer as hidden

    monkeypatch.setattr(hidden, "SEAL_GOLD_LEAK", seal)
    monkeypatch.setattr(hidden, "HIDDEN_AGENT_RECOVERY", recovery)
    monkeypatch.setattr(hidden, "HIDDEN_AGENT_NETWORK_ISOLATION", True)
    monkeypatch.setattr(hidden, "USE_INSTANCE_IMAGE", True)
    monkeypatch.setenv("DOCKER_RUNTIME_KWARGS", '{"mem_limit":"6g"}')
    metadata = SimpleNamespace(
        agent_class="CodeActAgent",
        max_iterations=70,
        llm_config=LLMConfig(),
        eval_output_dir=str(tmp_path),
    )
    config = hidden.get_config(
        pd.Series({"instance_id": "astropy__astropy-13033"}), metadata
    )
    assert config.sandbox.docker_runtime_kwargs["cap_drop"] == ["NET_ADMIN", "NET_RAW"]
    assert config.sandbox.docker_runtime_kwargs["mem_limit"] == "6g"
