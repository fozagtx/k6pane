"""Hermes plugin catalog admission rules, checked offline.

Mirrors the checks `hermes plugins validate` runs at catalog admission
(hermes_cli/plugin_validate.py and the install scanner) plus the written
catalog rules, so CI fails here before a catalog PR would. Rule numbers refer
to the "Admission rules" list in plugin-catalog/README.md.
"""

import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "k6pane"
MANIFEST = yaml.safe_load((ROOT / "plugin.yaml").read_text())
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
CODE_FILES = sorted([ROOT / "__init__.py", *PKG.rglob("*.py")])

# Config types the Hermes loader type-checks (hermes_cli/plugins_manifest.py::_CONFIG_SCHEMA_TYPES).
CONFIG_TYPES = {"str", "string", "int", "integer", "float", "number", "bool", "boolean",
                "list", "array", "dict", "object", "secret"}
# The Hermes modules k6pane imports; every one is on the public plugin API list
# (website/docs/developer-guide/plugins/public-api.md, generated from hermes_cli/data/plugin_api.json).
PUBLIC_HERMES_IMPORTS = {"agent.secret_scope", "plugins.plugin_storage"}
HERMES_TOP_LEVEL = {"agent", "hermes_cli", "plugins", "tools", "gateway", "hermes_constants", "hermes_state",
                    "hermes_platform", "cli", "cron", "providers", "pm", "utils", "run_agent", "model_tools"}


def _tree(path):
    return ast.parse(path.read_text(), filename=str(path))


def _absolute_imports(path):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno


# --- manifest checks (plugin_validate: manifest fields, requires_hermes, config schema, requires_env) ---

def test_manifest_required_fields_and_name():
    for field in ("name", "version", "description"):
        assert MANIFEST.get(field), f"plugin.yaml missing {field}"
    assert re.fullmatch(r"[a-z0-9_-]{1,64}", MANIFEST["name"])


def test_version_is_consistent_everywhere():
    """Rule 14: `version` matches the pinned code."""
    import k6pane

    versions = {
        "plugin.yaml": str(MANIFEST["version"]),
        "pyproject.toml": PYPROJECT["project"]["version"],
        "k6pane/__init__.py": k6pane.__version__,
        "root __init__.py": re.search(r'__version__ = "([^"]+)"', (ROOT / "__init__.py").read_text()).group(1),
    }
    assert len(set(versions.values())) == 1, versions


def test_requires_hermes_is_a_semver_floor():
    """Rule 14: a SemVer floor (">=0.21.5"), never CalVer, and it must parse strictly."""
    spec = MANIFEST.get("requires_hermes")
    assert spec, "declare requires_hermes in plugin.yaml (older Hermes enforces only that one)"
    clauses = [c.strip() for c in str(spec).split(",") if c.strip()]
    assert any(c.startswith(">=") for c in clauses)
    for clause in clauses:
        m = re.fullmatch(r"(>=|<=|==|!=|>|<)\s*v?(\d+)\.(\d+)(?:\.(\d+))?", clause)
        assert m, f"requires_hermes clause {clause!r} does not parse"
        assert int(m.group(2)) < 100, f"{clause!r} looks like a CalVer date, not a SemVer floor"


def test_config_schema_shape():
    schema = MANIFEST.get("config_schema") or {}
    assert isinstance(schema, dict)
    for key, spec in schema.items():
        assert isinstance(spec, dict), key
        assert str(spec.get("type", "str")).lower() in CONFIG_TYPES, key
        if "required" in spec:
            assert isinstance(spec["required"], bool), key
        assert spec.get("description"), f"config_schema.{key} needs a description (renders in the settings form)"


def test_requires_env_upper_snake():
    for entry in MANIFEST.get("requires_env") or []:
        name = entry if isinstance(entry, str) else entry.get("name", "")
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), name


def test_loadable_entrypoint():
    assert (ROOT / "__init__.py").is_file(), "Hermes needs __init__.py beside plugin.yaml"


def test_pyproject_installs_nothing_into_hermes():
    """No [build-system], extras or dependency groups: Hermes reads that as 'no Python declaration',
    so installing the plugin never asks for dependency consent or changes Hermes' environment."""
    assert "build-system" not in PYPROJECT
    project = PYPROJECT["project"]
    assert project.get("dependencies", []) == []
    assert not any(project.get("optional-dependencies", {}).values())
    assert not any(PYPROJECT.get("dependency-groups", {}).values())
    assert PYPROJECT.get("tool", {}).get("uv", {}).get("package") is not True
    assert not MANIFEST.get("python_dependencies") and not MANIFEST.get("pip_dependencies")


