"""Test entry point: scrub credentials before imports; prohibit outbound sockets."""
import os
import socket
import sys
from pathlib import Path

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
for key in list(os.environ):
    if key.startswith(('NOTION_', 'KIS_', 'KAKAO_', 'GMAIL_', 'KRX_')):
        os.environ.pop(key)

def blocked(*args, **kwargs):
    raise AssertionError('Offline tests: network prohibited')

socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.socket.sendto = blocked
socket.create_connection = blocked
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'tests')]
import pytest
raise SystemExit(pytest.main(sys.argv[1:] or ['-q', 'tests']))
