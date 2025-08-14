import os

from ci_utils.common.helpers import run_cmd
from ci_utils.common.logger import get_logger
logger = get_logger(__name__)


class GPFSGaneshaManager:
    def __init__(self, session):
        """Handles GPFS and NFS-Ganesha installation and setup on a VM.
        Args:
            session (RemoteSession): Remote session to the VM.
        """
        self.session = session
        self.export = "/ibm/scale_volume"

    # -------------------------------
    # Install pre-requisites on VM
    # -------------------------------
    def intall_pre_reqs_on_vm(self):
        logger.info("[STEP]: Installing pre-requisite packages for GPFS and Ganesha on the VM")
        run_cmd(self.session, "dnf install -y rpcbind yum-utils centos-release-ceph epel-release unzip --skip-broken")
        run_cmd(self.session, "systemctl start rpcbind")
        run_cmd(self.session, " sudo setenforce 0")

    # -------------------------------
    # Install Ganesha with GPFS support
    # -------------------------------
    def install_ganesha(self, test_workspace: str):
        logger.info("[STEP]: Installing NFS-Ganesha with GPFS support on the VM")
        yum_repo = os.getenv("GPFS_YUM_REPO", "")
        if yum_repo:
            self._install_from_repo(yum_repo)
        else:
            self._build_from_source(test_workspace)

        self.start_ganesha_service()

    # -------------------------------
    # Install Ganesha from repo or build from source
    # -------------------------------
    def _install_from_repo(self, yum_repo: str):
        logger.info("[STEP]: Installing Ganesha from yum repo...")
        run_cmd(self.session, "yum-config-manager --add-repo=http://artifacts.ci.centos.org/nfs-ganesha/nightly/libntirpc/libntirpc-latest.repo")
        run_cmd(self.session, f"yum-config-manager --add-repo={yum_repo}")
        run_cmd(self.session, "dnf -y install gpfs.nfs-ganesha nfs-ganesha-gluster glusterfs-ganesha")

    def _build_from_source(self, test_workspace: str):
        logger.info("[STEP]: Building Ganesha from source...")

        BASE_PACKAGES="git bison flex cmake gcc-c++ libacl-devel krb5-devel dbus-devel rpm-build redhat-rpm-config gdb"
        BUILDREQUIRES_EXTRA="libnsl2-devel libnfsidmap-devel libwbclient-devel userspace-rcu-devel libcephfs-devel"
    
        run_cmd(self.session, f"dnf install --enablerepo=crb -y {BASE_PACKAGES} {BUILDREQUIRES_EXTRA} libacl-devel libblkid-devel libcap-devel redhat-rpm-config rpm-build libgfapi-devel xfsprogs-devel selinux-policy-devel sqlite --skip-broken")
        cmake_binary, _ = run_cmd(self.session, "which cmake")
        # out, code = run_cmd(self.session,
        #     f"cd {test_workspace}/nfs-ganesha && "
        #     "rm -rf build && "
        #     "mkdir -p build && "
        #     "cd build && "
        #     f"{cmake_binary} ../src -DCMAKE_BUILD_TYPE=Maintainer "
        #     "-DUSE_FSAL_GPFS=ON -DUSE_DBUS=ON -D_MSPAC_SUPPORT=OFF"
        #     "-DMONITORING=ON -DUSE_MONITORING=ON && "
        #     "make dist"
        # )
        build_dir = f"{test_workspace}/nfs-ganesha/build"
        src_dir = f"{test_workspace}/nfs-ganesha"

        out, code = run_cmd(self.session, [
            f"bash -c 'cd {src_dir} && rm -rf {build_dir} && "
            f"mkdir -p {build_dir} && cd {build_dir} && "
            f"{cmake_binary} {src_dir}/src -DCMAKE_BUILD_TYPE=Maintainer "
            "-DUSE_FSAL_GPFS=ON -DUSE_DBUS=ON -D_MSPAC_SUPPORT=OFF "
            "-DMONITORING=ON -DUSE_MONITORING=ON && make dist'"
        ])

        logger.info("GPFS Make CephFS output: %s", out)
        logger.info("GPFS Make CephFS exit code: %d", code)
        assert code == 0, f"GPFS Make CephFS tests failed"
        
        run_cmd(self.session, f"ls -la {src_dir}")
        run_cmd(self.session, f"ls -la {build_dir}")
        
        out, code = run_cmd(self.session, f"ls {build_dir}/nfs-ganesha-*.tar.gz")
        logger.info("Tarball ls output: %s", out)
        tarball = out.strip().splitlines()[0]

        run_cmd(
            self.session,
            # f"export GERRIT_PROJECT='ffilz/nfs-ganesha' && "
            # f"export GERRIT_REFSPEC='refs/changes/30/1219130/10' && "
            # f"export VFS_VOLUME='pynfs' && "
            # f"export ENABLE_ACL='True' && "
            f"bash -c 'cd {build_dir} && "
            f"rpmbuild -ta --define \"_srcrpmdir {build_dir}\" "
            f"--define \"_rpmdir {build_dir}\" {tarball}'"
        )
        
        # Get RPM arch
        rpm_arch, _ = run_cmd(self.session, "rpm -E '%{_arch}'")
        logger.info("RPM Arch: %s", rpm_arch)

        ganesha_version, _ = run_cmd(
            self.session,
            f"bash -c 'cd {build_dir} && rpm -q --qf '%{{VERSION}}-%{{RELEASE}}' -p *.src.rpm'"
        )
        logger.info("Ganesha Version: %s", ganesha_version)

        # Check for NTIRPC
        ntirpc_check_cmd = f"bash -c 'cd {build_dir} && ls {rpm_arch}/libntirpc-devel*.rpm || true'"
        ntirpc_files, _ = run_cmd(self.session, ntirpc_check_cmd)

        if ntirpc_files.strip():
            ntirpc_version, _ = run_cmd(
                self.session,
                f"bash -c 'cd {build_dir} && rpm -q --qf '%{{VERSION}}-%{{RELEASE}}' -p {rpm_arch}/libntirpc-devel*.rpm'"
            )
            ntirpc_rpm = f"{rpm_arch}/libntirpc-{ntirpc_version}.{rpm_arch}.rpm"
            logger.info("NTIRPC Version: %s", ntirpc_version)
            logger.info("NTIRPC RPM: %s", ntirpc_rpm)

        # Install built RPMs
        run_cmd(self.session, f"bash -c 'cd {build_dir} && rpm -e gpfs.nfs-ganesha gpfs.nfs-ganesha-gpfs --nodeps'")
        run_cmd(self.session, f"bash -c 'cd {build_dir} && dnf -y install {{x86_64,noarch}}/*.rpm'")


        # Create minimal ganesha.conf
        ganesha_conf = "NFSv4 { Graceless = true; }"
        cmd = f"bash -c 'cat > /etc/ganesha/ganesha.conf <<EOF\n{ganesha_conf}\nEOF'"
        run_cmd(self.session, cmd)
        run_cmd(self.session, "cat /etc/ganesha/ganesha.conf")

        logger.info("NFS-Ganesha build, install, and minimal config complete.")

    # -------------------------------
    # Start Ganesha service
    # -------------------------------
    def start_ganesha_service(self):
        logger.info("[STEP]: Starting NFS-Ganesha service...")
        out, rc = run_cmd(self.session, "systemctl start nfs-ganesha", check=False)
        if rc != 0:
            logger.error("Failed to start nfs-ganesha: %s", out)
            run_cmd(self.session, "systemctl status nfs-ganesha", check=False)
            run_cmd(self.session, "journalctl -xe", check=False)
            assert False, "Failed to start nfs-ganesha service"
        logger.info("NFS-Ganesha started successfully.")

    # -------------------------------
    # Setup and export NFS volume
    # -------------------------------
    def export_nfs_volume(self):
        logger.info("[STEP]: Exporting NFS volume...")
        run_cmd(self.session, "/usr/lpp/mmfs/bin/mmuserauth service create --data-access-method file --type userdefined")
        run_cmd(self.session, f"/usr/lpp/mmfs/bin/mmnfs export add {self.export} -c \"*(Access_Type=RW,Squash=none)\"")

        # logger.info("Fixing ganesha config conflicts...")
        # run_cmd(self.session, "systemctl stop nfs-ganesha || true")
        # run_cmd(self.session, "/usr/lpp/mmfs/bin/mmnfs config change MINOR_VERSIONS=0,1")
        # run_cmd(self.session, "sleep 20")
        # run_cmd(self.session, "sed -i.bak -e '41d' /var/mmfs/ces/nfs-config/gpfs.ganesha.main.conf")
        # run_cmd(self.session, "sleep 5")
        # run_cmd(self.session, "systemctl daemon-reload")

        # rc, _ = run_cmd(self.session, "systemctl start nfs-ganesha", check=False)
        # if rc != 0:
        #     run_cmd(self.session, "systemctl status nfs-ganesha.service")
        #     run_cmd(self.session, "journalctl -xe")
        #     raise RuntimeError("NFS-Ganesha failed to restart after export")

        # run_cmd(self.session, "systemctl status nfs-ganesha")
        # logger.info("NFS volume exported successfully.")