import subprocess
import os
import tempfile

import paramiko
from ci_utils.common.logger import get_logger
logger = get_logger(__name__)

# -----------------------
# Remote Session Connectivity
# -----------------------
class RemoteSession:
    # def __init__(self, node_ip, user="root", key_file=None, password=None, port=22, default_dir=None):
    #     self.node_ip = node_ip
    #     self.user = user
    #     self.key_file = key_file
    #     self.password = password
    #     self.port = port
    #     self.default_dir = default_dir
    #     self.client = None

    # def connect(self):
    #     logger.info(f"[STEP]: Connecting to {self.node_ip} as {self.user}")
    #     self.client = paramiko.SSHClient()
    #     self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    #     if self.key_file:
    #         key = paramiko.RSAKey.from_private_key_file(self.key_file)
    #         self.client.connect(hostname=self.node_ip, port=self.port, username=self.user, pkey=key)
    #     elif self.password:
    #         self.client.connect(hostname=self.node_ip, port=self.port, username=self.user, password=self.password)
    #     else:
    #         raise ValueError("Must provide either key_file or password")

    #     logger.info(f"Connected to {self.node_ip}")

    # def run(self, cmds, timeout=3600):
    #     if isinstance(cmds, str):
    #         cmds = [cmds]

    #     results = []
    #     for full_cmd in cmds:
    #         logger.info(f"[STEP]: Running command: {full_cmd} on {self.node_ip}")
    #         stdin, stdout, stderr = self.client.exec_command(full_cmd, timeout=timeout)
    #         out = stdout.read().decode()
    #         err = stderr.read().decode()
    #         code = stdout.channel.recv_exit_status()
    #         results.append((out, err, code))

    #     return results if len(results) > 1 else results[0]

    # def close(self):
    #     if self.client:
    #         self.client.close()
    #         logger.info(f"SSH connection to {self.node_ip} closed")
    def __init__(self, node_ip, user="root", key_file=None, port=22, default_dir=None, password=None):
        """
        Manage a persistent SSH session to a remote node.
        Args:
            node_ip (str): Remote node IP address.
            user (str): SSH user for the remote node.
            key_file (str): Path to SSH private key file.
            port (int): SSH port (default 22).
            default_dir (str): Default directory to run commands in.
        """
        self.node_ip = node_ip
        self.user = user
        self.password = password
        self.key_file = key_file
        self.port = port
        test_name = os.path.basename(default_dir) if default_dir else "default"
        self.control_path = os.path.join(tempfile.gettempdir(), f"ssh-{node_ip}-{port}-{test_name}.sock")
        self._connected = False
        self.default_dir = default_dir

    # -----------------------
    # SSH Connection to Remote Node
    # -----------------------
    def connect(self):
        logger.info(f"[STEP]: Establishing SSH connection to {self.node_ip}")
        ssh_cmd = [
            "ssh",
            "-M",
            "-N",
            "-f",
            "-o", "ControlPersist=yes",
            "-o", f"ControlPath={self.control_path}",
            "-p", str(self.port),
            "-o", "StrictHostKeyChecking=no",
        ]
        if self.key_file:
            ssh_cmd.extend(["-i", self.key_file])

        if self.password:
            # prepend sshpass
            ssh_cmd = ["sshpass", "-p", self.password] + ssh_cmd

        ssh_cmd.append(f"{self.user}@{self.node_ip}")

        logger.info("Opening persistent SSH connection: %s", " ".join(ssh_cmd))
        subprocess.check_call(ssh_cmd)
        self._connected = True

    # -----------------------
    # Run Command(s) on Remote Node
    # -----------------------
    def run(self, cmds, timeout=3600):
        logger.info(f"[STEP]: Running command(s) {cmds} on {self.node_ip}")
        if isinstance(cmds, str):
            cmds = [cmds]

        results = []
        for full_cmd in cmds:
            ssh_cmd = [
                "ssh",
                "-o", f"ControlPath={self.control_path}",
                f"{self.user}@{self.node_ip}",
                full_cmd,
            ]

            # if password is provided, prepend sshpass
            if self.password:
                ssh_cmd = ["sshpass", "-p", self.password] + ssh_cmd

            logger.info(f"[INFO] Complete SSH command: {' '.join(ssh_cmd)}")
            logger.info("Running on %s: %s", self.node_ip, full_cmd)
            proc = subprocess.Popen(
                ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True
            )
            try:
                out, err = proc.communicate(timeout=timeout)
                results.append((out, err, proc.returncode))
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                logger.error(f"Command timed out after {timeout} seconds: {full_cmd}")
                results.append((out, err, -1))

        return results if len(results) > 1 else results[0]

    # -----------------------
    # Close SSH Connection
    # -----------------------
    def close(self):
        logger.info(f"[STEP]: Closing SSH connection to {self.node_ip}")
        if self._connected:
            ssh_cmd = [
                "ssh",
                "-O", "exit",
                "-o", f"ControlPath={self.control_path}",
                f"{self.user}@{self.node_ip}",
            ]
            logger.info("Closing SSH connection to %s", self.node_ip)
            subprocess.call(ssh_cmd)
            self._connected = False

class RemoteSessionThroughJump(RemoteSession):
    def __init__(self, jump_session, node_ip, user="root", key_file=None, default_dir=None):
        """
        jump_session: RemoteSession already connected to jump host (baremetal)
        node_ip: VM IP
        """
        super().__init__(node_ip=node_ip, user=user, key_file=key_file, default_dir=default_dir)
        self.jump_session = jump_session

    def run(self, cmds, timeout=3600):
        """
        Run command on VM via baremetal node session
        """
        logger.info(f"[STEP]: Running command(s) {cmds} on {self.node_ip} via jump host {self.jump_session.node_ip}")
        if isinstance(cmds, str):
            cmds = [cmds]

        results = []
        for cmd in cmds:
            ssh_cmd = f"ssh -i {self.key_file} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {self.user}@{self.node_ip} {cmd}"
            out, err, code = self.jump_session.run(ssh_cmd, timeout)
            results.append((out, err, code))

        return results if len(results) > 1 else results[0]