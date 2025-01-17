# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from unittest.mock import patch

import pytest

from get_list_eks_images import (
    apply_chart_info,
    apply_image_mapping,
    fetch_chart_info,
    update_helm,
)


@pytest.fixture
def mock_workloads_data():
    workload_path = "tests/test_payloads/workloads_data.json"
    with open(workload_path, encoding="utf-8") as workload_file:
        return json.load(workload_file)


def mock_helm_show(command, chart_name, version):
    data = {}
    show_subchart_path = "tests/test_payloads/parsed_charts_data.json"
    with open(show_subchart_path, encoding="utf-8") as subchart_file:
        data = json.load(subchart_file)
    if command == "chart":
        return data["kyverno_policy_reporter"]["chart"]
    elif command == "values":
        return data["kyverno_policy_reporter"]["values"]
    return {}


def mock_show_subchart(project_path, workload, name, subchart, version):
    data = {}
    show_subchart_path = "tests/test_payloads/parsed_charts_data.json"
    with open(show_subchart_path, encoding="utf-8") as subchart_file:
        data = json.load(subchart_file)
    return data["kyverno_policy_reporter"]["subcharts"]


@patch("get_list_eks_images.helm.show", side_effect=mock_helm_show)
@patch("get_list_eks_images.helm.show_subchart", side_effect=mock_show_subchart)
def test_fetch_chart_info(mock_show_subchart, mock_helm_show, mock_workloads_data):
    # Call the function being tested
    result = fetch_chart_info(mock_workloads_data)

    # Load expected result
    with open("tests/test_payloads/chart_fetch_result.json", encoding="utf-8") as workload_file:
        test_case_result = json.load(workload_file)
    # Assert the result matches the expected output
    assert result == test_case_result


@patch("get_list_eks_images.helm.add_repo")
@patch("get_list_eks_images.helm.update_repos")
@patch("get_list_eks_images.get_credentials")
def test_update_helm(mock_get_credentials, mock_update_repos, mock_add_repo, mock_workloads_data):
    # Mock return value for get_credentials
    mock_get_credentials.return_value = ("username", "password")

    # Call the function
    update_helm(True, mock_workloads_data)
    # Assert that helm.add_repo was called for each workload
    mock_add_repo.assert_any_call(
        "kyverno_policy_reporter",
        "https://customdns/charts/helm-remote-kyverno-gh-io-policy-rep/",
        "username",
        "password",
    )
    assert mock_add_repo.call_count == len(mock_workloads_data)

    # Assert that helm.update_repos was called once
    mock_update_repos.assert_called_once()


def test_update_helm_no_update(mock_workloads_data):
    # Mock the logger to check if the function short-circuits
    with patch("get_list_eks_images.logger") as mock_logger:
        update_helm(False, mock_workloads_data)

        # Assert no calls to logger.info
        mock_logger.info.assert_not_called()


def test_apply_chart_info():
    # Load the test data
    with open("tests/test_payloads/workloads_data.json", encoding="utf-8") as workload_file:
        workloads_data = json.load(workload_file)
    with open("tests/test_payloads/parsed_charts_data.json", encoding="utf-8") as parsed_charts_file:
        parsed_charts_data = json.load(parsed_charts_file)

    result = apply_chart_info(
        workloads_data, parsed_charts_data, "123456789012.dkr.ecr.us-east-1.amazonaws.com/idffulltest-", []
    )
    with open("tests/test_payloads/custom_chart_values_testcase.json", encoding="utf-8") as workload_file:
        test_case_result = json.load(workload_file)
    assert result == test_case_result


def test_apply_image_mapping():
    mappings = {
        "docker.io": "somedns/docker-remote-hub-docker-com",
        "public.ecr.aws": "somedns/docker-remote-public-ecr-aws",
    }

    assert (
        apply_image_mapping("public.ecr.aws/cloudwatch-agent/cloudwatch-agent:1.247358.0b252413", mappings)
        == "somedns/docker-remote-public-ecr-aws/cloudwatch-agent/cloudwatch-agent:1.247358.0b252413"
    )
    assert (
        apply_image_mapping("docker.io/grafana/grafana:latest", mappings)
        == "somedns/docker-remote-hub-docker-com/grafana/grafana:latest"
    )
