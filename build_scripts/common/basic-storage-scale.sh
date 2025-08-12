#!/bin/sh
#
# Setup a simple gluster environment and export a volume through NFS-Ganesha.
#
# This script uses the following environment variables:/
# - GLUSTER_VOLUME: name of the gluster volume to create
#                   this name will also be used as name for the export
#
# The YUM_REPO and GERRIT_* variables are mutually exclusive.
#
# - YUM_REPO: URL to the yum repository (.repo file) for the NFS-Ganesha
#             packages. When this option is used, libntirpc-latest is enabled
#             as well. Leave empty in case patches from Gerrit need testing.
#
# - GERRIT_HOST: when triggered from a new patch submission, this is set to the
#                git server that contains the repository to use.
#
# - GERRIT_PROJECT: project that triggered the build (like ffilz/nfs-ganesha).
#
# - GERRIT_REFSPEC: git tree-ish that can be fetched and checked-out for testing.

set -x
set -euo pipefail

#THE FOLLOWING LINES OF CODE DOWNLOADS STORAGE SCALE, INSTALLS IT AND CREATES A CLUSTER
#----------------------------------------------------------------------------------------------
############################
# Utility functions
############################
log() {
    echo -e "[INFO] $*"
}

error_exit() {
    echo -e "[ERROR] $*" >&2
    exit 1
}

run_cmd() {
    local cmd="$1"
    log "Running: $cmd"
    if ! eval "$cmd"; then
        error_exit "Command failed: $cmd"
    fi
}

check_file_exists() {
    local file="$1"
    [[ -f "$file" ]] || error_exit "File not found: $file"
}

check_service_running() {
    local service="$1"
    if ! systemctl is-active --quiet "$service"; then
        error_exit "Service not running: $service"
    fi
}

############################
# Step 1: Setup AWS CLI
############################
setup_aws_cli() {
    log "Setting up AWS CLI"
    run_cmd "dnf install -y unzip"
    run_cmd "curl -s https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o awscliv2.zip"
    run_cmd "unzip -qq awscliv2.zip"
    run_cmd "chmod +x ./aws/*"
    run_cmd "./aws/install"
    run_cmd "aws --version"

    run_cmd "aws configure set aws_access_key_id ${AWS_ACCESS_KEY}"
    run_cmd "aws configure set aws_secret_access_key ${AWS_SECRET_KEY}"
    run_cmd "aws configure set default_region_name ${AWS_DEFAULT_REGION}"
}

############################
# Step 2: Download & Install Spectrum Scale
############################
install_spectrum_scale() {
    local WORKDIR="$1"
    mkdir -p "$WORKDIR"
    cd "$WORKDIR" || error_exit "Failed to cd to $WORKDIR"

    run_cmd "aws s3api get-object --bucket centos-ci --key version_to_use.txt version_to_use.txt"
    VERSION_TO_USE=$(cat version_to_use.txt)
    log "Using Spectrum Scale version: ${VERSION_TO_USE}"

    run_cmd "aws s3api get-object --bucket centos-ci --key ${VERSION_TO_USE} ${VERSION_TO_USE}"
    run_cmd "mkdir INSTALLER_PATH"
    run_cmd "unzip ${VERSION_TO_USE} -d INSTALLER_PATH/"

    INSTALLER_VERSION=$(ls INSTALLER_PATH/ --ignore='*.md5' --ignore='*.README' --ignore='*.pgp')
    INSTALLER=$(readlink -f "INSTALLER_PATH/${INSTALLER_VERSION}")
    run_cmd "chmod +x ${INSTALLER}"
    run_cmd "${INSTALLER} --silent"
}

############################
# Step 3: Configure Spectrum Scale Cluster
############################
configure_scale_cluster() {
    STORAGE_SCALE_VOLUME="scale_volume"
    export PATH="$PATH:$(readlink -f /usr/lpp/mmfs/*/ansible-toolkit/)"

    run_cmd "spectrumscale setup -s 127.0.0.1 --storesecret"
    run_cmd "spectrumscale node add $(hostname) -n"
    run_cmd "spectrumscale node add $(hostname) -p"
    run_cmd "spectrumscale config protocols -e $USABLE_IP"
    run_cmd "spectrumscale node add -a $(hostname)"
    run_cmd "spectrumscale config gpfs -c $(hostname)_cluster"

    run_cmd "dd if=/dev/zero of=/home/nsd1_c84f2u09 bs=1M count=8192"
    run_cmd "spectrumscale nsd add -p $(hostname) -u dataAndMetadata -fs ${STORAGE_SCALE_VOLUME} -fg 1 /home/nsd1_c84f2u09"
    run_cmd "spectrumscale config protocols -f ${STORAGE_SCALE_VOLUME} -m /ibm/${STORAGE_SCALE_VOLUME}"

    run_cmd "spectrumscale enable nfs"
    run_cmd "spectrumscale enable smb"
    run_cmd "spectrumscale callhome disable"
    run_cmd "spectrumscale config perfmon -r off"

    run_cmd "spectrumscale install --precheck"
    run_cmd "spectrumscale install"
    run_cmd "spectrumscale deploy --precheck"
    run_cmd "spectrumscale deploy"
}

