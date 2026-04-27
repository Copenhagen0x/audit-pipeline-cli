"""SSH abstraction for VPS operations.

Wraps subprocess calls to ssh/scp with the audit pipeline's conventions.
"""

import subprocess
from pathlib import Path


class SSHClient:
    """A thin wrapper around the system `ssh` command, parameterized by host + key."""

    def __init__(self, host: str, ssh_key: str | Path) -> None:
        self.host = host
        self.ssh_key = str(ssh_key)
        self.base_args = [
            "ssh",
            "-i", self.ssh_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            self.host,
        ]

    def run(self, command: str, timeout: int = 60, capture: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command on the remote host."""
        args = self.base_args + [command]
        return subprocess.run(
            args,
            check=False,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )

    def run_or_die(self, command: str, timeout: int = 60) -> str:
        """Run a command; raise on non-zero exit."""
        result = self.run(command, timeout=timeout, capture=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed (exit {result.returncode}): {command}\n"
                f"stderr: {result.stderr}"
            )
        return result.stdout

    def scp_to(self, local_path: str | Path, remote_path: str) -> None:
        """Copy a local file to the remote host."""
        args = [
            "scp",
            "-i", self.ssh_key,
            "-o", "StrictHostKeyChecking=no",
            str(local_path),
            f"{self.host}:{remote_path}",
        ]
        result = subprocess.run(args, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"scp to {self.host} failed (exit {result.returncode})")

    def scp_from(self, remote_path: str, local_path: str | Path) -> None:
        """Copy a remote file to local."""
        args = [
            "scp",
            "-i", self.ssh_key,
            "-o", "StrictHostKeyChecking=no",
            f"{self.host}:{remote_path}",
            str(local_path),
        ]
        result = subprocess.run(args, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"scp from {self.host} failed (exit {result.returncode})")

    def ping(self) -> bool:
        """Quick reachability check. Returns True if SSH connection works."""
        result = self.run("echo connected", timeout=10)
        return result.returncode == 0 and "connected" in result.stdout
