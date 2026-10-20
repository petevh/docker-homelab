#!/usr/bin/env python3
"""Keep the Code nodes of workflows/bank-statements.json in sync with their readable
sources in workflows/src/.

The workflow JSON holds each Code node as one escaped string, which is unreadable in
a diff. The sources in workflows/src/ are the files to edit; this script copies them
into the JSON.

  python3 tools/build-workflow.py            # embed the sources into the JSON
  python3 tools/build-workflow.py --check    # exit 1 if the JSON is out of sync
  python3 tools/build-workflow.py --extract  # the reverse: refresh src/ from the JSON

Then re-import into n8n (see docs/bank-statements-workflow.md).
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "workflows" / "bank-statements.json"
SOURCES = {  # node name -> source file
    "Match Statements": ROOT / "workflows" / "src" / "match-statements.js",
    "Fetch Linked PDFs": ROOT / "workflows" / "src" / "fetch-linked-pdfs.js",
    "Record Folder": ROOT / "workflows" / "src" / "record-folder.js",
    "Folder Error": ROOT / "workflows" / "src" / "folder-error.js",
    "Decrypt Error": ROOT / "workflows" / "src" / "decrypt-error.js",
    "Failure Summary": ROOT / "workflows" / "src" / "failure-summary.js",
}


def load():
    raw = WORKFLOW.read_text(encoding="utf-8")
    data = json.loads(raw)
    return raw, data, (data[0] if isinstance(data, list) else data)


def dump(raw, data):
    out = json.dumps(data, indent=2, ensure_ascii=False)
    out = re.sub(r"\[\s*(-?\d+),\s*(-?\d+)\s*\]", r"[\1, \2]", out)  # keep node positions on one line
    WORKFLOW.write_text(out + ("\n" if raw.endswith("\n") else ""), encoding="utf-8")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--embed"
    raw, data, wf = load()
    nodes = {n["name"]: n for n in wf["nodes"]}
    drift = []
    for name, path in SOURCES.items():
        code = nodes[name]["parameters"]["jsCode"]
        if mode == "--extract":
            path.write_text(code, encoding="utf-8")
        elif code != path.read_text(encoding="utf-8"):
            drift.append(name)
            nodes[name]["parameters"]["jsCode"] = path.read_text(encoding="utf-8")
    if mode == "--check":
        print("out of sync: " + ", ".join(drift) if drift else "in sync")
        sys.exit(1 if drift else 0)
    if mode == "--extract":
        print("sources refreshed from the workflow JSON")
    elif drift:
        dump(raw, data)
        print("embedded: " + ", ".join(drift))
    else:
        print("already in sync")


if __name__ == "__main__":
    main()
