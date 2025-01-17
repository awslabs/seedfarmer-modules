# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# type: ignore

import json
import os
from string import Template
from typing import Any, Dict, List, Optional, cast

import cdk_nag
import requests
import yaml
from aws_cdk import Aspects, Aws, CfnJson, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_aps as aps
from aws_cdk import aws_autoscaling as asg
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks as eks
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_ssm as ssm
from aws_cdk.lambda_layer_kubectl_v29 import KubectlV29Layer
from cdk_nag import NagSuppressions
from constructs import Construct, IConstruct

from helpers import (
    deep_merge,
    get_ami_version,
    get_az_from_subnet,
    get_chart_release,
    get_chart_repo,
    get_chart_values,
    get_chart_version,
    get_image,
)
from utils.iam import fetch_global_ecr_account
from utils.k8s import convert_node_labels_to_k8sargs, convert_taints_to_k8sargs

project_dir = os.path.dirname(os.path.abspath(__file__))

"""
We are loading the data from the commonly shared data folder,
which has the below addon's helm chart/container images metadata
"""

ALB_CONTROLLER = "alb_controller"
AWS_VPC_CNI = "aws_vpc_cni"
CALICO = "calico"
CERT_MANAGER = "cert_manager"
CLOUDWATCH_AGENT = "cloudwatch_agent"
CLUSTER_AUTOSCALER = "cluster_autoscaler"
EBS_CSI_DRIVER = "ebs_csi_driver"
EFS_CSI_DRIVER = "efs_csi_driver"
EXTERNAL_DNS = "external_dns"
EXTERNAL_SECRETS = "external_secrets"
FLUENTBIT = "fluentbit"
FSX_DRIVER = "fsx_driver"
GRAFANA = "grafana"
KURED = "kured"
KYVERNO = "kyverno"
KYVERNO_POLICY_REPORTER = "kyverno_policy_reporter"
METRICS_SERVER = "metrics_server"
NGINX_CONTROLLER = "nginx_controller"
PROMETHEUS_STACK = "prometheus_stack"
SECRETS_MANAGER_CSI_DRIVER = "secrets_manager_csi_driver"
SECRETS_STORE_CSI_DRIVER_PROVIDER_AWS = "secrets_store_csi_driver_provider_aws"
NVIDIA_DEVICE_PLUGIN = "nvidia_device_plugin"


