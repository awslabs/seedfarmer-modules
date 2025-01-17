# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_env_vars():
    """Fixture to mock environment variables."""
    with patch.dict(
        "os.environ",
        {
            "AWS_ACCOUNT_ID": "123456789012",
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_PARTITION": "aws",
        },
    ):
        yield


def test_main(mock_env_vars):
    """Test the main function with mocked ECRUtils."""
    # Mock the ECRUtils class
    with patch("delete_repos.ECRUtils") as MockECRUtils:  # Update with the correct module name
        # Create a mock instance of ECRUtils
        mock_ecr_utils = MagicMock()
        MockECRUtils.return_value = mock_ecr_utils

        # Mock sys.argv
        test_prefix = "test-prefix"
        with patch.object(sys, "argv", ["delete_repos.py", test_prefix]):
            # Import and call the main function
            from delete_repos import main

            main(test_prefix)

            # Assertions
            MockECRUtils.assert_called_once_with("123456789012", "us-west-2", "amazonaws.com")
            mock_ecr_utils.cleanup_ecr_repos.assert_called_once_with(prefix=test_prefix)
