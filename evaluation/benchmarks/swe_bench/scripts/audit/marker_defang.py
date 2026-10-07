#!/usr/bin/env python3
"""Project raw model/agent artifacts into LLM-safe text.

Why this exists
---------------
OpenHands completions and run logs record the model's *raw* output verbatim.
That output contains literal function-call markup. When such text is pasted
into an agent conversation it stops being "content under discussion" and
becomes "protocol on the wire": the next generation continues the markup
inside a tool-call argument stream, the JSON never closes, and the turn dies
with a malformed-response failure.

The fix is not to read more carefully. The fix is to never put the literal
markers in the conversation at all. This tool is the only supported way to
look at these artifacts:

  * it rewrites every protocol-shaped marker into an inert guillemet form,
    e.g. a live function tag carrying ``name="x"`` becomes
    ``\u00abfunction:x\u00bb``; the delimiter characters themselves are
    gone, so no parser - ours or a model's - can match them;
  * it reports *structure* (counts, families, verdicts) instead of raw text,
    which is what the analysis actually needed;
  * it refuses to emit anything that still looks like a live marker, so a
    silent regression in the rules cannot leak.

Subcommands
-----------
  scan    Structural report for completions / logs. Facts only, never content.
  show    Defanged excerpt of one field. Rewritten, so it is safe to read.
  search  Grep with defanged output. Use this instead of raw grep on logs.
  check   Safety gate: exit 1 if the input still holds live markers.
  selftest  Assert the rules against a corpus of real corruptions.

Marker samples in this file are assembled from fragments on purpose. A test
corpus that spells the markers out would itself be poison for anyone who
opens this file in an agent session.

Usage
-----
  marker_defang.py scan   COMPLETION.json [COMPLETION.json ...]
  marker_defang.py scan   RUN.ndjson --field response
  marker_defang.py show   COMPLETION.json --field response --max-chars 800
  marker_defang.py search RUN.log --pattern 'DSML' --context 2
  marker_defang.py check  SUSPECT.txt
  marker_defang.py selftest

Reads recorded artifacts only; never touches a runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# --- marker taxonomy -------------------------------------------------------
#
# Two independent sources of trouble, handled by two mechanisms:
#
#   1. the fullwidth-pipe banner, which is a bare token sequence; and
#   2. angle-bracket tags, which are structural.
#
# Both are matched case-insensitively and allow whitespace after the opening
# bracket, because that is exactly the latitude the real providers use.

_BANNER_KEYWORD = "DSML"
_BANNER_RE = re.compile(r"[|\uff5c]{1,4}\s*" + _BANNER_KEYWORD + r"\s*[|\uff5c]{1,4}", re.I)

# Tag names that carry protocol meaning. Anything outside this set is left
# alone, so ordinary HTML/XML inside code samples survives untouched.
_TAG_KEYWORDS = frozenset(
    {
        "function",
        "functions",
        "function_call",
        "function_calls",
        "parameter",
        "parameters",
        "invoke",
        "invokes",
        "calls",
        "call",
        "tool_call",
        "tool_calls",
        "tool_use",
        "tool_result",
        "arg_key",
        "arg_value",
        "antml",
    }
)

_TAG_NAME = r"[A-Za-z_][A-Za-z0-9_:.\-]*"
# A complete tag. The body is kept short and bracket-free so that a stray '<'
# in prose cannot swallow a whole paragraph while resynchronising.
_CLOSED_TAG_RE = re.compile(r"<\s*(/?)\s*(" + _TAG_NAME + r")\b([^<>\n]{0,200}?)>")
# An opening bracket whose tag never closes - a truncated stream still poisons.
_OPEN_TAG_RE = re.compile(r"<\s*(/?)\s*(" + _TAG_NAME + r")\b")

_VALUE_RE = re.compile(
    r"""(?:\s*=\s*|\s+name\s*=\s*)\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>]+))""",
    re.I,
)


def _base_name(name: str) -> str:
    """Strip any namespace prefix, e.g. ``antml:invoke`` -> ``invoke``."""
    return name.split(":")[-1].lower()


def _is_marker_tag(name: str) -> bool:
    return _base_name(name) in _TAG_KEYWORDS


def _extract_value(rest: str) -> str:
    """Pull the tool/parameter name out of a tag body, if it carries one."""
    match = _VALUE_RE.search(rest)
    if match is None:
        return ""
    value = next((group for group in match.groups() if group), "")
    # Keep it inert: a value that reintroduces a delimiter would undo the fix.
    return re.sub(r"[<>\n]|[|\uff5c]", "", value).strip()


def defang(text: str) -> str:
    """Rewrite every live protocol marker into an inert, readable form.

    The transformation replaces the delimiter characters rather than the
    keywords, so the result still reads naturally - ``\u00abfunction:execute_bash\u00bb``
    - while no longer matching any markup grammar.
    """
    if not text:
        return ""

    out = _BANNER_RE.sub("\u00ab" + _BANNER_KEYWORD + "\u00bb", text)

    def closed(match: re.Match[str]) -> str:
        closing, name, rest = match.group(1), match.group(2), match.group(3)
        if not _is_marker_tag(name):
            return match.group(0)
        value = _extract_value(rest)
        label = _base_name(name) if not value else f"{_base_name(name)}:{value}"
        return f"\u00ab{'/' if closing else ''}{label}\u00bb"

    out = _CLOSED_TAG_RE.sub(closed, out)

    def opener(match: re.Match[str]) -> str:
        closing, name = match.group(1), match.group(2)
        if not _is_marker_tag(name):
            return match.group(0)
        return f"\u00ab{'/' if closing else ''}{_base_name(name)}"

    return _OPEN_TAG_RE.sub(opener, out)


def _iter_marker_tags(text: str):
    """Yield the name of every protocol-shaped tag, live form only."""
    for match in _CLOSED_TAG_RE.finditer(text):
        name = match.group(2)
        if _is_marker_tag(name):
            yield name
    for match in _OPEN_TAG_RE.finditer(text):
        name = match.group(2)
        if _is_marker_tag(name):
            yield name


def live_markers(text: str) -> dict[str, int]:
    """Count still-live markers, keyed by family. Empty dict means inert."""
    text = text or ""
    found: dict[str, int] = {}
    banners = len(_BANNER_RE.findall(text))
    if banners:
        found["dsml_banner"] = banners

    for name in _iter_marker_tags(text):
        key = f"tag:{_base_name(name)}"
        found[key] = found.get(key, 0) + 1
    return found


_DEFANGED_RE = re.compile(r"\u00ab(/)?([A-Za-z_][A-Za-z0-9_.\-]*)(?::([^\u00bb]*))?\u00bb")


def marker_census(text: str) -> dict[str, int]:
    """Count markers of any form, live or already defanged.

    This is the analytical signal: a response that carries more than one
    family is a response that mixed formats, which is the condition that
    breaks the converter downstream.
    """
    text = text or ""
    census: dict[str, int] = {}

    def bump(key: str) -> None:
        census[key] = census.get(key, 0) + 1

    banners = len(_BANNER_RE.findall(text)) + len(
        re.findall(r"\u00ab" + _BANNER_KEYWORD + r"\u00bb", text)
    )
    if banners:
        census["dsml_banner"] = banners

    for name in _iter_marker_tags(text):
        bump(f"tag:{_base_name(name)}")
    for match in _DEFANGED_RE.finditer(text):
        label = _base_name(match.group(2))
        if label in _TAG_KEYWORDS:
            bump(f"tag:{label}")
    return census


def assert_inert(text: str, origin: str) -> str:
    """Fail loudly rather than emitting a live marker downstream."""
    remaining = live_markers(text)
    if remaining:
        raise SystemExit(
            f"refusing to emit live markers from {origin}: {sorted(remaining)}"
        )
    return text


def _emit(text: str, origin: str) -> None:
    sys.stdout.write(assert_inert(text, origin))


# --- artifact projection ---------------------------------------------------


def _message_view(response: object) -> dict[str, object]:
    """Normalise an OpenAI- or Anthropic-shaped response into one view."""
    view: dict[str, object] = {"content": "", "tool_calls": 0, "reasoning_len": 0}
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            if isinstance(message, dict):
                view["content"] = message.get("content") or ""
                calls = message.get("tool_calls") or []
                view["tool_calls"] = len(calls) if isinstance(calls, list) else 0
                reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                view["reasoning_len"] = len(reasoning) if isinstance(reasoning, str) else 0
                return view
        blocks = response.get("content")
        if isinstance(blocks, list):
            texts, calls, reasoning = [], 0, 0
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    texts.append(block.get("text") or "")
                elif kind == "thinking":
                    reasoning += len(block.get("thinking") or "")
                elif kind == "tool_use":
                    calls += 1
            view["content"] = "".join(texts)
            view["tool_calls"] = calls
            view["reasoning_len"] = reasoning
    return view


def _json_health(text: str, max_regions: int = 50, max_region_chars: int = 400_000) -> dict[str, int]:
    """Count brace-balanced JSON regions and how many fail to parse.

    A tool-call argument stream that resumed its own markup produces a
    perfectly marker-free string that is nevertheless invalid JSON. Detecting
    that is a separate question from defanging, so it gets a separate check.
    """
    regions: list[str] = []
    depth = 0
    start: int | None = None
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    if len(regions) < max_regions:
                        regions.append(text[start : index + 1][:max_region_chars])
                    start = None

    failures = 0
    for region in regions:
        try:
            json.loads(region)
        except ValueError:
            failures += 1
    return {"json_regions": len(regions), "json_parse_failures": failures}


def _classify(view: dict[str, object]) -> dict[str, object]:
    """Turn a response view into a format verdict plus its evidence."""
    content = view["content"] if isinstance(view["content"], str) else ""
    census = marker_census(content)
    families = sorted({key.partition(":")[2] or key for key in census})
    fenced = bool(re.search(r"```(?:json|tool_code)?\s*\{", content))
    health = _json_health(content)

    if view["tool_calls"]:
        verdict = "native_tool_calls"
    elif not content.strip():
        verdict = "empty"
    elif len(families) > 1:
        verdict = "mixed_markup"
    elif families:
        verdict = "single_markup"
    elif fenced:
        verdict = "fenced_json"
    else:
        verdict = "prose"

    return {
        "verdict": verdict,
        "families": families,
        "mixed": len(families) > 1,
        "marker_census": census,
        "fenced_json": fenced,
        **health,
    }


def scan_completion(path: str) -> dict[str, object]:
    """Structural facts about one recorded completion. No raw text escapes."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        payload = json.load(handle)

    report: dict[str, object] = {"file": path, "bytes": os.path.getsize(path)}
    if not isinstance(payload, dict):
        report["kind"] = "unrecognised"
        return report

    report["kind"] = "completion"
    response = payload.get("response")
    if response is not None:
        view = _message_view(response)
        report["response"] = {
            "content_len": len(view["content"]) if isinstance(view["content"], str) else 0,
            "tool_calls": view["tool_calls"],
            "reasoning_len": view["reasoning_len"],
            **_classify(view),
        }

    converted = payload.get("fncall_response")
    if converted is not None:
        view = _message_view(converted)
        report["fncall_response"] = {
            "content_len": len(view["content"]) if isinstance(view["content"], str) else 0,
            "tool_calls": view["tool_calls"],
        }

    for key in ("messages", "fncall_messages"):
        history = payload.get(key)
        if isinstance(history, list):
            report[key] = {
                "messages": len(history),
                "chars": len(json.dumps(history, ensure_ascii=False)),
            }
    return report


