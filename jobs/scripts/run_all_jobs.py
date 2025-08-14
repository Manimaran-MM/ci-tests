#!/usr/bin/env python3
import subprocess
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET

WORKSPACE = os.environ.get("WORKSPACE", ".")

# Define your jobs
jobs = [
    {
        "name": "checkpatch",
        "cwd": WORKSPACE,
        "cmd": "bash ./ci-tests/jobs/scripts/checkpatch.sh"
    },
    {
        "name": "fsal_cephfs",
        "cwd": WORKSPACE,
        "env": {
            "JOB_LABEL": "FSAL_CephFS_Jobs",
            "TEST_SCRIPT": f"{WORKSPACE}/ci-tests/build_scripts/build-fsal/build-fsal_cephfs.sh"
        },
        "cmd": "bash ./ci-tests/jobs/scripts/fsal-build.sh"
    }
    # {
    #     "name": "fsal_vfs",
    #     "cwd": WORKSPACE,
    #     "cmd": "bash ./ci-tests/build_scripts/build-fsal/build-fsal_vfs.sh"
    # },
    # {
    #     "name": "fsal_gpfs",
    #     "cwd": WORKSPACE,
    #     "cmd": "bash ./ci-tests/build_scripts/build-fsal/build-fsal_gpfs.sh"
    # },
    # {
    #     "name": "fsal_rgw",
    #     "cwd": WORKSPACE,
    #     "cmd": "bash ./ci-tests/build_scripts/build-fsal/build-fsal_rgw.sh"
    # }
]

def run_job(job):
    print(f"Starting job {job['name']}...", flush=True)
    start_time = time.time()

    # Use stdbuf to force line-buffered output
    cmd = f"stdbuf -oL -eL {job['cmd']}"

    proc = subprocess.Popen(
        cmd,
        cwd=job["cwd"],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        shell=True
    )

    output_lines = []
    for line in proc.stdout:
        print(line, end='', flush=True)  # live output
        output_lines.append(line)

    returncode = proc.wait()
    elapsed = time.time() - start_time

    print(f"Job {job['name']} completed with code {returncode}", flush=True)
    return {
        "name": job["name"],
        "returncode": returncode,
        "output": "".join(output_lines),
        "time": elapsed
    }

def write_junit(results, output_file):
    testsuites = ET.Element('testsuites')
    testsuite = ET.SubElement(testsuites, 'testsuite', name="CI Jobs", tests=str(len(results)))

    for result in results:
        testcase = ET.SubElement(testsuite, 'testcase', 
                                 classname="ci.tests", 
                                 name=result['name'], 
                                 time=str(round(result["time"], 2)))
        if result["returncode"] != 0:
            failure = ET.SubElement(testcase, 'failure', message="Job failed")
            failure.text = result["output"]

    tree = ET.ElementTree(testsuites)
    tree.write(output_file, encoding='utf-8', xml_declaration=True)

def main():
    all_results = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = {executor.submit(run_job, job): job for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            all_results.append(result)

    # Write results for Jenkins
    output_file = os.path.join(WORKSPACE, "results.xml")
    write_junit(all_results, output_file)
    print(f"JUnit results written to {output_file}")

if __name__ == "__main__":
    main()
