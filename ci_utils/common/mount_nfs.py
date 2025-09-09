import os
import json
import subprocess
from typing import Dict, Any

from ci_utils.common.helpers import run_cmd
from ci_utils.common.logger import get_logger

logger = get_logger(__name__)

class NFSConfig:
    def __init__(self, session, server_ip, version="4.0", export="/nfs/cephfs", mount_dir="/mnt/nfs"):
        self.session = session
        self.version = version
        self.server_ip = server_ip
        self.export = export
        self.mount_dir = mount_dir

    def mount_nfs(self):
        logger.info(f"[STEP]: Mounting NFS v{self.version} export {self.export} at {self.mount_dir} on session {self.session}")

        if self.is_nfs_mounted():
            logger.warning(f"NFS v{self.version} already mounted at {self.mount_dir}, skipping mount")
            return True
        
        logger.info("No existing mount found, proceeding with mount")
        run_cmd(self.session, f"mkdir -p {self.mount_dir}")

        try:
            logger.info(f"Attempting NFS v{self.version} mount at {self.mount_dir}")
            run_cmd(
                self.session,
                f"mount -t nfs -o vers={self.version} {self.server_ip}:{self.export} {self.mount_dir}"
            )

            if self.is_nfs_mounted():
                logger.info(f"NFS v{self.version} mounted successfully at {self.mount_dir}")
                return True
            else:
                raise RuntimeError(f"NFS v{self.version} mount verification failed at {self.mount_dir}")
        
        except Exception as e:
            log_content, _ = run_cmd(self.session, "cat /var/log/ganesha.log", check=False)
            logger.error(f"[ERROR] Failed to mount NFS v{self.version}. Log:\n{log_content} Error:\n {e}")
            raise RuntimeError(f"NFS v{self.version} mount failed")

    def is_nfs_mounted(self):
        """Return True if mount_dir is already a mountpoint"""
        run_cmd(self.session, f"mount | grep {self.mount_dir}", check=False)
        _, code = run_cmd(self.session, f"mountpoint -q {self.mount_dir}", check=False)
        if code == 0:
            logger.info(f"{self.mount_dir} is already a mountpoint")
            return True
        else:
            logger.info(f"{self.mount_dir} is not a mountpoint")
            return False

    def unmount_nfs(self, force=False):
        logger.info(f"[STEP]: Unmounting NFS v{self.version} at {self.mount_dir} on session {self.session}")

        if not self.is_nfs_mounted():
            logger.error(f"NFS v{self.version} not mounted at {self.mount_dir}")
            return False
        
        logger.info(f"Attempting NFS v{self.version} unmount at {self.mount_dir}")
        if force:
            run_cmd(
                self.session,
                f"umount -f {self.mount_dir}"
            )
        else:
            run_cmd(
                self.session,
                f"umount {self.mount_dir}"
            )

        if not self.is_nfs_mounted():
            logger.info(f" NFS v{self.version} umounted successfully at {self.mount_dir}")
            return True
        else:
            raise RuntimeError(f"NFS v{self.version} unmount verification failed at {self.mount_dir}")
    