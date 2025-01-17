# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="function")
def mock_env_vars(request):
    env_vars = {
        "SEEDFARMER_PARAMETER_HELM_DISTRO_URL": "https://test.com/helm.tar.gz",
        "SEEDFARMER_PARAMETER_HELM_DISTRO_SECRET_NAME": "test_secret",
        "SEEDFARMER_PARAMETER_HELM_DISTRO_SECRET_KEY": "test_key",
    }
    if request.node.get_closest_marker("null_url"):
        del env_vars["SEEDFARMER_PARAMETER_HELM_DISTRO_SECRET_NAME"]
    with patch.dict("os.environ", env_vars, clear=True):
        yield


@pytest.fixture(scope="function")
def mock_credentials(request):
    with patch("replication.utils.get_credentials") as mock_get_credentials:
        if request.param == "no_creds":
            mock_get_credentials.return_value = (None, None)
        else:
            mock_get_credentials.return_value = ("user", "pwd")
        yield mock_get_credentials


@pytest.fixture(scope="function", autouse=False)
def mock_requests_get():
    with patch("requests.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [b"test_content"]
        mock_get.return_value = mock_response
        yield mock_get


@pytest.fixture(scope="function", autouse=False)
def mock_tarfile():
    with patch("tarfile.open") as mock_tar:
        mock_tar_instance = MagicMock()
        mock_tar.return_value.__enter__.return_value = mock_tar_instance
        yield mock_tar_instance


@pytest.fixture(scope="function", autouse=False)
def mock_shutil():
    with patch("shutil.move") as mock_move, patch("shutil.rmtree") as mock_rmtree:
        yield mock_move, mock_rmtree


@pytest.fixture(scope="function", autouse=False)
def mock_os():
    with patch("os.remove") as mock_remove:
        yield mock_remove


@pytest.fixture(scope="function", autouse=False)
def mock_logger():
    with patch("replication.logging.logger") as mock_logger:
        yield mock_logger


@pytest.mark.parametrize("mock_credentials", ["no_creds", "creds"], indirect=True)
def test_install_helm_success(
    mock_env_vars, mock_credentials, mock_requests_get, mock_tarfile, mock_shutil, mock_os, mock_logger
):
    from install_helm import install_helm

    # Call the function under test
    install_helm()

    # Validate HTTP request
    if mock_credentials == "no_creds":
        mock_requests_get.assert_called_once_with("https://test.com/helm.tar.gz", stream=True, auth=(("user", "pwd")))
    else:
        mock_requests_get.assert_called_once_with("https://test.com/helm.tar.gz", stream=True)

    # Validate tar extraction
    mock_tarfile.extractall.assert_called_once()

    # Validate file move
    mock_shutil[0].assert_called_once_with("linux-amd64/helm", "/usr/local/bin/helm")

    # Validate cleanup
    mock_shutil[1].assert_called_once_with("linux-amd64")  # shutil.rmtree
    mock_os.assert_called_once_with("helm.tar.gz")  # os.remove


@pytest.mark.parametrize("mock_credentials", ["no_creds", "creds"], indirect=True)
@pytest.mark.null_url
def test_install_helm_no_url(
    mock_env_vars, mock_credentials, mock_requests_get, mock_tarfile, mock_shutil, mock_os, mock_logger
):
    from install_helm import install_helm

    # Call the function under test
    install_helm()

    # Validate HTTP request and other mocks
    mock_requests_get.assert_called_once_with("https://test.com/helm.tar.gz", stream=True)
    mock_tarfile.extractall.assert_called_once()
    mock_shutil[0].assert_called_once_with("linux-amd64/helm", "/usr/local/bin/helm")
    mock_shutil[1].assert_called_once_with("linux-amd64")
    mock_os.assert_called_once_with("helm.tar.gz")
