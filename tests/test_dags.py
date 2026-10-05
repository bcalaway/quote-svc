"""Static checks on dags/ (Airflow itself isn't installed in this image).

The platform's rules for app DAGs (docs/app-platform.md in nyc_pa_aws_gitops,
ADR-0031): ids start with the app's name and `__`, no dots; imports are
Airflow, the platform helper and the standard library only, because DAG
files run inside the shared Airflow, not in this app's image.
"""

import ast
import sys
from pathlib import Path

import pytest

DAGS = sorted((Path(__file__).resolve().parent.parent / "dags").rglob("*.py"))
ALLOWED_TOP = {"airflow", "home_platform_jobs", "__future__"} | set(sys.stdlib_module_names)


def test_there_are_dags():
    assert DAGS


@pytest.mark.parametrize("path", DAGS, ids=lambda p: p.name)
def test_imports_are_airflow_or_stdlib(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for n in names:
            assert n.split(".")[0] in ALLOWED_TOP, f"{path.name}: imports {n}"


@pytest.mark.parametrize("path", DAGS, ids=lambda p: p.name)
def test_dag_ids(path):
    tree = ast.parse(path.read_text())
    ids = [
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "dag_id" and isinstance(kw.value, ast.Constant)
    ]
    assert ids, f"{path.name}: no literal dag_id"
    for dag_id in ids:
        assert dag_id.startswith("quote_svc__"), dag_id
        assert "." not in dag_id and dag_id == dag_id.lower(), dag_id
