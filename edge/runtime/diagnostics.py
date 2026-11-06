"""Runtime exception logging without locals or unredacted credential values."""
import os
import re
import traceback


def log_exception(logger, message, error):
    """Log the complete exception chain and stack, redacting secret-bearing text.

    Preformat rather than attach raw exc_info: other handlers must not render
    the original sensitive exception again. Never capture frame locals or read
    credential files. Omit source text (which may contain literal secrets),
    while retaining every frame's filename, line number and function.
    """
    diagnostic = traceback.TracebackException.from_exception(
        error, capture_locals=False, lookup_lines=False)

    def omit_source(exc):
        exc.stack = traceback.StackSummary.from_list([
            traceback.FrameSummary(frame.filename, frame.lineno, frame.name,
                                   lookup_line=False, line='')
            for frame in exc.stack])
        for child in (exc.__cause__, exc.__context__):
            if child is not None:
                omit_source(child)
        for child in getattr(exc, 'exceptions', None) or ():
            omit_source(child)

    omit_source(diagnostic)
    text = ''.join(diagnostic.format())
    for key, value in os.environ.items():
        if value and any(part in key.upper() for part in
                         ('TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'API_KEY', 'PRIVATE_KEY')):
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'-----BEGIN[^\n]*PRIVATE KEY-----.*?-----END[^\n]*PRIVATE KEY-----',
                  '[REDACTED PRIVATE KEY]', text, flags=re.S)
    text = re.sub(r'(?i)Bearer\s+[^\s,;\"\']+', 'Bearer [REDACTED]', text)
    text = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+',
                  '[REDACTED TOKEN]', text)
    text = re.sub(
        r"(?i)([\"']?(?:access_token|refresh_token|id_token|token|password|client_secret|private_key|api_key|authorization)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r'\1[REDACTED]', text)
    logger.error('%s\n%s', message, text)