class Eks(Stack):  # type: ignore
    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        partition: str,
        project_name: str,
        deployment_name: str,
        module_name: str,
        vpc_id: str,
        controlplane_subnet_ids: List[str],
        dataplane_subnet_ids: List[str],
        eks_version: str,
        eks_compute_config: Dict[Any, Any],
        eks_addons_config: Optional[Dict[Any, Any]],
        custom_subnet_ids: Optional[List[str]],
        codebuild_sg_id: Optional[str],
        mountpoint_buckets: Optional[List[str]],
        replicated_ecr_images_metadata: Optional[Dict[Any, Any]],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            scope,
            id,
            description="This stack deploys EKS Cluster, Managed Nodegroup(s) with standard plugins/addons",
            **kwargs,
        )

        dep_mod = f"{project_name}-{deployment_name}-{module_name}"

        # used to tag AWS resources. Tag Value length cant exceed 256 characters
        full_dep_mod = dep_mod[:256] if len(dep_mod) > 256 else dep_mod

        Tags.of(scope=cast(IConstruct, self)).add(key="Deployment", value=full_dep_mod)

        self._partition = partition
        # Importing the VPC
        self.vpc = ec2.Vpc.from_lookup(
            self,
            "VPC",
            vpc_id=vpc_id,
        )

        # DataPlane Subnets
        self.dataplane_subnets = []
        for idx, subnet_id in enumerate(dataplane_subnet_ids):
            self.dataplane_subnets.append(
                ec2.Subnet.from_subnet_id(scope=self, id=f"dp-subnet{idx}", subnet_id=subnet_id)
            )

        # ControlPlane Subnets
        self.controlplane_subnets = []
        for idx, subnet_id in enumerate(controlplane_subnet_ids):
            self.controlplane_subnets.append(
                ec2.Subnet.from_subnet_id(scope=self, id=f"cp-subnet{idx}", subnet_id=subnet_id)
            )

        # EKS cluster kubectl lambda handler role
        kubectl_lambda_role = self._create_cluster_kubectl_lambda_role(
            project_name, deployment_name, module_name, self.region, self.account
        )

        # EKS Cluster Role
        cluster_admin_role = self._create_cluster_admin_role(
            project_name,
            deployment_name,
            module_name,
            self.region,
            self.account,
            kubectl_lambda_role,
        )

        # EKS Node Role
        self.node_role = self._create_node_role(project_name, deployment_name, module_name, self.region)

        # Creates an EKS Cluster
        eks_cluster, cm_patch, patches = self._create_eks_cluster(
            vpc=self.vpc,
            dataplane_subnets=self.dataplane_subnets,
            controlplane_subnets=self.controlplane_subnets,
            cluster_admin_role=cluster_admin_role,
            kubectl_lambda_role=kubectl_lambda_role,
            eks_version=eks_version,
            eks_compute_config=eks_compute_config,
            eks_addons_config=eks_addons_config,
            codebuild_sg_id=codebuild_sg_id,
            # KMS key for Kubernetes secrets envelope encryption
            secrets_key=kms.Key(self, "SecretsKey")
            if eks_compute_config.get("eks_secrets_envelope_encryption")
            else None,
            project_name=project_name,
            deployment_name=deployment_name,
            module_name=module_name,
        )

        # Create the Service Account
        sg_pods_service_account = self._create_service_account(eks_cluster)

        # Using CUSTOM CIDR feature of EKS
        custom_subnet_values = {}
        if custom_subnet_ids:
            custom_subnet_ids = get_az_from_subnet(custom_subnet_ids)
            custom_subnets_values = {}
            for subnet_id, subnet_availability_zone in custom_subnet_ids.items():
                custom_subnets_values[subnet_availability_zone] = {"id": subnet_id}

            custom_subnet_values = {
                "init": {
                    "env": {
                        "AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG": True,
                        "ENI_CONFIG_LABEL_DEF": "failure-domain.beta.kubernetes.io/zone",
                    },
                },
                "env": {
                    "AWS_VPC_K8S_CNI_CUSTOM_NETWORK_CFG": True,
                    "ENI_CONFIG_LABEL_DEF": "failure-domain.beta.kubernetes.io/zone",
                },
                "eniConfig": {
                    "create": True,
                    "subnets": custom_subnets_values,
                },
            }

        # Unfortunately vpc addon is always installed last, after the nodes are created and
        # we are unable to influence pod cidrs without recreating the nodes after the cluster creation.
        # To mitigate this and support out-of-the-box custom cidr for pods, we install
        # VPC CNI as a helm chart instead of vpc addon.
        vpc_cni_chart = self._create_vpc_cni_chart(
            eks_cluster,
            eks_version,
            replicated_ecr_images_metadata,
            sg_pods_service_account,
            custom_subnet_values,
            cm_patch,
            patches,
        )

        # Add Managed Node Group(s)
        if eks_compute_config.get("eks_nodegroup_config"):
            # Spot InstanceType
            if eks_compute_config.get("eks_node_spot"):
                node_capacity_type = eks.CapacityType.SPOT
            # OnDemand InstanceType
            else:
                node_capacity_type = eks.CapacityType.ON_DEMAND

            for ng in eks_compute_config.get("eks_nodegroup_config", [{}]):
                if ng.get("eks_self_managed"):
                    self._create_self_managed_node_group(
                        dep_mod,
                        eks_cluster,
                        ng,
                        vpc_cni_chart,
                    )
                else:
                    self._create_managed_node_group(eks_cluster, eks_version, ng, node_capacity_type, vpc_cni_chart)

                if ng.get("install_nvidia_device_plugin"):
                    self._install_nvidia_device_plugin(eks_cluster, eks_version, replicated_ecr_images_metadata)

        # AWS Load Balancer Controller
        if eks_addons_config.get("deploy_aws_lb_controller"):
            awslbcontroller_chart = self._create_aws_lb_controller(
                eks_cluster, eks_version, vpc_id, replicated_ecr_images_metadata, eks_addons_config
            )

        if eks_addons_config.get("deploy_nginx_controller"):
            self._create_nginx_controller(eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config)

        # AWS S3 CSI Driver
        if eks_addons_config.get("deploy_aws_s3_csi"):
            self._create_s3_csi_addon(eks_cluster, project_name, mountpoint_buckets)

        # AWS Cloudwatch Observability Driver
        if eks_addons_config.get("deploy_cloudwatch_observability_addon"):
            # https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-setup-EKS-addon.html
            self._create_cloudwatch_observability_addon(eks_cluster)

        # AWS EBS CSI Driver
        if eks_addons_config.get("deploy_aws_ebs_csi"):
            self._deploy_ebs_csi_driver(
                eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # AWS EFS CSI Driver
        if eks_addons_config.get("deploy_aws_efs_csi"):
            self._deploy_efs_csi_driver(
                eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # AWS FSx CSI Driver does not work in isolated subnets at the time of developing the module
        if eks_addons_config.get("deploy_aws_fsx_csi"):
            self._deploy_fsx_csi_driver(
                eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # Cluster Autoscaler
        if eks_addons_config.get("deploy_cluster_autoscaler"):
            self._deploy_cluster_autoscaler(
                eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # Metrics Server (required for the Horizontal Pod Autoscaler (HPA))
        if eks_addons_config.get("deploy_metrics_server"):
            self._deploy_metrics_server(eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config)

        if eks_addons_config.get("deploy_external_dns"):
            self._deploy_external_dns(eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config)

        # Secrets Manager CSI Driver
        if eks_addons_config.get("deploy_secretsmanager_csi"):
            self._deploy_secrets_store_csi_driver(
                eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # Kubernetes External Secrets
        if eks_addons_config.get("deploy_external_secrets"):
            self._deploy_external_secrets_controller(
                eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # CloudWatch Container Insights - Metrics
        if eks_addons_config.get("deploy_cloudwatch_container_insights_metrics"):
            self._deploy_cloudwatch_container_insights_metrics(
                eks_cluster, eks_version, project_dir, replicated_ecr_images_metadata, eks_addons_config
            )
        # AWS Distro for Opentelemetry
        if eks_addons_config.get("deploy_adot"):
            self._deploy_adot_and_cert_manager(
                eks_cluster, eks_version, eks_addons_config, replicated_ecr_images_metadata
            )

        # CloudWatch Container Insights - Logs
        if eks_addons_config.get("deploy_cloudwatch_container_insights_logs"):
            self._deploy_fluent_bit_cloudwatch(
                eks_cluster,
                eks_version,
                replicated_ecr_images_metadata,
                eks_addons_config,
            )

        # Amazon Managed Prometheus (AMP)
        if eks_addons_config.get("deploy_amp"):
            amp_sa, amp_workspace, amp_prometheus_chart = self._deploy_amazon_managed_prometheus(
                eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # Self-Managed Grafana for AMP
        if eks_addons_config.get("deploy_grafana_for_amp"):
            self._deploy_grafana_for_amp(
                eks_cluster,
                project_dir,
                eks_version,
                amp_sa,
                amp_workspace,
                replicated_ecr_images_metadata,
                eks_addons_config,
                amp_prometheus_chart,
                awslbcontroller_chart,
            )

        if eks_addons_config.get("deploy_kured"):
            # https://kubereboot.github.io/charts/
            self._deploy_kured(eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config)

        if eks_addons_config.get("deploy_calico"):
            self._deploy_calico(
                eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # Kyverno policies
        if eks_addons_config.get("deploy_kyverno"):
            self._deploy_kyverno(
                eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
            )

        # Configure EKS/K8s RBAC with ready to assume roles based on org reqs
        self._configure_rbac(eks_cluster)

        # Outputs
        self.eks_cluster = eks_cluster
        self.eks_cluster_masterrole = cluster_admin_role
        self.kubectl_lambda_role = kubectl_lambda_role

        # Add Aspects
        Aspects.of(self).add(cdk_nag.AwsSolutionsChecks())

        # Add suppressions
        self._add_suppressions()

    def _create_self_managed_node_group(self, dep_mod, eks_cluster, ng_config, vpc_cni_chart):
        """
        Creates a Self Managed Node Group with the specified configuration.
        """

        KUBELET_EXTRA_ARGS = ""

        if ng_config.get("eks_node_labels"):
            labels = convert_node_labels_to_k8sargs(ng_config.get("eks_node_labels"))
            KUBELET_EXTRA_ARGS = f"--node-labels {labels}"

        if ng_config.get("eks_node_taints"):
            taints = convert_taints_to_k8sargs(ng_config.get("eks_node_taints"))
            KUBELET_EXTRA_ARGS = f"{KUBELET_EXTRA_ARGS} --register-with-taints {taints}"

        self_managed_nodegroup = eks_cluster.add_auto_scaling_group_capacity(
            f"self-managed-{ng_config.get('eks_ng_name')}",
            desired_capacity=ng_config.get("eks_node_quantity"),
            max_capacity=ng_config.get("eks_node_max_quantity"),
            min_capacity=ng_config.get("eks_node_min_quantity"),
            update_policy=asg.UpdatePolicy.rolling_update(
                max_batch_size=1,
                min_instances_in_service=ng_config.get("eks_node_quantity"),
            ),
            instance_type=ec2.InstanceType(str(ng_config.get("eks_node_instance_type"))),
            bootstrap_options=eks.BootstrapOptions(
                kubelet_extra_args=KUBELET_EXTRA_ARGS if KUBELET_EXTRA_ARGS else None,
            ),
            vpc_subnets=ec2.SubnetSelection(subnets=self.dataplane_subnets),
            auto_scaling_group_name=f"{dep_mod}-{ng_config.get('eks_ng_name')}",
            group_metrics=[asg.GroupMetrics.all()],
            instance_monitoring=asg.Monitoring.DETAILED,
            signals=asg.Signals.wait_for_all(timeout=Duration.minutes(10)),
        )

        self_managed_nodegroup.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy")
        )
        self_managed_nodegroup.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly")
        )

        launch_config = cast(asg.CfnLaunchConfiguration, self_managed_nodegroup.node.try_find_child("LaunchConfig"))
        launch_config.metadata_options = asg.CfnLaunchConfiguration.MetadataOptionsProperty(
            http_tokens="required", http_put_response_hop_limit=2
        )
        launch_config.block_device_mappings = [
            asg.CfnLaunchConfiguration.BlockDeviceMappingProperty(
                device_name="/dev/xvda",
                ebs=asg.CfnLaunchConfiguration.BlockDeviceProperty(
                    encrypted=True, volume_size=ng_config.get("eks_node_disk_size"), volume_type="gp2"
                ),
            )
        ]

        Tags.of(self_managed_nodegroup).add(
            "k8s.io/cluster-autoscaler/" + eks_cluster.cluster_name,
            "owned",
            apply_to_launched_instances=True,
        )

        Tags.of(self_managed_nodegroup).add(
            "k8s.io/cluster-autoscaler/enabled",
            "true",
            apply_to_launched_instances=True,
        )
        self_managed_nodegroup.node.add_dependency(vpc_cni_chart)

    def _create_managed_node_group(self, eks_cluster, eks_version, ng_config, node_capacity_type, vpc_cni_chart):
        """
        Creates an Amazon Managed Node Group with the specified configuration.
        """
        lt = ec2.CfnLaunchTemplate(
            self,
            f"ng-lt-{str(ng_config.get('eks_ng_name'))}",
            launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                instance_type=str(ng_config.get("eks_node_instance_type")),
                block_device_mappings=[
                    ec2.CfnLaunchTemplate.BlockDeviceMappingProperty(
                        device_name="/dev/xvda",
                        ebs=ec2.CfnLaunchTemplate.EbsProperty(
                            volume_type="gp3",
                            volume_size=ng_config.get("eks_node_disk_size"),
                            encrypted=True,
                        ),
                    )
                ],
                metadata_options=ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                    http_tokens="required", http_put_response_hop_limit=2
                ),
            ),
        )

        nodegroup = eks_cluster.add_nodegroup_capacity(
            f"cluster-default-{ng_config.get('eks_ng_name')}",
            capacity_type=node_capacity_type,
            ami_type=eks.NodegroupAmiType.AL2_X86_64_GPU
            if ng_config.get("use_gpu_ami")
            else eks.NodegroupAmiType.AL2_X86_64,
            desired_size=ng_config.get("eks_node_quantity"),
            min_size=ng_config.get("eks_node_min_quantity"),
            max_size=ng_config.get("eks_node_max_quantity"),
            force_update=False,
            launch_template_spec=eks.LaunchTemplateSpec(id=lt.ref, version=lt.attr_latest_version_number),
            labels=ng_config.get("eks_node_labels") if ng_config.get("eks_node_labels") else None,
            release_version=get_ami_version(str(eks_version)),
            subnets=ec2.SubnetSelection(subnets=self.dataplane_subnets),
            node_role=self.node_role,
            taints=[
                eks.TaintSpec(
                    key=taint.get("key"),
                    value=taint.get("value"),
                    effect=eks.TaintEffect.NO_SCHEDULE
                    if taint.get("effect") == "NoSchedule"
                    else eks.TaintEffect.PREFER_NO_SCHEDULE,
                )
                for taint in ng_config.get("eks_node_taints")
            ]
            if ng_config.get("eks_node_taints")
            else None,
        )

        nodegroup.node.add_dependency(vpc_cni_chart)

    def _create_eks_cluster(
        self,
        project_name: str,
        deployment_name: str,
        module_name: str,
        vpc: ec2.Vpc,
        dataplane_subnets: List[ec2.Subnet],
        controlplane_subnets: List[ec2.Subnet],
        cluster_admin_role: iam.Role,
        kubectl_lambda_role: iam.Role,
        eks_version: str,
        eks_compute_config: Dict[Any, Any],
        eks_addons_config: Optional[Dict[Any, Any]] = None,
        codebuild_sg_id: Optional[str] = None,
        secrets_key: Optional[kms.Key] = None,
    ) -> eks.Cluster:
        """
        Creates an Amazon EKS cluster with the specified configuration.
        """

        CIDRS = []
        if eks_compute_config.get("ips_to_whitelist_from_ssm"):
            cidrs_from_ssm = self._fetch_cidrs_from_ssm(json.loads(eks_compute_config.get("ips_to_whitelist_from_ssm")))
            CIDRS.extend(cidrs_from_ssm)
        if eks_compute_config.get("ips_to_whitelist_adhoc"):
            cidrs_adhoc = json.loads(eks_compute_config.get("ips_to_whitelist_adhoc"))
            CIDRS.extend(cidrs_adhoc)

        if eks_compute_config.get("eks_api_endpoint_private"):
            api_endpoint = eks.EndpointAccess.PRIVATE
        else:
            api_endpoint = eks.EndpointAccess.PUBLIC_AND_PRIVATE.only_from(
                "0.0.0.0/0"
            )  # by default, opens up to 0.0.0.0/0

            if eks_compute_config.get("ips_to_whitelist_from_ssm") or eks_compute_config.get("ips_to_whitelist_adhoc"):
                aws_cidrs = self._fetch_aws_cidrs()
                CIDRS.extend(aws_cidrs)
                api_endpoint = eks.EndpointAccess.PUBLIC_AND_PRIVATE.only_from(*CIDRS)

        # Create the EKS cluster
        eks_cluster = eks.Cluster(
            self,
            "cluster",
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnets=controlplane_subnets)],
            cluster_name=f"{project_name}-{deployment_name}-{module_name}-cluster",
            masters_role=cluster_admin_role,
            endpoint_access=api_endpoint,
            version=eks.KubernetesVersion.of(str(eks_version)),
            kubectl_layer=KubectlV29Layer(self, "Kubectlv29Layer"),
            kubectl_lambda_role=kubectl_lambda_role,
            default_capacity=0,
            secrets_encryption_key=secrets_key if eks_compute_config.get("eks_secrets_envelope_encryption") else None,
            cluster_logging=[
                eks.ClusterLoggingTypes.API,
                eks.ClusterLoggingTypes.AUDIT,
                eks.ClusterLoggingTypes.AUTHENTICATOR,
                eks.ClusterLoggingTypes.CONTROLLER_MANAGER,
                eks.ClusterLoggingTypes.SCHEDULER,
            ],
        )

        # Whitelist traffic between Codebuild SG and EKS SG when the APIServer is private
        if eks_compute_config.get("eks_api_endpoint_private") and codebuild_sg_id:
            codebuild_sg = ec2.SecurityGroup.from_security_group_id(self, "eks-codebuild-sg", codebuild_sg_id)
            eks_cluster.kubectl_security_group.add_ingress_rule(
                codebuild_sg,
                ec2.Port.all_traffic(),
                description="Allowing traffic between private codebuild (codeseeder) and private API server",
            )

        # Manage core-dns, kube-proxy and vpc-cni as Server Side softwares using Addons
        coredns_addon = eks.CfnAddon(
            self,
            "coredns",
            addon_name="coredns",
            resolve_conflicts="OVERWRITE",
            cluster_name=eks_cluster.cluster_name,
        )
        kube_proxy_addon = eks.CfnAddon(
            self,
            "kube-proxy",
            addon_name="kube-proxy",
            resolve_conflicts="OVERWRITE",
            cluster_name=eks_cluster.cluster_name,
        )
        coredns_addon.node.add_dependency(eks_cluster)
        kube_proxy_addon.node.add_dependency(eks_cluster)

        # Patch the existing aws-node resources to fix Helm ownership errors
        cm_patch, patches = self._patch_aws_node_resources(eks_cluster)

        return eks_cluster, cm_patch, patches

    def _patch_aws_node_resources(self, eks_cluster: eks.Cluster):
        """
        Patches the existing aws-node resources to fix Helm ownership errors.
        """
        patch_types = ["DaemonSet", "ClusterRole", "ClusterRoleBinding", "ServiceAccount"]
        patches = []
        for kind in patch_types:
            patch = eks.KubernetesPatch(
                self,
                "CNI-Patch-" + kind,
                cluster=eks_cluster,
                resource_name=kind + "/aws-node",
                resource_namespace="kube-system",
                apply_patch={
                    "metadata": {
                        "annotations": {
                            "meta.helm.sh/release-name": "aws-vpc-cni",
                            "meta.helm.sh/release-namespace": "kube-system",
                        },
                        "labels": {"app.kubernetes.io/managed-by": "Helm"},
                    }
                },
                restore_patch={},
                patch_type=eks.PatchType.STRATEGIC,
            )

            # We don't want to clean this up on Delete - it is a one-time patch to let the Helm Chart own the resources
            patch_resource = patch.node.find_child("Resource")
            patch_resource.apply_removal_policy(RemovalPolicy.RETAIN)
            # Keep track of all the patches to set dependencies down below
            patches.append(patch)

        # since 1.14 chart, ConfigMap object needs to be patched
        cm_patch = eks.KubernetesPatch(
            self,
            "Patch-" + "ConfigMap",
            cluster=eks_cluster,
            resource_name="ConfigMap" + "/amazon-vpc-cni",
            resource_namespace="kube-system",
            apply_patch={
                "metadata": {
                    "annotations": {
                        "meta.helm.sh/release-name": "aws-vpc-cni",
                        "meta.helm.sh/release-namespace": "kube-system",
                    },
                    "labels": {"app.kubernetes.io/managed-by": "Helm"},
                }
            },
            restore_patch={},
            patch_type=eks.PatchType.STRATEGIC,
        )
        cm_patch_resource = cm_patch.node.find_child("Resource")
        cm_patch_resource.apply_removal_policy(RemovalPolicy.RETAIN)

        return cm_patch, patches

    def _create_cluster_admin_role(
        self, project_name, deployment_name, module_name, region, account, kubectl_lambda_role
    ):
        """
        Creates the EKS cluster admin role.
        """

        cluster_admin_role = iam.Role(
            self,
            "ClusterAdminRole",
            role_name=f"{project_name}-{deployment_name}-{module_name}-{region}-masterrole",
            assumed_by=iam.PrincipalWithConditions(
                iam.AccountPrincipal(account),
                conditions={
                    "ArnLike": {
                        "aws:PrincipalArn": (
                            # NOTE: this is known to cause issues when using long deployment names
                            # for resources with autogenerated names that may be truncated to not fit the pattern
                            f"arn:{self._partition}:iam::{account}:role/{project_name}-{deployment_name}-*"
                        )
                    }
                },
            ),
        )

        # Explicitly allow kubectl handler role assume cluster admin role
        cluster_admin_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[kubectl_lambda_role.role_arn],
            )
        )

        cluster_admin_policy_statement_json_1 = {
            "Effect": "Allow",
            "Action": [
                "eks:DescribeCluster",
                "iam:Get*",
                "cloudformation:List*",
                "cloudformation:Describe*",
                "cloudformation:Get*",
            ],
            "Resource": "*",
        }

        cluster_admin_role.add_to_principal_policy(iam.PolicyStatement.from_json(cluster_admin_policy_statement_json_1))

        return cluster_admin_role

    def _create_node_role(self, project_name, deployment_name, module_name, region):
        """
        Creates the EKS node role.
        """

        node_role = iam.Role(
            self,
            "NodeRole",
            role_name=f"{project_name}-{deployment_name}-{module_name}-{region}-noderole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Role for EKS nodes",
        )
        node_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerRegistryReadOnly")
        )
        node_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKSWorkerNodePolicy"))

        return node_role

    def _create_cluster_kubectl_lambda_role(self, project_name, deployment_name, module_name, region, account):
        """
        Creates the EKS cluster kubectl lambda handler role.
        """

        kubectl_lambda_role = iam.Role(
            self,
            "ClusterKubectlHandlerRole",
            role_name=f"{project_name}-{deployment_name}-{module_name}-{region}-kubectl",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Role for EKS kubectl lambda handler",
        )
        kubectl_lambda_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )
        kubectl_lambda_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole")
        )

        return kubectl_lambda_role

    def _create_vpc_cni_chart(
        self,
        eks_cluster,
        eks_version,
        replicated_ecr_images_metadata,
        sg_pods_service_account,
        custom_subnet_values,
        cm_patch,
        patches,
    ):
        """
        Creates the VPC CNI Helm chart.
        """

        GLOBAL_ECR_ACCOUNT = fetch_global_ecr_account(self.region)

        vpc_cni_chart = eks_cluster.add_helm_chart(
            "aws-vpc-cni",
            chart=get_chart_release(str(eks_version), AWS_VPC_CNI, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), AWS_VPC_CNI, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), AWS_VPC_CNI, replicated_ecr_images_metadata),
            release="aws-vpc-cni",
            namespace="kube-system",
            values=deep_merge(
                {
                    "init": {
                        "image": {
                            "region": self.region,
                            "account": GLOBAL_ECR_ACCOUNT,
                            "domain": Aws.URL_SUFFIX,
                        },
                        "env": {"DISABLE_TCP_EARLY_DEMUX": True},
                    },
                    "nodeAgent": {
                        "image": {
                            "region": self.region,
                            "account": GLOBAL_ECR_ACCOUNT,
                            "domain": Aws.URL_SUFFIX,
                        },
                    },
                    "image": {
                        "region": self.region,
                        "domain": Aws.URL_SUFFIX,
                        "account": GLOBAL_ECR_ACCOUNT,
                    },
                    "env": {"ENABLE_POD_ENI": True},
                    "serviceAccount": {"create": False, "name": "aws-node-helm"},
                    "originalMatchLabels": True,
                },
                custom_subnet_values,
                get_chart_values(replicated_ecr_images_metadata, AWS_VPC_CNI),
            ),
        )

        vpc_cni_chart.node.add_dependency(sg_pods_service_account)
        vpc_cni_chart.node.add_dependency(cm_patch)
        for patch in patches:
            vpc_cni_chart.node.add_dependency(patch)

        return vpc_cni_chart

    def _create_service_account(self, eks_cluster):
        """
        Creates the service account for the aws-node pods.
        """
        sg_pods_service_account = eks_cluster.add_service_account(
            "aws-node", name="aws-node-helm", namespace="kube-system"
        )
        sg_pods_service_account.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEKS_CNI_Policy")
        )
        return sg_pods_service_account

    def _create_aws_lb_controller(
        self, eks_cluster, eks_version, vpc_id, replicated_ecr_images_metadata, eks_addons_config
    ):
        """
        Creates the AWS Load Balancer Controller.
        """
        awslbcontroller_service_account = eks_cluster.add_service_account(
            "aws-load-balancer-controller",
            name="aws-load-balancer-controller",
            namespace="kube-system",
        )

        awslbcontroller_policy_document_path = os.path.join(
            project_dir, "addons-iam-policies", "ingress-controller.json"
        )
        with open(awslbcontroller_policy_document_path) as json_file:
            awslbcontroller_policy_document_json = json.load(json_file)

        awslbcontroller_policy = iam.Policy(
            self,
            "awslbcontrollerpolicy",
            document=iam.PolicyDocument.from_json(awslbcontroller_policy_document_json),
        )
        awslbcontroller_service_account.role.attach_inline_policy(awslbcontroller_policy)

        awslbcontroller_chart = eks_cluster.add_helm_chart(
            "aws-load-balancer-controller",
            chart=get_chart_release(str(eks_version), ALB_CONTROLLER, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), ALB_CONTROLLER, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), ALB_CONTROLLER, replicated_ecr_images_metadata),
            release="awslbcontroller",
            namespace="kube-system",
            values=deep_merge(
                {
                    "clusterName": eks_cluster.cluster_name,
                    "region": self.region,
                    "vpcId": vpc_id,
                    "serviceAccount": {
                        "create": False,
                        "name": "aws-load-balancer-controller",
                    },
                    "replicaCount": 2,
                    "podDisruptionBudget": {"maxUnavailable": 1},
                    "resources": {"requests": {"cpu": "0.25", "memory": "0.5Gi"}},
                },
                get_chart_values(replicated_ecr_images_metadata, ALB_CONTROLLER),
            ),
        )
        awslbcontroller_chart.node.add_dependency(awslbcontroller_service_account)
        return awslbcontroller_chart

    def _create_nginx_controller(self, eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config):
        """
        Creates the NGINX Ingress Controller.
        """
        if (
            "value" in eks_addons_config.get("deploy_nginx_controller")
            and eks_addons_config.get("deploy_nginx_controller")["value"]
        ):
            nginx_controller_service_account = eks_cluster.add_service_account(
                "nginx-controller",
                name="nginx-controller",
                namespace="kube-system",
            )

            nginx_controller_policy_document_path = os.path.join(
                project_dir, "addons-iam-policies", "ingress-controller.json"
            )
            with open(nginx_controller_policy_document_path) as json_file:
                nginx_controller_policy_document_json = json.load(json_file)

            nginx_controller_policy = iam.Policy(
                self,
                "nginxcontrollerpolicy",
                document=iam.PolicyDocument.from_json(nginx_controller_policy_document_json),
            )
            nginx_controller_service_account.role.attach_inline_policy(nginx_controller_policy)
            custom_values = {}
            if "nginx_additional_annotations" in eks_addons_config.get("deploy_nginx_controller"):
                custom_values = {
                    "controller": {
                        "configAnnotations": eks_addons_config.get("deploy_nginx_controller")[
                            "nginx_additional_annotations"
                        ]
                    }
                }
            # For more info check out https://github.com/kubernetes/ingress-nginx/tree/main/charts/ingress-nginx
            nginx_controller_chart = eks_cluster.add_helm_chart(
                "nginx-controller",
                chart=get_chart_release(str(eks_version), NGINX_CONTROLLER, replicated_ecr_images_metadata),
                version=get_chart_version(str(eks_version), NGINX_CONTROLLER, replicated_ecr_images_metadata),
                repository=get_chart_repo(str(eks_version), NGINX_CONTROLLER, replicated_ecr_images_metadata),
                release="nginx-controller",
                namespace="kube-system",
                values=deep_merge(
                    custom_values,
                    get_chart_values(replicated_ecr_images_metadata, NGINX_CONTROLLER),
                ),
            )
            nginx_controller_chart.node.add_dependency(nginx_controller_service_account)

    def _create_s3_csi_addon(self, eks_cluster, project_name, mountpoint_buckets):
        """
        Creates the AWS S3 CSI Driver addon for the EKS cluster.
        """
        if mountpoint_buckets:
            arns = [f"arn:{self._partition}:s3:::{bucket}" for bucket in mountpoint_buckets]
            arns_with_paths = [f"arn:{self._partition}:s3:::{bucket}/*" for bucket in mountpoint_buckets]
        else:
            arns = [f"arn:{self._partition}:s3:::{project_name}*"]
            arns_with_paths = [f"arn:{self._partition}:s3:::{project_name}*/*"]

        s3_csi_storageclass = eks_cluster.add_manifest(
            "S3CSIStorageClass",
            {
                "kind": "StorageClass",
                "apiVersion": "storage.k8s.io/v1",
                "metadata": {"name": "s3-csi"},
                "parameters": {"type": "standard"},
                "provisioner": "s3.csi.aws.com",
            },
        )

        # Create service account
        s3_csi_service_account = eks_cluster.add_service_account(
            "s3-csi-driver-sa",
            name="s3-csi-driver-sa",
            namespace="kube-system",
            labels={"app.kubernetes.io/name": "aws-mountpoint-s3-csi-driver"},
        )

        s3_csi_service_account.role.attach_inline_policy(
            iam.Policy(
                self,
                "S3Policy",
                statements=[
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        resources=arns,
                        actions=["s3:ListBucket"],
                    ),
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        resources=arns_with_paths,
                        actions=["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:DeleteObject"],
                    ),
                ],
            )
        )

        s3_addon = eks.CfnAddon(
            self,
            "s3-addon",
            addon_name="aws-mountpoint-s3-csi-driver",
            resolve_conflicts="OVERWRITE",
            cluster_name=eks_cluster.cluster_name,
            service_account_role_arn=s3_csi_service_account.role.role_arn,
        )
        s3_addon.node.add_dependency(eks_cluster)
        s3_addon.node.add_dependency(s3_csi_storageclass)
        s3_addon.node.add_dependency(s3_csi_service_account)

    def _create_cloudwatch_observability_addon(self, eks_cluster):
        """
        Creates the AWS CloudWatch Observability addon for the EKS cluster.
        """
        # IRSA for CW Addon
        cw_obs_role = iam.Role(
            self,
            "CWSARole",
            assumed_by=iam.FederatedPrincipal(
                eks_cluster.open_id_connect_provider.open_id_connect_provider_arn,
                assume_role_action="sts:AssumeRoleWithWebIdentity",
                conditions={
                    "StringEquals": CfnJson(
                        self,
                        "CWSARoleProvider",
                        value={f"{eks_cluster.cluster_open_id_connect_issuer}:aud": "sts.amazonaws.com"},
                    )
                },
            ),
            managed_policies=[
                iam.ManagedPolicy.from_managed_policy_arn(
                    self,
                    "CloudWatchAgentServerPolicy",
                    managed_policy_arn=f"arn:{self._partition}:iam::aws:policy/CloudWatchAgentServerPolicy",
                )
            ],
        )

        cw_obs_addon = eks.CfnAddon(
            self,
            "cw-obs-addon",
            addon_name="amazon-cloudwatch-observability",
            resolve_conflicts="OVERWRITE",
            cluster_name=eks_cluster.cluster_name,
            service_account_role_arn=cw_obs_role.role_arn,
        )
        cw_obs_addon.node.add_dependency(eks_cluster)

    def _deploy_ebs_csi_driver(
        self, eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
    ):
        """
        Deploys the AWS EBS CSI Driver addon for the EKS cluster.
        """
        awsebscsidriver_service_account = eks_cluster.add_service_account(
            "awsebscsidriver", name="awsebscsidriver", namespace="kube-system"
        )

        # Reference: https://github.com/kubernetes-sigs/aws-ebs-csi-driver/blob/master/docs/example-iam-policy.json
        awsebscsidriver_policy_document_json_path = os.path.join(project_dir, "addons-iam-policies", "ebs-csi-iam.json")
        with open(awsebscsidriver_policy_document_json_path) as json_file:
            awsebscsidriver_policy_document_json = json.load(json_file)

        # Attach the necessary permissions
        awsebscsidriver_policy = iam.Policy(
            self,
            "awsebscsidriverpolicy",
            document=iam.PolicyDocument.from_json(awsebscsidriver_policy_document_json),
        )
        awsebscsidriver_service_account.role.attach_inline_policy(awsebscsidriver_policy)

        # Install the AWS EBS CSI Driver
        # https://github.com/kubernetes-sigs/aws-ebs-csi-driver/tree/master/charts/aws-ebs-csi-driver
        awsebscsi_chart = eks_cluster.add_helm_chart(
            "aws-ebs-csi-driver",
            chart=get_chart_release(str(eks_version), EBS_CSI_DRIVER, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), EBS_CSI_DRIVER, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), EBS_CSI_DRIVER, replicated_ecr_images_metadata),
            release="awsebscsidriver",
            namespace="kube-system",
            values=deep_merge(
                {
                    "controller": {
                        "region": self.region,
                        "serviceAccount": {
                            "create": False,
                            "name": "awsebscsidriver",
                        },
                    },
                    "node": {
                        "serviceAccount": {
                            "create": False,
                            "name": "awsebscsidriver",
                        }
                    },
                },
                get_chart_values(replicated_ecr_images_metadata, EBS_CSI_DRIVER),
            ),
        )
        awsebscsi_chart.node.add_dependency(awsebscsidriver_service_account)

        # Set up the StorageClass pointing at the new CSI Driver
        # https://github.com/kubernetes-sigs/aws-ebs-csi-driver/blob/master/examples/kubernetes/dynamic-provisioning/specs/storageclass.yaml
        ebs_csi_storageclass = eks_cluster.add_manifest(
            "EBSCSIStorageClassGP2",
            {
                "kind": "StorageClass",
                "apiVersion": "storage.k8s.io/v1",
                "metadata": {"name": "ebs-gp2"},
                "parameters": {"type": "gp2", "encrypted": "true"},
                "provisioner": "ebs.csi.aws.com",
                "volumeBindingMode": "WaitForFirstConsumer",
            },
        )
        ebs_csi_storageclass.node.add_dependency(awsebscsi_chart)

        # Set up the StorageClass pointing at the new CSI Driver
        # https://github.com/kubernetes-sigs/aws-ebs-csi-driver/blob/master/examples/kubernetes/dynamic-provisioning/specs/storageclass.yaml
        ebs_csi_storageclass_gp3 = eks_cluster.add_manifest(
            "EBSCSIStorageClassGP3",
            {
                "kind": "StorageClass",
                "apiVersion": "storage.k8s.io/v1",
                "metadata": {"name": "ebs-gp3"},
                "parameters": {"type": "gp3", "encrypted": "true"},
                "provisioner": "ebs.csi.aws.com",
                "volumeBindingMode": "WaitForFirstConsumer",
            },
        )
        ebs_csi_storageclass_gp3.node.add_dependency(awsebscsi_chart)

    def _deploy_efs_csi_driver(
        self,
        eks_cluster,
        project_dir,
        eks_version,
        replicated_ecr_images_metadata,
        eks_addons_config,
    ):
        """
        Deploys the AWS EFS CSI Driver addon for the EKS cluster.
        """
        awsefscsidriver_service_account = eks_cluster.add_service_account(
            "awsefscsidriver", name="awsefscsidriver", namespace="kube-system"
        )

        awsefscsidriver_policy_statement_json_path = os.path.join(
            project_dir, "addons-iam-policies", "efs-csi-iam.json"
        )
        with open(awsefscsidriver_policy_statement_json_path) as json_file:
            awsefscsidriver_policy_statement_json = json.load(json_file)

        # Attach the necessary permissions
        awsefscsidriver_policy = iam.Policy(
            self,
            "awsefscsidriverpolicy",
            document=iam.PolicyDocument.from_json(awsefscsidriver_policy_statement_json),
        )
        awsefscsidriver_service_account.role.attach_inline_policy(awsefscsidriver_policy)

        awsefscsi_chart = eks_cluster.add_helm_chart(
            "aws-efs-csi-driver",
            chart=get_chart_release(str(eks_version), EFS_CSI_DRIVER, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), EFS_CSI_DRIVER, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), EFS_CSI_DRIVER, replicated_ecr_images_metadata),
            release="awsefscsidriver",
            namespace="kube-system",
            values=deep_merge(
                {
                    "controller": {
                        "serviceAccount": {
                            "create": False,
                            "name": "awsefscsidriver",
                        },
                        "deleteAccessPointRootDir": True,
                    },
                    "node": {
                        "serviceAccount": {
                            "create": False,
                            "name": "awsefscsidriver",
                        }
                    },
                },
                get_chart_values(replicated_ecr_images_metadata, EFS_CSI_DRIVER),
            ),
        )

        awsefscsi_chart.node.add_dependency(awsefscsidriver_service_account)

    def _deploy_fsx_csi_driver(
        self, eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
    ):
        """
        Deploys the AWS FSx CSI Driver addon for the EKS cluster.
        """
        awsfsxcsidriver_service_account = eks_cluster.add_service_account(
            "awsfsxcsidriver", name="awsfsxcsidriver", namespace="kube-system"
        )

        awsfsxcsidriver_policy_statement_json_path = os.path.join(
            project_dir, "addons-iam-policies", "fsx-csi-iam.json"
        )
        with open(awsfsxcsidriver_policy_statement_json_path) as json_file:
            awsfsxcsidriver_policy_statement_json = json.load(json_file)

        # Attach the necessary permissions
        awsfsxcsidriver_policy = iam.Policy(
            self,
            "awsfsxcsidriverpolicy",
            document=iam.PolicyDocument.from_json(awsfsxcsidriver_policy_statement_json),
        )
        awsfsxcsidriver_service_account.role.attach_inline_policy(awsfsxcsidriver_policy)

        # Install the AWS FSx CSI Driver
        # https://github.com/kubernetes-sigs/aws-fsx-csi-driver/tree/release-0.9/charts/aws-fsx-csi-driver
        awsfsxcsi_chart = eks_cluster.add_helm_chart(
            "aws-fsx-csi-driver",
            chart=get_chart_release(str(eks_version), FSX_DRIVER, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), FSX_DRIVER, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), FSX_DRIVER, replicated_ecr_images_metadata),
            release="awsfsxcsidriver",
            namespace="kube-system",
            values=deep_merge(
                {
                    "controller": {
                        "serviceAccount": {
                            "create": False,
                            "name": "awsfsxcsidriver",
                        }
                    },
                    "node": {
                        "serviceAccount": {
                            "create": False,
                            "name": "awsfsxcsidriver",
                        }
                    },
                },
                get_chart_values(replicated_ecr_images_metadata, FSX_DRIVER),
            ),
        )
        awsfsxcsi_chart.node.add_dependency(awsfsxcsidriver_service_account)

    def _deploy_cluster_autoscaler(
        self,
        eks_cluster,
        project_dir,
        eks_version,
        replicated_ecr_images_metadata,
        eks_addons_config,
    ):
        """
        Deploys the Cluster Autoscaler Helm chart for the EKS cluster for node level autoscaling.
        """
        clusterautoscaler_service_account = eks_cluster.add_service_account(
            "clusterautoscaler", name="clusterautoscaler", namespace="kube-system"
        )

        clusterautoscaler_policy_statement_json_path = os.path.join(
            project_dir, "addons-iam-policies", "cluster-autoscaler-iam.json"
        )
        with open(clusterautoscaler_policy_statement_json_path) as json_file:
            clusterautoscaler_policy_statement_json = json.load(json_file)

        # Attach the necessary permissions
        clusterautoscaler_policy = iam.Policy(
            self,
            "clusterautoscalerpolicy",
            document=iam.PolicyDocument.from_json(clusterautoscaler_policy_statement_json),
        )
        clusterautoscaler_service_account.role.attach_inline_policy(clusterautoscaler_policy)

        # Install the Cluster Autoscaler
        # For more info see https://github.com/kubernetes/autoscaler/tree/master/charts/cluster-autoscaler
        clusterautoscaler_chart = eks_cluster.add_helm_chart(
            "cluster-autoscaler",
            chart=get_chart_release(str(eks_version), CLUSTER_AUTOSCALER, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), CLUSTER_AUTOSCALER, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), CLUSTER_AUTOSCALER, replicated_ecr_images_metadata),
            release="clusterautoscaler",
            namespace="kube-system",
            values=deep_merge(
                {
                    "autoDiscovery": {"clusterName": eks_cluster.cluster_name},
                    "awsRegion": self.region,
                    "rbac": {
                        "serviceAccount": {
                            "create": False,
                            "name": "clusterautoscaler",
                        }
                    },
                    "replicaCount": 2,
                    "extraArgs": {
                        "skip-nodes-with-system-pods": False,
                        "balance-similar-node-groups": True,
                    },
                },
                get_chart_values(replicated_ecr_images_metadata, CLUSTER_AUTOSCALER),
            ),
        )
        clusterautoscaler_chart.node.add_dependency(clusterautoscaler_service_account)

    def _deploy_kured(self, eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config):
        """
        Deploys the Kured (Kubernetes Reboot Daemon) addon for the EKS cluster.
        """
        # Install the Kured addon
        eks_cluster.add_helm_chart(
            "kured",
            chart=get_chart_release(str(eks_version), KURED, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), KURED, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), KURED, replicated_ecr_images_metadata),
            release="kured",
            namespace="kured",
            values=deep_merge(
                {
                    "nodeSelector": {"kubernetes.io/os": "linux"},
                },
                get_chart_values(replicated_ecr_images_metadata, KURED),
            ),
        )

    def _deploy_calico(self, eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config):
        """
        Deploys the Calico network policy plugin for the EKS cluster.
        """
        calico_values = get_chart_values(replicated_ecr_images_metadata, CALICO)
        if calico_values and "tigeraOperator" in calico_values and "registry" in calico_values["tigeraOperator"]:
            # https://docs.tigera.io/calico/3.25/reference/installation/api#operator.tigera.io/v1.InstallationSpec
            calico_values["installation"] = {}
            calico_values["installation"]["registry"] = calico_values["tigeraOperator"]["registry"]

        # https://docs.projectcalico.org/charts
        calico_chart = eks_cluster.add_helm_chart(
            "tigera-operator",
            chart=get_chart_release(str(eks_version), CALICO, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), CALICO, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), CALICO, replicated_ecr_images_metadata),
            values=deep_merge(
                calico_values,
            ),
            release="calico",
            namespace="tigera-operator",
        )

        with open(os.path.join(project_dir, "network-policies/default-allow-kube-system.json"), "r") as f:
            default_allow_kube_system_policy_file = f.read()

        allow_kube_system_policy = eks_cluster.add_manifest(
            "default-allow-kube-system", json.loads(default_allow_kube_system_policy_file)
        )

        allow_kube_system_policy.node.add_dependency(calico_chart)

        with open(os.path.join(project_dir, "network-policies/default-allow-tigera-operator.json"), "r") as f:
            default_allow_tigera_operator_policy_file = f.read()

        allow_tigera_operator_policy = eks_cluster.add_manifest(
            "default-allow-tigera-operator", json.loads(default_allow_tigera_operator_policy_file)
        )

        allow_tigera_operator_policy.node.add_dependency(allow_kube_system_policy)

        with open(os.path.join(project_dir, "network-policies/default-deny.json"), "r") as f:
            default_deny_policy_file = f.read()

        default_deny_policy = eks_cluster.add_manifest("default-deny-policy", json.loads(default_deny_policy_file))

        default_deny_policy.node.add_dependency(allow_tigera_operator_policy)

    def _deploy_kyverno(self, eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config):
        """
        Deploys the Kyverno policy engine plugin for the EKS cluster.
        """
        if "value" in eks_addons_config.get("deploy_kyverno") and eks_addons_config.get("deploy_kyverno")["value"]:
            # https://kyverno.github.io/kyverno/
            kyverno_chart = eks_cluster.add_helm_chart(
                "kyverno",
                chart=get_chart_release(str(eks_version), KYVERNO, replicated_ecr_images_metadata),
                version=get_chart_version(str(eks_version), KYVERNO, replicated_ecr_images_metadata),
                repository=get_chart_repo(str(eks_version), KYVERNO, replicated_ecr_images_metadata),
                values=deep_merge(
                    {
                        "resources": {
                            "limits": {"memory": "4Gi"},
                            "requests": {"cpu": "1", "memory": "1Gi"},
                        }
                    },
                    get_chart_values(replicated_ecr_images_metadata, KYVERNO),
                ),
                release="kyverno",
                namespace="kyverno",
            )

            if eks_addons_config.get("deploy_calico"):
                with open(os.path.join(project_dir, "network-policies/default-allow-kyverno.json"), "r") as f:
                    default_allow_kyverno_policy_file = f.read()

                allow_kyverno_policy = eks_cluster.add_manifest(
                    "default-allow-kyverno", json.loads(default_allow_kyverno_policy_file)
                )

                allow_kyverno_policy.node.add_dependency(kyverno_chart)

            if "kyverno_policies" in eks_addons_config.get("deploy_kyverno"):
                all_policies = eks_addons_config.get("deploy_kyverno")["kyverno_policies"]
                for policy_type, policies in all_policies.items():
                    for policy in policies:
                        f = open(
                            os.path.join(project_dir, "kyverno-policies", policy_type, f"{policy}.yaml"),
                            "r",
                        ).read()
                        manifest_yaml = list(yaml.load_all(f, Loader=yaml.FullLoader))
                        previous_manifest = None
                        for value in manifest_yaml:
                            manifest_name = value["metadata"]["name"]
                            manifest = eks_cluster.add_manifest(manifest_name, value)
                            if previous_manifest is None:
                                manifest.node.add_dependency(kyverno_chart)
                            else:
                                manifest.node.add_dependency(previous_manifest)
                            previous_manifest = manifest

            kyverno_policy_reporter_chart = eks_cluster.add_helm_chart(
                "kyverno-policy-reporter",
                chart=get_chart_release(str(eks_version), KYVERNO_POLICY_REPORTER, replicated_ecr_images_metadata),
                version=get_chart_version(str(eks_version), KYVERNO_POLICY_REPORTER, replicated_ecr_images_metadata),
                repository=get_chart_repo(str(eks_version), KYVERNO_POLICY_REPORTER, replicated_ecr_images_metadata),
                release="policy-reporter",
                namespace="policy-reporter",
                values=deep_merge(
                    {
                        "kyvernoPlugin": {"enabled": True},
                        "ui": {
                            "enabled": True,
                            "plugins": {"kyverno": True},
                        },
                    },
                    get_chart_values(
                        replicated_ecr_images_metadata,
                        KYVERNO_POLICY_REPORTER,
                    ),
                ),
            )

            kyverno_policy_reporter_chart.node.add_dependency(kyverno_chart)

    def _deploy_metrics_server(self, eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config):
        """
        Deploys the Metrics Server helm chart for application level autoscaling.
        """
        # Install the Metrics Server addon
        eks_cluster.add_helm_chart(
            "metrics-server",
            chart=get_chart_release(str(eks_version), METRICS_SERVER, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), METRICS_SERVER, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), METRICS_SERVER, replicated_ecr_images_metadata),
            release="metricsserver",
            namespace="kube-system",
            values=deep_merge(
                {"resources": {"requests": {"cpu": "0.25", "memory": "0.5Gi"}}},
                get_chart_values(replicated_ecr_images_metadata, METRICS_SERVER),
            ),
        )

    def _deploy_external_dns(self, eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config):
        """
        Deploys the External DNS for the EKS cluster.
        """
        externaldns_service_account = eks_cluster.add_service_account(
            "external-dns", name="external-dns", namespace="kube-system"
        )

        # Create the PolicyStatements to attach to the role
        # See https://github.com/kubernetes-sigs/external-dns/blob/master/docs/tutorials/aws.md#iam-policy
        # NOTE that this will give External DNS access to all Route53 zones
        # For production you'll likely want to replace 'Resource *' with specific resources
        externaldns_policy_statement_json_1 = {
            "Effect": "Allow",
            "Action": ["route53:ChangeResourceRecordSets"],
            "Resource": [f"arn:{self._partition}:route53:::hostedzone/*"],
        }
        externaldns_policy_statement_json_2 = {
            "Effect": "Allow",
            "Action": ["route53:ListHostedZones", "route53:ListResourceRecordSets"],
            "Resource": ["*"],
        }

        # Attach the necessary permissions
        externaldns_service_account.add_to_principal_policy(
            iam.PolicyStatement.from_json(externaldns_policy_statement_json_1)
        )
        externaldns_service_account.add_to_principal_policy(
            iam.PolicyStatement.from_json(externaldns_policy_statement_json_2)
        )

        # Deploy External DNS from the bitnami Helm chart
        # For more info see https://github.com/kubernetes-sigs/external-dns/tree/master/charts/external-dns
        # Changed from the Bitnami chart for Graviton/ARM64 support
        externaldns_chart = eks_cluster.add_helm_chart(
            "external-dns",
            chart=get_chart_release(str(eks_version), EXTERNAL_DNS, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), EXTERNAL_DNS, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), EXTERNAL_DNS, replicated_ecr_images_metadata),
            release="externaldns",
            namespace="kube-system",
            values=deep_merge(
                {
                    "serviceAccount": {"create": False, "name": "external-dns"},
                    "resources": {"requests": {"cpu": "0.25", "memory": "0.5Gi"}},
                },
                get_chart_values(replicated_ecr_images_metadata, EXTERNAL_DNS),
            ),
        )
        externaldns_chart.node.add_dependency(externaldns_service_account)

    def _deploy_secrets_store_csi_driver(
        self, eks_cluster, project_dir, eks_version, replicated_ecr_images_metadata, eks_addons_config
    ):
        """
        Deploys the Secrets Store CSI Driver for the EKS cluster.
        """
        # https://docs.aws.amazon.com/secretsmanager/latest/userguide/integrating_csi_driver.html

        # First we install the Secrets Store CSI Driver Helm Chart
        # https://github.com/kubernetes-sigs/secrets-store-csi-driver/tree/main/charts/secrets-store-csi-driver
        eks_cluster.add_helm_chart(
            "csi-secrets-store",
            chart=get_chart_release(str(eks_version), SECRETS_MANAGER_CSI_DRIVER, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), SECRETS_MANAGER_CSI_DRIVER, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), SECRETS_MANAGER_CSI_DRIVER, replicated_ecr_images_metadata),
            release="csi-secrets-store",
            namespace="kube-system",
            # Since sometimes you want these secrets as environment variables enabling syncSecret
            # For more info see https://secrets-store-csi-driver.sigs.k8s.io/topics/sync-as-kubernetes-secret.html
            values=deep_merge(
                {"syncSecret": {"enabled": True}},
                get_chart_values(
                    replicated_ecr_images_metadata,
                    SECRETS_MANAGER_CSI_DRIVER,
                ),
            ),
        )
        # Install the AWS Provider
        # See https://github.com/aws/secrets-store-csi-driver-provider-aws for more info

        # Create the IRSA Mapping
        secrets_csi_sa = eks_cluster.add_service_account(
            "secrets-csi-sa",
            name="csi-secrets-store-provider-aws",
            namespace="kube-system",
        )

        secrets_csi_policy_statement_json_1 = {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
            ],
            "Resource": ["*"],
        }
        secrets_csi_sa.add_to_principal_policy(iam.PolicyStatement.from_json(secrets_csi_policy_statement_json_1))

        # Deploy the manifests from secrets-store-csi-driver-provider-aws.yaml
        secrets_store_csi_driver_image = get_image(
            str(eks_version), replicated_ecr_images_metadata, SECRETS_STORE_CSI_DRIVER_PROVIDER_AWS
        )
        t = Template(
            open(os.path.join(project_dir, "secrets-config/secrets-store-csi-driver-provider-aws.yaml"), "r").read()
        )

        # Substitute the image name in the secrets-store-csi-driver-provider-aws.yaml file
        secrets_csi_provider_yaml_file = t.substitute(image=str(secrets_store_csi_driver_image))
        secrets_csi_provider_yaml = list(yaml.load_all(secrets_csi_provider_yaml_file, Loader=yaml.FullLoader))
        loop_iteration = 0
        for value in secrets_csi_provider_yaml:
            loop_iteration = loop_iteration + 1
            manifest_id = "SecretsCSIProviderManifest" + str(loop_iteration)
            manifest = eks_cluster.add_manifest(manifest_id, value)
            manifest.node.add_dependency(secrets_csi_sa)

    def _deploy_external_secrets_controller(
        self, eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config
    ):
        """
        Deploys the External Secrets Controller for the EKS cluster.
        """

        # Deploy the External Secrets Controller
        # Create the Service Account
        externalsecrets_service_account = eks_cluster.add_service_account(
            "kubernetes-external-secrets",
            name="kubernetes-external-secrets",
            namespace="kube-system",
        )

        # Define the policy in JSON
        externalsecrets_policy_statement_json_1 = {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetResourcePolicy",
                "secretsmanager:GetSecretValue",
                "secretsmanager:DescribeSecret",
                "secretsmanager:ListSecretVersionIds",
            ],
            "Resource": ["*"],
        }

        # Add the policies to the service account
        externalsecrets_service_account.add_to_principal_policy(
            iam.PolicyStatement.from_json(externalsecrets_policy_statement_json_1)
        )

        # Deploy the Helm Chart
        # https://github.com/external-secrets/external-secrets/tree/main/deploy/charts/external-secrets
        eks_cluster.add_helm_chart(
            "external-secrets",
            chart=get_chart_release(str(eks_version), EXTERNAL_SECRETS, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), EXTERNAL_SECRETS, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), EXTERNAL_SECRETS, replicated_ecr_images_metadata),
            release="external-secrets",
            namespace="kube-system",
            values=deep_merge(
                {
                    "env": {"AWS_REGION": self.region},
                    "serviceAccount": {
                        "name": "kubernetes-external-secrets",
                        "create": False,
                    },
                    "securityContext": {"fsGroup": 65534},
                    "resources": {"requests": {"cpu": "0.25", "memory": "0.5Gi"}},
                },
                get_chart_values(replicated_ecr_images_metadata, EXTERNAL_SECRETS),
            ),
        )

    def _deploy_cloudwatch_container_insights_metrics(
        self, eks_cluster, eks_version, project_dir, replicated_ecr_images_metadata, eks_addons_config
    ):
        """
        Deploys the CloudWatch Container Insights addon for the EKS cluster.
        """
        # https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Container-Insights-setup-metrics.html

        # Create the Service Account
        cw_container_insights_sa = eks_cluster.add_service_account(
            "cloudwatch-agent", name="cloudwatch-agent", namespace="kube-system"
        )
        cw_container_insights_sa.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchAgentServerPolicy")
        )

        with open(os.path.join(project_dir, "monitoring-config/cwagentconfig.json"), "r", encoding="utf-8") as f:
            cwagentconfig_content = f.read()

        # Set up the settings ConfigMap
        eks_cluster.add_manifest(
            "CWAgentConfigMap",
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "data": {
                    "cwagentconfig.json": cwagentconfig_content,
                },
                "metadata": {"name": "cwagentconfig", "namespace": "kube-system"},
            },
        )

        # Import cloudwatch-agent.yaml to a list of dictionaries and submit them as a manifest to EKS
        cloudwatch_agent_image = get_image(str(eks_version), replicated_ecr_images_metadata, CLOUDWATCH_AGENT)
        t = Template(open(os.path.join(project_dir, "monitoring-config/cloudwatch-agent.yaml"), "r").read())
        # Substitute the image name in the cwagentconfig.json file
        cw_agent_yaml_file = t.substitute(image=str(cloudwatch_agent_image))
        cw_agent_yaml = list(yaml.load_all(cw_agent_yaml_file, Loader=yaml.FullLoader))
        loop_iteration = 0
        for value in cw_agent_yaml:
            loop_iteration = loop_iteration + 1
            manifest_id = "CWAgent" + str(loop_iteration)
            eks_cluster.add_manifest(manifest_id, value)

    def _deploy_adot_and_cert_manager(
        self, eks_cluster, eks_version, eks_addons_config, replicated_ecr_images_metadata
    ):
        """
        Deploys the ADOT (AWS Distro for OpenTelemetry) and Cert-Manager addons for the EKS cluster.
        """
        # Deploy the ADOT addon
        adot_addon = eks.CfnAddon(
            self,
            "adot",
            addon_name="adot",
            resolve_conflicts="OVERWRITE",
            cluster_name=eks_cluster.cluster_name,
        )
        adot_addon.node.add_dependency(eks_cluster)

        # Create the Cert-Manager namespace
        namespace_manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "cert-manager"}}
        cert_manager_namespace = eks_cluster.add_manifest("cert-manager", namespace_manifest)

        # Create the Cert-Manager service account
        cert_manager_service_account = eks_cluster.add_service_account(
            "cert-manager",
            name="cert-manager",
            namespace="cert-manager",
        )
        cert_manager_service_account.node.add_dependency(cert_manager_namespace)

        # Define the IAM policy for Cert-Manager
        cert_manager_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "acm:DescribeCertificate",
                "acm:ListCertificates",
                "acm:GetCertificate",
                "route53:ChangeResourceRecordSets",
                "route53:GetChange",
                "route53:GetHostedZone",
                "route53:ListHostedZones",
                "route53:ListResourceRecordSets",
            ],
            resources=["*"],
        )
        cert_manager_policy = iam.Policy(
            self,
            "cert-manager-policy",
            policy_name="cert-manager-policy",
            statements=[cert_manager_policy_statement],
        )
        cert_manager_service_account.role.attach_inline_policy(cert_manager_policy)

        # Deploy the Cert-Manager Helm chart
        cert_manager_chart = eks_cluster.add_helm_chart(
            "cert-manager",
            chart=get_chart_release(str(eks_version), CERT_MANAGER, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), CERT_MANAGER, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), CERT_MANAGER, replicated_ecr_images_metadata),
            release="cert-manager",
            namespace="cert-manager",
            create_namespace=False,
            values=deep_merge(
                {
                    "installCRDs": True,
                    "extraArgs": ["--dns01-recursive-nameservers-only=false"],
                    "serviceAccount": {
                        "create": False,
                        "name": cert_manager_service_account.service_account_name,
                        "annotations": {
                            "eks.amazonaws.com/role-arn": cert_manager_service_account.role.role_arn,
                        },
                    },
                    "webhook": {"securePort": 10260, "hostNetwork": True},
                },
                get_chart_values(replicated_ecr_images_metadata, CERT_MANAGER),
            ),
        )
        cert_manager_chart.node.add_dependency(cert_manager_service_account)

    def _deploy_fluent_bit_cloudwatch(
        self, eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config
    ):
        """
        Deploys the Fluent Bit for CloudWatch Logs in the EKS cluster - diff from CW Addon.
        """
        # Create the Service Account
        fluentbit_cw_service_account = eks_cluster.add_service_account(
            "fluentbit-cw", name="fluentbit-cw", namespace="kube-system"
        )

        fluentbit_cw_policy_statement_json_1 = {
            "Effect": "Allow",
            "Action": [
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
                "logs:DescribeLogGroups",
                "logs:CreateLogStream",
                "logs:CreateLogGroup",
                "logs:PutRetentionPolicy",
            ],
            "Resource": ["*"],
        }

        # Add the policies to the service account
        fluentbit_cw_service_account.add_to_principal_policy(
            iam.PolicyStatement.from_json(fluentbit_cw_policy_statement_json_1)
        )

        # https://github.com/fluent/helm-charts/tree/main/charts/fluent-bit
        fluentbit_chart_cw = eks_cluster.add_helm_chart(
            "fluentbit-cw",
            chart=get_chart_release(str(eks_version), FLUENTBIT, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), FLUENTBIT, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), FLUENTBIT, replicated_ecr_images_metadata),
            release="fluent-bit-cw",
            namespace="kube-system",
            values=deep_merge(
                {
                    "serviceAccount": {"create": False, "name": "fluentbit-cw"},
                    "config": {
                        "outputs": "[OUTPUT]\n    Name cloudwatch_logs\n    Match   *\n    region "
                        + self.region
                        + "\n    log_group_name "
                        + eks_cluster.cluster_name
                        + "fluent-bit-cloudwatch\n    log_stream_prefix from-fluent-bit-\n "
                        + "   auto_create_group true\n    log_retention_days "
                        + str(self.node.try_get_context("cloudwatch_container_insights_logs_retention_days"))
                        + "\n",
                        "filters.conf": "[FILTER]\n  Name  kubernetes\n  Match  kube.*\n  Merge_Log  On\n  Buffer_Size  0\n  Kube_Meta_Cache_TTL  300s\n"  # noqa: E501
                        + "[FILTER]\n    Name modify\n    Match *\n    Rename log log_data\n    Rename stream stream_name\n"  # noqa: E501
                        + "[FILTER]\n    Name record_modifier\n    Match *\n    Record log_type startup\n"
                        + "[FILTER]\n    Name record_modifier\n    Match *\n    Record log_type shutdown\n"
                        + "[FILTER]\n    Name record_modifier\n    Match *\n    Record log_type error\n"
                        + "[FILTER]\n    Name record_modifier\n    Match *\n    Record log_type exception\n"
                        + "[FILTER]\n    Name record_modifier\n    Match *\n    Record log_type ingestion\n"
                        + "[FILTER]\n    Name record_modifier\n    Match *\n    Record log_type processing\n"
                        + "[FILTER]\n    Name record_modifier\n    Match *\n    Record log_type output\n"
                        + "[FILTER]\n    Name record_modifier\n    Match *\n    Record log_type performance\n"
                        + "\n",
                    },
                },
                get_chart_values(replicated_ecr_images_metadata, FLUENTBIT),
            ),
        )
        fluentbit_chart_cw.node.add_dependency(fluentbit_cw_service_account)

    def _deploy_amazon_managed_prometheus(
        self, eks_cluster, eks_version, replicated_ecr_images_metadata, eks_addons_config
    ):
        """
        Deploys the Amazon Managed Service for Prometheus (AMP) for the EKS cluster.
        """
        # https://aws.amazon.com/blogs/mt/getting-started-amazon-managed-service-for-prometheus/
        # Create AMP workspace
        amp_workspace = aps.CfnWorkspace(self, "AMPWorkspace")

        # Create IRSA mapping
        amp_sa = eks_cluster.add_service_account("amp-sa", name="amp-iamproxy-service-account", namespace="kube-system")

        # Associate the IAM Policy
        amp_policy_statement_json_1 = {
            "Effect": "Allow",
            "Action": [
                "aps:RemoteWrite",
                "aps:QueryMetrics",
                "aps:GetSeries",
                "aps:GetLabels",
                "aps:GetMetricMetadata",
            ],
            "Resource": ["*"],
        }
        amp_sa.add_to_principal_policy(iam.PolicyStatement.from_json(amp_policy_statement_json_1))

        # Install Prometheus with a low 1 hour local retention to ship the metrics to the AMP
        # https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack
        # Changed this from just Prometheus to the Prometheus Operator for the additional functionality
        # Changed this to not use EBS PersistentVolumes so it'll work with Fargate Only Clusters
        # This should be acceptable as the metrics are immediatly streamed to the AMP
        amp_prometheus_chart = eks_cluster.add_helm_chart(
            "prometheus-chart",
            chart=get_chart_release(str(eks_version), PROMETHEUS_STACK, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), PROMETHEUS_STACK, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), PROMETHEUS_STACK, replicated_ecr_images_metadata),
            release="prometheus-for-amp",
            namespace="kube-system",
            values=deep_merge(
                {
                    "prometheus": {
                        "serviceAccount": {
                            "create": False,
                            "name": "amp-iamproxy-service-account",
                            "annotations": {
                                "eks.amazonaws.com/role-arn": amp_sa.role.role_arn,
                            },
                        },
                        "prometheusSpec": {
                            "storageSpec": {"emptyDir": {"medium": "Memory"}},
                            "remoteWrite": [
                                {
                                    "queueConfig": {
                                        "maxSamplesPerSend": 1000,
                                        "maxShards": 200,
                                        "capacity": 2500,
                                    },
                                    "url": amp_workspace.attr_prometheus_endpoint + "api/v1/remote_write",
                                    "sigv4": {"region": self.region},
                                }
                            ],
                            "retention": "1h",
                            "resources": {"limits": {"cpu": 1, "memory": "1Gi"}},
                        },
                    },
                    "alertmanager": {"enabled": False},
                    "grafana": {"enabled": False},
                    "prometheusOperator": {
                        "admissionWebhooks": {"enabled": False},
                        "tls": {"enabled": False},
                        "resources": {"requests": {"cpu": "0.25", "memory": "0.5Gi"}},
                    },
                    "kubeControllerManager": {"enabled": False},
                    "kubeScheduler": {"enabled": False},
                    "kubeProxy": {"enabled": False},
                    "nodeExporter": {"enabled": False},
                },
                get_chart_values(replicated_ecr_images_metadata, PROMETHEUS_STACK),
            ),
        )
        amp_prometheus_chart.node.add_dependency(amp_sa)
        return amp_sa, amp_workspace, amp_prometheus_chart

    def _install_nvidia_device_plugin(self, eks_cluster, eks_version, replicated_ecr_images_metadata):
        """
        Installs the NVIDIA Device Plugin for the EKS cluster.
        """

        plugin = "nvidia-device-plugin"
        nvidia_device_plugin_namespace = eks_cluster.add_manifest(
            f"{plugin}-namespace",
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": plugin},
            },
        )

        # This should be acceptable as the metrics are immediatly streamed to the AMP
        nvidia_device_plugin_chart = eks_cluster.add_helm_chart(
            plugin,
            chart=get_chart_release(str(eks_version), NVIDIA_DEVICE_PLUGIN, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), NVIDIA_DEVICE_PLUGIN, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), NVIDIA_DEVICE_PLUGIN, replicated_ecr_images_metadata),
            release=plugin,
            namespace=plugin,
            create_namespace=False,
            values=deep_merge(
                {
                    "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                    "affinity": {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchExpressions": [
                                            {"key": "nvidia.com/gpu.present", "operator": "In", "values": ["true"]}
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                },
                get_chart_values(replicated_ecr_images_metadata, NVIDIA_DEVICE_PLUGIN),
            ),
        )

        nvidia_device_plugin_chart.node.add_dependency(nvidia_device_plugin_namespace)

    def _deploy_grafana_for_amp(
        self,
        eks_cluster,
        project_dir,
        eks_version,
        amp_sa,
        amp_workspace,
        replicated_ecr_images_metadata,
        eks_addons_config,
        amp_prometheus_chart,
        awslbcontroller_chart,
    ):
        """
        Deploys a self-managed Grafana instance to visualize the AMP metrics.
        """
        # Install the Grafana Helm chart
        # Install a self-managed Grafana to visualise the AMP metrics
        # NOTE You likely want to use the AWS Managed Grafana (AMG) in production
        # We are using this as AMG requires SSO/SAML and is harder to include in the template
        # NOTE We are not enabling PersistentVolumes to allow this to run in Fargate making this immutable
        # Any changes to which dashboards to use should be deployed via the ConfigMaps in order to persist
        # https://github.com/grafana/helm-charts/tree/main/charts/grafana#sidecar-for-dashboards
        # For more information see https://github.com/grafana/helm-charts/tree/main/charts/grafana
        amp_grafana_chart = eks_cluster.add_helm_chart(
            "amp-grafana-chart",
            chart=get_chart_release(str(eks_version), GRAFANA, replicated_ecr_images_metadata),
            version=get_chart_version(str(eks_version), GRAFANA, replicated_ecr_images_metadata),
            repository=get_chart_repo(str(eks_version), GRAFANA, replicated_ecr_images_metadata),
            release="grafana-for-amp",
            namespace="kube-system",
            values=deep_merge(
                {
                    "serviceAccount": {
                        "name": "amp-iamproxy-service-account",
                        "annotations": {"eks.amazonaws.com/role-arn": amp_sa.role.role_arn},
                        "create": False,
                    },
                    "grafana.ini": {"auth": {"sigv4_auth_enabled": True}},
                    "service": {
                        "type": "LoadBalancer",
                        "annotations": {
                            "service.beta.kubernetes.io/aws-load-balancer-type": "nlb-ip",
                            "service.beta.kubernetes.io/aws-load-balancer-internal": "true",
                        },
                    },
                    "datasources": {
                        "datasources.yaml": {
                            "apiVersion": 1,
                            "datasources": [
                                {
                                    "name": "Prometheus",
                                    "type": "prometheus",
                                    "access": "proxy",
                                    "url": amp_workspace.attr_prometheus_endpoint,
                                    "isDefault": True,
                                    "editable": True,
                                    "jsonData": {
                                        "httpMethod": "POST",
                                        "sigV4Auth": True,
                                        "sigV4AuthType": "default",
                                        "sigV4Region": self.region,
                                    },
                                }
                            ],
                        }
                    },
                    "sidecar": {
                        "dashboards": {
                            "enabled": True,
                            "label": "grafana_dashboard",
                        }
                    },
                    "resources": {"requests": {"cpu": "0.25", "memory": "0.5Gi"}},
                },
                get_chart_values(replicated_ecr_images_metadata, GRAFANA),
            ),
        )
        amp_grafana_chart.node.add_dependency(amp_prometheus_chart)
        amp_grafana_chart.node.add_dependency(awslbcontroller_chart)

        # Dashboards for Grafana from the grafana-dashboards.yaml file
        grafana_dashboards_yaml_file = open("monitoring-config/grafana-dashboards.yaml", "r")
        grafana_dashboards_yaml = list(yaml.load_all(grafana_dashboards_yaml_file, Loader=yaml.FullLoader))
        grafana_dashboards_yaml_file.close()
        loop_iteration = 0
        for value in grafana_dashboards_yaml:
            loop_iteration = loop_iteration + 1
            manifest_id = "GrafanaDashboard" + str(loop_iteration)
            eks_cluster.add_manifest(manifest_id, value)

    def _configure_rbac(self, eks_cluster):
        """
        Configures the RBAC (Role-Based Access Control) for the EKS cluster.
        """
        eks_cluster.add_manifest(
            "cluster-role",
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": "system-access"},
                "rules": [
                    {
                        "apiGroups": ["", "storage.k8s.io"],
                        "resources": ["persistentVolumes", "nodes", "storageClasses"],
                        "verbs": ["get", "watch", "list"],
                    }
                ],
            },
        )

        # Defining the read only access role
        readonly_role_access_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "readonly-role"},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["*"],
                    "verbs": ["get", "list", "watch"],
                }
            ],
        }

        # Deploying the read only access role to the EKS cluster
        eks_cluster.add_manifest("readonly_role_access_manifest", readonly_role_access_manifest)

        # Defining the admin role access role
        admin_role_access_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "admin-role"},
            "rules": [
                {
                    "apiGroups": ["*"],
                    "resources": ["*"],
                    "verbs": [
                        "get",
                        "list",
                        "watch",
                        "create",
                        "delete",
                        "update",
                        "patch",
                    ],
                }
            ],
        }

        # Deploying the admin role access role to the EKS cluster
        eks_cluster.add_manifest("admin_role_access_manifest", admin_role_access_manifest)

        # Defining the poweruser role access role
        poweruser_role_access_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "poweruser-role"},
            "rules": [
                {
                    "apiGroups": ["", "apps", "extensions"],
                    "resources": ["*"],
                    "verbs": [
                        "get",
                        "list",
                        "watch",
                        "create",
                        "delete",
                        "update",
                        "patch",
                    ],
                }
            ],
        }

        # Deploying the poweruser role access role to the EKS cluster
        eks_cluster.add_manifest("poweruser_role_access_manifest", poweruser_role_access_manifest)

        # Define the RoleBinding manifest as a dictionary for readonly role
        readonly_role_binding_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "readonly-role-binding"},
            "subjects": [
                {
                    "kind": "Group",
                    "name": "readonly-group",
                    "apiGroup": "rbac.authorization.k8s.io",
                }
            ],
            "roleRef": {
                "kind": "Role",
                "name": "readonly-role",
                "apiGroup": "rbac.authorization.k8s.io",
            },
        }

        # Deploy the RoleBinding manifest to the EKS cluster for readonly role
        eks_cluster.add_manifest("readonly_role_binding_manifest", readonly_role_binding_manifest)

        # Define the RoleBinding manifest as a dictionary for admin role
        admin_role_binding_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "admin-role-binding"},
            "subjects": [
                {
                    "kind": "Group",
                    "name": "admin",
                    "apiGroup": "rbac.authorization.k8s.io",
                }
            ],
            "roleRef": {
                "kind": "ClusterRole",
                "name": "admin-role",
                "apiGroup": "rbac.authorization.k8s.io",
            },
        }

        # Deploy the RoleBinding manifest to the EKS cluster for admin role
        eks_cluster.add_manifest("admin_role_binding_manifest", admin_role_binding_manifest)

        # Define the RoleBinding manifest as a dictionary for poweruser role
        poweruser_role_binding_manifest = {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "poweruser-role-binding"},
            "subjects": [
                {
                    "kind": "Group",
                    "name": "poweruser-group",
                    "apiGroup": "rbac.authorization.k8s.io",
                }
            ],
            "roleRef": {
                "kind": "Role",
                "name": "poweruser-role",
                "apiGroup": "rbac.authorization.k8s.io",
            },
        }

        # Deploy the RoleBinding manifest to the EKS cluster for admin role
        eks_cluster.add_manifest("poweruser_role_binding_manifest", poweruser_role_binding_manifest)

    def _add_suppressions(self) -> None:
        NagSuppressions.add_stack_suppressions(
            self,
            apply_to_nested_stacks=True,
            suppressions=[
                {
                    "id": "AwsSolutions-IAM5",
                    "reason": "Resource access restriced to IDF resources",
                },
                {
                    "id": "AwsSolutions-IAM4",
                    "reason": "Managed Policies are for service account roles only",
                },
                {
                    "id": "AwsSolutions-EKS1",
                    "reason": "No Customer data resides on the compute resources",
                },
                {
                    "id": "AwsSolutions-KMS5",
                    "reason": "The KMS Symmetric key does not have automatic key rotation enabled",
                },
                {
                    "id": "AwsSolutions-AS3",
                    "reason": "The ASG does not have notifications setup",
                },
                {"id": "AwsSolutions-L1", "reason": "Suppress error caused by python_3_12 release in December"},
                {"id": "AwsSolutions-SF1", "reason": "Log all is not required"},
                {"id": "AwsSolutions-SF2", "reason": "X-Ray tracing is not required"},
            ],
        )

    def _fetch_cidrs_from_ssm(self, ips_to_whitelist: List[str]):
        """
        Fetches the CIDR ranges from the list of SSM Parameters.
        """
        cidrs_list = []

        for parameter_name in ips_to_whitelist:
            string_value = ssm.StringParameter.from_string_parameter_attributes(
                self, f"SSM-{parameter_name}", parameter_name=parameter_name
            ).string_value
            cidrs_list.append(string_value)

        return cidrs_list

    def _fetch_aws_cidrs(self):
        """
        Fetches the CIDR ranges from the AWS Services
        https://docs.aws.amazon.com/vpc/latest/userguide/aws-ip-work-with.html#filter-json-file
        """

        aws_cidrs_list = []
        aws_url = "https://ip-ranges.amazonaws.com/ip-ranges.json"
        aws_ips = requests.get(aws_url, allow_redirects=True).json()

        for item in aws_ips["prefixes"]:
            if item.get("service") in ("CODEBUILD") and item.get("region") == self.region:
                aws_cidrs_list.append(item["ip_prefix"])

        return aws_cidrs_list
