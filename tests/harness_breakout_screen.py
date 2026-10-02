"""[오늘의 돌파] 화면만 그리는 AppTest 하네스 (테스트 전용).

app.py 전체(비밀번호·탭·사이드바)를 태우지 않고 render_breakout 하나만
그린다. 읽을 데이터 폴더는 BREAKOUT_TEST_DATA_DIR 환경변수로 받는다 -
테스트가 만든 임시 폴더를 가리키게 해서 저장소의 실제 data/를 건드리지
않는다.
"""

import os
import pathlib

import app
import screener

screener.DATA_DIR = pathlib.Path(os.environ["BREAKOUT_TEST_DATA_DIR"])

app.render_breakout(capital=10_000_000, ai_unlocked=False)
