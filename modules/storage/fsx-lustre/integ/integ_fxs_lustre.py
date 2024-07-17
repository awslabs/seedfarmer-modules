import datetime
import logging
import os
import sys
from ast import literal_eval
from typing import Dict, List

import aws_cdk as cdk
import aws_cdk.cloud_assembly_schema as cas
import aws_cdk.integ_tests_alpha as integration
import boto3

sys.path.append("../")

from stack import FsxFileSystem  # noqa: E402

app = cdk.App()
timestamp = datetime.datetime.now()
logger = logging.getLogger(__name__)


def get_module_dependencies(resource_keys: List[str]) -> Dict[str, str]:
    ssm = boto3.client("ssm", region_name="us-east-1")
    dependencies = {}
    try:
        for key in resource_keys:
            dependencies[key] = ssm.get_parameter(Name=f"/module-integration-tests/{key}")["Parameter"]["Value"]
    except Exception as e:
        print(f"issue getting dependencies: {e}")
    return dependencies


dependencies = get_module_dependencies(
    # Add any required resource identifiers here
    resource_keys=["vpc-id", "vpc-private-subnets"]
)

integration.IntegTest(
    app,
    "Integration Tests Fsx Lustre Module",
    test_cases=[
        FsxFileSystem(
            app,
            "integ-fsx-lustre",
            project_name="integ",
            deployment_name="testing",
            module_name="fsx-lustre",
            vpc_id=dependencies["vpc-id"],
            fs_deployment_type="PERSISTENT_2",
            private_subnet_ids=literal_eval(dependencies["vpc-private-subnets"]),
            stack_description=f"Integration testing: {timestamp.hour}:{timestamp.minute}:{timestamp.second}",
            storage_throughput=125,
            storage_capacity=1200,
            env=cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"), region="us-east-1"),
        ),
    ],
    regions=["us-east-1"],
    enable_lookups=True,
    diff_assets=True,
    stack_update_workflow=True,
    cdk_command_options=cas.CdkCommands(
        deploy=cas.DeployCommand(args=cas.DeployOptions(require_approval=cas.RequireApproval.NEVER, json=True)),
        destroy=cas.DestroyCommand(args=cas.DestroyOptions(force=True)),
    ),
)
app.synth()
