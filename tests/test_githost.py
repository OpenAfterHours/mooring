import pytest

from mooring import githost


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("github.com", "github.com"),
        ("ghe.service.group", "ghe.service.group"),
        ("GHE.Service.Group", "ghe.service.group"),
        ("https://ghe.service.group", "ghe.service.group"),
        ("https://ghe.service.group/", "ghe.service.group"),
        ("https://ghe.service.group/some/path", "ghe.service.group"),
        ("http://ghe.service.group:8443/", "ghe.service.group:8443"),
        ("ghe.service.group:8443", "ghe.service.group:8443"),
        ("", "github.com"),
        ("   ", "github.com"),
    ],
)
def test_normalize_host(value, expected):
    assert githost.normalize_host(value) == expected


@pytest.mark.parametrize("value", ["not a host", "ghe..service", "-ghe.com", "ghe.com:80x"])
def test_normalize_host_rejects_garbage(value):
    with pytest.raises(ValueError, match="Not a valid GitHub host"):
        githost.normalize_host(value)


def test_api_root_three_host_classes():
    assert githost.api_root("github.com") == "https://api.github.com"
    assert githost.api_root("corp.ghe.com") == "https://api.corp.ghe.com"
    assert githost.api_root("ghe.service.group") == "https://ghe.service.group/api/v3"


def test_web_root():
    assert githost.web_root("github.com") == "https://github.com"
    assert githost.web_root("corp.ghe.com") == "https://corp.ghe.com"
    assert githost.web_root("ghe.service.group") == "https://ghe.service.group"
