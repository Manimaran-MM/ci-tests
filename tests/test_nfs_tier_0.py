import os
import re
from ci_utils.ceph.ceph_setup import CephGaneshaSetup
from ci_utils.common.mount_nfs import NFSConfig
from ci_utils.common.remote_session import RemoteSession, RemoteSessionThroughJump
from ci_utils.cthon.cthon_setup import CthonManager
from ci_utils.gpfs.aws_setup import AWSSetupForGPFS
from ci_utils.gpfs.gpfs_setup import SpectrumScaleInstaller
from ci_utils.nfs_ganesha.gpfs_ganesha_setup import GPFSGaneshaManager
from ci_utils.nfs_ganesha.nfs_ganesha_setup import GaneshaManager
from ci_utils.nfs_ganesha.vfs_nfs_ganesha_setup import VFSGaneshaManager
from ci_utils.pynfs.pynfs_setup import PyNFSManager
from ci_utils.vfs.vfs_setup import VFSVolumeExporter
from ci_utils.virtual_machine.vm_setup import VMManager
import pytest
from ci_utils.common.logger import get_logger
from ci_utils.common.logger import set_test_name
from ci_utils.common.helpers import *


logger = get_logger(__name__)

# -----------------------
# Predefined paths
# -----------------------
WORKSPACE = os.getenv("WORKSPACE", "/tmp")
SESSION_FILE = os.path.join(WORKSPACE, "duffy_session.json")
FAILURE_FILE = os.path.join(WORKSPACE, "failures")
os.makedirs(FAILURE_FILE, exist_ok=True)
SUMMARY_FILE = os.path.join(WORKSPACE, "summary_cthon_pynfs.txt")
SUMMARY_STATUS = os.path.join(WORKSPACE, "summary_status.txt")

# -------------------------
# Fixtures - Sessions level
# -------------------------
@pytest.fixture(scope="session")
def all_nodes():
    session_file = SESSION_FILE
    logger.info("[Fixtures - Session]: Getting all reserved nodes from Duffy session file: %s", session_file)
    session_data = read_json_file(session_file)
    return session_data.get("nodes", [])

# -----------------------
# Fixtures - Test level
# -----------------------
@pytest.fixture(autouse=True)
def attach_test_name(request):
    logger.info("[Fixtures - Test]: Setting test name for logging")
    set_test_name(request.node.name)

@pytest.fixture
def create_session(all_nodes, request):
    logger.info("[Fixtures - Test]: Creating remote session(s)")
    
    node_param = getattr(request, "param", 0)

    if isinstance(node_param, int):
        node_param = [node_param]

    sessions = []
    for idx in node_param:
        node_ip = all_nodes[idx]
        test_name_param = request.node.name
        test_name = re.sub(r"\[.*\]$", "", test_name_param)
        default_dir = f"/root/{test_name}_{idx}"

        session = RemoteSession(node_ip=node_ip, user="root", default_dir=default_dir)
        session.connect()
        session.run(f"mkdir -p {default_dir}")
        scp_copy(node_ip, f"{WORKSPACE}/nfs-ganesha", remote_dir=default_dir)
        sessions.append((session, default_dir, node_ip))

    yield sessions if len(sessions) > 1 else sessions[0]
    
    # Teardown
    logger.info("[Fixtures - Test]: Closing remote session(s)")
    for sess, _, _ in sessions:
        sess.close()


# -----------------------
# Actual Tests Starts Here
# -----------------------

