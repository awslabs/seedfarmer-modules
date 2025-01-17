# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import sys
from typing import Dict, Optional

from replication.ecr.ecr_utils import ECRUtils
from replication.logging import logger
from replication.utils import export_results, get_credentials, run_command

successful_replication = []
failed_replication = []

aws_region = os.getenv("AWS_DEFAULT_REGION")
aws_account_id = os.getenv("AWS_ACCOUNT_ID")
aws_partition = os.getenv("AWS_PARTITION", "aws")
aws_domain = "amazonaws.com" if aws_partition == "aws" else "amazonaws.com.cn"

repo_secret = os.getenv("SEEDFARMER_PARAMETER_HELM_REPO_SECRET_NAME", None)
repo_key = os.getenv("SEEDFARMER_PARAMETER_HELM_REPO_SECRET_KEY", None)


# Pull and push Docker image
def pull_and_push_image(
    src_repo: str, src: str, target_ecr_tag: str, username: Optional[str] = None, password: Optional[str] = None
) -> bool:
    try:
        if username and password:
            # logger.info(f"Username and PWD found {username} to log into the src docker repo")
            login_cmd = ["docker", "login", "-u", username, "-p", password, src_repo]
            logged_in = run_command(login_cmd, shell=False)  # tyep: ignore
            if not logged_in:
                logger.info(f"Failed to login to {src_repo}")
                return False
        logger.info(f"Pulling image {src}")
        pull_image = run_command(["docker", "pull", src], shell=False)
        tag_image = run_command(["docker", "tag", src, target_ecr_tag], shell=False)
        logger.info(f"Pushing image {target_ecr_tag}")
        push_image = run_command(["docker", "push", target_ecr_tag], shell=False)
        run_command(["docker", "rmi", src], shell=False)
        if False in [pull_image, tag_image, push_image]:
            return False
        return True

    except Exception as e:
        logger.info(f"Error: {e}")
        return False


# Create workflow
def create(
    ecr_utils: ECRUtils,
    image_repl: Dict[str, str],
    src_repo_user: Optional[str] = None,
    src_repo_pwd: Optional[str] = None,
) -> None:
    try:
        src = image_repl["src"]
        target = image_repl["target"]
        src_info = src.split(":")
        src_repo = src_info[0]
        # src_version = src_info[1]

        target_info = target.split(":")
        target_repo = target_info[0]
        if target_repo.startswith(f"{aws_account_id}.dkr.ecr"):
            s = target_repo.split("/")
            target_repo = "/".join(s[1:])

        target_version = target_info[1]
        ecr_utils.create_repository(target_repo)
        if not ecr_utils.image_exists(target_repo, target_version):
            logger.info(f"{target_repo}:{target_version} not found, fetching")
            result_push = pull_and_push_image(src_repo, src, target, src_repo_user, src_repo_pwd)
            if not result_push:
                failed_replication.append(image_repl)
                return
        else:
            logger.info(f"{target_version} found in {target_repo}, skipping replication")
    except Exception as e:
        logger.info(f"Error: {e}")
        failed_replication.append(image_repl)
        return
    successful_replication.append(image_repl)


def main() -> None:
    input_file = "./updated_images.json"
    # Check if the input file exists
    if not os.path.isfile(input_file):
        logger.info(f"Error: {input_file} not found!")
        sys.exit(1)
    image_data = {}
    # Load JSON data
    with open(input_file, "r") as f:
        image_data = json.load(f)
    # Log in to the source repository
    repo_user, repo_password = get_credentials(repo_secret, repo_key)  # type:ignore
    if not repo_user or not repo_password:
        logger.info("Not using any auth for source repos for images")
        repo_user, repo_password = None, None
    else:
        logger.info("Using auth for source repos for images")
    ecr_utils = ECRUtils(aws_account_id, aws_region, aws_domain)  # type:ignore
    try:
        ecr_utils.login_to_ecr(type="docker")
    except Exception:
        logger.info("Cannot log into ECR, stopping the replication entirely")
        exit(1)
    for image_repl in image_data:
        create(ecr_utils, image_repl, repo_user, repo_password)
    export_results("Successfully replicated images", successful_replication)  # type:ignore
    export_results("FAILED replicated image", failed_replication)  # type:ignore
    logger.info("Script completed.")
    logger.info("------------------------------------------------")


# Main entry point
if __name__ == "__main__":
    main()
