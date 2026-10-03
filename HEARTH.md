# Hearth — project instructions

This app: Python-stdlib web server fronting Ollama. Localhost only (127.0.0.1:8410)

- Behaviour: `hearth/*.py` (agent.py = prompt/tool loop, tools.py = tools, workspace.py = jailed file I/O, server.py = HTTP/API, store.py = chats/settings/memory). UI: `web/`.
- Deploy: `cd ~/hearth && bash install.sh` then `systemctl --user restart hearth.service`.
- Keep file tools jailed through workspace.py — never add raw open()/subprocess/rm on arbitrary paths, and no shell command execution.
