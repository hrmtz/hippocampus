"""deja-code chunker — all-depth function extraction via tree-sitter.

Design: docs/designs/DEJA_CODE.md §4. The load-bearing requirement (dual-magi
R1 CRITICAL r1-pipeline-1) is that functions are extracted at ANY nesting depth
with parent-chain qualified symbols: real-world reuse happens as "renamed and
nested inside another function" (e.g. player.js attachVoice.barsStart, ported
from another page's animStart). Top-level-only chunking is structurally blind to it.

Pure parsing module: no DB, no embed, no filesystem policy (see policy.py).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tree_sitter_language_pack import get_parser

MIN_LINES = 4          # functions shorter than this are boilerplate noise (§4.2)
CHAR_CAP = 6000        # payload pre-cap; token-level truncation happens at embed
                       # time (max_length=1024) and is a SEPARATE limit (§4.2)

EXT_LANG = {
    "py": "python",
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "sh": "bash",
    "bash": "bash",
    "html": "html",
}

_JS_FN_TYPES = {"function_declaration", "function_expression", "arrow_function",
                "method_definition", "generator_function_declaration"}
_PY_FN_TYPES = {"function_definition"}
_PY_CLASS_TYPES = {"class_definition"}
_BASH_FN_TYPES = {"function_definition"}


@dataclass(frozen=True)
class Chunk:
    symbol: str        # qualified, e.g. "attachVoice.barsStart" / "Cls.method"
    kind: str          # function | method | class | script_fn
    start_line: int    # 1-based, inclusive
    end_line: int
    content: str
    content_sha: str
    truncated: bool


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _mk_chunk(symbol: str, kind: str, start: int, end: int, text: str) -> Chunk:
    truncated = len(text) > CHAR_CAP
    if truncated:
        text = text[:CHAR_CAP]
    return Chunk(symbol, kind, start, end, text, _sha(text), truncated)


def _js_name(node):
    """Name for a JS/TS function-ish node, incl. `const x = () => {}` binding."""
    if node.type in ("function_declaration", "generator_function_declaration",
                     "method_definition"):
        ident = node.child_by_field_name("name")
        return ident.text.decode(errors="replace") if ident else None
    parent = node.parent
    if parent is None:
        return None
    if parent.type == "variable_declarator":
        ident = parent.child_by_field_name("name")
        return ident.text.decode(errors="replace") if ident else None
    if parent.type == "pair":
        key = parent.child_by_field_name("key")
        return key.text.decode(errors="replace") if key else None
    if parent.type == "assignment_expression":
        left = parent.child_by_field_name("left")
        return left.text.decode(errors="replace") if left else None
    return None


def _field_name(node):
    ident = node.child_by_field_name("name")
    return ident.text.decode(errors="replace") if ident else None


def _py_class_header(node, src: bytes) -> tuple[int, int, str]:
    """Class chunk = declaration line(s) + docstring only (§4.2); methods are
    their own chunks, so the full class body would double-count everything."""
    header_end_byte = node.start_byte
    header_end_line = node.start_point[0] + 1
    body = node.child_by_field_name("body")
    if body is not None:
        header_end_byte = body.start_byte
        header_end_line = body.start_point[0] + 1
        first = body.named_children[0] if body.named_children else None
        # docstring shape varies by grammar version: bare `string`, or
        # `expression_statement > string`
        if first is not None and (
                first.type == "string"
                or (first.type == "expression_statement" and first.named_children
                    and first.named_children[0].type == "string")):
            header_end_byte = first.end_byte
            header_end_line = first.end_point[0] + 1
    text = src[node.start_byte:header_end_byte].decode(errors="replace")
    return node.start_point[0] + 1, header_end_line, text


def _walk(root, src: bytes, lang: str, base_kind: str, line_offset: int = 0):
    """Yield Chunk for every named function at any depth, parent-chain qualified."""
    if lang in ("javascript", "typescript"):
        fn_types, class_types, namer = _JS_FN_TYPES, set(), _js_name
    elif lang == "python":
        fn_types, class_types, namer = _PY_FN_TYPES, _PY_CLASS_TYPES, _field_name
    elif lang == "bash":
        fn_types, class_types, namer = _BASH_FN_TYPES, set(), _field_name
    else:
        return

    stack = [(root, ())]
    while stack:
        node, chain = stack.pop()
        for child in reversed(node.children):
            child_chain = chain
            name = None
            if child.type in fn_types:
                name = namer(child)
                if name:
                    start = child.start_point[0] + 1
                    end = child.end_point[0] + 1
                    if end - start + 1 >= MIN_LINES:
                        kind = ("method" if child.type == "method_definition"
                                or (chain and lang == "python") else base_kind)
                        text = src[child.start_byte:child.end_byte].decode(
                            errors="replace")
                        yield _mk_chunk(".".join((*chain, name)), kind,
                                        start + line_offset, end + line_offset,
                                        text)
            elif child.type in class_types:
                name = namer(child)
                if name:
                    start, end, text = _py_class_header(child, src)
                    if text.strip():
                        yield _mk_chunk(".".join((*chain, name)), "class",
                                        start + line_offset, end + line_offset,
                                        text)
            if name:
                child_chain = (*chain, name)
            stack.append((child, child_chain))


def _html_scripts(src: bytes):
    """Yield (script_bytes, line_offset) for each <script> element's raw text."""
    tree = get_parser("html").parse(src)
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "script_element":
            for child in node.children:
                if child.type == "raw_text":
                    yield (src[child.start_byte:child.end_byte],
                           child.start_point[0])
        stack.extend(node.children)


def chunk_source(src: bytes, ext: str) -> list[Chunk]:
    """Chunk raw source bytes whose file extension is `ext` (no leading dot).

    Unknown extensions yield []. Parse errors are fail-soft: tree-sitter always
    returns a tree; whatever parsed cleanly is extracted.
    """
    lang = EXT_LANG.get(ext)
    if lang is None:
        return []
    if lang == "html":
        chunks: list[Chunk] = []
        for script_src, offset in _html_scripts(src):
            tree = get_parser("javascript").parse(script_src)
            chunks.extend(_walk(tree.root_node, script_src, "javascript",
                                "script_fn", line_offset=offset))
        return chunks
    tree = get_parser(lang).parse(src)
    return list(_walk(tree.root_node, src, lang, "function"))


def chunk_file(path: str) -> list[Chunk]:
    """Chunk a file on disk. Raises OSError on unreadable path (caller policy
    decides whether that is skip+warn or fatal)."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    with open(path, "rb") as fh:
        return chunk_source(fh.read(), ext)
