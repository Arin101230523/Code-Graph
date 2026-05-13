"""
parser.py - tree-sitter AST walker, compatible with tree-sitter 0.23+
"""
from __future__ import annotations
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Literal

try:
    import tree_sitter_python as tspython
    import tree_sitter_typescript as tstypescript
    from tree_sitter import Language, Parser, Node
except ImportError as e:
    raise ImportError("Run: pip install tree-sitter tree-sitter-python tree-sitter-typescript") from e

NodeKind = Literal["function", "class", "module"]
EdgeKind = Literal["calls", "imports", "contains"]

@dataclass
class CodeNode:
    id: str
    name: str
    kind: NodeKind
    filepath: str
    start_line: int
    end_line: int
    docstring: str = ""

@dataclass
class CodeEdge:
    src: str
    dst: str
    kind: EdgeKind

@dataclass
class ParseResult:
    nodes: list[CodeNode] = field(default_factory=list)
    edges: list[CodeEdge] = field(default_factory=list)

PY_LANGUAGE  = Language(tspython.language())
TS_LANGUAGE  = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())

def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

def _child_of_type(node: Node, *types: str) -> Node | None:
    for c in node.children:
        if c.type in types:
            return c
    return None

def _children_of_type(node: Node, *types: str) -> list[Node]:
    return [c for c in node.children if c.type in types]

def _walk(node: Node):
    yield node
    for c in node.children:
        yield from _walk(c)

def _enclosing(node: Node, target_type: str, registry: dict) -> object | None:
    cur = node.parent
    while cur:
        if cur.type == target_type:
            return registry.get(cur.start_byte)
        cur = cur.parent
    return None

def _py_docstring(body: Node, source: bytes) -> str:
    if not body:
        return ""
    for child in body.children:
        if child.type == "expression_statement":
            for gc in child.children:
                if gc.type == "string":
                    return _text(gc, source).strip("\"' \n")[:200]
    return ""

def parse_python(filepath: str, source: bytes) -> ParseResult:
    tree   = Parser(PY_LANGUAGE).parse(source)
    root   = tree.root_node
    result = ParseResult()
    mod_id = f"{filepath}::__module__"
    result.nodes.append(CodeNode(id=mod_id, name=os.path.basename(filepath),
        kind="module", filepath=filepath, start_line=1, end_line=root.end_point[0]+1))

    cls_reg: dict[int, CodeNode] = {}
    fn_reg:  dict[int, CodeNode] = {}

    for node in _walk(root):
        if node.type == "class_definition":
            nn = _child_of_type(node, "identifier")
            if not nn: continue
            name = _text(nn, source)
            cn = CodeNode(id=f"{filepath}::{name}", name=name, kind="class",
                filepath=filepath, start_line=node.start_point[0]+1, end_line=node.end_point[0]+1)
            result.nodes.append(cn)
            result.edges.append(CodeEdge(src=mod_id, dst=cn.id, kind="contains"))
            cls_reg[node.start_byte] = cn

    for node in _walk(root):
        if node.type == "function_definition":
            nn = _child_of_type(node, "identifier")
            if not nn: continue
            name = _text(nn, source)
            enc_cls = _enclosing(node, "class_definition", cls_reg)
            q = f"{enc_cls.name}.{name}" if enc_cls else name
            body = _child_of_type(node, "block")
            fn = CodeNode(id=f"{filepath}::{q}", name=q, kind="function",
                filepath=filepath, start_line=node.start_point[0]+1,
                end_line=node.end_point[0]+1, docstring=_py_docstring(body, source))
            result.nodes.append(fn)
            pid = enc_cls.id if enc_cls else mod_id
            result.edges.append(CodeEdge(src=pid, dst=fn.id, kind="contains"))
            fn_reg[node.start_byte] = fn

    seen: set[tuple] = set()
    for node in _walk(root):
        if node.type == "call":
            # direct call: foo()
            callee = _child_of_type(node, "identifier")
            if not callee:
                # attribute call: obj.method()
                attr = _child_of_type(node, "attribute")
                if attr:
                    ids = _children_of_type(attr, "identifier")
                    callee = ids[-1] if ids else None
            if callee:
                cname = _text(callee, source)
                enc_fn = _enclosing(node, "function_definition", fn_reg)
                src_id = enc_fn.id if enc_fn else mod_id
                key = (src_id, cname)
                if key not in seen:
                    seen.add(key)
                    result.edges.append(CodeEdge(src=src_id, dst=cname, kind="calls"))

        elif node.type == "import_from_statement":
            parts = [c for c in node.children if c.type in ("dotted_name","identifier","aliased_import")]
            for p in parts[1:]:
                result.edges.append(CodeEdge(src=mod_id, dst=_text(p, source), kind="imports"))

        elif node.type == "import_statement":
            for c in node.children:
                if c.type in ("dotted_name", "aliased_import"):
                    result.edges.append(CodeEdge(src=mod_id, dst=_text(c, source), kind="imports"))

    return result

