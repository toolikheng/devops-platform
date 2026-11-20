#!/usr/bin/env bash
# cost-check.sh - read-only scan of the AWS account for resources that cost money while they exist.
#
# The home region (AWS_REGION, default ap-southeast-1) gets the full list of checks. Every other
# enabled region is checked for the most common leftovers: instances, volumes and Elastic IPs.
# Only describe/list/get calls are made. They are free; the Cost Explorer API is billed per request
# and is never called.
#
# Exit codes: 0 nothing billable found, 1 billable leftovers found,
#             2 a check could not run (an incomplete scan is never reported as clean).
set -uo pipefail

HOME_REGION="${AWS_REGION:-ap-southeast-1}"
found=0
errors=0
show_ok=1
err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

# check LABEL REGION AWS_ARGS...
# Runs one read-only listing whose text output has one resource per line, and reports the result.
check() {
  local label="$1" region="$2" out
  shift 2
  if ! out="$(aws "$@" --region "$region" --output text 2>"$err_file")"; then
    printf '  ERROR  %s (%s): %s\n' "$label" "$region" "$(head -n 1 "$err_file")"
    errors=$((errors + 1))
    return
  fi
  # An empty result prints nothing, or "None" for a null.
  out="$(grep -v -x -e '' -e 'None' <<<"$out")"
  if [ -z "$out" ]; then
    [ "$show_ok" -eq 1 ] && printf '  ok     %s\n' "$label"
  else
    local lines
    mapfile -t lines <<<"$out"
    printf '  FOUND  %s (%s):\n' "$label" "$region"
    printf '           %s\n' "${lines[@]}"
    found=$((found + ${#lines[@]}))
  fi
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

echo "Region $HOME_REGION (full check):"
check "EC2 instances (not terminated)" "$HOME_REGION" ec2 describe-instances \
  --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]'
check "EBS volumes" "$HOME_REGION" ec2 describe-volumes --query 'Volumes[].[VolumeId,Size,State]'
check "EBS snapshots (owned)" "$HOME_REGION" ec2 describe-snapshots --owner-ids self \
  --query 'Snapshots[].[SnapshotId,VolumeSize]'
check "Elastic IPs" "$HOME_REGION" ec2 describe-addresses --query 'Addresses[].[PublicIp,AllocationId]'
check "Public IPv4 addresses in use" "$HOME_REGION" ec2 describe-network-interfaces \
  --query 'NetworkInterfaces[?Association.PublicIp].[NetworkInterfaceId,Association.PublicIp]'
check "NAT gateways" "$HOME_REGION" ec2 describe-nat-gateways --filter Name=state,Values=pending,available \
  --query 'NatGateways[].[NatGatewayId,State]'
check "Interface VPC endpoints" "$HOME_REGION" ec2 describe-vpc-endpoints \
  --filters Name=vpc-endpoint-type,Values=Interface,GatewayLoadBalancer \
  --query 'VpcEndpoints[].[VpcEndpointId,ServiceName]'
check "Load balancers (ALB/NLB/GWLB)" "$HOME_REGION" elbv2 describe-load-balancers \
  --query 'LoadBalancers[].[LoadBalancerName,Type]'
check "Classic load balancers" "$HOME_REGION" elb describe-load-balancers \
  --query 'LoadBalancerDescriptions[].[LoadBalancerName]'
check "EKS clusters" "$HOME_REGION" eks list-clusters --query 'clusters[]'
check "RDS DB instances" "$HOME_REGION" rds describe-db-instances \
  --query 'DBInstances[].[DBInstanceIdentifier,DBInstanceClass]'
check "RDS DB clusters" "$HOME_REGION" rds describe-db-clusters --query 'DBClusters[].[DBClusterIdentifier]'
check "Secrets Manager secrets" "$HOME_REGION" secretsmanager list-secrets --query 'SecretList[].[Name]'

# A resource started in the wrong region is a classic surprise bill, so look everywhere for the big three.
other_regions=0
if ! regions="$(aws ec2 describe-regions --query 'Regions[].RegionName' --output text \
    --region "$HOME_REGION" 2>"$err_file")"; then
  echo "  ERROR  listing enabled regions: $(head -n 1 "$err_file")"
  errors=$((errors + 1))
else
  show_ok=0
  for region in $regions; do
    [ "$region" = "$HOME_REGION" ] && continue
    other_regions=$((other_regions + 1))
    check "EC2 instances (not terminated)" "$region" ec2 describe-instances \
      --filters Name=instance-state-name,Values=pending,running,stopping,stopped \
      --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]'
    check "EBS volumes" "$region" ec2 describe-volumes --query 'Volumes[].[VolumeId,Size,State]'
    check "Elastic IPs" "$region" ec2 describe-addresses --query 'Addresses[].[PublicIp,AllocationId]'
  done
  echo "Other enabled regions: $other_regions checked for instances, volumes and Elastic IPs."
fi

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
echo "✓ No billable leftovers ($HOME_REGION in full, plus $other_regions other regions)."
echo "  Not listed: \$0-at-rest persistent resources (S3 state bucket, budgets, IAM, SSM Standard parameters)."
