from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.dependency(scope="module")

# TODO: add a separate substituted-path scenario and a separate
# eval_store=auto + build_store=temp scenario. The shared fixture intentionally
# uses a single temp store for eval and build until that path is robust.


@pytest.mark.dependency(name="scenario:store")
async def test_scenario_starts_with_empty_temporary_store(pynix_store_scenario):
    scenario = pynix_store_scenario

    assert scenario.store_url.startswith("local?root=")
    assert scenario.store_root.exists()
    assert scenario.hello_path is None
    assert scenario.text_path is None
    assert scenario.local_log_path is None


@pytest.mark.dependency(name="scenario:add-file", depends=["scenario:store"])
async def test_scenario_adds_file_with_pynix(pynix_store_scenario, request: pytest.FixtureRequest):
    scenario = pynix_store_scenario

    text_path = await scenario.add_text_file("scenario-message\n", test_name=request.node.nodeid)

    assert text_path.startswith("/nix/store/")
    assert scenario.physical_path(text_path).read_text() == "scenario-message\n"


@pytest.mark.dependency(name="scenario:build-hello", depends=["scenario:store"])
async def test_scenario_adds_local_package_with_pynix(pynix_store_scenario, request: pytest.FixtureRequest):
    scenario = pynix_store_scenario

    hello_path = await scenario.build_hello(test_name=request.node.nodeid)

    assert hello_path.startswith("/nix/store/")
    assert (scenario.physical_path(hello_path) / "bin" / "hello").exists()


@pytest.mark.dependency(name="scenario:build-local-log", depends=["scenario:build-hello"])
async def test_scenario_builds_local_derivation_and_forwards_logs(
    pynix_store_scenario,
    request: pytest.FixtureRequest,
):
    scenario = pynix_store_scenario

    log_path = await scenario.build_local_log_derivation(test_name=request.node.nodeid)

    assert log_path.startswith("/nix/store/")
    assert scenario.physical_path(log_path).read_text() == "log-output\n"
    assert scenario.last_logs is not None
    assert any(entry.get("event") == "nix build log" and entry.get("message") == "pynix-log-line" for entry in scenario.last_logs)


@pytest.mark.dependency(
    name="scenario:read-log",
    depends=["scenario:build-local-log"],
)
async def test_scenario_reads_local_build_log_with_pynix(pynix_store_scenario, request: pytest.FixtureRequest):
    scenario = pynix_store_scenario

    stdout, _stderr = await scenario.run_pynix(
        ["log", scenario.require_local_log_path(), "--store", scenario.store_url],
        test_name=request.node.nodeid,
    )

    assert "pynix-log-line" in stdout


@pytest.mark.dependency(
    name="scenario:query-store",
    depends=["scenario:add-file", "scenario:build-hello"],
)
async def test_scenario_reuses_paths_for_follow_up_store_queries(
    pynix_store_scenario,
    request: pytest.FixtureRequest,
):
    scenario = pynix_store_scenario

    data = await scenario.run_pynix_json(
        ["store", "is-valid-path", scenario.require_text_path(), "--store", scenario.store_url],
        test_name=request.node.nodeid,
    )
    assert isinstance(data, dict)
    assert data == {"path": scenario.require_text_path(), "valid": True}

    stdout, _stderr = await scenario.run_pynix(
        ["store", "ls", scenario.require_hello_path(), "--json", "--store", scenario.store_url],
        test_name=request.node.nodeid,
    )
    entries = json.loads(stdout)
    assert {"name": "bin", "type": "directory"} in entries["entries"]
