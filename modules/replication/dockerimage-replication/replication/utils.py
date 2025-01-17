# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple, Union

import boto3
from botocore.exceptions import ClientError
from deepmerge import always_merger

from replication.logging import logger

USER = "username"
PWD = "password"


def deep_merge(*dicts: Dict[Any, Any]) -> Dict[Any, Any]:
    """Merges two dictionaries

    Returns:
        dict: Merged dictionary
    """
    merged: Dict[Any, Any] = {}
    for d in dicts:
        tmp = deepcopy(d)
        merged = always_merger.merge(merged, tmp)
    return merged


def get_credentials(secret_name: str, secret_key: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    if not secret_name:
        return (None, None)

    session = boto3.session.Session()
    client = session.client(service_name="secretsmanager")
    try:
        get_secret_value_response = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        print(f"Error fetching secret, check the name and make sure you have permissions -- {e}")
        exit(1)
    secret = get_secret_value_response["SecretString"]
    secret = json.loads(secret)
    secret = secret[secret_key] if secret_key else secret
    if USER in secret and PWD in secret:
        return (secret[USER], secret[PWD])
    else:
        return (None, None)


def mask_sensitive_data(command: str) -> str:
    """Mask sensitive data like passwords in the command string."""
    if "--password" in command:
        parts = command.split("--password", 1)
        if len(parts) > 1:
            before_password = parts[0]
            after_password = parts[1].split(" ", 1)  # Split after the password
            if len(after_password) > 1:
                # Reassemble with masked password
                return f"{before_password}--password ****** {after_password[1]}"
            else:
                return f"{before_password}--password ******"
    return command


def run_command(
    command: Union[str, List[str]],
    input: Optional[str] = None,
    capture_output: Optional[bool] = True,
    shell: Optional[bool] = True,
    error_indicator: Optional[str] = "ERROR",
) -> bool:
    """Run a shell command."""
    try:
        result = subprocess.run(command, input=input, shell=shell, check=True, text=True, capture_output=capture_output)  # type: ignore
        if error_indicator in result.stderr:
            logger.info(f"Error Occurred execution command {mask_sensitive_data(command)}")  # type: ignore
            return False
        else:
            return True
    except subprocess.CalledProcessError as e:
        # Sanitize the command to hide passwords
        sanitized_command = mask_sensitive_data(command)  # type: ignore
        logger.info(f"Error running command: {sanitized_command}\n{e.stderr if capture_output else ''}")
        return False


def export_results(message: str, replication_result: Dict[str, str]) -> None:
    logger.info(message)
    if replication_result:
        for im in replication_result:
            logger.info(f"    {json.dumps(im, indent=2)}")
    else:
        logger.info("NONE")