def parse_typescript(filepath: str, source: bytes, tsx: bool = False) -> ParseResult:
    lang = TSX_LANGUAGE if tsx else TS_LANGUAGE
    tree = Parser(lang).parse(source)
    root = tree.root_node
    result = ParseResult()
    mod_id = f"{filepath}::__module__"
    result.nodes.append(CodeNode(id=mod_id, name=os.path.basename(filepath),
        kind="module", filepath=filepath, start_line=1, end_line=root.end_point[0]+1))

    cls_reg: dict[int, CodeNode] = {}
    for node in _walk(root):
        if node.type == "class_declaration":
            nn = _child_of_type(node, "type_identifier")
            if not nn: continue
            name = _text(nn, source)
            cn = CodeNode(id=f"{filepath}::{name}", name=name, kind="class",
                filepath=filepath, start_line=node.start_point[0]+1, end_line=node.end_point[0]+1)
            result.nodes.append(cn)
            result.edges.append(CodeEdge(src=mod_id, dst=cn.id, kind="contains"))
            cls_reg[node.start_byte] = cn

    fn_types = {"function_declaration", "method_definition", "arrow_function"}
    for node in _walk(root):
        if node.type not in fn_types: continue
        nn = _child_of_type(node, "identifier", "property_identifier")
        if not nn: continue
        name = _text(nn, source)
        enc_cls = _enclosing(node, "class_declaration", cls_reg)
        q = f"{enc_cls.name}.{name}" if enc_cls else name
        fn = CodeNode(id=f"{filepath}::{q}", name=q, kind="function",
            filepath=filepath, start_line=node.start_point[0]+1, end_line=node.end_point[0]+1)
        result.nodes.append(fn)
        result.edges.append(CodeEdge(src=enc_cls.id if enc_cls else mod_id, dst=fn.id, kind="contains"))

    return result

EXTENSIONS = {".py": "python", ".ts": "typescript", ".tsx": "tsx"}
SKIP_DIRS  = {"node_modules",".git","__pycache__",".venv","venv","dist","build",
               ".mypy_cache",".pytest_cache",".tox","site-packages",".eggs"}

def parse_repo(repo_path: str) -> ParseResult:
    combined  = ParseResult()
    root_path = Path(repo_path).resolve()
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            ext = Path(fname).suffix
            if ext not in EXTENSIONS: continue
            full = os.path.join(dirpath, fname)
            rel  = os.path.relpath(full, root_path).replace("\\", "/")
            try:
                source = Path(full).read_bytes()
                lang   = EXTENSIONS[ext]
                if lang == "python":
                    pr = parse_python(rel, source)
                else:
                    pr = parse_typescript(rel, source, tsx=(lang=="tsx"))
                combined.nodes.extend(pr.nodes)
                combined.edges.extend(pr.edges)
            except Exception as exc:
                print(f"[parser] skipping {rel}: {exc}")
    return combined
