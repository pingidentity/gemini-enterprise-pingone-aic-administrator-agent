"""Build with the pinned SDK, then load only the extracted deployment artifacts."""

import io
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import vertexai
from google.auth.credentials import AnonymousCredentials
from vertexai.agent_engines import _agent_engines as sdk

from deployment import deploy


class ArtifactBucket:
    """Replace Cloud Storage I/O without replacing SDK packing/serialization."""

    name = "offline-packaging-test"

    def __init__(self):
        self.contents = {}

    def blob(self, name):
        contents = self.contents

        class Blob:
            def upload_from_string(self, data):
                contents[name] = data.encode() if isinstance(data, str) else data

            @contextmanager
            def open(self, mode):
                with io.BytesIO(contents[name] if mode == "rb" else b"") as stream:
                    yield stream
                    if mode == "wb":
                        contents[name] = stream.getvalue()

        return Blob()


@pytest.mark.parametrize("operation", ["create", "update"])
def test_deployment_archive_loads_in_a_fresh_runtime(monkeypatch, tmp_path, operation):
    bucket = ArtifactBucket()
    resource = "projects/offline-project/locations/us-central1/reasoningEngines/123"
    monkeypatch.setattr(sdk, "_get_gcs_bucket", lambda **kwargs: bucket)
    monkeypatch.setattr(deploy, "_runtime_env", dict)
    monkeypatch.setattr(deploy, "_confirm", lambda *args: None)

    def initialize(**kwargs):
        vertexai.init(
            project="offline-project",
            location="us-central1",
            credentials=AnonymousCredentials(),
        )

    monkeypatch.setattr(deploy, "_init", initialize)

    def package(agent, **kwargs):
        sdk._prepare(
            agent_engine=agent.clone(),
            requirements=kwargs["requirements"],
            extra_packages=kwargs["extra_packages"],
            project="offline-project",
            location="us-central1",
            staging_bucket="gs://offline-packaging-test",
            gcs_dir_name="bundle",
        )
        return SimpleNamespace(resource_name=resource)

    monkeypatch.setattr(deploy.agent_engines, "create", package)
    monkeypatch.setattr(
        deploy.agent_engines,
        "update",
        lambda *, resource_name, agent_engine, **kwargs: package(
            agent_engine, **kwargs
        ),
    )
    exports = []
    monkeypatch.setattr(
        deploy,
        "export_agent_card",
        lambda resource_name, output_dir: exports.append((Path.cwd(), output_dir)),
    )
    # Reproduce callers importing the deployer from outside the repository.
    monkeypatch.chdir(tmp_path)
    if operation == "create":
        deploy.create(Path("registration"))
    else:
        deploy.update(resource, Path("registration"))
    assert Path.cwd() == tmp_path
    assert exports == [(tmp_path, Path("registration"))]

    runtime = tmp_path / "managed-runtime"
    runtime.mkdir()
    (runtime / "agent.pkl").write_bytes(bucket.contents[f"bundle/{sdk._BLOB_FILENAME}"])
    with tarfile.open(
        fileobj=io.BytesIO(bucket.contents[f"bundle/{sdk._EXTRA_PACKAGES_FILE}"]),
        mode="r:gz",
    ) as archive:
        names = archive.getnames()
        archive.extractall(runtime, filter="data")

    probe = """
import pathlib, sys, cloudpickle
sys.path.insert(0, str(pathlib.Path.cwd()))
agent = cloudpickle.loads(pathlib.Path('agent.pkl').read_bytes())
from ping_admin_agent.runtime import PingAdminA2aAgent
from ping_admin_agent.ui_protocol import SUPPORTED_CATALOG_IDS
from ping_admin_agent.ui_results import RuntimeResult
from ping_admin_agent.ui_views import render_result
import ping_admin_agent
assert isinstance(agent, PingAdminA2aAgent)
assert agent.agent_executor is None
package = pathlib.Path(ping_admin_agent.__file__).parent
assert package.parent == pathlib.Path.cwd()
assert (package / 'assets/ping-identity-logo.png').is_file()
for catalog in SUPPORTED_CATALOG_IDS:
    assert render_result(RuntimeResult(), 'packaging-check', catalog).messages()
print('Loaded packaged runtime, both UI catalogs, and branding assets')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=runtime,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ping_admin_agent/__init__.py" in names
    assert all(
        name == "ping_admin_agent" or name.startswith("ping_admin_agent/")
        for name in names
    )


@pytest.mark.parametrize("operation", ["create", "update"])
def test_deployment_failure_restores_callers_working_directory(
    monkeypatch, tmp_path, operation
):
    monkeypatch.setattr(deploy, "_init", lambda **kwargs: None)
    monkeypatch.setattr(deploy, "_confirm", lambda *args: None)
    monkeypatch.setattr(deploy, "_runtime_env", dict)
    monkeypatch.setattr(deploy, "_wrapped_app", lambda: object())

    def fail(*args, **kwargs):
        raise RuntimeError("Simulated deployment failure")

    monkeypatch.setattr(deploy.agent_engines, operation, fail)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="Simulated deployment failure"):
        if operation == "create":
            deploy.create()
        else:
            deploy.update("projects/offline/locations/us-central1/reasoningEngines/123")
    assert Path.cwd() == tmp_path
