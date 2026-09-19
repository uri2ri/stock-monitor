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
            journal.deliver(backend, owner)
        # Runs on quiet rounds too. Isolated from all trading decisions.
        import breakout_reconcile
        breakout_reconcile.run(backend)
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
