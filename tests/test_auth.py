import pytest
import responses

from mooring import auth


def _device():
    return auth.DeviceCode(
        device_code="dev123",
        user_code="ABCD-1234",
        verification_uri="https://github.com/login/device",
        interval=5,
        expires_in=900,
    )


@responses.activate
def test_start_device_flow():
    responses.add(
        responses.POST,
        auth.DEVICE_CODE_URL,
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
    assert "client123" in responses.calls[0].request.body


@responses.activate
def test_poll_until_token_with_slow_down():
    responses.add(responses.POST, auth.TOKEN_URL, json={"error": "authorization_pending"})
    responses.add(
        responses.POST, auth.TOKEN_URL, json={"error": "slow_down", "interval": 10}
    )
    responses.add(responses.POST, auth.TOKEN_URL, json={"access_token": "gho_token"})

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
    responses.add(responses.POST, auth.TOKEN_URL, json={"error": error})
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