# -------------------------
# Test 1: Cthon with CephFS
# Node allocation: 1 (index 1)
# -------------------------
@pytest.mark.parametrize("create_session", [0], indirect=True)
@pytest.mark.timeout(1200) 
def test_setup_cephfs(create_session):
    logger.info("[TEST START]: Setup CephFS")
    
    remote_session, test_workspace, server_node = create_session

    logger.info("[TEST NODE DETAILS]: Node: %s", server_node)
    logger.info("[TEST WORKSPACE DETAILS]: Workspace: %s", test_workspace)
    logger.info("[TEST SESSION DETAILS]: Session: %s", remote_session)

    _, code = run_cmd(
        remote_session,
        f"cd {test_workspace}/nfs-ganesha && "
        "rm -rf build && "
        "mkdir -p build && "
        "cd build && "
        "cmake ../src -DCMAKE_BUILD_TYPE=Maintainer -DUSE_FSAL_GLUSTER=OFF -DUSE_FSAL_CEPH=ON -DUSE_FSAL_RGW=OFF -DUSE_DBUS=ON -DUSE_ADMIN_TOOLS=ON && "
        "make && "
        "make install", check=False
    )

    assert code == 0, f"Cthon Make CephFS tests failed"

    logger.info("Ceph setup")
    ceph_setup = CephGaneshaSetup(session=remote_session)
    subvol_path = ceph_setup.full_setup()

    logger.info("NFS Ganesha setup")
    ganesha_setup = GaneshaManager(
        session=remote_session,
        subvol_path=subvol_path,
        cephfs_name=ceph_setup.cephfs_name
    )
    ganesha_setup.setup()