############################
# Step 4: Build or Install NFS-Ganesha
############################
build_nfs_ganesha() {
    log "Installing or building NFS-Ganesha"

    # Common repo setup
    run_cmd dnf -y install yum-utils epel-release unzip --skip-broken
    run_cmd subscription-manager repos --enable codeready-builder-for-rhel-$(rpm -E %rhel)-$(uname -m)-rpms
	run_cmd dnf -y install rpcbind
	run_cmd systemctl start rpcbind

    if [[ -n "${YUM_REPO}" ]]; then
        # Install prebuilt packages
		run_cmd yum-config-manager --add-repo=http://artifacts.ci.centos.org/nfs-ganesha/nightly/libntirpc/libntirpc-latest.repo
        run_cmd yum-config-manager --add-repo="${YUM_REPO}"
        run_cmd dnf -y install gpfs.nfs-ganesha gpfs.nfs-ganesha-utils gpfs.nfs-ganesha-gpfs

		run_cmd systemctl enable --now nfs-ganesha.service

        check_service_running "nfs-ganesha"
    else
        # Build from Gerrit
        local GERRIT_HOST="${GERRIT_HOST:-review.gerrithub.io}"
        local GERRIT_PROJECT="${GERRIT_PROJECT:-ffilz/nfs-ganesha}"
        local GERRIT_REFSPEC="${GERRIT_REFSPEC:-refs/heads/next}"
        local GIT_REPO
        GIT_REPO=$(basename "${GERRIT_PROJECT}")
        local GIT_URL="https://${GERRIT_HOST}/${GERRIT_PROJECT}"

        local BASE_PACKAGES="git bison flex cmake gcc-c++ libacl-devel libblkid-devel libcap-devel rpm-build redhat-rpm-config gdb"
        local BUILDREQUIRES_EXTRA="libnsl2-devel libnfsidmap-devel libwbclient-devel userspace-rcu-devel libcephfs-devel"

        run_cmd dnf install -y ${BASE_PACKAGES} libgfapi-devel xfsprogs-devel --skip-broken
        run_cmd dnf install --enablerepo=crb -y ${BUILDREQUIRES_EXTRA} --skip-broken
        run_cmd dnf -y install selinux-policy-devel sqlite --skip-broken

        run_cmd git init "${GIT_REPO}"
        pushd "${GIT_REPO}"
        if ! run_cmd git fetch --depth=1 "${GIT_URL}" "${GERRIT_REFSPEC}"; then
            sleep 2
            run_cmd git fetch "${GIT_URL}" "${GERRIT_REFSPEC}"
        fi
        run_cmd git checkout -b "${GERRIT_REFSPEC}" FETCH_HEAD
        run_cmd git submodule update --recursive --init || git submodule sync

        mkdir build && cd build
        run_cmd cmake -DCMAKE_BUILD_TYPE=Maintainer -DUSE_FSAL_GPFS=ON ../src
        run_cmd make dist
        run_cmd rpmbuild -ta --define "_srcrpmdir $PWD" --define "_rpmdir $PWD" *.tar.gz

        run_cmd dnf -y install {x86_64,noarch}/*.rpm
        popd
    fi
}

############################
# Step 5: Export NFS Volume
############################
export_nfs_volume() {
    STORAGE_SCALE_VOLUME="scale_volume"
    run_cmd "/usr/lpp/mmfs/bin/mmuserauth service create --data-access-method file --type userdefined"
    run_cmd "/usr/lpp/mmfs/bin/mmnfs export add /ibm/${STORAGE_SCALE_VOLUME} -c \"*(Access_Type=RW,Squash=none)\""
}

############################
# Main Script Execution
############################
main() {
    log "Starting Basic Storage Scale setup"
    WORKSPACE_PATH=$(pwd)
    WORKDIR="$WORKSPACE_PATH/DOWNLOAD_STORAGE_SCALE"

    setup_aws_cli
    install_spectrum_scale "$WORKDIR"
    configure_scale_cluster
    build_nfs_ganesha
    export_nfs_volume

    log "Validating services"
    check_service_running "nfs-ganesha"

    log "Setup completed successfully"
}

main "$@"
