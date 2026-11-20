#!/usr/bin/env bash
# cost-check.sh - read-only scan of the AWS account for resources that cost money while they exist.
#
# Every enabled region is checked for the usual leftovers (instances, volumes, Elastic IPs). The home
# region (AWS_REGION, default ap-southeast-1) is also checked for every other billable service this
# project could create. Only describe/list/get calls are made. They are free; the Cost Explorer API
# is billed per request and is never called.
#
# Exit codes: 0 nothing billable found, 1 billable leftovers found,
#             2 a check could not run (an incomplete scan is never reported as clean).
set -uo pipefail

HOME_REGION="${AWS_REGION:-ap-southeast-1}"
checks=0
found=0
errors=0
err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

# check LABEL REGION AWS_ARGS...
# Runs one read-only listing whose text output has one resource per line. Prints only what needs
# attention: the resources it found, or why the check could not run.
check() {
  local label="$1" region="$2" out
  shift 2
  checks=$((checks + 1))
  if ! out="$(aws "$@" --region "$region" --output text 2>"$err_file")"; then
    check_failed "$label" "$region"
    return
  fi
  report "$label" "$region" "$out"
}

# check_failed LABEL REGION: says why a check could not run. An incomplete scan is never reported as clean.
check_failed() {
  printf 'ERROR  %s (%s): %s\n' "$1" "$2" "$(head -n 1 "$err_file")"
  errors=$((errors + 1))
}

# report LABEL REGION OUTPUT: lists the resources in OUTPUT (one per line), if there are any.
report() {
  local lines
  # An empty result prints nothing, or "None" for a null.
  mapfile -t lines < <(grep -v -x -e '' -e 'None' <<<"$3")
  if [ "${#lines[@]}" -gt 0 ]; then
    printf 'FOUND  %s (%s):\n' "$1" "$2"
    printf '         %s\n' "${lines[@]}"
    found=$((found + ${#lines[@]}))
  fi
}

# Customer-managed KMS keys cost USD 1 a month each. list-keys also returns the free AWS-managed keys
# (aws/ssm, aws/ebs), so each key is described. Keys already scheduled for deletion have been dealt with.
check_kms_keys() {
  local region="$1" label="KMS keys (customer-managed)" keys key info customer=""
  checks=$((checks + 1))
  if ! keys="$(aws kms list-keys --query 'Keys[].KeyId' --region "$region" --output text 2>"$err_file")"; then
    check_failed "$label" "$region"
    return
  fi
  for key in $keys; do
    [ "$key" = None ] && continue
    if ! info="$(aws kms describe-key --key-id "$key" --query 'KeyMetadata.[KeyManager,KeyState]' \
        --region "$region" --output text 2>"$err_file")"; then
      check_failed "$label" "$region"
      return
    fi
    case "$info" in
      CUSTOMER*PendingDeletion) ;;
      CUSTOMER*) customer+="$key ${info#CUSTOMER}"$'\n' ;;
    esac
  done
  report "$label" "$region" "$customer"
}

# The classic surprise bills: something left running, or started in the wrong region.
check_common_leftovers() {
  local region="$1"
  check "EC2 instances (not terminated)" "$region" ec2 describe-instances \
    --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
    --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]'
  check "EBS volumes" "$region" ec2 describe-volumes --query 'Volumes[].[VolumeId,Size,State]'
  check "Elastic IPs" "$region" ec2 describe-addresses --query 'Addresses[].[PublicIp,AllocationId]'
}

