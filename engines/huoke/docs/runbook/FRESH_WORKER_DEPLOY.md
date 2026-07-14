# Fresh Worker Deploy Runbook

Use this when a worker is in a bad or mixed state and test data can be discarded.
The process deletes the worker project directory and redeploys a clean copy from
the coordinator workspace.

## What The Script Resets

- Stops old `service_wrapper.py`, `server.py`, and direct uvicorn launches.
- Deletes `C:\openclaw\mobile-auto-project` on the worker.
- Copies the coordinator's current code and global YAML/JSON settings.
- Resets worker-local runtime state: `data/`, `logs/`, DB files, device aliases,
  device registry, and cluster state.
- Writes worker-specific `config/cluster.yaml`.
- Writes worker `config/launch.env` and persistent env vars:
  `OPENCLAW_PORT=8000`, `OPENCLAW_HOST=0.0.0.0`.
- Recreates the `OpenClaw-Worker` scheduled task.
- Starts one `service_wrapper.py`, which starts one `server.py`.

## Command Template

Run from the coordinator workspace:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_fresh_worker.ps1 `
  -HostIp 192.168.0.101 `
  -HostId w03 `
  -HostName W03 `
  -CoordinatorUrl http://192.168.0.117:18080
```

For W175:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_fresh_worker.ps1 `
  -HostIp 192.168.0.175 `
  -HostId worker-175 `
  -HostName W175 `
  -CoordinatorUrl http://192.168.0.117:18080
```

For a brand-new worker, add `-InstallRequirements` if Python packages have not
been installed yet. The machine must already have Python, ADB/platform-tools,
SSH access, and USB debugging authorized on phones.

## Verification

After deploy:

```powershell
Invoke-RestMethod http://<worker-ip>:8000/health
Invoke-RestMethod http://127.0.0.1:18080/cluster/overview
ssh administrator@<worker-ip> "adb devices"
```

Expected process shape on each worker:

```text
service_wrapper.py
server.py
```

No extra `cmd.exe ... service_wrapper`, direct uvicorn, or duplicate wrapper
processes should remain.

## 2026-05-15 Baseline

- Coordinator: `http://192.168.0.117:18080`
- Worker port: `8000`
- W03: `192.168.0.101`, `host_id=w03`, `host_name=W03`
- W175: `192.168.0.175`, `host_id=worker-175`, `host_name=W175`

Deployment result:

- W03 fresh deployed successfully. Process shape is one `service_wrapper.py`
  plus one `server.py`; scheduled task is `OpenClaw-Worker`.
- W175 fresh deployed successfully with the same process shape and scheduled
  task.
- W03 ADB sees 13 usable devices; `KJNNT4DULV8DDYOB` is present but
  `unauthorized`, so it needs USB debugging authorization on the phone.
- W175 ADB currently sees 13 usable devices. The previous `4HUSIB4TBQC69TJZ`
  is not visible to `adb devices` after an ADB server restart, so check the
  physical phone/USB/hub side.

Implementation notes captured from this deploy:

- Do not remove the project root directory itself over Windows OpenSSH. The SSH
  shell may hold the directory as its current working directory. The script
  cleans the contents of `C:\openclaw\mobile-auto-project` instead.
- Disable PowerShell progress output before `Expand-Archive`; otherwise
  `Write-Progress` can fail under OpenSSH.
- Use PowerShell ScheduledTasks APIs instead of raw `schtasks /create` so Python
  paths with spaces, such as `C:\Program Files\Python313\python.exe`, are quoted
  correctly.
- The package excludes runtime and sensitive files such as `.env`, logs, data,
  DBs, root screenshots, `device_aliases.json`, `device_registry.json`, and
  `cluster_state.json`.
