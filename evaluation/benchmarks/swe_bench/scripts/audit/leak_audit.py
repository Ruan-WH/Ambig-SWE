#!/usr/bin/env python3
"""Audit how much of a run's success could have come from gold-patch leakage.

The question is not whether a patch happens to look like the gold patch, but
whether the agent had the answer handed to it. The primary metric is therefore
scoped to *history* receipts: gold lines that came back from a command able to
reveal pre-existing commits.

Excluded on purpose:
  * bare `git diff` / `git diff --stat` / `git diff --cached`, which show the
    agent's own uncommitted work and would otherwise count the agent's correct
    fix as leakage;
  * duplicate tool calls, which appear in every later completion snapshot.
Receipts and commands are de-duplicated by (command, output) before counting, so
one `git apply` stays one.

Patch comparison uses the whole unified diff - file paths plus added *and*
removed lines - not just added lines.

Per instance:

  gold_added_lines / gold_removed_lines / gold_files
  history_calls                   distinct git-history commands
  gold_in_receipts                gold change lines seen in a history receipt
  saw_gold_in_receipt             gold_in_receipts > 0
  saw_all_gold_in_receipt         every gold change line was in a history receipt
  gold_anywhere                   gold lines anywhere in the completions
                                  (contaminated by the agent's own patch, so it
                                  is reported for contrast only and must not be
                                  used to argue about leakage)
  fix_commit                      commit whose own diff segment holds gold lines
  git_apply_calls                 distinct `git apply` commands
  model_added_lines / model_removed_lines / model_files
  model_added_in_gold / model_removed_in_gold
  files_identical                 the two patches touch the same files
  same_files_and_changes          same files and the same added/removed lines.
                                  Context lines, hunk positions and file order
                                  are ignored, so this is NOT a byte-identical
                                  comparison of the full patches.
  nonempty_patch / resolved

Usage:
  leak_audit.py --eval-output-dir DIR --dataset-json FILE [--grader-report FILE]
                [--out FILE] [--recover-fix-commits FILE]

Reads recorded artifacts only; never touches a runtime.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import json
import os
import re
import sys

MIN_RECEIPT_LINE_LEN = 15

COMMIT_HEADER_RE = re.compile(r'commit ([0-9a-f]{7,40})')
HISTORY_CMD_RE = re.compile(
    r'\bgit\s+(?:show|log|cat-file|rev-list|blame|reflog|whatchanged)\b'
)
DIFF_CMD_RE = re.compile(r'\bgit\s+diff\b')
# A revision-ish token: HEAD, a sha, or anything carrying ~ ^ or @{...}.
REV_TOKEN_RE = re.compile(
    r'^(?:HEAD|FETCH_HEAD|ORIG_HEAD|MERGE_HEAD|[0-9a-f]{7,40}'
    r'|[^\s]*[~^][^\s]*|[^\s]*@\{[^\s]*)$'
)
GIT_APPLY_RE = re.compile(r'\bgit\s+apply\b')
GIT_CHERRY_RE = re.compile(r'\bgit\s+cherry-pick\b')


def _diff_reveals_history(command: str) -> bool:
    """True only for `git diff` between two revisions.

    `git diff`, `git diff --cached` and `git diff HEAD` all put the agent's own
    worktree or index on the '+' side, so they must not count as leakage.
    """
    for segment in re.split(r'&&|\|\||;|\|', command):
        if not DIFF_CMD_RE.search(segment):
            continue
        body = segment.split(' -- ')[0]
        tokens = body.split()
        index = next(
            (
                i
                for i, token in enumerate(tokens)
                if token == 'diff' and i and tokens[i - 1].endswith('git')
            ),
            None,
        )
        if index is None:
            continue
        revisions = 0
        for token in tokens[index + 1 :]:
            if token.startswith('-'):
                continue
            if '..' in token:
                return True
            if REV_TOKEN_RE.match(token):
                revisions += 1
        if revisions >= 2:
            return True
    return False


def is_history_command(command: str) -> bool:
    """True when the command can reveal commits that predate the agent."""
    if HISTORY_CMD_RE.search(command):
        return True
    if DIFF_CMD_RE.search(command):
        return _diff_reveals_history(command)
    return False


def strip_diff_prefix(path: str) -> str:
    path = path.strip()
    if path in ('/dev/null', ''):
        return ''
    if path.startswith(('a/', 'b/')):
        return path[2:]
    return path


def parse_patch(patch: str | None) -> dict[str, dict[str, collections.Counter]]:
    """Unified diff -> {path: {'added': Counter, 'removed': Counter}}.

    A small state machine keeps hunk bodies apart from file headers, so a
    removed line that happens to read `--- x` (or an added line reading
    `+++ x`) is counted as content instead of being mistaken for a header.
    """
    files: dict[str, dict[str, collections.Counter]] = {}
    state: str | None = None
    pending_old = ''
    current: str | None = None
    for raw in patch.splitlines() if patch else []:
        if raw.startswith('diff --git '):
            state = 'after_diff'
            pending_old = ''
            current = None
            continue
        if raw.startswith('--- ') and state in (None, 'after_diff'):
            pending_old = strip_diff_prefix(raw[4:])
            state = 'after_old'
            continue
        if raw.startswith('+++ ') and state == 'after_old':
            new_path = strip_diff_prefix(raw[4:])
            current = new_path or pending_old
            pending_old = ''
            if current:
                files.setdefault(
                    current,
                    {
                        'added': collections.Counter(),
                        'removed': collections.Counter(),
                    },
                )
            state = 'in_hunk'
            continue
        if state == 'in_hunk' and current:
            if raw.startswith('@@') or raw.startswith('\\'):
                continue
            if raw.startswith('+'):
                files[current]['added'][raw[1:]] += 1
            elif raw.startswith('-'):
                files[current]['removed'][raw[1:]] += 1
            continue
        # other header lines (index, mode, rename, similarity, ...) are ignored
    return files


def signature_from_parsed(parsed: dict[str, dict[str, collections.Counter]]) -> dict:
    return {
        path: (frozenset(counts['added'].items()), frozenset(counts['removed'].items()))
        for path, counts in parsed.items()
    }


def patch_signature(patch: str | None) -> dict[str, tuple]:
    return signature_from_parsed(parse_patch(patch))


def change_lines_from_parsed(
    parsed: dict[str, dict[str, collections.Counter]],
    min_len: int = MIN_RECEIPT_LINE_LEN,
) -> list[str]:
    """Substantive added + removed lines, used to spot gold text in receipts."""
    lines = []
    for counts in parsed.values():
        for kind in ('added', 'removed'):
            for line, count in counts[kind].items():
                text = line.strip()
                if len(text) >= min_len:
                    lines.extend([text] * count)
    return lines


def change_lines(patch: str | None, min_len: int = MIN_RECEIPT_LINE_LEN) -> list[str]:
    return change_lines_from_parsed(parse_patch(patch), min_len)


def _completion_files(eval_output_dir: str, instance_id: str) -> list[str]:
    comp_dir = os.path.join(eval_output_dir, 'llm_completions', instance_id)
    if not os.path.isdir(comp_dir):
        return []
    return [
        os.path.join(comp_dir, name)
        for name in sorted(os.listdir(comp_dir))
        if os.path.isfile(os.path.join(comp_dir, name))
    ]


def read_raw_completions(eval_output_dir: str, instance_id: str) -> str:
    chunks = []
    for path in _completion_files(eval_output_dir, instance_id):
        try:
            with open(path, encoding='utf-8', errors='ignore') as f:
                chunks.append(f.read())
        except OSError:
            continue
    return ''.join(chunks)


def collect_activity(
    eval_output_dir: str, instance_id: str
) -> tuple[list[str], dict[str, None], int]:
    """Return (history receipt outputs, distinct commands, distinct call count).

    Every completion file is a full conversation snapshot, so the same call
    appears many times; de-duplicating by (command, output) is what keeps counts
    like `git apply` meaningful.
    """
    history_receipts: dict[tuple[str, str], None] = {}
    distinct_calls: dict[tuple[str, str], None] = {}
    for path in _completion_files(eval_output_dir, instance_id):
        try:
            with open(path, encoding='utf-8', errors='ignore') as f:
                obj = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        messages = obj.get('fncall_messages') or []
        id_to_cmd: dict[str, str] = {}
        for message in messages:
            role = message.get('role')
            if role == 'assistant':
                for call in message.get('tool_calls') or []:
                    function = call.get('function') or {}
                    try:
                        arguments = json.loads(function.get('arguments') or '{}')
                    except json.JSONDecodeError:
                        arguments = {}
                    command = arguments.get('command') or ''
                    if isinstance(command, str):
                        id_to_cmd[call.get('id')] = command
            elif role == 'tool':
                command = id_to_cmd.get(message.get('tool_call_id'))
                if not command:
                    continue
                output = message.get('content') or ''
                distinct_calls[(command, output)] = None
                if is_history_command(command):
                    history_receipts[(command, output)] = None

    outputs = [output for _, output in history_receipts]
    commands = {command: None for command, _ in distinct_calls}
    return outputs, commands, len(distinct_calls)


def recover_fix_commit(
    receipts: list[str], gold_lines: list[str]
) -> tuple[str | None, int]:
    """Commit whose own diff segment holds the most gold lines.

    Attribution happens inside a single receipt, so the result does not depend
    on receipt order. Ties break on the sha.
    """
    if not gold_lines:
        return None, 0
    votes: collections.Counter[str] = collections.Counter()
    for text in receipts:
        if not text:
            continue
        headers = [(m.start(), m.group(1)) for m in COMMIT_HEADER_RE.finditer(text)]
        if not headers:
            continue
        positions = [position for position, _ in headers]
        for line in gold_lines:
            start = text.find(line)
            while start != -1:
                index = bisect.bisect_right(positions, start) - 1
                if index >= 0:
                    seg_end = (
                        positions[index + 1]
                        if index + 1 < len(positions)
                        else len(text)
                    )
                    if start < seg_end:
                        votes[headers[index][1]] += 1
                start = text.find(line, start + 1)

    if not votes:
        return None, 0
    sha, hits = sorted(votes.items(), key=lambda item: (-item[1], item[0]))[0]
    return sha, hits


def load_model_patches(eval_output_dir: str) -> dict[str, str]:
    patches: dict[str, str] = {}
    swebench = os.path.join(eval_output_dir, 'output.swebench.jsonl')
    if os.path.exists(swebench):
        with open(swebench, encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                patches[row.get('instance_id', '')] = row.get('model_patch') or ''
    output = os.path.join(eval_output_dir, 'output.jsonl')
    if os.path.exists(output):
        with open(output, encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                instance_id = row.get('instance_id', '')
                if patches.get(instance_id):
                    continue
                test_result = row.get('test_result') or {}
                patches[instance_id] = test_result.get('git_patch') or ''
    return patches


def load_resolved(grader_report: str | None) -> set[str]:
    if not grader_report or not os.path.exists(grader_report):
        return set()
    try:
        with open(grader_report, encoding='utf-8') as f:
            return set(json.load(f).get('resolved_ids', []))
    except (OSError, json.JSONDecodeError):
        return set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--eval-output-dir', required=True)
    parser.add_argument('--dataset-json', required=True)
    parser.add_argument('--grader-report', default=None)
    parser.add_argument('--out', default=None)
    parser.add_argument(
        '--recover-fix-commits',
        default=None,
        help='write {instance_id: fix_commit} JSON for known_fix_commits.json',
    )
    args = parser.parse_args()

    with open(args.dataset_json, encoding='utf-8') as f:
        dataset = {row['instance_id']: row for row in json.load(f)}
    patches = load_model_patches(args.eval_output_dir)
    resolved = load_resolved(args.grader_report)

    rows = []
    for instance_id, instance in dataset.items():
        gold_patch = instance.get('patch')
        gold_parsed = parse_patch(gold_patch)
        gold_sig = signature_from_parsed(gold_parsed)
        gold_lines = change_lines_from_parsed(gold_parsed)
        gold_added = sum(len(c['added']) for c in gold_parsed.values())
        gold_removed = sum(len(c['removed']) for c in gold_parsed.values())

        raw_text = read_raw_completions(args.eval_output_dir, instance_id)
        receipts, commands, distinct_calls = collect_activity(
            args.eval_output_dir, instance_id
        )
        receipt_text = '\n'.join(receipts)

        in_receipts = sum(1 for line in gold_lines if line in receipt_text)
        anywhere = sum(1 for line in gold_lines if line in raw_text)
        history_calls = sum(1 for c in commands if is_history_command(c))
        fix_commit, fix_hits = recover_fix_commit(receipts, gold_lines)

        model_patch = patches.get(instance_id)
        model_parsed = parse_patch(model_patch)
        model_sig = signature_from_parsed(model_parsed)
        model_added = sum(len(c['added']) for c in model_parsed.values())
        model_removed = sum(len(c['removed']) for c in model_parsed.values())
        gold_added_set = {line for c in gold_parsed.values() for line in c['added']}
        gold_removed_set = {
            line for c in gold_parsed.values() for line in c['removed']
        }

        rows.append(
            {
                'instance_id': instance_id,
                'gold_added_lines': gold_added,
                'gold_removed_lines': gold_removed,
                'gold_files': sorted(gold_sig),
                'history_calls': history_calls,
                'distinct_tool_calls': distinct_calls,
                'gold_in_receipts': in_receipts,
                'gold_change_lines': len(gold_lines),
                'saw_gold_in_receipt': in_receipts > 0,
                'saw_all_gold_in_receipt': bool(gold_lines)
                and in_receipts == len(gold_lines),
                'gold_anywhere': anywhere,
                'fix_commit': fix_commit,
                'fix_commit_gold_lines': fix_hits,
                'git_apply_calls': sum(
                    1 for c in commands if GIT_APPLY_RE.search(c)
                ),
                'git_cherry_pick_calls': sum(
                    1 for c in commands if GIT_CHERRY_RE.search(c)
                ),
                'model_added_lines': model_added,
                'model_removed_lines': model_removed,
                'model_files': sorted(model_sig),
                'model_added_in_gold': len(
                    {
                        line
                        for counts in model_parsed.values()
                        for line in counts['added']
                    }
                    & gold_added_set
                ),
                'model_removed_in_gold': len(
                    {
                        line
                        for counts in model_parsed.values()
                        for line in counts['removed']
                    }
                    & gold_removed_set
                ),
                'files_identical': bool(gold_sig) and set(gold_sig) == set(model_sig),
                'same_files_and_changes': bool(gold_sig) and gold_sig == model_sig,
                'nonempty_patch': bool((model_patch or '').strip()),
                'resolved': instance_id in resolved if resolved else None,
            }
        )

    header = (
        f'{"instance_id":28s} {"g+":>3s} {"g-":>3s} {"recv":>4s} {"all":>5s} '
        f'{"apply":>5s} {"m+":>4s} {"m-":>3s} {"inGold":>6s} {"files":>5s} '
        f'{"same":>5s} {"resolved":>8s}  fix_commit'
    )
    print(header)
    print('-' * len(header))
    for row in rows:
        print(
            f'{row["instance_id"]:28s} {row["gold_added_lines"]:3d} '
            f'{row["gold_removed_lines"]:3d} {row["gold_in_receipts"]:4d} '
            f'{str(row["saw_all_gold_in_receipt"]):>5s} '
            f'{row["git_apply_calls"]:5d} {row["model_added_lines"]:4d} '
            f'{row["model_removed_lines"]:3d} {row["model_added_in_gold"]:6d} '
            f'{str(row["files_identical"]):>5s} '
            f'{str(row["same_files_and_changes"]):>5s} '
            f'{str(row["resolved"]):>8s}  {row["fix_commit"] or "-"}'
        )

    resolved_rows = [r for r in rows if r['resolved']]
    saw_receipt = [r for r in rows if r['saw_gold_in_receipt']]
    saw_all = [r for r in rows if r['saw_all_gold_in_receipt']]
    same_change_set = [r for r in rows if r['same_files_and_changes']]
    resolved_clean = [r for r in resolved_rows if not r['saw_gold_in_receipt']]

    print()
    print(f'instances                            : {len(rows)}')
    print(f'saw gold lines in a history receipt  : {len(saw_receipt)}')
    print(f'saw every gold line in a history receipt: {len(saw_all)}')
    print(f'same files and changed lines as gold : {len(same_change_set)}')
    if resolved:
        print(f'resolved (grader)                    : {len(resolved_rows)}')
        print(
            '  resolved and saw every gold line   : '
            f'{len([r for r in resolved_rows if r["saw_all_gold_in_receipt"]])}'
        )
        print(
            '  resolved, no gold seen in receipts : '
            f'{len(resolved_clean)}'
            '  (no leakage observed within audit scope; not proof of none)'
        )
    else:
        print('resolved                             : n/a (no --grader-report)')

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        print(f'\nWrote {args.out}')

    if args.recover_fix_commits:
        commits = {r['instance_id']: r['fix_commit'] for r in rows if r['fix_commit']}
        with open(args.recover_fix_commits, 'w', encoding='utf-8') as f:
            json.dump(commits, f, indent=2, ensure_ascii=False)
        print(f'Wrote {len(commits)} fix commits to {args.recover_fix_commits}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