# Every other billable service this project could create, in the one region it works in.
check_home_region_services() {
  local region="$1"
  check "EBS snapshots (owned)" "$region" ec2 describe-snapshots --owner-ids self \
    --query 'Snapshots[].[SnapshotId,VolumeSize]'
  check "Public IPv4 addresses in use" "$region" ec2 describe-network-interfaces \
    --query 'NetworkInterfaces[?Association.PublicIp].[NetworkInterfaceId,Association.PublicIp]'
  check "NAT gateways" "$region" ec2 describe-nat-gateways --filter Name=state,Values=pending,available \
    --query 'NatGateways[].[NatGatewayId,State]'
  check "Interface VPC endpoints" "$region" ec2 describe-vpc-endpoints \
    --filters Name=vpc-endpoint-type,Values=Interface,GatewayLoadBalancer \
    --query 'VpcEndpoints[].[VpcEndpointId,ServiceName]'
  check "Load balancers (ALB/NLB/GWLB)" "$region" elbv2 describe-load-balancers \
    --query 'LoadBalancers[].[LoadBalancerName,Type]'
  check "Classic load balancers" "$region" elb describe-load-balancers \
    --query 'LoadBalancerDescriptions[].[LoadBalancerName]'
  check "EKS clusters" "$region" eks list-clusters --query 'clusters[]'
  check "RDS DB instances" "$region" rds describe-db-instances \
    --query 'DBInstances[].[DBInstanceIdentifier,DBInstanceClass]'
  check "RDS DB clusters" "$region" rds describe-db-clusters --query 'DBClusters[].[DBClusterIdentifier]'
  check "Secrets Manager secrets" "$region" secretsmanager list-secrets --query 'SecretList[].[Name]'
  # A request costs nothing itself, but an open or active one (persistent, or waiting for capacity) can
  # start an instance on its own. Closed and cancelled one-time requests are history. Home region only:
  # the instances such a request starts are already checked in every region.
  check "Spot requests (open or active)" "$region" ec2 describe-spot-instance-requests \
    --filters Name=state,Values=open,active \
    --query 'SpotInstanceRequests[].[SpotInstanceRequestId,State,Type]'
  # Services the budget deny policy blocks creating. Made by hand, each would bill while it exists.
  # Route 53 is global, so one call covers every region.
  check "Route 53 hosted zones" "$region" route53 list-hosted-zones --query 'HostedZones[].[Id,Name]'
  check "ECR repositories" "$region" ecr describe-repositories --query 'repositories[].[repositoryName]'
  check "Lightsail instances" "$region" lightsail get-instances --query 'instances[].[name,state.name]'
  check "Lightsail databases" "$region" lightsail get-relational-databases \
    --query 'relationalDatabases[].[name,state]'
  check "SageMaker notebook instances" "$region" sagemaker list-notebook-instances \
    --query 'NotebookInstances[].[NotebookInstanceName,NotebookInstanceStatus]'
  check "SageMaker endpoints" "$region" sagemaker list-endpoints --query 'Endpoints[].[EndpointName,EndpointStatus]'
  check_kms_keys "$region"
}

echo "== AWS cost check (read-only) =="
if ! identity="$(aws sts get-caller-identity --query Arn --output text --region "$HOME_REGION" 2>"$err_file")"; then
  echo "ERROR: cannot identify the caller: $(head -n 1 "$err_file")"
  echo "Log in first: aws login --profile devops"
  exit 2
fi
echo "Identity: $identity"
case "$identity" in
  *:root) echo "WARNING: this is the root user. Use the IAM user instead (aws login --profile devops)." ;;
esac

if ! regions="$(aws ec2 describe-regions --query 'Regions[].RegionName' --output text \
    --region "$HOME_REGION" 2>"$err_file")"; then
  printf 'ERROR  listing enabled regions: %s (checking %s only)\n' "$(head -n 1 "$err_file")" "$HOME_REGION"
  errors=$((errors + 1))
  regions="$HOME_REGION"
fi
region_count=0
for region in $regions; do
  region_count=$((region_count + 1))
  check_common_leftovers "$region"
done
check_home_region_services "$HOME_REGION"

echo "Ran $checks checks: $HOME_REGION in full, and instances, volumes and Elastic IPs in $region_count enabled regions."
echo
if [ "$found" -gt 0 ]; then
  echo "✗ Billable leftovers found: $found. Destroy them (make cloud-down, or the console if Terraform"
  echo "  lost track of them), then run make cost-check again."
fi
if [ "$errors" -gt 0 ]; then
  echo "✗ $errors check(s) could not run, so the account is NOT confirmed clean."
fi
[ "$found" -gt 0 ] && exit 1
[ "$errors" -gt 0 ] && exit 2
echo "✓ No billable leftovers."
echo "  Not listed: \$0-at-rest persistent resources (S3 state bucket, budgets, IAM, SSM Standard parameters)."
