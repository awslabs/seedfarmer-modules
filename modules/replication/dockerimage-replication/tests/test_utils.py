# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from unittest.mock import MagicMock, Mock, patch

import pytest
from botocore.exceptions import ClientError

from replication.logging import boto3_logger, logger
from replication.utils import PWD, USER, deep_merge, get_credentials, mask_sensitive_data, run_command


@pytest.fixture
def mock_boto3_session():
    with patch("boto3.session.Session") as mock_session:
        mock_client = Mock()
        mock_session.return_value.client.return_value = mock_client
        yield mock_client


def test_deep_merge():
    d1 = {"a": 1, "b": 2}
    d2 = {"b": 3, "c": 4}
    d3 = {"c": 5, "d": 6}

    result = deep_merge(d1, d2, d3)
    assert result == {"a": 1, "b": 3, "c": 5, "d": 6}


def test_logger_main(caplog):
    logger.info("first message")
    assert "first message" in caplog.text
    assert "INFO" in caplog.text


def test_logger_boto3(caplog):
    boto3_logger.warning("first message")
    assert "first message" in caplog.text
    assert "WARN" in caplog.text


def test_get_credentials_empty_secret_name():
    result = get_credentials("")
    assert result == (None, None)


def test_get_credentials_valid(mock_boto3_session):
    secret_name = "test-secret"
    secret_key = "test-key"
    mock_response = {"SecretString": json.dumps({"test-key": {USER: "test-user", PWD: "test-password"}})}
    mock_boto3_session.get_secret_value.return_value = mock_response

    result = get_credentials(secret_name, secret_key)

    assert result == ("test-user", "test-password")
    mock_boto3_session.get_secret_value.assert_called_once_with(SecretId=secret_name)


def test_get_credentials_without_secret_key(mock_boto3_session):
    secret_name = "test-secret"
    mock_response = {"SecretString": json.dumps({USER: "test-user", PWD: "test-password"})}
    mock_boto3_session.get_secret_value.return_value = mock_response

    result = get_credentials(secret_name)

    assert result == ("test-user", "test-password")
    mock_boto3_session.get_secret_value.assert_called_once_with(SecretId=secret_name)


def test_get_credentials_missing_user_pwd(mock_boto3_session):
    secret_name = "test-secret"
    secret_key = "test-key"
    mock_response = {"SecretString": json.dumps({"test-key": {"OTHER_KEY": "value"}})}
    mock_boto3_session.get_secret_value.return_value = mock_response

    result = get_credentials(secret_name, secret_key)

    assert result == (None, None)
    mock_boto3_session.get_secret_value.assert_called_once_with(SecretId=secret_name)


def test_get_credentials_client_error(mock_boto3_session):
    secret_name = "test-secret"
    mock_boto3_session.get_secret_value.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "get_secret_value"
    )

    with pytest.raises(SystemExit):
        get_credentials(secret_name)
    mock_boto3_session.get_secret_value.assert_called_once_with(SecretId=secret_name)


def test_mask_sensitive_data():
    assert mask_sensitive_data("somecommand --password mypassword") == "somecommand --password ****** mypassword"
    assert mask_sensitive_data("somecommand --password") == "somecommand --password ******"
    assert mask_sensitive_data("somecommand") == "somecommand"


@patch("subprocess.run")
def test_run_command_success(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    result = run_command("echo test")
    assert result is True
    mock_run.assert_called_once_with("echo test", input=None, shell=True, check=True, text=True, capture_output=True)
