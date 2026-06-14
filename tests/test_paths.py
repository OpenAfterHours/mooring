"""Workspace-location hints (cloud-sync folder detection)."""

from pathlib import Path

import pytest

from mooring import paths


@pytest.mark.parametrize(
    ("workspace", "provider"),
    [
        ("C:/Users/phil/OneDrive/Documents/mooring/acme/nbs", "OneDrive"),
        ("C:/Users/phil/OneDrive - Contoso/Documents/mooring/nbs", "OneDrive"),
        ("C:/Users/phil/Dropbox/mooring/nbs", "Dropbox"),
        ("G:/My Drive/mooring/nbs", "Google Drive"),
        ("C:/Users/phil/Box/mooring/nbs", "Box"),
        ("C:/Users/phil/iCloudDrive/mooring/nbs", "iCloud"),
        # local paths and lookalikes must NOT trip the heuristic
        ("C:/Users/phil/Documents/mooring/nbs", ""),
        ("/home/phil/projects/sandbox/mooring/nbs", ""),  # 'sandbox' != 'box'
        ("C:/dev/toolbox/mooring/nbs", ""),
    ],
)
def test_synced_folder_provider(workspace, provider):
    assert paths.synced_folder_provider(Path(workspace)) == provider


def test_synced_folder_hint_text():
    hint = paths.synced_folder_hint(Path("C:/Users/phil/OneDrive/Documents/mooring/nbs"))
    assert "OneDrive" in hint
    assert "MOORING_WORKSPACE" in hint
    assert paths.synced_folder_hint(Path("C:/dev/mooring/nbs")) == ""
