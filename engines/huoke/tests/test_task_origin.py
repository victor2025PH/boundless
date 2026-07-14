# -*- coding: utf-8 -*-
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_with_origin_setdefault():
    from src.host.task_origin import with_origin

    a = with_origin({"x": 1}, "api")
    assert a["_created_via"] == "api"
    b = with_origin({"_created_via": "keep"}, "api")
    assert b["_created_via"] == "keep"
