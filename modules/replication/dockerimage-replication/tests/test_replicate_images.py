# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import mock_open, patch

import pytest


@pytest.fixture
def mock_environment_variables(monkeypatch):
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_PARTITION", "aws")
    monkeypatch.setenv("SEEDFARMER_PARAMETER_HELM_REPO_SECRET_NAME", "secret_name")
    monkeypatch.setenv("SEEDFARMER_PARAMETER_HELM_REPO_SECRET_KEY", "secret_key")


@patch("replication.utils.run_command")
@patch("replication.logging.logger")
def test_pull_and_push_image(mock_logger, mock_run_command):
    from replicate_images import pull_and_push_image

    # Mock run_command responses
    mock_run_command.side_effect = [True, True, True, True, True]

    # Call the function
    result = pull_and_push_image(
        src_repo="source-repo",
        src="source-image:latest",
        target_ecr_tag="123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image:latest",
        username="testuser",
        password="testpassword",
    )

    # Assertions
    assert result is True
    mock_run_command.assert_any_call(
        ["docker", "login", "-u", "testuser", "-p", "testpassword", "source-repo"],
        shell=False,
    )
    mock_run_command.assert_any_call(["docker", "pull", "source-image:latest"], shell=False)
    mock_run_command.assert_any_call(
        [
            "docker",
            "tag",
            "source-image:latest",
            "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image:latest",
        ],
        shell=False,
    )
    mock_run_command.assert_any_call(
        [
            "docker",
            "push",
            "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image:latest",
        ],
        shell=False,
    )
    mock_run_command.assert_any_call(["docker", "rmi", "source-image:latest"], shell=False)


@patch("replication.ecr.ecr_utils.ECRUtils")
@patch("replication.utils.run_command")
@patch("replication.logging.logger")
def test_create(mock_logger, mock_run_command, MockECRUtils, mock_environment_variables):
    from replicate_images import create

    # Mock ECRUtils methods
    ecr_utils_mock = MockECRUtils.return_value
    ecr_utils_mock.create_repository.return_value = True
    ecr_utils_mock.image_exists.return_value = False

    # Mock pull_and_push_image
    with patch("replicate_images.pull_and_push_image") as mock_pull_and_push:
        mock_pull_and_push.return_value = True

        # Mock input data
        image_repl = {
            "src": "source-image:latest",
            "target": "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image:latest",
        }

        # Call the function
        create(ecr_utils_mock, image_repl, "testuser", "testpassword")

        # Assertions
        ecr_utils_mock.create_repository.assert_called_once_with(
            "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image"
        )
        ecr_utils_mock.image_exists.assert_called_once_with(
            "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image", "latest"
        )
        mock_pull_and_push.assert_called_once_with(
            "source-image",
            "source-image:latest",
            "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image:latest",
            "testuser",
            "testpassword",
        )


@patch("replicate_images.ECRUtils")
@patch("boto3.client")  # Mock boto3 client
@patch("os.path.isfile")
@patch(
    "builtins.open",
    new_callable=mock_open,
    read_data="""
        [
            {"src":"source-image:latest",
            "target": "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image:latest"
            }
        ]
        """,
)
@patch("replication.utils.get_credentials")
@patch("replicate_images.export_results")
@patch("replication.logging.logger")
def test_main(
    mock_logger,
    mock_export_results,
    mock_get_credentials,
    mock_open,
    mock_isfile,
    mock_boto3_client,
    MockECRUtils,
    mock_environment_variables,
):
    from replicate_images import main

    # Mock file existence
    mock_isfile.return_value = True

    # Mock boto3 client behavior
    mock_ecr_client = mock_boto3_client.return_value
    mock_ecr_client.describe_repositories.return_value = {"repositories": []}

    # Mock ECRUtils methods
    ecr_utils_mock = MockECRUtils.return_value
    ecr_utils_mock.login_to_ecr.return_value = True
    ecr_utils_mock.image_exists.return_value = False

    # Mock get_credentials
    mock_get_credentials.return_value = ("testuser", "testpassword")

    # Call the main function
    main()

    # Assertions
    MockECRUtils.assert_called_once_with(None, None, "amazonaws.com")
    ecr_utils_mock.login_to_ecr.assert_called_once_with(type="docker")
    # print("Export Results Mock Calls:", mock_export_results.mock_calls)
    mock_export_results.assert_any_call(
        "Successfully replicated images",
        [{"src": "source-image:latest", "target": "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image:latest"}],
    )
    mock_export_results.assert_any_call(
        "FAILED replicated image",
        [{"src": "source-image:latest", "target": "123456789012.dkr.ecr.us-west-2.amazonaws.com/target-image:latest"}],
    )
