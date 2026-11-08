# kaboost:begin tdd-commits:script v1 sha=e181071c9fe8 pre=0 created=1
"""Audit TDD red/green commit pairs.

A red commit contains only the failing tests; its message has the trailer `TDD: red` and a line
`Expected failing: <test ids>`. A later green commit with the same task ID (`KB-123: ...` at the start of the
subject or `... (KB-123)` at its end) has the trailer
`TDD: green` and contains the solution. The audit checks the structure and, with --run, executes the project's
test command in detached worktrees: the named tests must fail on the red commit and pass on the green one.

Usage: python scripts/kaboost_audit_tdd.py [REV_RANGE] [--run 'python -m unittest {tests}'] [--test-path-pattern REGEX]
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The task ID opens the subject (`KB-7: ...`) or closes it in parentheses (`fix: ... (KB-7)`).
TASK = re.compile(r'^([A-Za-z]+-\d+)\b|\(([A-Za-z]+-\d+)\)\s*$')
# Test ids come from commit messages and are substituted into a shell command, so only plain ids pass.
SAFE_TESTS = re.compile(r'^(?!-)[\w./:\[\]-]+( (?!-)[\w./:\[\]-]+)*$')


def git(*args, cwd=None):
    done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f'git {" ".join(args)} failed: {done.stderr.strip()}')
    return done.stdout


def commits(rev_range, cwd):
    """Commits oldest first as dicts with sha, subject, body and changed files."""
    shas = git('rev-list', '--reverse', '--no-merges', rev_range, cwd=cwd).split()
    result = []
    for sha in shas:
        message = git('show', '-s', '--format=%B', sha, cwd=cwd)
        files = git('show', '--format=', '--name-only', sha, cwd=cwd).split()
        subject = message.splitlines()[0] if message.strip() else ''
        trailer = re.search(r'^TDD:\s*(red|green)\s*$', message, re.M | re.I)
        expected = re.search(r'^Expected failing:\s*(.+)$', message, re.M)
        task = TASK.search(subject)
        result.append({'sha': sha, 'subject': subject, 'files': files, 'kind': trailer.group(1).lower() if trailer else None,
                       'expected': expected.group(1).strip() if expected else '', 'task': (task.group(1) or task.group(2)) if task else None})
    return result


def pair_up(items):
    """Return (pairs, problems); a red is paired with the next green of the same task."""
    pairs, problems, open_red = [], [], {}
    for item in items:
        if item['kind'] == 'red':
            if not item['task']:
                problems.append(f"{item['sha'][:8]}: red commit subject must name a task ID ('KB-7: ...' or '... (KB-7)')")
            if not item['expected']:
                problems.append(f"{item['sha'][:8]}: red commit has no 'Expected failing:' line")
            if item['task'] in open_red:
                problems.append(f"{open_red[item['task']]['sha'][:8]}: red commit has no green commit before the next red of {item['task']}")
            open_red[item['task']] = item
        elif item['kind'] == 'green':
            red = open_red.pop(item['task'], None)
            if red is None:
                problems.append(f"{item['sha'][:8]}: green commit of {item['task']} has no earlier red commit")
            else:
                pairs.append((red, item))
    for task, red in open_red.items():
        problems.append(f"{red['sha'][:8]}: red commit of {task} has no green commit")
    return pairs, problems


def check_structure(pairs, test_pattern):
    problems = []
    is_test = re.compile(test_pattern).search
    for red, green in pairs:
        outside = [f for f in red['files'] if not is_test(f)]
        if outside:
            problems.append(f"{red['sha'][:8]}: red commit changes non-test files: {', '.join(outside)}")
        if not any(not is_test(f) for f in green['files']):
            problems.append(f"{green['sha'][:8]}: green commit changes no non-test file")
    return problems


def run_at(sha, command, tests, repo):
    with tempfile.TemporaryDirectory(prefix='audit-tdd-') as temp:
        tree = Path(temp) / 'tree'
        git('worktree', 'add', '--detach', str(tree), sha, cwd=repo)
        try:
            return subprocess.run(command.replace('{tests}', tests), shell=True, cwd=tree, capture_output=True,
                                  text=True).returncode
        finally:
            git('worktree', 'remove', '--force', str(tree), cwd=repo)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('range', nargs='?', default='HEAD', help='revision range, for example origin/master..HEAD')
    parser.add_argument('--run', help="test command; '{tests}' is replaced by the red commit's Expected failing line")
    parser.add_argument('--test-path-pattern', default=r'(^|/)(tests?|spec)(/|_)|(_test|\.test|\.spec)\.|(^|/)test_',
                        help='regex that marks a path as a test file')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    pairs, problems = pair_up(commits(args.range, repo))
    problems += check_structure(pairs, args.test_path_pattern)
    for red, green in pairs:
        print(f"{red['task']}: red {red['sha'][:8]} -> green {green['sha'][:8]} ({red['expected']})")
        if args.run and not SAFE_TESTS.match(red['expected']):
            problems.append(f"{red['sha'][:8]}: unsafe test id in 'Expected failing:' (allowed: letters, digits, "
                            "_ . / : [ ] -, separated by single spaces); not run")
        elif args.run:
            red_code = run_at(red['sha'], args.run, red['expected'], repo)
            green_code = run_at(green['sha'], args.run, red['expected'], repo)
            if red_code == 0:
                problems.append(f"{red['sha'][:8]}: the expected tests pass on the red commit")
            if green_code != 0:
                problems.append(f"{green['sha'][:8]}: the expected tests fail on the green commit")
    if not pairs and not problems:
        print('No TDD red/green commits in range')
    for problem in problems:
        print(f'PROBLEM {problem}', file=sys.stderr)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
# kaboost:end tdd-commits:script
