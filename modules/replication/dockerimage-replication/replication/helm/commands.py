# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Helm commands"""

import os
import shlex
import shutil
import subprocess  # nosec B404
from typing import Any, Dict, Optional

import yaml


def _execute_command(command: str) -> str:
    """Executes arbitrary command

    Args:
        command (str): Command to execute

    Returns:
        str: Command execution result
    """
    cmd = shlex.split(command)
    executed_command = subprocess.Popen(
        cmd,
        shell=False,  # nosec B603
        text=True,
        universal_newlines=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stdout, _ = executed_command.communicate()
    return stdout


def _unarchive_repo(project_path: str, repo: str, chart: str, version: str) -> None:
    """Pulls and unarchives helm repository locally


    Args:
        project_path (str): Path where to unarchive the helm repository
        repo (str): Helm repository name
        chart (str): Helm chart name
        version (str): Helm chart version
    """
    _execute_command(f"helm pull {repo}/{chart} --version {version} --untar --untardir {project_path}")


def show(subcommand: str, chart: str, version: str) -> Any:
    """Shows helm values

    Args:
        subcommand (str): Helm show subcommand. Can be one of: all, chart, crds, readme, values
        chart (str): Helm chart name
        version (str): Helm chart version

    Returns:
        dict: Parsed helm show output
    """
    return yaml.safe_load(_execute_command(f"helm show {subcommand} {chart} --version {version}"))


def show_subchart(project_path: str, repo: str, chart: str, subchart: str, version: str) -> Dict[Any, Any]:
    """Shows helm values for subchart

    Args:
        project_path (str): Path where to unarchive the helm repository
        repo (str): Helm repository name
        chart (str): Helm subchart name
        subchart (str): Helm subchart name
        version (str): Helm subchart version

    Returns:
        dict: Parsed helm show output
    """
    _unarchive_repo(project_path, repo, chart, version)

    result: Dict[Any, Any] = {}
    result["chart"] = yaml.safe_load(
        _execute_command(f"helm show chart {os.path.join(project_path, chart, 'charts', subchart)}")
    )

    result["values"] = yaml.safe_load(
        _execute_command(f"helm show values {os.path.join(project_path, chart, 'charts', subchart)}")
    )

    shutil.rmtree(f"{project_path}/{chart}")

    return result


def add_repo(name: str, repo: str, username: Optional[str] = None, pwd: Optional[str] = None) -> None:
    """Adds a chart repository locally

    Args:
        name (str): Helm chart name
        repo (str): Helm repository name
    """
    all_set = False
    if username is not None and pwd is not None:
        all_set = True
    if all_set:
        _execute_command(f"helm repo add {name} {repo} --username {username} --password {pwd}")
    else:
        _execute_command(f"helm repo add {name} {repo}")


def update_repos() -> None:
    """Updates information of available charts locally from chart repositories"""
    _execute_command("helm repo update")
