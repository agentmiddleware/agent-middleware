"""The unsupported standalone generator refuses before discovery or writes."""

import sys

import pytest

from b2a_sdk import mcp


@pytest.mark.parametrize("services", [None, [], [{"name": "partner.search"}]])
@pytest.mark.parametrize("existing", [False, True])
def test_standalone_refusal_has_no_effects(tmp_path, monkeypatch, services, existing):
    def forbidden(*args, **kwargs):
        pytest.fail("retired generator must not discover tools")

    monkeypatch.setattr(mcp, "generate_manifest", forbidden)
    output = tmp_path / "server.py"
    if existing:
        output.write_text("keep existing file")
    with pytest.raises(RuntimeError, match="retired.*governed.*POST /mcp/messages"):
        mcp.generate_standalone_server(str(output), services=services)
    assert output.read_text() == "keep existing file" if existing else not output.exists()


def test_cli_refuses_cleanly_before_discovery(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("retired CLI must not discover tools")

    monkeypatch.setattr(mcp, "generate_manifest", forbidden)
    output = tmp_path / "server.py"
    monkeypatch.setattr(
        sys,
        "argv",
        ["mcp", "standalone", "--output", str(output), "--api-url", "https://unused.invalid"],
    )
    with pytest.raises(SystemExit) as exc:
        mcp.main()
    assert exc.value.code == 2
    assert "retired" in capsys.readouterr().err
    assert not output.exists()