def scan_text(path: str) -> dict[str, object]:
    """Census-only report for a log or any non-completion artifact."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    return {
        "file": path,
        "kind": "text",
        "bytes": len(text.encode("utf-8", errors="replace")),
        "lines": text.count("\n") + 1,
        "marker_census": marker_census(text),
        "awaiting_user_input": len(re.findall(r"AWAITING_USER_INPUT", text)),
    }


def scan(path: str) -> dict[str, object]:
    if path.endswith(".json"):
        try:
            return scan_completion(path)
        except (json.JSONDecodeError, OSError):
            pass
    return scan_text(path)


# --- field access for `show` ----------------------------------------------

_FIELD_PATHS = {
    "response": ("response",),
    "content": ("response", "choices", 0, "message", "content"),
    "reasoning": ("response", "choices", 0, "message", "reasoning_content"),
    "fncall_response": ("fncall_response",),
    "messages": ("messages",),
    "fncall_messages": ("fncall_messages",),
}


def _dig(payload: object, path: tuple[object, ...]) -> object:
    node = payload
    for step in path:
        if isinstance(step, int):
            if not isinstance(node, list) or step >= len(node):
                return None
            node = node[step]
        else:
            if not isinstance(node, dict) or step not in node:
                return None
            node = node[step]
    return node


def extract_field(path: str, field: str) -> object:
    with open(path, encoding="utf-8", errors="replace") as handle:
        payload = json.load(handle)
    if field == "raw":
        return payload
    return _dig(payload, _FIELD_PATHS[field])


# --- selftest corpus -------------------------------------------------------
#
# Assembled from fragments so this file is not itself a poison source.

_LT = "<"
_GT = ">"
_BAR = "|"
_FBAR = "\uff5c"

SELFTEST_CASES: list[tuple[str, str]] = [
    (
        "plain function tag",
        _LT + "function=execute_bash" + _GT + "pytest -q" + _LT + "/function" + _GT,
    ),
    (
        "parameter by name",
        _LT + 'parameter name="command"' + _GT + "git diff" + _LT + "/parameter" + _GT,
    ),
    ("function_calls envelope", _LT + "function_calls" + _GT + _LT + "/function_calls" + _GT),
    ("invoke with name", _LT + 'invoke name="execute_bash"' + _GT + _LT + "/invoke" + _GT),
    ("calls envelope", _LT + "calls" + _GT + _LT + "/calls" + _GT),
    ("fullwidth banner", _FBAR + _FBAR + "DSML" + _FBAR + _FBAR),
    ("ascii banner", _BAR + _BAR + "DSML" + _BAR + _BAR),
    ("namespaced tag", _LT + "antml:invoke" + _GT),
    ("truncated tag", _LT + "function=execute_bash"),
    ("arg tags", _LT + "arg_key" + _GT + "x" + _LT + "/arg_value" + _GT),
]

# Corruption observed in this repository's own session log: a tool-call
# argument stream that resumed its own markup. It carries no live marker -
# the damage is that it is invalid JSON - so it is asserted separately.
SELFTEST_BROKEN_JSON = [
    '{"command"' + "`.: " + '"cd /tmp", "description": "x"}',
    '{"command"' + _GT + "`.\n\nThe: " + '"cd /tmp"}',
]

SELFTEST_HARMLESS = [
    "plain prose with no markup at all",
    "<div class='x'>html is not a marker</div>",
    "a < b and c > d",
    "```json\n{\"command\": \"ls\"}\n```",
]


def selftest() -> int:
    failures: list[str] = []

    for label, sample in SELFTEST_CASES:
        if not live_markers(sample):
            failures.append(f"{label}: corpus entry was already inert")
            continue
        cleaned = defang(sample)
        remaining = live_markers(cleaned)
        if remaining:
            failures.append(f"{label}: still live after defang -> {sorted(remaining)}")
        if "marker" in cleaned and cleaned == sample:
            failures.append(f"{label}: defang was a no-op")

    for sample in SELFTEST_HARMLESS:
        if defang(sample) != sample:
            failures.append(f"harmless text was rewritten: {sample!r}")

    for sample in SELFTEST_BROKEN_JSON:
        if live_markers(sample):
            failures.append(f"broken-json corpus entry carried a live marker: {sample!r}")
        if defang(sample) != sample:
            failures.append(f"broken-json corpus entry was rewritten: {sample!r}")
        if _json_health(sample)["json_parse_failures"] < 1:
            failures.append(f"json guard missed a malformed region: {sample!r}")

    for label, sample in SELFTEST_CASES:
        assert_inert_ok = False
        try:
            assert_inert(sample, label)
        except SystemExit:
            assert_inert_ok = True
        if not assert_inert_ok:
            failures.append(f"{label}: assert_inert accepted a live marker")

    # The tool must survive its own gate. This file is read by agents, so a
    # marker sample spelled out here would poison every session that opens it.
    try:
        with open(__file__, encoding="utf-8", errors="replace") as handle:
            own_source = handle.read()
    except OSError:
        own_source = ""
    if own_source:
        self_markers = live_markers(own_source)
        if self_markers:
            failures.append(
                f"this file is itself a poison source: {sorted(self_markers)}"
            )

    if failures:
        for line in failures:
            print(f"FAIL {line}")
        return 1
    print(
        f"PASS {len(SELFTEST_CASES)} marker corpus, "
        f"{len(SELFTEST_BROKEN_JSON)} broken-json corpus, "
        f"{len(SELFTEST_HARMLESS)} harmless controls"
    )
    return 0


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="marker_defang.py",
        description="Project raw agent artifacts into LLM-safe text.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="structural report; emits facts, never content")
    p_scan.add_argument("paths", nargs="+")

    p_show = sub.add_parser("show", help="defanged excerpt of one field")
    p_show.add_argument("path")
    p_show.add_argument("--field", choices=sorted(_FIELD_PATHS) + ["raw"], default="content")
    p_show.add_argument("--max-chars", type=int, default=1200)
    p_show.add_argument("--out", help="write to this file instead of stdout")

    p_search = sub.add_parser("search", help="grep with defanged output")
    p_search.add_argument("path")
    p_search.add_argument("--pattern", required=True)
    p_search.add_argument("--context", type=int, default=0)
    p_search.add_argument("--max-matches", type=int, default=20)
    p_search.add_argument("--max-chars", type=int, default=300)

    p_check = sub.add_parser("check", help="exit 1 if live markers remain")
    p_check.add_argument("path", nargs="?")

    sub.add_parser("selftest", help="assert the rules against a marker corpus")

    args = parser.parse_args(argv)

    if args.command == "selftest":
        return selftest()

    if args.command == "scan":
        for path in args.paths:
            _emit(json.dumps(scan(path), ensure_ascii=False, indent=2), path)
            _emit("\n", path)
        return 0

    if args.command == "show":
        value = extract_field(args.path, args.field)
        if value is None:
            print(f"field {args.field!r} not present in {args.path}", file=sys.stderr)
            return 2
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        cleaned = defang(text)
        truncated = len(cleaned) > args.max_chars
        if truncated:
            cleaned = cleaned[: args.max_chars] + f"\n... [{len(cleaned) - args.max_chars} chars elided]"
        if args.out:
            # Writing to a file is the escape hatch for cases where even the
            # inert form is too large to read.
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(cleaned)
            print(f"wrote {len(cleaned)} inert chars to {args.out}")
        else:
            _emit(cleaned, args.path)
        return 0

    if args.command == "search":
        pattern = re.compile(args.pattern)
        with open(args.path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        shown = 0
        total = 0
        for index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            total += 1
            if shown >= args.max_matches:
                continue
            shown += 1
            low = max(0, index - args.context)
            high = min(len(lines), index + args.context + 1)
            for offset in range(low, high):
                cleaned = defang(lines[offset])[: args.max_chars]
                _emit(f"{offset + 1}: {cleaned}\n", args.path)
            if args.context:
                _emit("--\n", args.path)
        note = f"; {total - shown} more matched" if total > shown else ""
        print(f"[{shown} match(es) shown for pattern {args.pattern!r}{note}]")
        return 0

    if args.command == "check":
        if args.path:
            with open(args.path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            origin = args.path
        else:
            text = sys.stdin.read()
            origin = "<stdin>"
        remaining = live_markers(text)
        if remaining:
            print(f"UNSAFE {origin}: {sorted(remaining)}")
            return 1
        print(f"SAFE {origin}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