# --- capability probe (rule 6: declared capabilities must match reality) ---

_PROBE = r"""
import importlib.util, json, sys, types
plugin_dir = sys.argv[1]
recorded = {"tools": [], "hooks": [], "middleware": [], "skills": []}
REAL_METHODS = {"get_config", "set_config", "has_capability", "register_command", "register_cli_command",
                "register_platform", "inject_message", "register_skill"}

class RecordingContext:
    def register_tool(self, name, *a, **k): recorded["tools"].append(str(name))
    def register_hook(self, name, cb, **k): recorded["hooks"].append(str(name))
    def register_middleware(self, kind, cb): recorded["middleware"].append(str(kind))
    def register_skill(self, name, path, *a, **k): recorded["skills"].append([name, str(path)])
    def get_config(self, key, default=None): return default
    def __getattr__(self, name):
        if name in REAL_METHODS:
            return lambda *a, **k: None
        raise AttributeError(name)

ns = types.ModuleType("hermes_plugins"); ns.__path__ = []; sys.modules["hermes_plugins"] = ns
spec = importlib.util.spec_from_file_location("hermes_plugins.k6pane", plugin_dir + "/__init__.py",
                                              submodule_search_locations=[plugin_dir])
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
mod.register(RecordingContext())
print("PROBE:" + json.dumps(recorded))
"""


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    home = tmp_path_factory.mktemp("hermes-home")
    env = {k: v for k, v in os.environ.items() if not k.startswith("K6PANE_")}
    env["HERMES_HOME"] = str(home)
    out = subprocess.run([sys.executable, "-P", "-c", _PROBE, str(ROOT)], capture_output=True, text=True,
                         env=env, timeout=30, check=False)
    line = next((ln for ln in out.stdout.splitlines() if ln.startswith("PROBE:")), None)
    assert line, f"register() failed in isolation:\n{out.stderr}"
    return json.loads(line[len("PROBE:"):]), home


def test_declared_tools_hooks_middleware_match_registrations(probe):
    recorded, _ = probe
    assert sorted(recorded["tools"]) == sorted(MANIFEST.get("provides_tools") or [])
    assert sorted(recorded["hooks"]) == sorted(MANIFEST.get("provides_hooks") or [])
    assert sorted(recorded["middleware"]) == sorted(MANIFEST.get("provides_middleware") or [])


def test_tool_names_cannot_collide_with_builtins():
    assert all(t.startswith("k6pane_") for t in MANIFEST["provides_tools"])


def test_skill_registered_read_only_from_plugin_tree(probe):
    recorded, home = probe
    assert len(recorded["skills"]) == 1
    name, path = recorded["skills"][0]
    assert name == "load-testing" and Path(path).is_file() and Path(path).is_relative_to(ROOT)
    assert not (home / "skills").exists(), "register() must not copy skills into HERMES_HOME"


def test_register_writes_nothing(probe):
    _, home = probe
    assert list(home.iterdir()) == [], f"register() wrote into HERMES_HOME: {list(home.iterdir())}"


# --- install scanner, structural part (rule 7, rule 17) ---

def _tracked_files():
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    return [ROOT / p for p in out.stdout.decode().split("\0") if p]


