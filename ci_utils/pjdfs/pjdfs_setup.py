# Generated using IBM Bob

from time import sleep
from typing import List, Tuple, Optional

from ci_utils.common.helpers import run_cmd
from ci_utils.common.logger import get_logger
logger = get_logger(__name__)


class PJDFSManager:
    def __init__(self, session, server_ip, repo_url="https://github.com/pjd/pjdfstest.git", backend_type=None):
        """
        Manage PJDFS test runs on a remote session.

        Args:
            session: RemoteSession instance for running commands.
            server_ip: NFS server IP/hostname.
            repo_url: PJDFS repository URL.
            backend_type: Type of backend storage (e.g., 'ceph', 'acl_vfs', 'gpfs')
        """
        self.session = session
        self.repo_url = repo_url
        self.repo_dir = "/root/pjdfstest"
        self.server_ip = server_ip
        self.failure_log = "/root/pjdfs_failures.txt"
        self.backend_type = backend_type

    # ----------------------------
    # Install dependencies
    # ----------------------------
    def install_dependencies(self) -> None:
        logger.info("[TEST]: Installing PJDFS dependencies...")
        run_cmd(
            self.session,
            "dnf -y install wget git gcc gcc-c++ time make automake autoconf "
            "pkgconf pkgconf-pkg-config libtool bison flex perl perl-Time-HiRes "
            "perl-TAP-Harness python3 tar libaio-devel net-tools nfs-utils --skip-broken"
        )
        
        # Enable CRB repo and install libtirpc-devel
        run_cmd(self.session, "dnf install -y libtirpc-devel --enablerepo=crb", check=False)
        sleep(2)

    # ----------------------------
    # Clone and build PJDFS
    # ----------------------------
    def clone_and_build(self) -> None:
        logger.info("[TEST]: Cloning and building PJDFS...")
        run_cmd(self.session, f"rm -rf {self.repo_dir}")
        run_cmd(self.session, f"git clone --depth=1 {self.repo_url} {self.repo_dir}")
        run_cmd(self.session, f"cd {self.repo_dir} && autoreconf -ifs")
        run_cmd(self.session, f"cd {self.repo_dir} && ./configure")
        run_cmd(self.session, f"cd {self.repo_dir} && make pjdfstest")
        sleep(5)

    # ----------------------------
    # Mount NFS share
    # ----------------------------
    def mount_nfs(self, version: str, server: str, export: str, mount_point: str) -> bool:
        """
        Mount NFS share with specified version.

        Args:
            version: NFS version (e.g., "3", "4", "4.1", "4.2").
            server: NFS server IP/hostname.
            export: Export path.
            mount_point: Local mount point.

        Returns:
            bool: True if mount successful, False otherwise.
        """
        logger.info(f"[TEST]: Mounting NFS v{version} at {mount_point}...")
        
        # Create mount point
        run_cmd(self.session, f"mkdir -p {mount_point}")
        
        # Attempt mount
        mount_cmd = f"mount -t nfs -o vers={version} {server}:{export} {mount_point}"
        out, code = run_cmd(self.session, mount_cmd, check=False)
        
        if code != 0:
            logger.error(f"Failed to mount NFS v{version}: {out}")
            run_cmd(self.session, "cat /var/log/ganesha.log", check=False)
            return False
        
        # Verify mount
        verify_cmd = f"mountpoint -q {mount_point}"
        out, code = run_cmd(self.session, verify_cmd, check=False)
        
        if code == 0:
            logger.info(f"NFS v{version} successfully mounted at {mount_point}")
            run_cmd(self.session, f"mount | grep {mount_point}", check=False)
            return True
        else:
            logger.error(f"Mount verification failed for {mount_point}")
            return False

    # ----------------------------
    # Run PJDFS test for a specific version
    # ----------------------------
    def run_test(
        self,
        version: str,
        server: str,
        export: str = "/nfs/cephfs",
    ) -> Tuple[str, str, int]:
        """
        Run PJDFS test for a specific NFS version.

        Args:
            version: NFS version ("3", "4", "4.1", "4.2").
            server: NFS server IP/hostname.
            export: Export path (e.g., /nfs/cephfs).

        Returns:
            Tuple of (version, output, return_code).
        """
        logger.info("[TEST]: Running PJDFS tests for NFSv%s...", version)
        
        # Determine mount point based on version
        version_safe = version.replace(".", "")
        mount_point = f"/mnt/nfs_pjdfs_v{version_safe}"
        
        # Mount NFS
        if not self.mount_nfs(version, server, export, mount_point):
            error_msg = f"Failed to mount NFS v{version}"
            logger.error(error_msg)
            return version, error_msg, 1
        
        # Run PJDFS tests
        test_cmd = f"cd {mount_point} && prove -rv {self.repo_dir}/tests/"
        
        max_retries = 3
        wait_secs = 10
        
        out = ""
        code = 1
        
        for attempt in range(1, max_retries + 1):
            logger.info(f"PJDFS v{version} attempt {attempt}/{max_retries}...")
            
            out, code = run_cmd(self.session, test_cmd, check=False, timeout=7200)
            
            # Check if tests ran successfully
            if "All tests successful" in out or "Result: PASS" in out or code == 0:
                logger.info(f"PJDFS v{version} tests completed successfully")
                return version, out, code
            elif "Files=" in out and "Tests=" in out:
                # Tests ran but some may have failed
                logger.info(f"PJDFS v{version} tests finished with results")
                return version, out, code
            else:
                logger.warning(f"PJDFS v{version} tests may not have completed properly")
                if attempt < max_retries:
                    logger.info(f"Retrying after {wait_secs} seconds...")
                    sleep(wait_secs)
                else:
                    logger.error("All retries exhausted")
                    return version, out, code
        
        return version, out, code

    # ----------------------------
    # Collect and summarize failures
    # ----------------------------
    def collect_failures(self, outputs: List[Tuple[str, str, int]]) -> Tuple[bool, str, int]:
        """
        Collect and summarize test failures from PJDFS outputs.

        Args:
            outputs: List of tuples (version, output, return_code).

        Returns:
            Tuple of (fail_found, summary_text, return_code).
        """
        logger.info("[TEST]: Collecting PJDFS failures...")
        fail_found = False
        failure_summary = []
        summary_text = ""
        return_code = 0

        for version, out, code in outputs:
            # Parse PJDFS output for failures
            failures = []
            
            # Look for failed tests in prove output
            for line in out.splitlines():
                if "FAILED" in line or "Failed" in line or "not ok" in line:
                    failures.append(line.strip())
            
            # Check for test summary indicating failures
            if "Files=" in out and "Failed:" in out:
                fail_found = True
            
            if failures or code != 0:
                fail_found = True
                logger.warning(f"Failures detected in PJDFS v{version}")
                failure_summary.append(f"PJDFS v{version} test suite failures:")
                failure_summary.append("------------------------------")
                
                if failures:
                    failure_summary.extend(failures)
                else:
                    failure_summary.append(f"Tests exited with code {code}")
                
                failure_summary.append("")  # blank line
            
            if code != 0:
                return_code = code

        if failure_summary:
            summary_text = "\n".join(failure_summary)
            logger.error("Failure summary:\n%s", summary_text)
        else:
            logger.info("All PJDFS tests passed successfully")

        return fail_found, summary_text, return_code

    # ----------------------------
    # Cleanup mounts
    # ----------------------------
    def cleanup_mounts(self) -> None:
        """Unmount all PJDFS test mount points."""
        logger.info("[TEST]: Cleaning up PJDFS mounts...")
        mount_points = [
            "/mnt/nfs_pjdfs_v3",
            "/mnt/nfs_pjdfs_v4",
            "/mnt/nfs_pjdfs_v41",
            "/mnt/nfs_pjdfs_v42"
        ]
        
        for mount_point in mount_points:
            run_cmd(self.session, f"umount {mount_point}", check=False)
            run_cmd(self.session, f"rm -rf {mount_point}", check=False)

    # ----------------------------
    # Run all PJDFS tests
    # ----------------------------
    def run_all_tests(self, export: str, versions: Optional[List[str]] = None) -> Tuple[bool, str, int]:
        """
        Run all PJDFS test suites for specified NFS versions.

        Args:
            export: NFS export path.
            versions: List of NFS versions to test (default: ["3", "4", "4.1", "4.2"]).

        Returns:
            Tuple of (fail_found, summary_text, return_code).
        """
        logger.info("[TEST]: Running all PJDFS test suites")
        
        if versions is None:
            versions = ["3", "4", "4.1", "4.2"]
        
        self.install_dependencies()
        self.clone_and_build()

        results = []
        for version in versions:
            result = self.run_test(version, self.server_ip, export)
            results.append(result)

        # Cleanup
        self.cleanup_mounts()

        return self.collect_failures(results)

