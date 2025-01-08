# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Main application"""

import json
import os
import sys

import helmparser.helm.commands as helm
from helmparser.arguments import parse_args
from helmparser.logging import logger
from helmparser.parser import parser
from helmparser.utils.utils import deep_merge
from replication_utils.utils import get_credentials

project_path = os.path.realpath(os.path.dirname(__file__))
repo_secret = os.getenv("SEEDFARMER_PARAMETER_HELM_REPO_SECRET_NAME", None)
repo_key = os.getenv("SEEDFARMER_PARAMETER_HELM_REPO_SECRET_KEY", None)

def main() -> None:
    """Main handler"""
    args = parse_args(sys.argv[1:])

    logger.info("EKS version: %s", args.eks_version)

    logger.info(
        "EKS node AMI image version: %s",
        parser.get_ami_version(args.versions_dir, args.eks_version),
    )

    images_wip = []

    additional_images = parser.get_additional_images(args.versions_dir, args.eks_version)
    docker_mappings = parser.get_docker_mappings(args.versions_dir, args.eks_version)

    for _, image in additional_images.items():
        images_wip.append(image)

    updated_additional_images = {}
    for name, image in additional_images.items():
        updated_additional_images[name] = f"{args.registry_prefix}{image}"

    additional_images_json = {"additional_images": updated_additional_images}

    workloads_data = parser.get_workloads(args.versions_dir, args.eks_version)

    if args.update_helm:
        username,pwd = get_credentials(repo_secret, repo_key)
        for workload, values in workloads_data.items():
            logger.info("Syncing %s", workload)
            helm.add_repo(workload, values["repository"],username,pwd)

        helm.update_repos()

    custom_chart_values = {}

    parsed_charts = {}  # type: ignore
    for workload, values in workloads_data.items():
        parsed_charts[workload] = {}
        if "images" not in values:
            continue

        logger.debug("Getting %s data", workload)
        parsed_charts[workload]["chart"] = helm.show("chart", f"{workload}/{values['name']}", values["version"])

        parsed_charts[workload]["values"] = helm.show("values", f"{workload}/{values['name']}", values["version"])

        if "subcharts" in values:
            parsed_charts[workload]["subcharts"] = {}
            for subchart in values["subcharts"]:
                parsed_charts[workload]["subcharts"][subchart] = {}
                parsed_charts[workload]["subcharts"][subchart] = helm.show_subchart(
                    project_path, workload, values["name"], subchart, values["version"]
                )

    for workload, values in workloads_data.items():
        custom_chart_values[workload] = {
            "helm": {
                "name": values["name"],
                "repository": values["repository"],
                "version": values["version"],
            },
            "values": {},
        }
        if args.replicate_charts:
            custom_chart_values[workload]["helm"]["srcRepository"] =  values.get("repository")
            new_r_name =  values["repository"].rstrip('/').split('/')[-1]
            custom_chart_values[workload]["helm"]["repository"] = f"oci://{args.registry_prefix}{new_r_name}/{values['name']}"

        logger.debug("Chart %s:", workload)

        if "images" in values:
            logger.debug("\tImages:")

            for image_name, image_data in values["images"].items():
                registry = None
                repository = None
                tag = None

                # parse registries first
                for k, v in image_data.items():
                    if k == "registry":
                        registry = parser.parse_value(parsed_charts[workload], values, image_name, v, k)

                        if registry:
                            custom_chart_values[workload]["values"] = parser.add_branch_to_dict(
                                custom_chart_values[workload]["values"],
                                v,
                                f"{args.registry_prefix}{registry}",
                            )

                        continue

                # parse repositories and tags
                for k, v in image_data.items():
                    if k == "repository":
                        if "name" in v:
                            repository = v["name"]
                            continue

                        repository = parser.parse_value(parsed_charts[workload], values, image_name, v, k)

                        repository_in_chart_values = repository
                        if not registry:
                            repository_in_chart_values = f"{args.registry_prefix}{repository}"

                        custom_chart_values[workload]["values"] = parser.add_branch_to_dict(
                            custom_chart_values[workload]["values"],
                            v,
                            repository_in_chart_values,
                        )

                        continue

                    if k == "tag":
                        tag = parser.parse_value(parsed_charts[workload], values, image_name, v, k)

                        custom_chart_values[workload]["values"] = parser.add_branch_to_dict(
                            custom_chart_values[workload]["values"],
                            v,
                            tag,
                        )

                        continue

                # remove some values
                for k, v in image_data.items():
                    # set value to empty, e.g. digest as it will be different after push to ECR
                    if k == "remove":
                        custom_chart_values[workload]["values"] = parser.add_branch_to_dict(
                            custom_chart_values[workload]["values"],
                            v,
                            "",
                        )
                        continue

                image = repository
                if registry:
                    image = f"{registry}/{repository}"
                if tag:
                    image += f":{tag}"

                logger.debug("\t\t%s", image)                
                images_wip.append(image)

        logger.debug("\tCustom chart values:")
        logger.debug("\t\t%s", json.dumps(custom_chart_values[workload]))
    sorted_images = sorted(set(images_wip))
    updated_images = []
    
    def process_image(full_image,docker_mappings):
        if not docker_mappings:
            return full_image
        i_t = full_image.split(":")
        image = i_t[0]
        tag = i_t[1] if len(i_t) > 1 else "latest"
        if "." in image:
            s = image.rstrip('/').split('/')
            dns = s[0]
            reassembled_url = "/".join(s[1:])
            if dns in docker_mappings.keys():
                return f"{docker_mappings.get(dns)}/{reassembled_url}:{tag}"
        else:
            if docker_mappings.get("default"):
                return f"{docker_mappings.get('default')}/{image}:{tag}"
        return full_image
            
    for name, image in additional_images.items():
        working_image = process_image(image, docker_mappings)
        updated_images.append({"src":working_image,"target":f"{args.registry_prefix}{image}"})
    for image in images_wip:
        working_image = process_image(image, docker_mappings)
        updated_images.append({"src":working_image,"target":f"{args.registry_prefix}{image}"})
            
    ami_json = {"ami": {"version": parser.get_ami_version(args.versions_dir, args.eks_version)}}
    charts_json = {"charts": custom_chart_values}

    #  Add custom rules here....
    try:
        # cert-manager v1.6.x has removed the appVersion definition from crd...
        if custom_chart_values['cert_manager']['values']['appVersion']:
            del custom_chart_values['cert_manager']['values']['appVersion']
    
    except Exception as e:
        logger.error("Error: %s", e)
        raise e

    with open(
        os.path.join(project_path, "replication-result.json"),
        "w",
        encoding="utf-8",
    ) as file:
        file.write(json.dumps(deep_merge(ami_json, charts_json, additional_images_json)))

    with open(
        os.path.join(project_path, "updated_images.json"),
        "w",
        encoding="utf-8",
    ) as file:
        file.write(json.dumps(updated_images, indent=4))

if __name__ == "__main__":
    main()
