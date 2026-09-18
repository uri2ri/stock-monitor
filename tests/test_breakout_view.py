from pathlib import Path
from unittest.mock import MagicMock
from streamlit.testing.v1 import AppTest
import breakout_tracker as b
import breakout_view as v
import mailer

def test_dummy_list_and_detail():
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'tools/breakout_preview.py')).run()
    assert not at.exception
    assert len(at.dataframe) == 1
    assert len(at.dataframe[0].value) == 2
    assert at.dataframe[0].value.iloc[0]['자동매수 결과'] == '자금부족'
    at.selectbox[0].select(1).run()
    assert not at.exception
    assert any('갱신 실패' in x.value for x in at.warning)
    assert any('DEMO-ORDER' in x.value for x in at.markdown)

def test_summary_empty_and_uncertain():
    assert v.report_text([]) == v.report_html([]) == ''
    t = dict(ticker='DEMO', name='<unsafe>', status=b.STATUS_ACTIVE, outcome=b.OUTCOME_UNKNOWN)
    assert '미진입 확정' not in v.report_text([t])
    assert '상태미확인' in v.report_text([t])
    assert '&lt;unsafe&gt;' in v.report_html([t])

def test_email_summary_attached_without_sending(monkeypatch):
    for key in ('GMAIL_ADDRESS', 'GMAIL_APP_PASSWORD'):
        monkeypatch.setenv(key, 'test-only')
    monkeypatch.setattr(mailer, '_build_text', lambda *a: 'existing-text')
    monkeypatch.setattr(mailer, '_build_html', lambda *a: '<div>existing-html</div>')
    monkeypatch.setattr(mailer, 'build_subject', lambda *a: 'test')
    smtp = MagicMock()
    monkeypatch.setattr(mailer.smtplib, 'SMTP', lambda *a, **k: smtp)
    mailer.send_report_mail([], None, breakout_tracks=[dict(ticker='DEMO', status=b.STATUS_ACTIVE, outcome=b.OUTCOME_ORDER_SENT)])
    message = smtp.__enter__.return_value.send_message.call_args[0][0]
    assert '돌파 후 미진입' in message.get_body(('plain',)).get_content()
    assert '돌파 후 미진입' in message.get_body(('html',)).get_content()
    assert 'existing-html' in message.get_body(('html',)).get_content()
