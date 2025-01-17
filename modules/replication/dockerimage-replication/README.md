## Introduction

Docker Images and Helm Chart Replication

### Description

This module replicates Docker images and Helm charts from the list of provided Helm charts and any docker image from a public registry into an AWS account's Private ECR. For deploying EKS module or any container related apps in isolated subnets (which has access to AWS APIs via Private endpoints), the respective docker images and helm charts should be available internally in an ECR repo as a prerequisite. This module will generate two files for internal processing:

- `replication_result.json` - an inventory of the charts and image information, this provides the source and target address of the charts
- `updated_images.json` - the src and target of all images referenced

The `replication_result.json` gets copied to a new filename as indicated by the output parameter `S3Object` (see below).  This file serves as the chart value overrides when the helm charts are applied.  NOTE: this file can also apply changes to values when the charts are deployed on EKS.

ALL resulting ECR repositories (images and helm charts) are scoped to the project, not the deployment, so they can be used across deployments within a project.  




***CLEANUP***

The cleanup workflow invokes a python script which deletes the replicated docker images from ECR whose prefix starts with `project_name`. This may cause issues if the replicated images are being used by other applications in the same/cross account. The current `deployspec.yaml` doesn't call the python script to cleanup the images, however an end-user can evaluate the need/risk associated and uncomment the relevant instruction under `destroy` phase.  Also, you can run `delete_repos.py` with the project name to remove all ECR repos:
```bash
python delete_images.py <project-name>
```

## Image DNS Mappings
See [README-DATAFILES](README-DATAFILES.md)

## Helm DNS configuration
See [README-DATAFILES](README-DATAFILES.md)


## AWS Secret Support
When using custom DNS for Helm Charts or Images and they are protected by Basic Auth, you can provide an AWS SecretManager service entry.  The AWS Secret must contain the `username` and `password` (with those keys):
```json
{
  "username": "my-username",
  "password": "my-password"
}
```
If you want to support multiple auth credentials in a single secret, you can nest ONE LEVEL deep different credentials:
```json
{
  "artefactory": 
    {
    "username": "my-username",
    "password": "my-password"
    },
  "my-local-key":
    {
    "username": "my-username",
    "password": "my-password"
    },
  "some-key":
    {
    "username": "my-username",
    "password": "my-password"
    }  
}
```
If using nested auth credentials in the AWS SecretManager, be sure to leverage the `HelmRepoSecretKey` or `HelmDistroSecretKey` based on the use case.

The following `Optional Parameters` can be used with the AWS SecretsManager:
[`HelmRepoSecretName`, `HelmRepoSecretKey`,`HelmDistroSecretName`,`HelmDistroSecretKey`]


### Input Parameters

#### Required Parameters

- `eks_version`: The EKS Cluster version to lock the version to

#### Optional Parameters

- `HelmRepoSecretName`: If using a secret to access the helm or images hosted in a private DNS endpoint, this is the name of the AWS Secret that will provide a username and password 
- `HelmRepoSecretKey`: If the AWS Secret for the HelmRepo has a nested entry (one nest only) this  is the key used to access that nest
- `HelmDistroUrl`: If using a private DNS to host the helm CLI, this is the DNS that URL (full path with tar.qz name) used to fetch 
- `HelmDistroSecretName`: used with `HelmDistroUrl`, this is the name of the AWS Secret used for basic auth.  If not provided, no basic auth will be referenced.
- `HelmDistroSecretKey`:  If the AWS Secret for the HelmDistro has a nested entry (one nest only) this  is the key used to access that nest
 


#### Required Files

- `dataFiles`: The docker replication module consumes the EKS version specific helm charts inventory to replicate the docker images
- `<eks-version>.yaml` - the version override support for your cluster

There should be at least two (2) datafiles:
 - the `default.yaml` that has all the relative info (as provided by the idf repository)
 - the version yaml (ex `1.29.yaml`) that has the proper version updates to match your EKS version

#### Manifest Example declaration

```yaml
name: replication
path: git::https://github.com/awslabs/idf-modules.git//modules/replication/dockerimage-replication?ref=release/1.13.0
dataFiles:
  - filePath: data/eks_dockerimage-replication/versions/1.29.yaml
  - filePath: data/eks_dockerimage-replication/versions/default.yaml
parameters:
  - name: eks-version
    value: "1.29"
  - name: HelmRepoSecretName
    value: replicationbmw
  - name: HelmDistroSecretName
    value: replicationbmw
  - name: HelmDistroUrl
    value: https://somehostedurl.com/something/relativeurl/helm-v3.11.3-linux-amd64.tar.gz

```

### Module Metadata Outputs

- `S3Bucket` - the name of the bucket created to house the output values file used by EKS
- `S3FullPath` - the full path of the output values file (use this in the eks manifest!!)
- `S3Object` - the name of the file with the values 
- `s3_bucket`: same as `S3Bucket` but is considered deprecated
- `s3_full_path`: same as `S3FullPath` but is considered deprecated
- `s3_object`: same as `S3Object` but is considered deprecated

```json
{
  "repl": {
    "S3Bucket": "idftest-dkr-img-rep-md-us-east-1-123456789012",
    "S3FullPath": "idftest-dkr-img-rep-md-us-east-1-123456789012/repltestrepl-repl-repl-metadata.json",
    "S3Object": "repltestrepl-repl-repl-metadata.json",
    "s3_bucket": "idftest-dkr-img-rep-md-us-east-1-123456789012", // For backward compatibility
    "s3_full_path": "idftest-dkr-img-rep-md-us-east-1-123456789012/repltestrepl-repl-repl-metadata.json", // For backward compatibility
    "s3_object": "repltestrepl-repl-repl-metadata.json" // For backward compatibility
  }
}
```


