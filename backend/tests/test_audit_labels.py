"""Every action the backend audits has a readable label on the Logs → Audit trail page."""

import ast
import re
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
LOGS_PAGE = BACKEND.parent / "frontend" / "src" / "pages" / "Logs.tsx"
# f-string actions: the values the template can take.
TEMPLATES = {"manual_grant_": ("manual_grant_add", "manual_grant_remove")}


def _names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _names(node.body) | _names(node.orelse)
    if isinstance(node, ast.JoinedStr):
        prefix = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
        return set(TEMPLATES.get(prefix, (f"<unknown template {prefix}>",)))
    return {f"<not a literal: {ast.unparse(node)}>"}


def audited_actions() -> set[str]:
    actions: set[str] = set()
    for path in (BACKEND / "woosarr").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            # audit.record(session, actor, action, ...)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "record" \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "audit" and len(node.args) >= 3:
                actions |= _names(node.args[2])
    return actions


def labelled_actions() -> set[str]:
    text = LOGS_PAGE.read_text()
    block = text[text.index("const ACTION_LABEL"):]
    block = block[:block.index("};")]
    return set(re.findall(r"^\s+([a-z_]+):", block, re.M))


class AuditLabelTests(unittest.TestCase):
    def test_every_audited_action_has_a_label(self):
        actions = audited_actions()
        self.assertGreater(len(actions), 40)  # the walk found the calls
        self.assertIn("access_changed", actions)  # nested conditional expressions are followed
        missing = sorted(actions - labelled_actions())
        self.assertEqual(missing, [], "add these to ACTION_LABEL in frontend/src/pages/Logs.tsx")
