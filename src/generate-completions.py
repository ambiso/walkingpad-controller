"""Generate shell completions for walkingpad-client (build-time tool).

Usage: generate-completions.py OUTDIR

Imports the client's real argparse parser and emits fish, bash and zsh
completion scripts, so the completions can never drift from the CLI.
The generated files are installed into the standard completion search
paths (share/fish/vendor_completions.d, share/bash-completion/
completions, share/zsh/site-functions).
"""

import os
import sys

import shtab

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wp_client import build_parser  # noqa: E402

OUT_PATHS = {
    "fish": "share/fish/vendor_completions.d/walkingpad-client.fish",
    "bash": "share/bash-completion/completions/walkingpad-client",
    "zsh": "share/zsh/site-functions/_walkingpad-client",
}


def main():
    outdir = sys.argv[1]
    for shell, rel in OUT_PATHS.items():
        path = os.path.join(outdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(shtab.complete(build_parser(), shell=shell))
        print(path)


if __name__ == "__main__":
    main()
