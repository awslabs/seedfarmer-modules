# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import sys

from replication.ecr.ecr_utils import ECRUtils

aws_account_id = os.getenv("AWS_ACCOUNT_ID")
aws_region = os.getenv("AWS_DEFAULT_REGION")
aws_partition = os.getenv("AWS_PARTITION", "aws")
aws_domain = "amazonaws.com" if aws_partition == "aws" else "amazonaws.com.cn"


def main(prefix: str) -> None:
    ecr_utils = ECRUtils(aws_account_id, aws_region, aws_domain)  # type: ignore
    ecr_utils.cleanup_ecr_repos(prefix=prefix)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python delete_repos.py <prefix>")
        sys.exit(1)
    main(sys.argv[1])
