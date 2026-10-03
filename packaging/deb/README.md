# Debian package

Build a `.deb` for Hearth.

```bash
./packaging/deb/build-deb.sh            # writes hearth_<version>_all.deb to the repo root
./packaging/deb/build-deb.sh /tmp       # or to a directory of your choice
```

Needs `dpkg-dev` (`sudo apt install dpkg-dev`). The version comes from
`hearth/__init__.py`.

## Install / remove

```bash
sudo apt install ./hearth_1.0.0_all.deb   # resolves dependencies
# or
sudo dpkg -i hearth_1.0.0_all.deb && sudo apt -f install

sudo apt remove hearth
```

Hearth still needs **Ollama** (https://ollama.com); it is not an apt package, so
it is not pulled in automatically.

## What the package does

- Installs the program to `/usr/share/hearth`, a launcher at `/usr/bin/hearth`,
  a desktop entry, and an icon.
- Ships a **systemd *user* service** at `/usr/lib/systemd/user/hearth.service`
  and enables it for all users, so Hearth starts per-user at login and runs on
  `127.0.0.1:8410` only.
- Keeps each user's chats, memory and settings in `~/.local/share/hearth`.
  These are **left in place** when the package is removed.
