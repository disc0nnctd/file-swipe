# File Swipe

Review local files and folders as cards. Keep, archive, sort into destinations, or send items to a recovery folder. Includes previews, decision history, and optional AI suggestions.

## Setup

Requires Python 3, Pillow, and rsync (for copying between drives). Install FFmpeg for video previews.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
# Edit roots and destination paths before starting.
python server.py
```

Open http://127.0.0.1:8093. The server binds to localhost and has no built-in authentication. Use a trusted private tunnel for access from another device; do not expose it directly to the internet.

## Applying decisions

Review destinations and run `python apply.py --dry-run` first. Same-device operations can move files; cross-device operations copy them. Delete decisions move writable source files into `_fileswipe_trash`. The UI decision undo is not a general rollback for applied filesystem operations. Keep an independent backup.

## Optional AI

The configured `llm_url` receives filenames and file metadata. Use a local chat-completions endpoint, or understand the privacy implications of a remote endpoint. Rules remain available when the endpoint is unavailable.

This is an early self-hosted utility, not a unified cloud manager.
