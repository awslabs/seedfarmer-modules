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


# Mock logger to avoid logging during tests
@patch("replication.logging.logger")
def test_process_chart(mock_logger, mock_environment_variables):
    # Mock data
    chart_key = "test-chart"
    chart_data = {
        "helm": {
            "name": "example",
            "version": "1.0.0",
            "srcRepository": "oci://source-repo",
            "repository": "oci://target-repo",
        }
    }

    # Mock ECRUtils methods and constructor
    with patch("replicate_charts.ECRUtils") as MockECRUtils:
        ecr_utils_mock = MockECRUtils.return_value
        ecr_utils_mock.image_exists.return_value = False
        ecr_utils_mock.create_repository.return_value = True
        ecr_utils_mock.login_to_ecr.return_value = True

        # Mock run_command
        with patch("replicate_charts.run_command") as mock_run_command, patch(
            "replicate_charts.get_credentials"
        ) as mock_get_credentials:
            mock_run_command.return_value = True
            mock_get_credentials.return_value = ("username", "password")

            # Call process_chart
            from replicate_charts import process_chart

            process_chart(ecr_utils_mock, chart_key, chart_data)

            # Assertions
            ecr_utils_mock.image_exists.assert_called_once_with("target-repo", "1.0.0")
            ecr_utils_mock.create_repository.assert_called_once_with("target-repo")
            mock_run_command.assert_any_call(
                "helm registry login oci://source-repo --username username --password password"
            )
            mock_run_command.assert_any_call("helm pull test-chart/example --version 1.0.0")
            mock_run_command.assert_any_call("helm push example-1.0.0.tgz oci://target-repo")
            mock_run_command.assert_any_call("rm -f example-1.0.0.tgz")

            # Check logs
            mock_logger.info.assert_any_call("Processing chart: test-chart ")
            mock_logger.info.assert_any_call("Pushing chart: example-1.0.0.tgz to oci://target-repo")


@patch("replicate_charts.ECRUtils")
@patch("boto3.client")
@patch("os.path.isfile")
@patch(
    "builtins.open",
    new_callable=mock_open,
    read_data="""
    {"charts":
        {"test-chart":
            {"helm":
                {
                    "name": "example",
                    "version": "1.0.0",
                    "srcRepository": "oci://source-repo",
                    "repository": "oci://target-repo"
                }
            }
        }
    }
    """,
)
@patch("replicate_charts.get_credentials")
@patch("replicate_charts.export_results")
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
    from replicate_charts import main

    # Mock file existence
    mock_isfile.return_value = True

    # Mock boto3 client behavior
    mock_ecr_client = mock_boto3_client.return_value
    mock_ecr_client.describe_repositories.return_value = {"repositories": []}

    # Mock ECRUtils methods
    ecr_utils_mock = MockECRUtils.return_value
    ecr_utils_mock.login_to_ecr.return_value = True
    ecr_utils_mock.image_exists.side_effect = lambda repo_name, version: repo_name != "target-repo"
    mock_get_credentials.return_value = ("testuser", "testpassword")

    main()

    # Debugging output
    print("Export Results Mock Calls:", mock_export_results.mock_calls)

    # Assertions
    ecr_utils_mock.login_to_ecr.assert_called_once_with("helm")
    # mock_export_results.assert_any_call(
    #     "Successfully replicated charts",[]
    # )
    # mock_export_results.assert_any_call("FAILED replicated charts", ['example'])
