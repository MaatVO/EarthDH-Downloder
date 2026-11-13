"""One-step access from your own computer: SSH tunnel + server + browser, no manual port forwarding.

The launcher (``edh-dl connect HOST``, or the small scripts from ``edh-dl launcher sh|ps1``, which need only
``ssh`` on your computer) does:

  1. pick a free port on your computer and a random port on the server, and a fresh access token;
  2. ``ssh -L <local>:127.0.0.1:<remote> HOST '<edh-dl> serve --stdio --port <remote>'`` and send the token
     on the server's stdin (it never appears in a command line);
  3. wait for the server's ``EDH_DL_READY`` line, then open ``http://localhost:<local>/?token=...``.

Closing the launcher (Ctrl+C) closes the tunnel; the server notices (``EDH_DL_ALIVE`` heartbeats fail) and
exits. Download jobs are separate processes and keep running.
"""

import getpass
import os
import random
import shlex
import socket
import subprocess
import sys
import webbrowser

READY, BUSY, ALIVE = "EDH_DL_READY", "EDH_DL_PORT_BUSY", "EDH_DL_ALIVE"


def remote_cmd() -> str:
    """How to start this very program on the server (absolute paths: SSH commands get a minimal PATH)."""
    from .runner import _self_cmd

    return " ".join(shlex.quote(os.path.abspath(c) if i == 0 else c) for i, c in enumerate(_self_cmd()))


def default_host() -> str:
    """user@address as seen by the SSH session we are in, else user@hostname."""
    conn = os.environ.get("SSH_CONNECTION", "").split()
    addr = conn[2] if len(conn) >= 3 else socket.getfqdn()
    return f"{getpass.getuser()}@{addr}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def connect(host: str, cmd: str = "edh-dl", tries: int = 5) -> int:
    token = os.urandom(18).hex()
    for _ in range(tries):
        lport, rport = _free_port(), random.randint(20000, 60999)
        print(f"Connecting to {host} …", flush=True)
        p = subprocess.Popen(
            ["ssh", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
             "-L", f"{lport}:127.0.0.1:{rport}", host, f"{cmd} serve --stdio --port {rport}"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        p.stdin.write(token + "\n")
        p.stdin.close()
        busy = False
        try:
            for line in p.stdout:
                line = line.rstrip("\n")
                if line.startswith(READY):
                    url = f"http://localhost:{lport}/?token={token}"
                    print(f"Weather Dataset Downloader: {url}\nOpened in your browser. Ctrl+C closes it "
                          "(download jobs keep running on the server).", flush=True)
                    webbrowser.open(url)
                elif line.startswith(BUSY):
                    busy = True
                elif line and not line.startswith(ALIVE):
                    print(line, flush=True)
            rc = p.wait()
        except KeyboardInterrupt:
            p.terminate()
            print("\nclosed.")
            return 0
        if not busy:
            return rc
    print("could not find a free port on the server", file=sys.stderr)
    return 1


# ------------------------------------------------------------------ standalone scripts (only ssh needed)

SH = r"""#!/bin/sh
# Weather Dataset Downloader launcher: opens the web UI of edh-dl on the server in your browser, through SSH.
# Needs only ssh (macOS, Linux, WSL). Usage:  ./edh-connect [user@host]
# Ctrl+C closes the UI; download jobs keep running on the server.
HOST="${1:-__HOST__}"
CMD="__CMD__"
rnd() { od -An -N2 -tu2 /dev/urandom | tr -d ' \n'; }
TOKEN=$(od -An -N18 -tx1 /dev/urandom | tr -d ' \n')
open_url() {
  if [ "$(uname)" = Darwin ]; then open "$1"
  elif command -v wslview >/dev/null 2>&1; then wslview "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1
  else echo "open $1 in your browser"; fi
}
for try in 1 2 3 4 5; do
  LPORT=$((20000 + $(rnd) % 40000)); RPORT=$((20000 + $(rnd) % 40000))
  echo "Connecting to $HOST ..."
  printf '%s\n' "$TOKEN" | ssh -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
      -L "$LPORT:127.0.0.1:$RPORT" "$HOST" "$CMD serve --stdio --port $RPORT" | {
    rc=0
    while IFS= read -r line; do
      case "$line" in
        EDH_DL_READY*) URL="http://localhost:$LPORT/?token=$TOKEN"
                       echo "Weather Dataset Downloader: $URL"
                       echo "Opened in your browser. Ctrl+C closes it (download jobs keep running on the server)."
                       open_url "$URL" ;;
        EDH_DL_PORT_BUSY*) rc=75 ;;
        EDH_DL_ALIVE*|"") ;;
        *) printf '%s\n' "$line" ;;
      esac
    done
    exit $rc
  }
  rc=$?; [ $rc = 75 ] || exit $rc
done
"""

PS1 = r"""# Weather Dataset Downloader launcher for Windows: opens the web UI of edh-dl on the server in your browser, through SSH.
# Needs the OpenSSH client (built into Windows 10/11). Run:  powershell -ExecutionPolicy Bypass -File edh-connect.ps1
# Ctrl+C closes the UI; download jobs keep running on the server.
param([string]$Server = "__HOST__")
$Cmd = "__CMD__"
$b = New-Object byte[] 18
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
$Token = ($b | ForEach-Object { $_.ToString("x2") }) -join ""
for ($try = 0; $try -lt 5; $try++) {
  $LPort = Get-Random -Minimum 20000 -Maximum 60000
  $RPort = Get-Random -Minimum 20000 -Maximum 60000
  Write-Host "Connecting to $Server ..."
  $psi = New-Object System.Diagnostics.ProcessStartInfo "ssh"
  $psi.Arguments = "-T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L ${LPort}:127.0.0.1:${RPort} $Server `"$Cmd serve --stdio --port $RPort`""
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  $p.StandardInput.WriteLine($Token)
  $p.StandardInput.Close()
  $busy = $false
  while ($null -ne ($line = $p.StandardOutput.ReadLine())) {
    if ($line.StartsWith("EDH_DL_READY")) {
      $url = "http://localhost:$LPort/?token=$Token"
      Write-Host "Weather Dataset Downloader: $url"
      Write-Host "Opened in your browser. Ctrl+C closes it (download jobs keep running on the server)."
      Start-Process $url
    } elseif ($line.StartsWith("EDH_DL_PORT_BUSY")) { $busy = $true }
    elseif ($line -and -not $line.StartsWith("EDH_DL_ALIVE")) { Write-Host $line }
  }
  $p.WaitForExit()
  if (-not $busy) { exit $p.ExitCode }
}
"""


def script(kind: str, host: str | None = None, cmd: str | None = None) -> str:
    text = {"sh": SH, "ps1": PS1}[kind]
    c = cmd or remote_cmd()
    if kind == "ps1":
        c = c.replace('"', '`"')
    return text.replace("__HOST__", host or default_host()).replace("__CMD__", c)