def test_no_binaries_bytecode_or_escaping_symlinks():
    """Rule 17: no compiled binaries (by header) and no committed bytecode."""
    magic = (b"\x7fELF", b"MZ", b"\x00asm", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
             b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
    files = _tracked_files()
    assert len(files) < 400
    assert sum(f.stat().st_size for f in files if f.is_file()) < 10 * 1024 * 1024
    for f in files:
        assert f.suffix not in (".pyc", ".pyo"), f
        if f.is_symlink():
            assert f.resolve().is_relative_to(ROOT), f
            continue
        with open(f, "rb") as fh:
            assert not fh.read(4).startswith(magic), f"binary payload: {f}"


# --- no core overrides, public imports only (rules 3 and 8) ---

def test_only_public_hermes_imports():
    for path in CODE_FILES:
        for module, line in _absolute_imports(path):
            if module.split(".")[0] in HERMES_TOP_LEVEL:
                assert module in PUBLIC_HERMES_IMPORTS, f"{path.relative_to(ROOT)}:{line} imports {module}"


def test_no_runtime_overrides_of_hermes_core():
    for path in CODE_FILES:
        tree = _tree(path)
        hermes_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                    and node.module.split(".")[0] in HERMES_TOP_LEVEL:
                hermes_names.update(a.asname or a.name for a in node.names)
            elif isinstance(node, ast.Import):
                hermes_names.update((a.asname or a.name).split(".")[0] for a in node.names
                                    if a.name.split(".")[0] in HERMES_TOP_LEVEL)
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] \
                if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
            for t in targets:
                base = t
                while isinstance(base, (ast.Attribute, ast.Subscript)):
                    base = base.value
                if isinstance(t, (ast.Attribute, ast.Subscript)) and isinstance(base, ast.Name):
                    assert base.id not in hermes_names, f"{path.name}:{node.lineno} assigns into Hermes ({base.id})"
                    assert not (base.id == "sys" and "modules" in ast.unparse(t)), f"{path.name}:{node.lineno}"
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setattr" and node.args:
                first = node.args[0]
                assert not (isinstance(first, ast.Name) and first.id in hermes_names), f"{path.name}:{node.lineno}"


# --- no self-updating code (rule 3) ---

def test_no_network_clients_or_self_update():
    banned = {"urllib.request", "http.client", "requests", "httpx", "aiohttp", "urllib3", "ftplib"}
    for path in CODE_FILES:
        for module, line in _absolute_imports(path):
            assert module not in banned, f"{path.name}:{line} imports {module}"
        text = path.read_text()
        assert "git pull" not in text and "pip install" not in text, path


# --- credentials only through Hermes (rule 10) ---

def test_credentials_only_through_hermes_secret_scope():
    for path in CODE_FILES:
        text = path.read_text()
        for bad in ("auth.json", "load_dotenv", "dotenv_values", "keyring", ".netrc"):
            assert bad not in text, f"{path.name} references {bad}"
        assert not re.search(r"open\([^)]*\.env\b", text), f"{path.name} opens a .env file"
    redact_src = (PKG / "redact.py").read_text()
    assert "os.environ" not in redact_src and "hermes_host.get_secret(" in redact_src
    host_src = (PKG / "hermes_host.py").read_text()
    assert "from agent.secret_scope import get_secret" in host_src


def test_secret_reads_use_profile_scope_when_hermes_provides_it(monkeypatch):
    import types

    from k6pane import hermes_host

    calls = []
    fake = types.ModuleType("agent.secret_scope")
    fake.get_secret = lambda name, default=None: calls.append(name) or "from-scope"
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    monkeypatch.setenv("K6PANE_SECRET_X", "from-os-environ")
    assert hermes_host.get_secret("K6PANE_SECRET_X") == "from-scope"
    assert calls == ["K6PANE_SECRET_X"]

    def refuse(name, default=None):
        raise RuntimeError("no profile scope")

    fake.get_secret = refuse
    with pytest.raises(hermes_host.SecretUnavailable):
        hermes_host.get_secret("K6PANE_SECRET_X")


# --- approvals respected, no shell (rule 11) ---

def test_no_shell_execution():
    for path in CODE_FILES:
        text = path.read_text()
        assert "shell=True" not in text and "os.system(" not in text and "os.popen(" not in text, path
    users = [p.name for p in CODE_FILES if re.search(r"subprocess\.(run|Popen|call|check_output)\(", p.read_text())]
    assert sorted(users) == ["runner.py"], users


# --- risky behaviour disclosed (rule 13) ---

def test_readme_discloses_risky_behaviour():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## Disclosure", 1)
    assert len(section) == 2, "README needs a '## Disclosure' section"
    body = section[1].split("\n## ", 1)[0].lower()
    for topic in ("subprocess", "network", "writes", "reads", "credentials", "telemetry", "background"):
        assert topic in body, f"disclosure section does not mention {topic}"


def test_skill_fallback_on_hermes_without_skill_api_is_reported():
    from pathlib import Path

    from k6pane import hermes_host

    class OldCtx:  # Hermes 0.7.x: register_tool exists, register_skill does not
        pass

    path = Path("k6pane/skills/load-testing/SKILL.md").resolve()
    assert hermes_host.register_skill(OldCtx(), "load-testing", path) is False
    status = hermes_host.skill_status
    assert status["registered"] is False
    assert status["skills_dir"] == str(path.parent.parent)
    assert "skills.external_dirs" in status["fix"]
