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
        # *.github.com subdomains collapse to the apex (gh parity) so they never
        # fall through to the GHE-Server /api/v3 branch.
        ("www.github.com", "github.com"),
        ("gist.github.com", "github.com"),
        ("api.github.com", "github.com"),
        ("https://www.github.com/owner/repo", "github.com"),
        # a port on a GitHub-hosted host is dropped (it only exists for GHES).
        ("github.com:443", "github.com"),
        # data-residency tenant: bare tenant kept, sub-subdomains collapse to it.
        ("octocorp.ghe.com", "octocorp.ghe.com"),
        ("sub.octocorp.ghe.com", "octocorp.ghe.com"),
        ("api.octocorp.ghe.com", "octocorp.ghe.com"),
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


def test_api_root_survives_subdomained_input():
    """End-to-end: inputs that used to misroute now resolve to the right base."""
    # *.github.com no longer falls through to /api/v3, and api.github.com does
    # not double up into https://api.github.com/api/v3.
    assert githost.api_root(githost.normalize_host("www.github.com")) == "https://api.github.com"
    assert githost.api_root(githost.normalize_host("api.github.com")) == "https://api.github.com"
    # tenant sub-subdomains no longer over-prefix into api.sub.octocorp.ghe.com.
    assert (
        githost.api_root(githost.normalize_host("sub.octocorp.ghe.com"))
        == "https://api.octocorp.ghe.com"
    )
