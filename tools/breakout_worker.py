"""Separate post-trade observation worker; never places orders or sends messages."""
import argparse
import logging
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import breakout_outbox as journal
import breakout_tracker as tracker


def checkpoint_create():
    """Push the creating marker; any failure prevents the external POST.

    No rebase/force: concurrent remote changes must fail closed. The existing
    workflow owns serialization and configures the git identity beforehand.
    """
    relative = journal.path().resolve().relative_to(Path.cwd().resolve())
    if relative.as_posix() != 'data/breakout_outbox.json':
        raise RuntimeError('Unexpected production journal path')
    branch = os.environ.get('GITHUB_REF_NAME', '')
    if not branch:
        raise RuntimeError('Missing checkpoint branch')
    subprocess.run(['git', 'add', '--', str(relative)], check=True, timeout=15)
    subprocess.run(['git', 'commit', '--only', '-m',
                    'chore(tracking): pre-POST intent [skip ci]', '--', str(relative)],
                   check=True, timeout=15)
    subprocess.run(['git', 'push', 'origin', f'HEAD:refs/heads/{branch}'],
                   check=True, timeout=30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'deliver'])
    args = parser.parse_args()
    if not tracker.is_enabled():
        return 0
    owner = os.environ.get('GITHUB_RUN_ID', '') + ':' + os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if owner == ':' or os.environ.get('BREAKOUT_TRACK_WRITER') != 'auto-trade':
        raise RuntimeError('Only the serialized, checkpointed workflow may deliver')
    backend = tracker.get_store()
    if args.phase == 'prepare':
        if journal.load()['events']:
            import notion_repo
            holdings = {inp.ticker for _, inp in notion_repo.fetch_holdings()}
            journal.prepare(backend, holdings, owner)
    else:
        if journal.path().exists():
            committed = subprocess.check_output(['git', 'show', 'HEAD:data/breakout_outbox.json'])
            if committed != journal.path().read_bytes():
                raise RuntimeError('Journal is not checkpointed; refusing external writes')
            journal.deliver(backend, owner, checkpoint=checkpoint_create)
        # Runs on quiet rounds too. Isolated from all trading decisions.
        import breakout_reconcile
        breakout_reconcile.run(backend)
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
