import contextlib
from scripts.mac_live import RedactedStream

def test_calendar_server_logging_with_redacted_stream():
 import uvicorn,calendar_auth
 sink=RedactedStream()
 assert sink.isatty() is False and sink.write('synthetic confidential body')==len('synthetic confidential body')
 with contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
  config=uvicorn.Config(calendar_auth.app,host='127.0.0.1',port=8766,access_log=False,log_level='warning')
 assert config.access_log is False and config.port==8766
