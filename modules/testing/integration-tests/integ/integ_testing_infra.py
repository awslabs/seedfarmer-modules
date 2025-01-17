import datetime
import logging
import sys
import os
from typing import Dict, List

import aws_cdk as cdk
import aws_cdk.cloud_assembly_schema as cas  # type: ignore
import aws_cdk.integ_tests_alpha as integration  # type: ignore
import boto3  # type: ignore

sys.path.append("../")

from integration_tests_infra import IntegrationTestsInfrastructure  # noqa: E402

app = cdk.App()
timestamp = datetime.datetime.now()
logger = logging.getLogger(__name__)


def get_module_dependencies(resource_keys: List[str]) -> Dict[str, str]:
    ssm = boto3.client("ssm", region_name="us-east-1")
    dependencies = {}
    try:
        for key in resource_keys:
            dependencies[key] = ssm.get_parameter(
                Name=f"/module-integration-tests/{key}"
            )["Parameter"]["Value"]
    except Exception as e:
        print(f"issue getting dependencies: {e}")
    return dependencies


dependencies = get_module_dependencies(
    # Add any required resource identifiers here
    resource_keys=[]
)

integration.IntegTest(
    app,
    "Integration Tests Testing Infra Module",
    test_cases=[
        IntegrationTestsInfrastructure(
            app,
            "tests-infra",
            deployment_name="integ",
            module_name="efs",
            manifests=[],
            repo_owner="awslabs",
            repo_name="idf-modules",
            oauth_token_secret_name=os.getenv("OAUTH_TOKEN_SECRET_NAME"),  # type: ignore
            seedfarmer_project_name="integration-tests",
            assets_path="../artifacts",
            create_github_source_credentials=False,
        )
    ],
    enable_lookups=True,
    diff_assets=True,
    stack_update_workflow=True,
    cdk_command_options=cas.CdkCommands(
        deploy=cas.DeployCommand(
            args=cas.DeployOptions(
                require_approval=cas.RequireApproval.NEVER, json=True
            )
        ),
        destroy=cas.DestroyCommand(args=cas.DestroyOptions(force=True)),
    ),
)
app.synth()
