import asyncio
from types import SimpleNamespace

from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def make_container():
    return SimpleNamespace(
        attrs={
            "NetworkSettings": {
                "IPAddress": "",
                "Networks": {
                    "people-agent-network": {"IPAddress": "172.18.0.7"}
                },
                "Ports": {
                    "8080/tcp": [{"HostPort": "60674"}],
                    "9222/tcp": [{"HostPort": "60675"}],
                    "5901/tcp": [{"HostPort": "60676"}],
                },
            }
        }
    )


def close_sandbox(sandbox: DockerSandbox) -> None:
    asyncio.run(sandbox.client.aclose())


def test_from_container_uses_docker_network_inside_container(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda path: path == "/.dockerenv")

    sandbox = DockerSandbox._from_container(make_container(), "sandbox-test")
    try:
        assert sandbox._base_url == "http://172.18.0.7:8080"
        assert sandbox.cdp_url == "http://172.18.0.7:9222"
        assert sandbox.vnc_url == "ws://172.18.0.7:5901"
    finally:
        close_sandbox(sandbox)


def test_from_container_uses_published_ports_on_host(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda path: False)

    sandbox = DockerSandbox._from_container(make_container(), "sandbox-test")
    try:
        assert sandbox._base_url == "http://127.0.0.1:60674"
        assert sandbox.cdp_url == "http://127.0.0.1:60675"
        assert sandbox.vnc_url == "ws://127.0.0.1:60676"
    finally:
        close_sandbox(sandbox)
