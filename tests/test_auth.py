import pytest
import responses

from mooring import auth, paths


def _device(host="github.com"):
    return auth.DeviceCode(
        device_code="dev123",
        user_code="ABCD-1234",
        verification_uri=f"https://{host}/login/device",
        interval=5,
        expires_in=900,
        host=host,
    )


@responses.activate
def test_start_device_flow():
    responses.add(
        responses.POST,
        auth.device_code_url(),
        json={
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
            "expires_in": 900,
        },
    )
    device = auth.start_device_flow("client123")
    assert device.user_code == "ABCD-1234"
    assert device.host == "github.com"
    assert "client123" in responses.calls[0].request.body


@responses.activate
def test_device_flow_on_enterprise_host():
    responses.add(
        responses.POST,
        "https://ghe.example/login/device/code",
        json={
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://ghe.example/login/device",
            "interval": 5,
            "expires_in": 900,
        },
    )
    responses.add(
        responses.POST,
        "https://ghe.example/login/oauth/access_token",
        json={"access_token": "gho_ghe"},
    )
    device = auth.start_device_flow("client123", host="ghe.example")
    assert device.host == "ghe.example"
    # polling derives the token URL from the device's host
    assert auth.poll_once("client123", device).token == "gho_ghe"


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def test_device_flow_hint_default_host_suggests_enterprise():
    exc = RuntimeError("boom")
    exc.response = _Resp(404)
    msg = auth.device_flow_hint("github.com", exc)
    assert "github.com" in msg
    assert "404" in msg
    assert "GitHub Enterprise" in msg
    assert "--host" in msg


def test_device_flow_hint_enterprise_host_no_suggestion():
    exc = RuntimeError("boom")
    exc.response = _Resp(404)
    msg = auth.device_flow_hint("ghe.example", exc)
    assert "ghe.example" in msg
    assert "404" in msg
    assert "GitHub Enterprise" not in msg


def test_device_flow_hint_without_status_uses_message():
    msg = auth.device_flow_hint("ghe.example", RuntimeError("connection refused"))
    assert "connection refused" in msg


@responses.activate
def test_poll_until_token_with_slow_down():
    responses.add(responses.POST, auth.token_url(), json={"error": "authorization_pending"})
    responses.add(
        responses.POST, auth.token_url(), json={"error": "slow_down", "interval": 10}
    )
    responses.add(responses.POST, auth.token_url(), json={"access_token": "gho_token"})

    sleeps = []
    token = auth.poll_for_token(
        "client123", _device(), sleep=sleeps.append, clock=lambda: 0.0
    )
    assert token == "gho_token"
    assert sleeps == [5, 10]  # slow_down raised the interval


@responses.activate
@pytest.mark.parametrize(
    ("error", "match"),
    [("expired_token", "expired"), ("access_denied", "cancelled")],
)
def test_poll_terminal_errors(error, match):
    responses.add(responses.POST, auth.token_url(), json={"error": error})
    with pytest.raises(auth.AuthError, match=match):
        auth.poll_for_token("client123", _device(), sleep=lambda s: None, clock=lambda: 0.0)


def test_poll_for_token_times_out():
    clock_values = iter([0.0, 1000.0])
    with pytest.raises(auth.AuthError, match="expired"):
        auth.poll_for_token(
            "client123", _device(), sleep=lambda s: None, clock=lambda: next(clock_values)
        )


def test_env_token_takes_precedence():
    assert auth.get_token(env={"MOORING_TOKEN": "gho_env"}) == "gho_env"
    assert auth.get_token(env={"MOORING_TOKEN": "gho_env"}, host="ghe.example") == "gho_env"


# -- host-keyed storage (forced onto the file fallback) ------------------------


@pytest.fixture
def file_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    monkeypatch.setattr(paths, "user_config_dir", lambda: tmp_path)
    return tmp_path


def test_tokens_are_stored_per_host(file_tokens, capsys):
    auth.save_token("gho_public")
    auth.save_token("gho_ghe", host="ghe.example")
    assert auth.get_token(env={}) == "gho_public"
    assert auth.get_token(env={}, host="ghe.example") == "gho_ghe"
    # default host keeps the pre-0.2 filename so existing logins survive
    assert (file_tokens / "token").read_text("utf-8") == "gho_public"
    assert (file_tokens / "token-ghe.example").read_text("utf-8") == "gho_ghe"


def test_token_for_other_host_is_invisible(file_tokens, capsys):
    auth.save_token("gho_public")
    assert auth.get_token(env={}, host="ghe.example") is None
    auth.delete_token(host="ghe.example")  # no-op, must not touch the github.com token
    assert auth.get_token(env={}) == "gho_public"


def test_host_with_port_uses_safe_filename(file_tokens, capsys):
    auth.save_token("gho_port", host="ghe.example:8443")
    assert auth.get_token(env={}, host="ghe.example:8443") == "gho_port"
    assert (file_tokens / "token-ghe.example_8443").is_file()
