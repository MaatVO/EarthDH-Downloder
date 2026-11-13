# Remote use over SSH

Large downloads belong on a server: close to the storage, always on, with disk to spare. The app lets you drive
that server from your own computer, through one SSH connection, without installing anything on it by hand.

![What the app does over one SSH connection: connect using your SSH configuration, check the host key, find or
create the Python environment, upload the code bundle when it changed, start the agent, receive its hello, then
exchange JSON requests](diagrams/remote-connect.svg)

## The connection

[remote.py](../src/edh_downloader/remote.py) uses [AsyncSSH](https://asyncssh.readthedocs.io/), so no `ssh`
binary is needed, on Windows either. [sshconfig.py](../src/edh_downloader/sshconfig.py) reads `~/.ssh/config`, so
aliases, users, ports, identity files, certificates and `ProxyJump` work as they do with `ssh`.

- **Authentication:** key (with its passphrase when needed), SSH agent, or password. Passwords and passphrases
  are used for the connection only and never stored; `servers.json` remembers the host, user, port and key file.
- **Host keys:** an unknown host key stops the connection and shows its fingerprint. When you accept it, it is
  appended to the `known_hosts` file `ssh` would use for that host, as `ssh` does when you answer "yes".

## The environment on the server

The server needs Python 3.12 or newer. Everything else is set up by the app:

1. **A virtual environment** in `~/.local/share/edh-dl/venv-<deps id>`, where the id is a hash of the dependency
   list. It is created once, with `uv` when the server has it, otherwise `python3 -m venv` and `pip`. It needs
   internet access on the server, and the app asks before creating it. A new dependency list gets a new
   environment; an old one is not touched.
2. **The code** as a small zip, `agent/<build id>.zip` ([bundle.py](../src/edh_downloader/bundle.py)): the app's
   Python files, a few hundred KB. The build id is a hash of the content, so the zip is uploaded only when your
   app's code differs from what the server already has.
3. **Capabilities.** The app checks what the environment can import beyond the required packages: ecCodes (GRIB)
   and netCDF4 (NetCDF). A missing one only stops the CDS jobs that need it, with a message saying why.

## The agent

The server then runs `python -m edh_downloader agent` with `PYTHONPATH` set to the zip
([agent.py](../src/edh_downloader/agent.py)). The agent:

- sends one `hello` line (its version and capabilities);
- reads JSON requests on stdin and answers on stdout, one per line. Each request names a function of
  [service.py](../src/edh_downloader/service.py) and its arguments;
- runs each request in a thread, so a long one (a download sample) does not block the others;
- ends when the connection closes.

On your side, `backend.Remote` speaks this protocol and `backend.Local` calls the same functions in process, so
the UI behaves the same with a server and with "This computer".

The agent holds the server's keys, quota, catalogue cache, jobs and output stores. Keys never cross the
connection after you add them: the UI only ever sees names and fingerprints.

## Download jobs on the server

Jobs started from a server tab are launched on the server by the agent, as detached processes. Closing the tab,
the app or your computer ends the agent, not the jobs. Reconnecting shows them again, as they are.

## Several connections

The connect shell ([gui/connect.py](../src/edh_downloader/gui/connect.py)) shows one tab per connection: "This
computer" and each server, each in its own frame. Tabs are independent; quitting the app closes every connection
and leaves every job running.

## Without the app: the launcher

When you prefer not to run the app locally, the server's own `edh-dl` serves the web UI and a small script on your
computer opens it ([launcher.py](../src/edh_downloader/launcher.py)):

```bash
ssh me@server '/path/to/edh-dl launcher sh' > edh-connect && chmod +x edh-connect
./edh-connect                       # or ./edh-connect other@host
```

```powershell
ssh me@server "/path/to/edh-dl launcher ps1" > edh-connect.ps1
powershell -ExecutionPolicy Bypass -File edh-connect.ps1
```

The script needs only `ssh`. It starts `edh-dl serve` on the server, picks free ports on both ends, opens the
tunnel, passes a fresh access token through the SSH connection (never on a command line others could read), and
opens the browser. Closing it stops the web UI; jobs keep running.

`edh-dl serve` on its own prints the `ssh -L` command for a manual tunnel. The web UI requires its access token
unless started with `--no-token`.
