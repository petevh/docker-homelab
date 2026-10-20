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
SRC = ROOT / "workflows" / "src"
WORKFLOWS = {  # workflow file -> {node name -> source file}
    ROOT / "workflows" / "bank-statements.json": {
        "Match Statements": SRC / "match-statements.js",
        "Fetch Linked PDFs": SRC / "fetch-linked-pdfs.js",
        "Record Folder": SRC / "record-folder.js",
        "Folder Error": SRC / "folder-error.js",
        "Decrypt Error": SRC / "decrypt-error.js",
        "Run Summary": SRC / "run-summary.js",
    },
    ROOT / "workflows" / "monthly-summary.json": {
        "Monthly Summary": SRC / "monthly-summary.js",
    },
}


def load(workflow):
    raw = workflow.read_text(encoding="utf-8")
    data = json.loads(raw)
    return raw, data, (data[0] if isinstance(data, list) else data)


def dump(workflow, raw, data):
    out = json.dumps(data, indent=2, ensure_ascii=False)
    out = re.sub(r"\[\s*(-?\d+),\s*(-?\d+)\s*\]", r"[\1, \2]", out)  # keep node positions on one line
    workflow.write_text(out + ("\n" if raw.endswith("\n") else ""), encoding="utf-8")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--embed"
    drift = []
    for workflow, sources in WORKFLOWS.items():
        raw, data, wf = load(workflow)
        nodes = {n["name"]: n for n in wf["nodes"]}
        changed = False
        for name, path in sources.items():
            code = nodes[name]["parameters"]["jsCode"]
            if mode == "--extract":
                path.write_text(code, encoding="utf-8")
            elif code != path.read_text(encoding="utf-8"):
                drift.append(name)
                nodes[name]["parameters"]["jsCode"] = path.read_text(encoding="utf-8")
                changed = True
        if changed and mode == "--embed":
            dump(workflow, raw, data)
    if mode == "--check":
        print("out of sync: " + ", ".join(drift) if drift else "in sync")
        sys.exit(1 if drift else 0)
    if mode == "--extract":
        print("sources refreshed from the workflow JSON")
    elif drift:
        print("embedded: " + ", ".join(drift))
    else:
        print("already in sync")


if __name__ == "__main__":
    main()
