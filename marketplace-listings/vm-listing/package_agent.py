# Copyright 2026 Ping Identity
#
# Package the agent source for the Marketplace deployment package.
#
# Unlike Google's marketplace-agents-package reference, this does NOT wrap the
# agent in an AdkApp: PingAdminA2aAgent is a raw A2A executor class and is
# deployed as-is (entrypoint module/object come from main.tf). Secrets never
# enter the archive: the package ships source code only, and customers supply
# their tenant's Secret Manager IDs as deployment variables.

import argparse
import os
import shutil
import sys
import tarfile


def main():
    parser = argparse.ArgumentParser(description="Package agent source for Agent Engine.")
    parser.add_argument("--source", required=True, help="Path to the agent source directory")
    parser.add_argument("--output", default="assets/source.tar.gz", help="Output tar.gz path")
    parser.add_argument("--package-name", default="ping_admin_agent", help="Python package name")
    args = parser.parse_args()

    source_path = os.path.abspath(args.source)
    if not os.path.isdir(source_path):
        print(f"Error: source directory '{source_path}' not found")
        sys.exit(1)

    package_name = args.package_name
    output_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    temp_dir = "temp_package"
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)

    try:
        pkg_dir = os.path.join(temp_dir, package_name)
        shutil.copytree(
            source_path,
            pkg_dir,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git", ".venv", ".idea", ".vscode",
                "tests", "test", "deployment", "marketplace", "logos",
                "schemas", "docs", "*.md",
            ),
        )
        # The schemas directory is data the package imports at run time; the
        # repo keeps it beside the package source, not inside it.
        schemas_src = os.path.join(source_path, "schemas")
        schemas_dst = os.path.join(pkg_dir, "schemas")
        if os.path.isdir(schemas_src):
            shutil.copytree(schemas_src, schemas_dst, dirs_exist_ok=True)

        if not os.path.isfile(os.path.join(pkg_dir, "requirements.txt")):
            # requirements.txt lives at the repo root, beside the package.
            shutil.copy(
                os.path.join(os.path.dirname(source_path), "requirements.txt"),
                os.path.join(pkg_dir, "requirements.txt"),
            )

        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(pkg_dir, arcname=package_name)

        tfvars_path = os.path.join(os.getcwd(), "agent_config.auto.tfvars")
        with open(tfvars_path, "w") as f:
            f.write(f'agent_package_name = "{package_name}"\n')

        print(f"Packaged '{package_name}' -> '{output_path}'")
        print(f"Generated '{tfvars_path}'")
    except (OSError, shutil.Error, tarfile.TarError) as exc:
        print(f"Error packaging agent: {exc}")
        sys.exit(1)
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    main()
