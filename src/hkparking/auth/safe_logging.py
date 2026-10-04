"""Payload-deny boundary for protocol logs, including future descendants.
Not a promise of global secrecy: app code must never log credentials either.
Final listener uses deny-filtered handlers, no access logs and no error tracebacks.
"""
import logging
import logging.config

PREFIXES=('fastmcp.server.auth','mcp.server.auth','authlib','httpx','httpcore','uvicorn')


def protocol(name):
    return any(name==p or name.startswith(p+'.') for p in PREFIXES)


class ProtocolDeny(logging.Filter):
    def filter(self,record):
        return not protocol(record.name)


def install_redaction():
    previous=logging.getLogRecordFactory()
    if not getattr(previous,'_pocket_payload_safe',False):
        def factory(*args,**kwargs):
            record=previous(*args,**kwargs)
            if protocol(record.name):
                record.msg='Protocol event suppressed'
                record.args=()
                record.exc_info=None
                record.exc_text=None
                record.stack_info=None
            return record
        factory._pocket_payload_safe=True
        logging.setLogRecordFactory(factory)
    # Filters belong on HANDLERS, not ancestors. Record factory also covers
    # handlers added later by unrelated dictConfig until listener config runs.
    handlers=list(logging.getLogger().handlers)
    for logger in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(logger,logging.Logger):
            handlers.extend(logger.handlers)
    for handler in set(handlers):
        if not any(isinstance(f,ProtocolDeny) for f in handler.filters):
            handler.addFilter(ProtocolDeny())


def listener_log_config():
    return {'version':1,'disable_existing_loggers':False,
        'filters':{'protocol_deny':{'()':ProtocolDeny}},
        'formatters':{'event':{'format':'%(levelname)s %(name)s %(message)s'}},
        'handlers':{'safe':{'class':'logging.StreamHandler','stream':'ext://sys.stderr','formatter':'event','filters':['protocol_deny']}},
        'root':{'handlers':['safe'],'level':'WARNING'},
        'loggers':{p:{'handlers':['safe'],'level':'CRITICAL','propagate':False} for p in PREFIXES}}
