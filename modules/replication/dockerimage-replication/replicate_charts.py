# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import sys
import time
from typing import Any, Dict

from replication.ecr.ecr_utils import ECRUtils
from replication.logging import logger
from replication.utils import export_results, get_credentials, run_command

successful_replication = []
failed_replication = []

aws_account_id = os.getenv("AWS_ACCOUNT_ID")
aws_region = os.getenv("AWS_DEFAULT_REGION")
aws_partition = os.getenv("AWS_PARTITION", "aws")
aws_domain = "amazonaws.com" if aws_partition == "aws" else "amazonaws.com.cn"

repo_secret = os.getenv("SEEDFARMER_PARAMETER_HELM_REPO_SECRET_NAME", None)
repo_key = os.getenv("SEEDFARMER_PARAMETER_HELM_REPO_SECRET_KEY", None)


def process_chart(ecr_utils: ECRUtils, chart_key: str, chart_data: Dict[str, Any]) -> None:
    """Process a single chart from the JSON document."""
    logger.info(f"Processing chart: {chart_key} ")

    # Extract Helm and repository details
    helm = chart_data["helm"]
    name = helm["name"]
    version = helm["version"]
    src_repository = helm["srcRepository"]
    target_repository = helm["repository"]

    # Prepare for push
    chart_package = f"{name}-{version}.tgz"
    ecr_repo_name = f"{target_repository.replace('oci://', '').split('/', 1)[-1]}"
    ecr_repo_target = target_repository.replace(f"/{name}", "")

    if ecr_utils.image_exists(ecr_repo_name, version):
        logger.info(f"Chart {name} with version {version} already exists in {ecr_repo_name}. Skipping.")
        successful_replication.append(name)
        return

    # Log in to the source repository
    repo_user, repo_password = get_credentials(repo_secret, repo_key)  # type:ignore
    if repo_user and repo_password:
        logger.info(f"Logging into source Helm repository: {src_repository}")
        helm_login_command = f"helm registry login {src_repository} --username {repo_user} --password {repo_password}"
        if not run_command(helm_login_command):
            logger.info(f"Error: Failed to log in to source Helm repository {src_repository}. Skipping {name}.")
            failed_replication.append(name)
            return

    # Pull the chart
    chart_path = f"{src_repository}/{name}"

    logger.info(f"Pulling chart: {name} (Version: {version}) from {chart_path}")

    if not run_command(f"helm pull {chart_key}/{name} --version {version}"):
        logger.info(f"Error: Failed to pull chart: {chart_key}/{name} Skipping.")
        failed_replication.append(name)
        return
    time.sleep(2)

    logger.info(f" {chart_package}  {ecr_repo_name}")

    # Create the ECR repository if needed
    try:
        ecr_utils.create_repository(ecr_repo_name)
    except Exception as e:
        logger.info(f"Error: Could not ensure ECR repository exists. Skipping {name}. - {e}")
        failed_replication.append(name)
        return

    # Push the chart to the target ECR repository
    logger.info(f"Pushing chart: {chart_package} to {ecr_repo_target}")
    if not run_command(f"helm push {chart_package} {ecr_repo_target}"):
        logger.info(f"Error: Failed to push chart: {ecr_repo_name}. Skipping.")
        failed_replication.append(name)
        return

    successful_replication.append(name)

    # Clean up local chart package
    logger.info(f"Cleaning up local chart package: {chart_package}")
    run_command(f"rm -f {chart_package}")

    logger.info(f"Successfully processed {name} -> {target_repository}")
    logger.info("------------------------------------------------")

    time.sleep(2)


def main() -> None:
    """Main function."""
    # Input JSON file
    input_file = "./replication-result.json"

    # Check if the input file exists
    if not os.path.isfile(input_file):
        logger.info(f"Error: {input_file} not found!")
        sys.exit(1)

    # Load JSON data
    with open(input_file, "r") as f:
        chart_data = json.load(f)

    charts = chart_data.get("charts", {})
    if not charts:
        logger.info("No charts found in the input JSON.")
        return

    # Enable OCI experimental feature in Helm
    os.environ["HELM_EXPERIMENTAL_OCI"] = "1"

    ecr_utils = ECRUtils(aws_account_id, aws_region, aws_domain)  # type: ignore
    if not ecr_utils.login_to_ecr("helm"):
        logger.info("Cannot log into account ECR, skipping everything")
        return

    # Process each chart
    for chart_key, chart_data in charts.items():
        process_chart(ecr_utils, chart_key, chart_data)

    export_results("Successfully replicated charts", successful_replication)  # type: ignore
    export_results("FAILED replicated charts", failed_replication)  # type: ignore

    logger.info("Script completed.")


if __name__ == "__main__":
    main()
