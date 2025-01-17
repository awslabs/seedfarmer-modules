## DATAFILES REFERENCE

### Background
The datafiles serve to configure the EKS module with the proper helm chart and image information necessary to deploy on EKS.  There are two (2) files **necessary** for this support:
- `default.yaml` - this base in which the data is provided
- `<eks-version>.yaml` - the corresponding version file to override the version indicated in default.yaml to match the EKS cluster you are using
    - for example `1.29.yaml` corresponds to an EKS cluster v 1.29

If you are running an EKS cluster that has access to the public internet and do not want to change anything, there is no need for you to use the replication module.  Just refer to the proper data files in your manifest for the EKS module.

## Why the replication module
The replication module serves three (3) purposes
- supports EKS clusters that run in a private or isolate subnet
- provides an output file that can be used to pass all / any customizations to the `values` of a helm chart used by the EKS module 
- allows altering the source DNS where helm charts and images are fetched (and added to the output file)

### Support for EKS isolated clusters
The replication module will take and fetch ALL the images and Helm charts as defined in the combination of the `default.yaml` and the `1.29.yaml` to AWS ECR, creating one repository per helm chart or image.  The helm chart storage is OCI compliant and allows versioning of each chart.  The images are docker compliant and allow versioning of each image.

### Output file for value overrides in Helm charts
The outputs from the replication module are:
- a fully populated ECR repository (charts and images)
- a metadata file that contains all relative `value` information / configuration for the EKS cluster module to use when deploying

This metadata file (as referenced in the output of the replication module) is the MINIMUM amount of information necessary for:
- overriding the DNS to fetch a chart for deployment (CDK code in EKS module will pull from AWS ECR without the need for internet connectivity)
- overriding the DNS where the EKS cluster will pull images when deploying 

This file can be `augmented` with additional value overrides AS LONG AS the existing information is not altered.  The use-case here is to provide all the customizations that the helm chart can allow.  


### Altering the Source DNS Where Charts and Images are fetched
This feature will allow end users to change the DNS in which charts are fetched (for example a private helm-compliant repository) and change the DNS in which the images are fetched (for example a private docker-compliant repository).  

#### Changing the Helm DNS for charts
This **MUST** occur in the `default.yaml` file used.  The `default.yaml` defines where the helm chart is fetched.  For example, the following is defined:
```yaml
  alb_controller:
    name: aws-load-balancer-controller
    repository: "https://aws.github.io/eks-charts"
    version: 1.3.3
    images:
      alb_controller:
        repository:
          location: values
          path: image.repository
        tag:
          location: values
          path: image.tag
```

To change the DNS in which the the Helm chart is fetched, change the `repository` in the `default.yaml` :
```yaml
  alb_controller:
    name: aws-load-balancer-controller
    repository: "https://my-crazy-dns.com/something/charts"
    version: 1.3.3
    images:
      alb_controller:
        repository:
          location: values
          path: image.repository
        tag:
          location: values
          path: image.tag
```
You can do this for any chart in the file.  If your DNS is protected by Basic Auth, you MUST use the `HelmRepoSecretName` as defined in the [README](README.md) and comply with the standard as defined.  The `HelmRepoSecretKey` is optional.  

#### Changing the DNS for images
To allow for overriding the DNS in which images are fetched, this is done via a mapping defined in the `<eks-version>.yaml` file.  This does a batch replace for any DNS defined.

Let's use the `1.29.yaml` as an example.  This file currently has NO MAPPINGS defined.  But, you can alter it with a `docker_mappings` section.  For example:

```yaml
docker_mappings:
    quay.io: my-hosted-image/platform-docker-remote-quay-io
    registry.k8s.io: my-hosted-image/platform-docker-remote-registry-k8s-io
    docker.io: my-hosted-image/platform-docker-remote-hub-docker-com
    ghcr.io: my-hosted-image/platform-docker-remote-ghcr-io
    public.ecr.aws: my-hosted-image/platform-docker-remote-public-ecr-aws
    cr.fluentbit.io: my-hosted-image/platform-docker-remote-cr-fluentbit-io
    default: my-hosted-image/platform-docker-remote-hub-docker-com
```

This section tells the replication module to look for EACH key and replace with the value.  Any image that is hosed at `quay.io` will be replaced with `my-hosted-image/platform-docker-remote-quay-io`.

The `default` entry is enacted if an image does not match any key.  It is up to your discretion whether to add a `default` key or not.  Any DNS that does not match exactly as the key(s) provided will remain unmodified. 

As with the charts DNS: if your DNS is protected by Basic Auth, you MUST use the `HelmRepoSecretName` as defined in the [README](README.md) and comply with the standard as defined.  The `HelmRepoSecretKey` is optional. 