# -----------------------
# Test 2: NFSv4 create directories and file test
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_client_operation_create(create_session):
    (server_session, server_workspace, server_node), (client_session, client_workspace, client_node) = create_session
    logger.info("Running tests on node: %s", client_node)

    mnt_dir = "/mnt/nfs4"

    nfs_config = NFSConfig(session=client_session, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs() 
    run_cmd(client_session, f"mkdir -p {mnt_dir}/dir1/dir2/dir3")
    run_cmd(client_session, f"touch {mnt_dir}/dir1/dir2/dir3/file1.txt")
    out, code = run_cmd(client_session, f"ls -R {mnt_dir}")
    logger.info("Directory structure:\n%s", out)

    assert code == 0, "Failed to create directories and file on NFS mount"

# -----------------------
# Test 2: NFSv4 read/write test
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_read_write_file(create_session):
    (_, _, server_node), (client_session, _, _) = create_session
    
    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs() 

    file_path = f"{mnt_dir}/file.txt"
    create_file(client_session, file_path, "hello")

    content = read_file(client_session, file_path)
    assert content == "hello", f"Expected 'hello', got '{content}'"

# -----------------------
# Test 3: File permission test
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1, 2]], indirect=True)
def test_file_permission(create_session):
    (_, _, server_node), (client_session_1, _, _), (client_session_2, _, _) = create_session

    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session_1, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs() 

    file_path = f"{mnt_dir}/secret.txt"
    # Create users
    run_cmd(client_session_1, "sudo useradd -u 2001 user1")
    run_cmd(client_session_1, "sudo useradd -u 2002 user2")
    run_cmd(client_session_2, "sudo useradd -u 2002 user2")

    # run_cmd(client_session_1, f"sudo -u user1 bash -c 'echo secret > {file_path}'")
    run_cmd(client_session_1, f"sudo -u user1 touch {file_path}")
    run_cmd(client_session_1, f"sudo -u user1 bash -c 'echo secret > {file_path}'")

    run_cmd(client_session_1, f"sudo -u user1 chmod 600 {file_path}")

    nfs_config_2 = NFSConfig(session=client_session_2, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config_2.mount_nfs()

    _, code = run_cmd(client_session_2, f"sudo -u user2 cat {file_path}")
    assert code != 0, "User2 should not be able to read file owned by User1 with 600 perms"


# -----------------------
# Test 4: Directory creation and deletion test
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_directory_creation_deletion(create_session):
    (_, _, server_node), (client_session, _, _) = create_session

    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs() 

    dir_path = f"{mnt_dir}/dir1/dir2/dir3"
    make_dirs(client_session, dir_path)
    out = list_recursive(client_session, mnt_dir)
    assert "dir3" in out

    remove_path(client_session, f"{mnt_dir}/dir1")
    out = list_recursive(client_session, mnt_dir)
    assert "dir1" not in out

# -----------------------
# Test 5: File rename test
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_file_rename(create_session):
    (_, _, server_node), (client_session, _, _) = create_session

    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs()

    old = f"{mnt_dir}/old.txt"
    new = f"{mnt_dir}/new.txt"

    create_file(client_session, old)
    run_cmd(client_session, f"mv {old} {new}")

    assert check_file_exists(client_session, new) == 0
    assert check_file_exists(client_session, old) != 0

# -----------------------
# Test 6: Symlink test
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_symlink(create_session):
    (_, _, server_node), (client_session, _, _) = create_session

    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs()

    orig = f"{mnt_dir}/original.txt"
    link = f"{mnt_dir}/link.txt"
    create_file(client_session, orig, "data")

    run_cmd(client_session, f"ln -s {orig} {link}")
    content = read_file(client_session, link)

    assert content == "data"

# -----------------------
# Test 7: Hardlink test
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_hardlink(create_session):
    (_, _, server_node), (client_session, _, _) = create_session

    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs()

    fileA = f"{mnt_dir}/fileA.txt"
    fileB = f"{mnt_dir}/fileB.txt"
    create_file(client_session, fileA, "data")

    run_cmd(client_session, f"ln {fileA} {fileB}")
    out, _ = run_cmd(client_session, f"ls -li {fileA} {fileB}")

    # same inode expected
    inodes = {line.split()[0] for line in out.splitlines()}
    assert len(inodes) == 1, "Hardlink should share same inode"

# -----------------------
# Test 8: Unmount export test
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_unmount_export(create_session):
    (_, _, server_node), (client_session, _, _) = create_session

    mnt_dir = "/mnt/umount_nfs4"
    nfs_config = NFSConfig(session=client_session, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs()

    # Unmount
    status = nfs_config.unmount_nfs()
    assert status, "Failed to unmount NFS export"

# -------------------------------
# Test 9: Multiple clients mount test
# Node allocation: 3 (index 0,1,2)
# -------------------------------
@pytest.mark.parametrize("create_session", [[0, 1, 2]], indirect=True)
def test_multiple_client_mounts(create_session):
    (_, _, server_node), (client_session_1, _, _), (client_session_2, _, _) = create_session

    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session_1, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs() 

    nfs_config_2 = NFSConfig(session=client_session_2, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config_2.mount_nfs() 


    file_path = f"{mnt_dir}/shared.txt"
    create_file(client_session_1, file_path, "multiclient")

    content = read_file(client_session_2, file_path)
    assert content == "multiclient", "Data should be visible across clients"

# # -------------------------------
# # Test 10: NFSv4 ACLs test
# # Node allocation: 3 (index 0,1,2)
# # -------------------------------
# @pytest.mark.parametrize("create_session", [[0, 1, 2]], indirect=True)
# def test_nfsv4_acls(create_session):
#     (_, _, server_node), (client_session_1, _, _), (client_session_2, _, _) = create_session

#     mnt_dir = "/mnt/nfs4"
#     nfs_config = NFSConfig(session=client_session_1, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
#     nfs_config.mount_nfs() 

#     nfs_config_2 = NFSConfig(session=client_session_2, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
#     nfs_config_2.mount_nfs() 

#     file_path = f"{mnt_dir}/aclfile.txt"
#     create_file(client_session_1, file_path, "restricted")

#     # Apply ACL: give user2 read-only
#     run_cmd(client_session_1, f"nfs4_setfacl -a A::user2@:r {file_path}")

#     # Verify access as user2 on clientB
#     out, code = run_cmd(client_session_2, f"sudo -u user2 cat {file_path}")
#     assert code == 0 and "restricted" in out, "user2 should have read access"

# -------------------------------
# Test 11: File locking test
# Node allocation: 3 (index 0,1,2)
# -------------------------------
@pytest.mark.parametrize("create_session", [[0, 1, 2]], indirect=True)
def test_file_locking(create_session):
    (_, _, server_node), (client_session_1, _, _), (client_session_2, _, _) = create_session

    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session_1, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config.mount_nfs() 

    nfs_config_2 = NFSConfig(session=client_session_2, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config_2.mount_nfs() 

    file_path = f"{mnt_dir}/lockfile.txt"
    create_file(client_session_1, file_path, "locktest")

    # Client 1 acquires lock in background
    get_file_lock(client_session_1, file_path, hold_time=30, background=True)

    # Client 2 tries to write (should fail/timeout)
    _, code = run_cmd(client_session_2, f"timeout 5 sh -c 'echo blocked >> {file_path}'")
    assert code != 0, "Client 2 should be blocked while Client 1 holds lock"

    # Release lock (simplified cleanup)
    release_file_lock(client_session_1, file_path)

# # -------------------------------
# # Test 12: Delegations test
# # Node allocation: 3 (index 0,1,2)
# # -------------------------------
# @pytest.mark.parametrize("create_session", [[0, 1, 2]], indirect=True)
# def test_delegations(create_session):
#     (server_session, _, server_node), (client_session_1, _, _), (client_session_2, _, _) = create_session

#     mnt_dir = "/mnt/nfs4"
#     nfs_config = NFSConfig(session=client_session_1, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
#     nfs_config.mount_nfs() 

#     nfs_config_2 = NFSConfig(session=client_session_2, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
#     nfs_config_2.mount_nfs() 

#     file_path = f"{mnt_dir}/delegfile.txt"
#     create_file(client_session_1, file_path, "delegation")

#     # Client A opens file to get delegation
#     run_cmd(client_session_1, f"exec 3<>{file_path}")

#     # Check server log for delegation granted
#     out, _ = run_cmd(server_session, "grep 'delegation' /var/log/ganesha.log | tail -n 5")
#     assert "delegation" in out.lower(), "Delegation not granted"

#     # Client B access should trigger recall
#     read_file(client_session_2, file_path)
#     out, _ = run_cmd(server_session, "grep 'recall' /var/log/ganesha.log | tail -n 5")
#     assert "recall" in out.lower(), "Delegation not recalled"

# -----------------------
# Test 13: NLM locking test
# Node allocation: 3 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_nlm_locking(create_session):
    (_, _, _), (server_session, _, server_node) = create_session

    # Use locktest utility against server
    out, code = run_cmd(server_session, f"locktest -h {server_node}")
    assert code == 0, "NLM locktest failed"
    assert "lock" in out.lower(), "Expected lock/unlock activity in output"

# -----------------------
# Test 14: Cross-protocol consistency test (NFSv3 <-> NFSv4)
# Node allocation: 2 (index 0,1)
# -----------------------
@pytest.mark.parametrize("create_session", [[0, 1]], indirect=True)
def test_cross_protocol_consistency(create_session):
    (_, _, server_node), (client_session_1, _, _), (client_session_2, _, _) = create_session

    mnt_dir = "/mnt/nfs4"
    nfs_config = NFSConfig(session=client_session_1, server_ip=server_node, mount_dir=mnt_dir, version=3.0, export="/nfs/cephfs")
    nfs_config.mount_nfs() 

    nfs_config_2 = NFSConfig(session=client_session_2, server_ip=server_node, mount_dir=mnt_dir, version=4.0, export="/nfs/cephfs")
    nfs_config_2.mount_nfs() 

    file_path = f"{mnt_dir}/cross.txt"
    create_file(clientA, file_path, "crossdata")
    run_cmd(clientA, f"umount {MNT_DIR}")

    # Client B mounts v4
    mount_nfs(clientB, server_node, version="4.0")
    content = read_file(clientB, file_path)
    assert content == "crossdata", "Data not consistent across v3/v4 clients"
