"""Deploy template renderer (SPLIT_PLAN §5.2). No backends involved."""
from __future__ import annotations

from pathlib import Path

from pilgrim.tools.render_deploy import main

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "pilgrim" / "deploy"

EDGE_VALUES = {
    "EDGE_LISTEN": "127.0.0.1:18080",
    "WEB_ROOT": "/cfg/web",
    "CACHE_DIR": "/cfg/cache",
    "MEDIA_CACHE_MAX": "1g",
    "ORIGIN_SCHEME": "http",
    "ORIGIN_HOSTPORT": "127.0.0.1:18090",
    "ORIGIN_HOST": "127.0.0.1",
    "REALIP_INCLUDE": "/cfg/realip.conf",
    "CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
    "EXTRA_ALLOW_INCLUDE": "/cfg/extra-none.conf",
}

GATEWAY_VALUES = {
    "GATEWAY_LISTEN": "127.0.0.1:18090",
    "ORIGIN_PUBLIC_HOSTPORT": "127.0.0.1:5000",
    "TRUSTED_EDGE_NET": "127.0.0.1",
    "EXTRA_ALLOW_INCLUDE": "/cfg/extra-none.conf",
}


def _render(tmp_path: Path, template: Path, values: dict[str, str]) -> tuple[int, Path]:
    out = tmp_path / "rendered.conf"
    argv = [str(template), str(out)]
    for name, value in values.items():
        argv += ["--set", f"{name}={value}"]
    return main(argv), out


def test_replaces_placeholders(tmp_path):
    t = tmp_path / "t.conf"
    t.write_text("listen @@ADDR@@;\nserver_name @@NAME@@;\n")
    code, out = _render(tmp_path, t, {"ADDR": "127.0.0.1:8080", "NAME": "x"})
    assert code == 0
    assert out.read_text() == "listen 127.0.0.1:8080;\nserver_name x;\n"


def test_missing_value_fails_and_writes_nothing(tmp_path):
    t = tmp_path / "t.conf"
    t.write_text("a @@A@@ b @@B@@\n")
    code, out = _render(tmp_path, t, {"A": "1"})
    assert code != 0
    assert not out.exists()


def test_unknown_set_name_fails(tmp_path):
    t = tmp_path / "t.conf"
    t.write_text("@@A@@\n")
    code, out = _render(tmp_path, t, {"A": "1", "NOPE": "2"})
    assert code != 0
    assert not out.exists()


def test_value_cannot_smuggle_a_placeholder(tmp_path):
    t = tmp_path / "t.conf"
    t.write_text("@@A@@\n")
    code, out = _render(tmp_path, t, {"A": "@@B@@"})
    assert code != 0
    assert not out.exists()


def test_nginx_variables_pass_through_unchanged(tmp_path):
    t = tmp_path / "t.conf"
    t.write_text("set $client $remote_addr;\nlisten @@ADDR@@;\n")
    code, out = _render(tmp_path, t, {"ADDR": ":80"})
    assert code == 0
    text = out.read_text()
    assert "$remote_addr" in text
    assert "$client" in text


def test_edge_template_renders_clean(tmp_path):
    code, out = _render(tmp_path, DEPLOY / "edge" / "nginx.conf.template",
                        EDGE_VALUES)
    assert code == 0
    text = out.read_text()
    assert "@@" not in text
    assert "listen 127.0.0.1:18080;" in text
    assert "$remote_addr" in text
    assert "location = /edge-health" in text


def test_gateway_template_renders_clean(tmp_path):
    code, out = _render(tmp_path, DEPLOY / "gateway" / "nginx.conf.template",
                        GATEWAY_VALUES)
    assert code == 0
    text = out.read_text()
    assert "@@" not in text
    assert "listen 127.0.0.1:18090;" in text
    assert "allow 127.0.0.1;" in text
    assert "$http_x_real_ip" in text
