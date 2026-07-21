"""Build a batch file (gzipped JSON) on local disk.

Delivery to the NAS (local mount or over SSH) is handled by transport.py. We
always materialize the batch in a local outbox first so a failed/again-later
delivery can be retried without re-collecting.
"""
from __future__ import annotations

import gzip
import json
import os


def build_gz(out_path, result, compress=True):
    """Write `result` to out_path (gz if compress). Returns bytes written (raw)."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    data = json.dumps(result, ensure_ascii=False).encode("utf-8")
    tmp = out_path + ".building"
    if compress:
        with gzip.open(tmp, "wb") as f:
            f.write(data)
    else:
        with open(tmp, "wb") as f:
            f.write(data)
    os.replace(tmp, out_path)
    return len(data)
