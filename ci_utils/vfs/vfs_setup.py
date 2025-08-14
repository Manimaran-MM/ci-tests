import os
import time
from typing import Optional

from ci_utils.common.helpers import run_cmd

from ci_utils.common.logger import get_logger, set_test_name
logger = get_logger(__name__)


class VFSVolumeExporter:
    def __init__(self, session, vfs_volume: str, enable_acl: bool = False, security_label: bool = False):
        self.session = session
        self.vfs_volume = vfs_volume
        self.enable_acl = enable_acl
        self.security_label = security_label
        self.export_conf = f"/etc/ganesha/exports/export.{self.vfs_volume}.conf"

    def setup_environment(self):
        """Prepare ganesha directory and fetch dbus-send.sh script."""
        logger.info("Setting up ganesha environment...")
        run_cmd(self.session, "mkdir -p /usr/libexec/ganesha")
        run_cmd(self.session, "cd /usr/libexec/ganesha && dnf -y install wget")
        run_cmd(
            self.session,
            "cd /usr/libexec/ganesha && "
            "wget https://raw.githubusercontent.com/gluster/glusterfs/release-3.10/extras/ganesha/scripts/dbus-send.sh"
        )
        run_cmd(self.session, "chmod 755 /usr/libexec/ganesha/dbus-send.sh")

    def configure_export(self):
        """Create volume export configuration and update ganesha.conf."""
        logger.info(f"Configuring export for volume {self.vfs_volume}...")
        run_cmd(self.session, f"mkdir -p /{self.vfs_volume}")
        run_cmd(self.session, f"chmod ugo+w /{self.vfs_volume}")
        run_cmd(self.session, "mkdir -p /etc/ganesha/exports")

        export_conf_content = f"""
EXPORT {{
    Export_Id = 2;
    Path = "/{self.vfs_volume}";
    Pseudo = "/{self.vfs_volume}";
    Access_type = RW;
    Disable_ACL = True;
    Protocols = "3","4";
    Transports = "UDP","TCP";
    SecType = "sys";
    Security_Label = False;
    FSAL {{
        Name = VFS;
    }}
}}
"""
        # Write export config file
        cmd = f"echo '{export_conf_content}' > {self.export_conf}"
        run_cmd(self.session, cmd)

        # Append include to ganesha.conf
        run_cmd(self.session, f"echo '%include \"{self.export_conf}\"' >> /etc/ganesha/ganesha.conf")

        # Reload ganesha
        run_cmd(self.session, f"/usr/libexec/ganesha/dbus-send.sh /etc/ganesha on {self.vfs_volume}")

    def validate_export(self):
        """Check if the export is available, print logs if not."""
        time.sleep(5)  # Wait a bit for ganesha to reload
        logger.info("Validating ganesha export availability...")
        _, code = run_cmd(self.session, f"showmount -e | grep -q -w -e {self.vfs_volume}", check=False)
        if code != 0:
            logger.error("Export not found, printing debug logs...")
            run_cmd(self.session, "cat /var/log/ganesha/ganesha.log", check=False)
            run_cmd(self.session, "grep --with-filename -e '' /etc/ganesha/ganesha.conf", check=False)
            run_cmd(self.session, "grep --with-filename -e '' /etc/ganesha/exports/*.conf", check=False)
            raise RuntimeError(f"Export {self.vfs_volume} not found!")

    def enable_acl_if_required(self):
        """Enable ACL if requested."""
        if self.enable_acl:
            logger.info("Enabling ACL for volume...")
            run_cmd(self.session, f"sed -i s/'Disable_ACL = .*'/'Disable_ACL = false;'/g {self.export_conf}")
            run_cmd(self.session, f"cat {self.export_conf}")
            export_id, _ = run_cmd(self.session, f"grep 'Export_Id' {self.export_conf} | sed 's/^[[:space:]]*Export_Id.*=[[:space:]]*\\([0-9]*\\).*/\\1/'")
            run_cmd(
                self.session,
                f"dbus-send --type=method_call --print-reply --system "
                f"--dest=org.ganesha.nfsd /org/ganesha/nfsd/ExportMgr "
                f"org.ganesha.nfsd.exportmgr.UpdateExport string:{self.export_conf} "
                f"string:\"EXPORT(Export_Id = {export_id})\""
            )

    def enable_security_label_if_required(self):
        """Enable Security Label if requested."""
        if self.security_label:
            logger.info("Enabling Security_Label for volume...")
            run_cmd(self.session, f"sed -i s/'Security_Label = .*'/'Security_Label = True;'/g {self.export_conf}")
            run_cmd(self.session, f"cat {self.export_conf}")
            export_id, _ = run_cmd(self.session, f"grep 'Export_Id' {self.export_conf} | sed 's/^[[:space:]]*Export_Id.*=[[:space:]]*\\([0-9]*\\).*/\\1/'")
            run_cmd(
                self.session,
                f"dbus-send --type=method_call --print-reply --system "
                f"--dest=org.ganesha.nfsd /org/ganesha/nfsd/ExportMgr "
                f"org.ganesha.nfsd.exportmgr.UpdateExport string:{self.export_conf} "
                f"string:\"EXPORT(Export_Id = {export_id})\""
            )

    def export_volume(self):
        """Main workflow to export a volume."""
        self.setup_environment()
        self.configure_export()
        run_cmd(self.session, "sleep 5")
        self.validate_export()
        self.enable_acl_if_required()
        self.enable_security_label_if_required()
        logger.info("Export completed successfully.")
