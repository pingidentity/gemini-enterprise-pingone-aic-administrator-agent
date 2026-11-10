#!/usr/bin/env bash
# Copyright 2026 Ping Identity
#
# Build the Marketplace deployment package (the zip selected in Producer
# Portal) from the agent source in this checkout. The zip and
# assets/source.tar.gz are build outputs and are not committed, so every
# package carries the current agent code. Running Terraform from a checkout
# needs this build first: main.tf embeds assets/source.tar.gz.
#
# Usage: ./build_package.sh [output.zip]   (relative to this directory)
set -euo pipefail

cd "$(dirname "$0")"
repo_root="$(cd ../.. && pwd)"
output="${1:-pingone-aic-administrator-agent.zip}"

python3 package_agent.py --source "$repo_root/ping_admin_agent" --output assets/source.tar.gz

files=(
  main.tf
  variables.tf
  outputs.tf
  metadata.yaml
  metadata.display.yaml
  marketplace_test.tfvars
  agent_config.auto.tfvars
  README.md
  assets/source.tar.gz
  assets/class_methods.json
  modules/agent-engine
)
rm -f "$output"
zip -q -X -r "$output" "${files[@]}" -x '*.DS_Store' '*/.terraform/*'
unzip -l "$output"
