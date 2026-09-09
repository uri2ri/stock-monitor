# PR #1 7년 재검증 준비 (실행은 로컬에서)

이 문서는 PR #1(`fix/backtest-execution-accounting`, 포트폴리오 시뮬레이션
회계·체결 버그 수정)의 실제 7년 재검증(dev+holdout)을 **로컬 접속 후** 진행하기
위한 준비 자료다. 이 문서를 만든 세션에서는 KRX 네트워크·실제 7년
OHLCV/업종 데이터에 접근할 수 없어 실행은 하지 않았고, 아래 내용만 정리했다.

- 실행 설정의 근거를 "확인된 과거 설정 / 현재 코드 기본값 / 확인 불가"로 구분 (§1)
- 업종 데이터 의존성과 한계 (§2)
- 로컬에서 쓸 정확한 실행 명령과 비교 절차 (§3)
- 필수·선택 파일 체크리스트 (§4)
- 사전 점검 스크립트: [`check_revalidation_data.py`](../check_revalidation_data.py)
  (읽기 전용, 네트워크 없음 - `python check_revalidation_data.py --period full`)

## 0. 커밋 기준

| 구분 | 커밋 | 비고 |
|---|---|---|
| 수정 전 | `76f70c2` (`Merge branch 'claude/holdings-search-error-kfynqa'`) | PR #1의 3개 수정 커밋(`3cf1e56`)의 직전 부모. `fix/backtest-execution-accounting` 브랜치가 여기서 갈라졌다. |
| 수정 후 | `488382d` (현재 PR #1 head) | 3차 커밋까지 반영된 최종 검증 커밋. |

PR 본문에 있던 `737f0c4...`는 **이 두 커밋과 무관**하다 - 그 프리픽스는 이후
저장소에 자동 알림(chore) 커밋으로 우연히 생긴 것이고(`origin/main`에서
`737f0c4 chore(auto-trade): 돌파 알림 2026-09-09 09:03 [skip ci]`로 실존 확인),
`backtest.py`와는 아무 관계가 없다. 비교에는 위 두 SHA만 쓴다.

`76f70c2`와 `488382d` 사이에는 `backtest.py`·`tests/`·`requirements-dev.txt`만
바뀌었고(`git diff --stat 76f70c2 488382d`), `data/` 아래 어떤 파일도 바뀌지
않았다 - 두 커밋을 번갈아 `git checkout`해도 로컬 데이터 파일(추적 대상이든
`.gitignore` 대상이든)이 서로 다르게 남을 걱정은 없다.

## 1. 실행 설정의 근거

앞서 (다른 세션에서) 보고된 실행 설정은 **코드의 기본값**이었고, PR #1이
설명하는 과거 7년 백테스트에서 실제로 그 설정을 썼다고 확인된 적은 없었다.
저장소 커밋 이력을 뒤져 실제 실행 근거를 찾은 결과는 다음과 같다.

### 1.1 확인된 과거 설정 (커밋 근거 있음)

`origin/main`의 커밋 `dc7d1d0`(`fix(stats): ticker_stats.json을 dev 정식
기준선으로 재생성`, 2026-09-09) 커밋 메시지가 실행 명령을 그대로 남겼다:

```
backtest.py --portfolio --period dev --capital-mode fixed
```

→ `data/backtest_portfolio_dev_v2.csv` (479건, dev 구간). 이 커밋은 "dev 구간
공식 기준선"이라고 스스로 명명했고, 같은 커밋에서 app.py 문구도 "7년 구간"에서
"dev 구간(2019-09~2023-08)"으로 정정했다(다만 `app.py`의
`render_backtest_history` 함수 docstring에는 아직 "7년 백테스트"라는 옛 표현이
남아 있다 - 이 PR 범위 밖이라 건드리지 않았다).

같은 커밋 메시지는 "holdout_runs.log가 없어 홀드아웃 실행은 아니었음을
확인했다"고 명시한다 - 즉 **holdout 구간은 이 저장소 이력상 한 번도 실행된
적이 없다.**

이 명령에는 `--risk`·`--stop-mult`·`--unit-caps`·`--breakout-period`·
`--vol-mult`·`--breakeven-ratchet`·`--regime-gate`·`--market`·`--kospi200` 중
아무 플래그도 없다 - 즉 **dev 구간의 "확인된 과거 설정"은 그 자체로 코드
기본값과 동일**하다(아래 1.2). `core.py`는 PR #1에서 건드리지 않았으므로
당시 기본값과 현재 기본값이 같다.

별도로 `origin/main` 커밋 `410062f`(`feat(stats): 전종목 돌파 이력 통계로
전환`)가 `--period dev --capital-mode fixed --unit-caps 4/none/none
--max-unit-ratio 999 --max-daily-entries 999999`로 만든
`data/ticker_breakout_history.csv`가 있다. 이건 **계좌 제약(상관군 캡·유닛
금액 상한·일일 진입 상한)을 의도적으로 다 풀어 "신호 자체의 결과"만 보려는
별도 실험**이고, 실계좌 재현이 목적인 이번 회귀 비교에는 쓰면 안 된다(그
`--max-unit-ratio`/`--max-daily-entries` 플래그는 애초에 `fix/backtest-
execution-accounting` 브랜치가 `76f70c2`에서 갈라진 뒤 `main`에서만 추가됐다가
지금은 다시 사라졌다 - 이 PR 브랜치의 `backtest.py`에는 해당 플래그가 없다).

### 1.2 현재 코드 기본값 (플래그 미지정 시)

| 항목 | 기본값 | 출처 |
|---|---|---|
| 리스크(1유닛) | `RISK_PER_TRADE = 0.005` | core.py |
| 손절폭 | `STOP_ATR_MULT = 2.0` (×ATR) | core.py |
| 피라미딩 스텝 | `PYRAMID_ATR_STEP = 0.5` (×ATR) | core.py |
| 유닛 캡 | 종목당 4 / 상관군당 6 / 전체 12 | core.py |
| 진입 돌파 기간 | `HIGH_PERIOD = 20`일 | core.py |
| 거래량 필터 | 없음(`vol_mult_min=None`) | backtest.py |
| 본전 래칫 | 꺼짐(`breakeven_ratchet=False`) | backtest.py |
| 국면 필터 | 꺼짐(`regime_gate=False`) | backtest.py |
| 시장 | `ALL`(코스피+코스닥) | backtest.py |
| 코스피200 필터 | 꺼짐 | backtest.py |
| 시작 자본 | `STARTING_CAPITAL = 10,000,000`원 | backtest.py |
| 슬리피지 / 비용 | `0.3%` / `0.25%`(매수·매도 각각) | backtest.py, CLI로 변경 불가 |
| 자본 모드 | `--period dev`면 `fixed`, `holdout`/`full`이면 `compound`가 기본 (명시 가능) | backtest.py CLI 로직 |
| 업종 캡 갱신 주기 | 30 달력일 | backtest.py `SECTOR_REFRESH_DAYS` |

### 1.3 확인 불가 (과거 실행 근거 없음)

- **holdout 구간(2023-09~2026-08) 설정 전체** - 실행 이력 자체가 없다(§1.1).
  실제 계좌 재현 목적이면 코드 기본값(`capital-mode compound`)이 적용되지만,
  "과거에 이 설정으로 실제 holdout을 돌려봤다"는 근거는 없다.
- **`full`(dev+holdout 통합, 7년 전체) 구간 설정** - dev/holdout 구간 개념
  자체가 `f4153cc`(2026-09-03)에 처음 생겼다. 그 이전에 있었던(그리고
  `app.py`의 "7년" 문구의 근원으로 추정되는) 옛 `data/backtest_portfolio_
  trades.csv`는 dc7d1d0 커밋 메시지에 따르면 "dev/holdout 구간 개념이
  코드에 생기기 전, 다른 설정으로 실행된 낡은 산출물"이고 이미
  `data/archive/`로 옮겨졌다 - 어떤 설정으로 만들어졌는지 커밋 이력에
  남아 있지 않다. **이 옛 산출물을 이번 재검증의 "기존 7년 결과"로 쓰면
  안 된다.**

### 1.4 결론 - 이번 비교의 명명

- holdout·full 구간의 과거 설정을 확인할 수 없으므로, 이번 재검증은
  **"기본 설정으로 새로 실행한 수정 전후 비교"**로 부른다.
  **"기존 7년 결과 재현"이라고 표현하지 않는다.**
- dev 구간만은 §1.1의 커밋 근거로 "코드 기본값 = 확인된 과거 설정"이 되므로,
  dev 비교는 예외적으로 "과거 dev 기준선과 같은 설정"이라고 말할 수 있다
  (다만 산출물 자체, 즉 거래 내역 CSV는 재현이 아니라 **새로 실행**한 것이다
  - 회계·체결 로직이 이번 PR로 바뀌었으므로 거래 결과가 옛 CSV와 다를 수
  있는 게 정상이다).

## 2. 업종 데이터 의존성

`run_portfolio_backtest()`(backtest.py:1043)와 `screener.load_sector_map()`을
읽은 결과.

### 2.1 OHLCV 외에 필요한 것

- **`data/backtest_ohlcv.parquet`** (`.gitignore` 대상, 로컬에서
  `backtest_data.py`로 생성) - 전종목 OHLCV. 없으면 `_load_full_raw()`가
  `FileNotFoundError`로 즉시 중단한다(backtest.py:268-271).
- **업종 맵** - 아래 2.2.
- `--market`을 `ALL`이 아닌 값으로 줄 때만 `data/ticker_market.csv`.
- `--kospi200`을 줄 때만 `data/kospi200_history.csv`.
- `--regime-gate`를 줄 때만 `data/index_regime_KOSPI.csv`·
  `data/index_regime_KOSDAQ.csv`.
- 이번 비교(§3)는 §1.4에 따라 이 3개 플래그를 모두 안 쓰므로, 실질적으로
  **OHLCV + 업종 맵**만 있으면 된다.

### 2.2 업종 캐시 - 경로·형식·시점별 분류

`screener.load_sector_map(day, refresh=False)`(screener.py:861-911):

- **최근 날짜**(`오늘 - day ≤ SECTOR_MAX_AGE_DAYS(7)`)면 공용 캐시
  `data/sector_map.json` (`{"built_on", "as_of", "map"}` 구조, `map`은
  `{티커: 업종명}`)을 `SECTOR_MAX_AGE_DAYS` 동안 재사용한다.
- **과거 날짜**(백테스트가 조회하는 대부분의 날짜)면 날짜 전용 캐시
  `data/sector_map_YYYYMMDD.json`을 쓴다. 이 파일들은 `.gitignore` 대상이라
  로컬에만 있을 수 있고, **저장소에 하나도 커밋돼 있지 않다.**
- **시점별 분류를 지원한다** - 과거 `day`를 넘기면 그 시점의 KRX 업종 지수
  구성으로 만든 맵을 쓴다(생존편향 없음). 다만 `run_portfolio_backtest()`가
  이 조회를 매일 하지 않고 **`SECTOR_REFRESH_DAYS=30` 달력일마다 한 번만**
  갱신한다(backtest.py:1298-1304) - 실운영(7일)보다 완화된 근사다. 이유는
  backtest.py 자체 주석(816-820행)에 실측돼 있다: 과거 시점 업종 맵 1회
  조회에 KRX 로그인 포함 **약 60~70초**가 걸려서, 7일 주기로는 3년·전종목
  백테스트가 비현실적으로 오래 걸린다.
- **캐시가 없으면** `build_sector_map(day)`가 KRX 네트워크(`pykrx`,
  `.env`의 `KRX_ID`/`KRX_PW` 필요)를 호출해 새로 만들고 캐시에 저장한다.
  로컬에 캐시가 없는 날짜가 많으면 7년 전체 실행이 "몇 분"이 아니라
  "몇 시간" 단위로 늘어날 수 있다(§2.3의 개수 참고).

### 2.3 조회 실패 시 동작 - 업종 한도가 사실상 무력화될 수 있는가

**그렇다 - 확인됨.**

- `build_sector_map()`이 KRX 호출에 모두 실패하면 빈 `dict`를 돌려주고,
  `load_sector_map()`은 "업종 정보를 만들지 못했습니다 - '미분류'로
  표시됩니다"라고 경고한 뒤, 그나마 있는 옛 캐시 파일이라도 있으면 그걸
  돌려주고 없으면 **빈 dict `{}`**를 돌려준다(screener.py:903-911).
- `run_portfolio_backtest()`는 `load_sector_map()` 호출 자체가 예외를 던져도
  "이전 맵 유지"로 그냥 넘어간다(backtest.py:1300-1303) - 첫 갱신
  시점(시뮬레이션 첫날)에 실패하면 "이전 맵"도 없으므로 빈 `{}`가 그대로
  쓰인다.
- 신규 진입 후보에 업종을 매길 때: `sig["sector"] = sector_map.get(ticker,
  "")`(backtest.py:1458) - 맵에 없으면(맵이 비어 있으면 전부 없음) **빈
  문자열**이 된다.
- 상관군 캡 검사는 전부 `if (sector and group_after > MAX_UNITS_GROUP)`
  형태다(backtest.py:1215-1216, 1276-1277, 1426-1427) - `sector`가 빈
  문자열이면 `and` 좌변이 falsy라 **캡 검사 자체가 스킵된다.** `전체 유닛
  캡(MAX_UNITS_TOTAL)`은 업종과 무관해 계속 걸리지만, **상관군(업종)
  캡(MAX_UNITS_GROUP)은 업종 조회가 실패한 기간 동안 사실상 무제한이
  된다.**
- 더 나쁜 점: 포지션이 일단 빈 업종("")으로 생성되면 이후 업종 맵이
  복구돼도 그 포지션의 추가매수 캡 검사는 `sector_map.get(ticker,
  pos.sector)`(1270, 1332, 1422행)로 **`pos.sector`(진입 시점에 박제된
  ""）**로 폴백한다 - 한 번 빈 업종으로 진입하면 **그 포지션이 청산될
  때까지 영구적으로** 상관군 캡 밖에 있게 된다.

### 2.4 수정 전후 비교에 강제할 절차

- 두 실행(수정 전 `76f70c2` / 수정 후 `488382d`) 모두 **같은 로컬
  `data/sector_map_*.json` 캐시 집합**을 읽어야 한다. 두 커밋 사이에
  `backtest.py` 외 파일은 안 바뀌었으니(§0), 같은 작업 디렉터리에서
  순서대로 체크아웃해 실행하면 자동으로 같은 캐시를 공유한다(worktree를
  쓰더라도 `data/sector_map*.json`을 두 worktree가 같은 파일을 보게
  symlink/복사할 것 - §3).
- **현재(최근) 업종 분류를 과거 전체 기간에 임의로 덮어써서 돌리지 않는다**
  - 즉 `sector_map.json`(최근용) 하나만 만들어두고 시점별 조회를 생략하는
    식의 편법 금지.
- **업종 정보를 비운 채(강제로 `{}`) 실행해 캡을 무력화한 상태로 성능을
  비교하지 않는다** - 위 2.3에서 보듯 이는 실제 조회 실패와 같은 결과를
  내므로, "버그 수정 효과"가 아니라 "상관군 캡이 없어진 효과"와
  뒤섞인다.
- 실행 전 `check_revalidation_data.py`로 필요한 `sector_map_YYYYMMDD.json`
  날짜 목록이 모두 로컬에 있는지 확인하고, **비어 있는 날짜가 하나라도
  있으면 그 사실을 기록만 하고 그대로 강행하지 말 것** - §3.5의 중단
  기준을 따른다.

## 3. 로컬 실행 절차 (수정 전후 비교)

### 3.1 준비

```bash
# 최신 커밋 확인
git fetch origin
git log --oneline -1 76f70c2   # 수정 전
git log --oneline -1 488382d   # 수정 후 (fix/backtest-execution-accounting HEAD)

# OHLCV 캐시가 최신인지 확인 (없으면 backtest_data.py로 생성 - 이번 세션의
# 범위 밖이므로 로컬에서 직접 실행)
python backtest_data.py   # 이미 data/backtest_ohlcv.parquet가 최신이면 생략 가능
```

두 worktree를 만들어 커밋별로 격리한다(현재 작업 디렉터리의 브랜치/미커밋
변경을 건드리지 않기 위함):

```bash
git worktree add ../stock-monitor-before 76f70c2
git worktree add ../stock-monitor-after  488382d
```

각 worktree는 `data/` 아래 `.gitignore` 대상 파일(특히
`backtest_ohlcv.parquet`, `sector_map_*.json`)이 비어 있다(git이 관리하지
않으므로). **같은 입력을 쓰도록 원본 작업 디렉터리의 해당 파일을 두
worktree 모두에 symlink**한다(복사하면 용량이 커지고, symlink면 한쪽에서
캐시가 새로 채워져도 다른 쪽이 즉시 같은 걸 보게 되어 §2.4 요구를 자동
만족한다):

```bash
for wt in ../stock-monitor-before ../stock-monitor-after; do
  ln -sf "$(pwd)/data/backtest_ohlcv.parquet" "$wt/data/backtest_ohlcv.parquet"
  for f in data/sector_map_*.json data/sector_map.json data/ticker_market.csv \
           data/kospi200_history.csv data/index_regime_KOSPI.csv \
           data/index_regime_KOSDAQ.csv; do
    [ -e "$f" ] && ln -sf "$(pwd)/$f" "$wt/$f"
  done
done
```

### 3.2 사전 점검 (각 worktree에서, 실행 전에 반드시)

```bash
cd ../stock-monitor-before   # 또는 ../stock-monitor-after - 둘 다 같은 data/를 보므로 결과는 같아야 함
python check_revalidation_data.py --period dev
python check_revalidation_data.py --period holdout   # holdout까지 갈 경우
```

종료 코드가 0이 아니면(업종 캐시 누락 등) §3.5를 따라 중단하고 이유를
기록한다. `check_revalidation_data.py`는 이 세션에서 이미 문법·로직을
합성 데이터로 스모크 테스트했다(네트워크·실제 데이터 불필요).

### 3.3 실행 명령 - 두 버전 동일

§1.4에 따라 **실험 플래그 없이 코드 기본값 그대로** 돌린다. dev 구간을
먼저 본다(§1.1의 유일한 확인된 과거 설정과 일치):

```bash
# --- before (../stock-monitor-before, commit 76f70c2) ---
cd ../stock-monitor-before
python backtest.py --portfolio --period dev --capital-mode fixed \
  --out data/compare/before/dev_portfolio.csv \
  2>&1 | tee data/compare/before/dev_run.log

# --- after (../stock-monitor-after, commit 488382d) ---
cd ../stock-monitor-after
python backtest.py --portfolio --period dev --capital-mode fixed \
  --out data/compare/after/dev_portfolio.csv \
  2>&1 | tee data/compare/after/dev_run.log
```

dev 비교가 문제 없으면(§3.4) holdout으로 넘어간다. **holdout은 실행마다
`data/holdout_runs.log`에 영구 기록되고, "몇 번째로 이 구간을 들여다봤는가"가
그대로 누적된다** - before/after 두 번 실행하면 2회로 기록된다. 이건 이
비교의 성격상(수정 전후를 같은 구간에서 봐야 함) 불가피하지만, holdout을
돌리기 전에 사용자에게 "지금 holdout 2회를 소비한다"는 걸 명시적으로
알리고 진행할 것(이 세션은 holdout/full 실행 자체를 하지 않는다 - 사용자
요청 시 로컬에서 진행):

```bash
python backtest.py --portfolio --period holdout --capital-mode compound \
  --out data/compare/{before,after}/holdout_portfolio.csv \
  2>&1 | tee data/compare/{before,after}/holdout_run.log
```

`--capital-mode`는 §1.2 기본값(dev=fixed, holdout=compound)을 그대로 쓰되,
두 실행에 **같은 값**을 명시적으로 적어 우연한 기본값 변경에 안전하게 한다.

### 3.4 결과 보존

`data/compare/{before,after}/`에 각각:

- `*_portfolio.csv` - `--out`으로 저장한 거래 상세(포트폴리오 리포트가
  콘솔에 찍는 지표: 거래수·승률·평균R·총손익·CAGR·MDD 등도 `*_run.log`에
  같이 남는다).
- `*_run.log` - 콘솔 전체 출력(`tee`).
- `manifest.json` - 아래 정보를 실행마다 기록:

```bash
cat > data/compare/before/manifest.json <<EOF
{
  "commit": "$(git -C ../stock-monitor-before rev-parse HEAD)",
  "command": "python backtest.py --portfolio --period dev --capital-mode fixed",
  "python": "$(python --version)",
  "pip_freeze_hash": "$(pip freeze | sha256sum | cut -d' ' -f1)",
  "backtest_ohlcv_sha256": "$(sha256sum data/backtest_ohlcv.parquet | cut -d' ' -f1)",
  "sector_cache_file_count": $(ls data/sector_map_*.json 2>/dev/null | wc -l),
  "host": "$(uname -a)",
  "run_at": "$(date -u +%FT%TZ)"
}
EOF
```

(`after`도 동일하게, 커밋만 다르게.) 기존 로컬 산출물(`data/backtest_
portfolio_*.csv`, `data/ticker_stats.json` 등)은 이 절차가 건드리지 않는다 -
모든 출력이 `data/compare/{before,after}/` 아래 새 경로로만 쓰인다.

### 3.5 중단 기준

다음 중 하나라도 해당하면 **비교를 중단하고 이유를 `manifest.json`(또는
콘솔 로그)에 남긴 뒤 사용자에게 보고한다** - 억지로 강행하지 않는다:

- `check_revalidation_data.py`가 0이 아닌 종료 코드를 낼 때(OHLCV 없음,
  시장/코스피200/국면 파일 없음 - 이번 비교는 그 플래그들을 안 쓰므로
  보통 해당 없음).
- 필요한 `sector_map_YYYYMMDD.json` 캐시가 다수 비어 있어(§2.2) 실행이
  KRX 네트워크에 크게 의존하게 될 때 - 개수를 보고하고, 사용자가 캐시를
  미리 채울지(시간이 오래 걸림을 인지시키고) 결정하게 한다.
- `before`/`after` 두 worktree가 서로 다른 `data/backtest_ohlcv.parquet`
  또는 `sector_map_*.json`을 보고 있는 게 발견될 때(symlink가 깨졌거나
  실수로 복사본을 따로 만든 경우) - §3.1을 다시 확인.
- holdout 실행 직전, 사용자가 "holdout 2회 소비"에 아직 동의하지 않았을 때.

## 4. 파일 체크리스트

### 4.1 필수 (이번 비교 절차 - dev/holdout, 플래그 없음)

- `data/backtest_ohlcv.parquet` - 없으면 `backtest_data.py`로 생성.
- `data/sector_map_YYYYMMDD.json` × (구간 내 30일 주기 갱신 횟수) - 없는
  날짜는 첫 실행 시 KRX 네트워크로 채워지며 1건당 약 60~70초.
  `check_revalidation_data.py`로 정확한 목록과 개수를 미리 확인.

### 4.2 선택 (플래그를 쓸 때만)

- `data/ticker_market.csv` - `--market KOSPI`/`--market KOSDAQ`.
- `data/kospi200_history.csv` - `--kospi200`.
- `data/index_regime_KOSPI.csv`, `data/index_regime_KOSDAQ.csv` -
  `--regime-gate`.

이번 §3 절차는 위 세 플래그를 쓰지 않으므로 선택 파일은 불필요하다.

### 4.3 이번 재검증에 쓰지 않는 것 (참고용으로만 존재)

- `data/ticker_stats.json`, `data/backtest_portfolio_dev_v2.csv`,
  `data/ticker_breakout_history.csv` - app.py 표시용/별도 실험 산출물.
  §1.1·§1.3에서 설명한 이유로 이번 수정 전후 비교의 기준선으로 쓰지 않는다.
- `data/archive/` (있다면) - dev/holdout 개념 이전의 옛 산출물.

## 5. 로컬 접속 후 다음 단계

이 문서와 `check_revalidation_data.py`까지가 이번 세션의 범위다. 로컬
접속 후 사용자가 실제 진행을 요청하면:

1. §3.1~3.2 (worktree 준비 + 사전 점검)
2. §3.3 dev 구간 before/after 실행 (`--capital-mode fixed`)
3. dev 결과 비교 - 버그 수정으로 거래 내역·지표가 어떻게 달라졌는지 확인
   (완전히 같아야 하는 게 아니라, PR이 설명한 버그들이 실제로 존재했던
   케이스에서만 달라지는 게 정상 - 회계 버그 수정이므로 청산 타이밍·평가액이
   달라지는 트레이드가 있을 수 있다).
4. 이상 없으면 holdout 진행 여부를 사용자에게 명시적으로 확인(§3.3의
   holdout 소비 경고) 후 §3.3 holdout 구간 실행.
5. `full` 통합 비교가 필요하면 같은 절차를 `--period full`로 반복(단, 이
   경우도 holdout 구간을 포함하므로 같은 소비 경고가 적용된다).
