"""Install or update the skill as a personal Claude Code skill.

Copies skills/ajimu-autopilot to ~/.claude/skills/ajimu-autopilot-lite. Idempotent: unchanged
files stay untouched, and files that exist only in the target are kept and reported.
It changes no project and no Claude Code setting.

Usage: python tools/install.py [--target FOLDER] [--check]
Exit status: 0 = installed or up to date, 1 = `--check` found differences or copying failed.
"""
import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SKILL = 'ajimu-autopilot-lite'
SOURCE = Path(__file__).resolve().parents[1] / 'skills' / SKILL
SKIP = {'__pycache__', '.DS_Store'}


def source_files():
    for path in sorted(SOURCE.rglob('*')):
        if path.is_file() and not SKIP.intersection(path.parts) and path.suffix != '.pyc':
            yield path.relative_to(SOURCE)


def plan(target):
    """Return (to_create, to_update, unchanged, only_in_target), as relative paths."""
    create, update, same = [], [], []
    wanted = set()
    for relative in source_files():
        wanted.add(relative)
        destination = target / relative
        if not destination.exists():
            create.append(relative)
        elif filecmp.cmp(SOURCE / relative, destination, shallow=False):
            same.append(relative)
        else:
            update.append(relative)
    extra = []
    if target.is_dir():
        extra = [path.relative_to(target) for path in sorted(target.rglob('*'))
                 if path.is_file() and path.relative_to(target) not in wanted
                 and not SKIP.intersection(path.parts) and path.suffix != '.pyc']
    return create, update, same, extra


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--target', type=Path, default=Path.home() / '.claude' / 'skills' / SKILL)
    parser.add_argument('--check', action='store_true', help='report differences without copying')
    args = parser.parse_args(argv)
    create, update, same, extra = plan(args.target)
    print(f'target: {args.target}')
    print(f'new: {len(create)}, changed: {len(update)}, unchanged: {len(same)}, kept (not in this release): {len(extra)}')
    for relative in extra:
        print(f'kept: {relative.as_posix()}')
    if args.check:
        for relative in create + update:
            print(f'differs: {relative.as_posix()}')
        return 1 if create or update else 0
    try:
        for relative in create + update:
            destination = args.target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / relative, destination)
    except OSError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    print('installed' if create or update else 'already up to date')
    return 0


if __name__ == '__main__':
    sys.exit(main())
