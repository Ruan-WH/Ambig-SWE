"""Tests for the marker-defanging safety tool.

Marker samples here are assembled from fragments on purpose. A test corpus
that spells the live markup out would itself be a poison source for any agent
session that opens this file, which is the very failure the tool prevents.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TOOL_PATH = HERE / 'scripts' / 'audit' / 'marker_defang.py'

LT = chr(60)
GT = chr(62)
FBAR = chr(0xFF5C)

LIVE_FUNCTION_TAG = (
    LT + 'function=execute_bash' + GT + 'pytest -q' + LT + '/function' + GT
)
LIVE_BANNER = FBAR + FBAR + 'DSML' + FBAR + FBAR
HARMLESS_HTML = "<div class='x'>html is not a marker</div>"


def _load_tool():
    spec = importlib.util.spec_from_file_location('marker_defang', TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['marker_defang'] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def md():
    return _load_tool()


def test_selftest_passes(md):
    assert md.selftest() == 0


def test_tool_source_is_not_a_poison_source(md):
    """Prove that opening this tool in an agent session injects no live markup."""
    own_source = TOOL_PATH.read_text(encoding='utf-8')
    assert md.live_markers(own_source) == {}


def test_defang_leaves_nothing_live_but_stays_readable(md):
    cleaned = md.defang(LIVE_FUNCTION_TAG + LIVE_BANNER)
    assert md.live_markers(cleaned) == {}
    assert 'execute_bash' in cleaned
    assert 'DSML' in cleaned


def test_defang_is_idempotent(md):
    once = md.defang(LIVE_FUNCTION_TAG + LIVE_BANNER)
    assert md.defang(once) == once


def test_harmless_markup_is_left_alone(md):
    assert md.defang(HARMLESS_HTML) == HARMLESS_HTML


def test_check_gate_exit_codes(md, tmp_path, capsys):
    dirty = tmp_path / 'dirty.txt'
    dirty.write_text(LIVE_FUNCTION_TAG, encoding='utf-8')
    assert md.main(['check', str(dirty)]) == 1
    assert 'UNSAFE' in capsys.readouterr().out

    clean = tmp_path / 'clean.txt'
    clean.write_text(md.defang(LIVE_FUNCTION_TAG), encoding='utf-8')
    assert md.main(['check', str(clean)]) == 0
    assert 'SAFE' in capsys.readouterr().out


def test_scan_classifies_mixed_markup_without_leaking_content(md, tmp_path):
    payload = {
        'response': {
            'choices': [
                {
                    'message': {
                        'content': LIVE_FUNCTION_TAG + LIVE_BANNER,
                        'tool_calls': [],
                    }
                }
            ]
        }
    }
    path = tmp_path / 'completion.json'
    path.write_text(json.dumps(payload), encoding='utf-8')

    report = md.scan(str(path))
    body = report['response']
    assert body['verdict'] == 'mixed_markup'
    assert body['mixed'] is True
    assert set(body['families']) == {'dsml_banner', 'function'}
    # The report carries facts only: nothing raw escapes with it.
    assert md.live_markers(json.dumps(report)) == {}


def test_search_reports_an_honest_count(md, tmp_path, capsys):
    path = tmp_path / 'three.txt'
    path.write_text('alpha\nbeta\ngamma\n', encoding='utf-8')
    assert md.main(['search', str(path), '--pattern', 'a', '--max-matches', '2']) == 0
    out = capsys.readouterr().out
    assert '2 match(es) shown' in out
    assert '1 more matched' in out
